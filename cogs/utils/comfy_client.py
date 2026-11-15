import os
import time
import uuid
import json
import logging
import subprocess
from enum import StrEnum
from typing import Optional, List, Dict, Any, Tuple

import requests

log = logging.getLogger(__name__)


# Fallbacks used when config.json is missing or incomplete.
_FALLBACK_COMFY_HOST = "127.0.0.1"
_FALLBACK_COMFY_PORT = 8188
READINESS_TIMEOUT_SEC = 120.0
READINESS_POLL_INTERVAL_SEC = 2.0
# After a generation timeout, poll this long for a cancelled prompt to leave
# the queue before giving up. Callers that wrap generate in asyncio.wait_for
# should add their own grace on top of the generation budget.
PROMPT_CANCEL_GRACE_SEC = 45.0


def _settings_from_config() -> Tuple[str, int, str, bool]:
    """Return (host, port, root, auto_start) from config.json when available."""
    try:
        from cogs.utils.config import load_config

        cfg = load_config().comfyui
        return cfg.host, int(cfg.port), str(cfg.resolved_root()), bool(cfg.auto_start)
    except Exception as exc:
        log.debug("ComfyUI settings falling back to defaults (%s)", exc)
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "ComfyUI_New"))
        return _FALLBACK_COMFY_HOST, _FALLBACK_COMFY_PORT, root, True


def get_comfy_settings() -> Tuple[str, int, str, bool]:
    return _settings_from_config()


# Back-compat names; prefer get_comfy_settings() / resolve inside call sites.
DEFAULT_COMFY_HOST = _FALLBACK_COMFY_HOST
DEFAULT_COMFY_PORT = _FALLBACK_COMFY_PORT


def comfy_base_url(host: str = None, port: int = None) -> str:
    if host is None or port is None:
        cfg_host, cfg_port, _, _ = _settings_from_config()
        host = host if host is not None else cfg_host
        port = port if port is not None else cfg_port
    return f"http://{host}:{port}"


def is_comfy_running(host: str = None, port: int = None, timeout: float = 2.0) -> bool:
    if host is None or port is None:
        cfg_host, cfg_port, _, _ = _settings_from_config()
        host = host if host is not None else cfg_host
        port = port if port is not None else cfg_port
    try:
        r = requests.get(f"{comfy_base_url(host, port)}/system_stats", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


class ComfySource(StrEnum):
    EXTERNAL = "external"
    BOT_STARTED = "bot_started"
    DOWN = "down"


def default_comfy_root() -> str:
    _, _, root, _ = _settings_from_config()
    return root


def comfy_log_path(comfy_root: Optional[str] = None) -> str:
    root = comfy_root or default_comfy_root()
    return os.path.join(root, "comfyui_bot.log")


def wait_until_ready(
    host: str = None,
    port: int = None,
    timeout_sec: float = READINESS_TIMEOUT_SEC,
    poll_interval_sec: float = READINESS_POLL_INTERVAL_SEC,
) -> bool:
    """Poll /system_stats until ComfyUI responds or timeout."""
    if host is None or port is None:
        cfg_host, cfg_port, _, _ = _settings_from_config()
        host = host if host is not None else cfg_host
        port = port if port is not None else cfg_port
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if is_comfy_running(host=host, port=port):
            return True
        time.sleep(poll_interval_sec)
    return False


class ComfyManager:
    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._source: ComfySource = ComfySource.DOWN
        self._last_health_check: float = 0.0
        self._host = DEFAULT_COMFY_HOST
        self._port = DEFAULT_COMFY_PORT

    def bot_owns_process(self) -> bool:
        return self._source == ComfySource.BOT_STARTED and self._process is not None

    def ensure_comfy_running(
        self,
        comfy_root: Optional[str] = None,
        host: str = None,
        port: int = None,
        extra_args: Optional[list] = None,
        readiness_timeout_sec: float = READINESS_TIMEOUT_SEC,
        auto_start: Optional[bool] = None,
    ) -> ComfySource:
        cfg_host, cfg_port, cfg_root, cfg_auto = _settings_from_config()
        host = host if host is not None else cfg_host
        port = port if port is not None else cfg_port
        comfy_root = comfy_root if comfy_root is not None else cfg_root
        if auto_start is None:
            auto_start = cfg_auto

        self._host = host
        self._port = port
        self._last_health_check = time.time()

        if is_comfy_running(host=host, port=port):
            if self._process is not None and self._process.poll() is None:
                self._source = ComfySource.BOT_STARTED
            else:
                self._source = ComfySource.EXTERNAL
            log.info(f"ComfyUI already running on {host}:{port} (source={self._source})")
            return self._source

        if not auto_start:
            self._source = ComfySource.DOWN
            raise RuntimeError(
                f"ComfyUI is not running on {host}:{port} and comfyui.auto_start is false. "
                "Start Comfy Desktop / ComfyUI_New first, or set comfyui.auto_start true."
            )

        process = start_comfy(comfy_root=comfy_root, host=host, port=port, extra_args=extra_args)
        if process is None:
            self._source = ComfySource.DOWN
            raise RuntimeError("Failed to start ComfyUI subprocess")

        self._process = process
        self._source = ComfySource.BOT_STARTED
        log.info(f"Started ComfyUI subprocess (pid={process.pid}); waiting for readiness...")

        if not wait_until_ready(
            host=host,
            port=port,
            timeout_sec=readiness_timeout_sec,
        ):
            self._source = ComfySource.DOWN
            raise RuntimeError(
                f"ComfyUI did not become ready within {readiness_timeout_sec}s "
                f"(check {comfy_log_path(comfy_root)})"
            )

        log.info(f"ComfyUI ready on {host}:{port} (source={self._source})")
        return self._source

    def get_status(self) -> Dict[str, Any]:
        cfg_host, cfg_port, cfg_root, _ = _settings_from_config()
        # Prefer last known host/port; fall back to config.
        host = self._host or cfg_host
        port = self._port or cfg_port
        running = is_comfy_running(host=host, port=port)
        self._last_health_check = time.time()
        pid = self._process.pid if self._process is not None else None
        if running and self._source == ComfySource.DOWN:
            self._source = ComfySource.EXTERNAL
        if not running:
            self._source = ComfySource.DOWN
        return {
            "running": running,
            "ready": running,
            "source": self._source.value,
            "host": host,
            "port": port,
            "root": cfg_root,
            "pid": pid,
            "bot_owns_process": self.bot_owns_process(),
            "last_health_check": self._last_health_check,
        }

    def shutdown_if_bot_started(self) -> None:
        if not self.bot_owns_process():
            return
        process = self._process
        if process is None:
            return
        log.info(f"Stopping bot-started ComfyUI (pid={process.pid})")
        try:
            process.terminate()
            process.wait(timeout=15)
        except Exception as e:
            log.warning(f"Error stopping ComfyUI process: {e}")
            try:
                process.kill()
            except Exception:
                pass
        finally:
            self._process = None
            self._source = ComfySource.DOWN


manager = ComfyManager()


def apply_config_defaults() -> None:
    """Reload host/port defaults from config into module globals and the manager."""
    global DEFAULT_COMFY_HOST, DEFAULT_COMFY_PORT
    host, port, root, _auto = _settings_from_config()
    DEFAULT_COMFY_HOST = host
    DEFAULT_COMFY_PORT = port
    manager._host = host
    manager._port = port
    log.info("ComfyUI config: host=%s port=%s root=%s", host, port, root)


def ensure_comfy_running(**kwargs) -> ComfySource:
    return manager.ensure_comfy_running(**kwargs)


def get_comfy_status() -> Dict[str, Any]:
    return manager.get_status()


def shutdown_if_bot_started() -> None:
    manager.shutdown_if_bot_started()


def start_comfy(comfy_root: str = None, host: str = None, port: int = None, extra_args: Optional[list] = None) -> Optional[subprocess.Popen]:
    """
    Start ComfyUI as a background process. Returns subprocess.Popen or None on error.

    comfy_root: path to the ComfyUI folder (where `main.py` lives). If None, uses config.comfyui.root.
    extra_args: list of additional CLI args to pass, e.g. ['--disable-auto-launch', '--fp8_e4m3fn-unet']
    """
    cfg_host, cfg_port, cfg_root, _ = _settings_from_config()
    if comfy_root is None:
        comfy_root = cfg_root
    if host is None:
        host = cfg_host
    if port is None:
        port = cfg_port

    main_py = os.path.join(comfy_root, 'main.py')
    if not os.path.isfile(main_py):
        log.error(f"ComfyUI main.py not found at: {main_py}")
        return None

    # Prefer ComfyUI's own venv (CUDA torch) over the bot process interpreter.
    comfy_python = os.sys.executable
    if os.name == "nt":
        candidate = os.path.join(comfy_root, ".venv", "Scripts", "python.exe")
    else:
        candidate = os.path.join(comfy_root, ".venv", "bin", "python")
    if os.path.isfile(candidate):
        comfy_python = candidate

    # Listen on the configured host/port so Desktop and the bot share one server.
    cmd = [
        comfy_python,
        main_py,
        "--listen",
        host if host not in ("127.0.0.1", "localhost") else "127.0.0.1",
        "--port",
        str(port),
        "--disable-auto-launch",
        "--dont-print-server",
    ]
    extra_paths = os.path.join(comfy_root, "extra_model_paths.yaml")
    if os.path.isfile(extra_paths):
        cmd.extend(["--extra-model-paths-config", extra_paths])
    if extra_args:
        cmd.extend(extra_args)

    # On Windows, DETACHED_PROCESS is useful; use creationflags for background process
    creationflags = 0
    try:
        # DETACHED_PROCESS (0x00000008) and CREATE_NEW_PROCESS_GROUP (0x00000200)
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    except Exception:
        creationflags = 0

    log_file = comfy_log_path(comfy_root)
    stderr_handle = subprocess.DEVNULL
    stderr_target = "DEVNULL"
    try:
        stderr_handle = open(log_file, "a", encoding="utf-8")
        stderr_target = log_file
    except OSError as e:
        fallback = f"{log_file}.{int(time.time())}.log"
        log.warning(f"Could not open ComfyUI log at {log_file} ({e}); trying {fallback}")
        try:
            stderr_handle = open(fallback, "a", encoding="utf-8")
            stderr_target = fallback
        except OSError as e2:
            log.warning(f"Could not open fallback ComfyUI log ({e2}); stderr discarded")

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    log.info(f"Starting ComfyUI: {' '.join(cmd)} (stderr -> {stderr_target})")
    try:
        p = subprocess.Popen(
            cmd,
            cwd=comfy_root,
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle,
            env=env,
            creationflags=creationflags,
        )
        return p
    except Exception as e:
        log.exception(f"Failed to start ComfyUI: {e}")
        return None


def post_prompt(prompt: dict, host: str = DEFAULT_COMFY_HOST, port: int = DEFAULT_COMFY_PORT, prompt_id: Optional[str] = None, number: Optional[float] = None, extra_data: Optional[dict] = None) -> dict:
    """
    Submit a ComfyUI prompt (graph) to the running ComfyUI server.

    - `prompt` should be the JSON structure exported by ComfyUI (API format).
    - Returns the JSON response from ComfyUI on success or raises requests.HTTPError.
    """
    if prompt_id is None:
        prompt_id = str(uuid.uuid4())
    payload = {
        "prompt": prompt,
        "prompt_id": prompt_id,
    }
    if number is not None:
        payload["number"] = number
    if extra_data is not None:
        payload["extra_data"] = extra_data

    url = f"{comfy_base_url(host, port)}/prompt"
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def ui_workflow_to_api_prompt(ui_workflow: dict) -> dict:
    """
    Convert a ComfyUI *UI workflow export* (with 'nodes' and 'links') into an
    API prompt graph suitable for POST /prompt.

    UI workflow format contains visual/layout metadata and represents links as
    an array: [link_id, from_node_id, from_slot, to_node_id, to_input_idx, type]
    while API format expects:

      {
        "node_id": {
          "class_type": "CheckpointLoaderSimple",
          "inputs": { "ckpt_name": "foo.safetensors" , "model": ["1", 0] }
        }
      }
    """
    nodes = ui_workflow.get("nodes") or []
    links = ui_workflow.get("links") or []

    node_by_id: Dict[str, dict] = {}
    for n in nodes:
        node_id = str(n.get("id"))
        node_by_id[node_id] = n

    # Build map: (to_node_id, to_input_idx) -> (from_node_id, from_output_slot)
    link_map: Dict[tuple[str, int], tuple[str, int]] = {}
    for l in links:
        # l: [link_id, from_node_id, from_slot, to_node_id, to_input_idx, type]
        if not isinstance(l, list) or len(l) < 5:
            continue
        from_node_id = str(l[1])
        from_slot = int(l[2])
        to_node_id = str(l[3])
        to_input_idx = int(l[4])
        link_map[(to_node_id, to_input_idx)] = (from_node_id, from_slot)

    passthrough_types = {"Reroute"}
    skip_types = {"Reroute", "Note"}

    def bypass_source_for_node_output(n: dict, output_slot: int) -> Optional[tuple[str, int]]:
        """
        Some nodes can be \"bypassed\" in UI (mode==4). For a small subset of node
        types, we can safely collapse them by wiring their outputs directly from
        their inputs.
        """
        if n.get("mode") != 4:
            return None
        node_type = n.get("type")
        # LoraLoader bypass: output 0 is model passthrough from input idx 0,
        # output 1 is clip passthrough from input idx 1.
        if node_type == "LoraLoader":
            passthrough_input_idx = 0 if output_slot == 0 else 1 if output_slot == 1 else None
            if passthrough_input_idx is None:
                return None
            src = link_map.get((str(n.get("id")), passthrough_input_idx))
            return src
        return None

    def resolve_source(node_id: str, slot: int) -> tuple[str, int]:
        """
        If the source node is a passthrough (e.g. Reroute), chase its incoming
        connection until we find a real executable node.
        """
        seen = set()
        cur_node, cur_slot = node_id, slot
        while True:
            if cur_node in seen:
                return cur_node, cur_slot
            seen.add(cur_node)
            n = node_by_id.get(cur_node)
            if not n or n.get("type") not in passthrough_types:
                # Collapse bypassed nodes when we know how to passthrough.
                if n:
                    bypass_src = bypass_source_for_node_output(n, cur_slot)
                    if bypass_src:
                        cur_node, cur_slot = str(bypass_src[0]), int(bypass_src[1])
                        continue
                return cur_node, cur_slot
            # Reroute: its first input is the passthrough
            src = link_map.get((cur_node, 0))
            if not src:
                return cur_node, cur_slot
            cur_node, cur_slot = src[0], src[1]

    api_prompt: Dict[str, dict] = {}
    for node_id, n in node_by_id.items():
        class_type = n.get("type")
        if not class_type:
            continue
        if class_type in skip_types:
            continue
        # If UI marks a LoraLoader as bypassed, skip it in API prompt.
        if class_type == "LoraLoader" and n.get("mode") == 4:
            continue

        inputs_obj: Dict[str, Any] = {}
        ui_inputs = n.get("inputs") or []
        widget_vals = n.get("widgets_values") or []
        widget_i = 0
        for idx, inp in enumerate(ui_inputs):
            inp_name = inp.get("name")
            if not inp_name:
                continue
            link = inp.get("link")
            if link is not None:
                src = link_map.get((node_id, idx))
                if src:
                    real_src = resolve_source(str(src[0]), int(src[1]))
                    inputs_obj[inp_name] = [str(real_src[0]), int(real_src[1])]
                continue

            # Unlinked input: take its widget value (by position), if any.
            if inp.get("widget") is not None and widget_i < len(widget_vals):
                inputs_obj[inp_name] = widget_vals[widget_i]
                widget_i += 1

        api_prompt[node_id] = {
            "class_type": class_type,
            "inputs": inputs_obj,
        }

    return api_prompt


def submit_saved_prompt_file(path: str, host: str = DEFAULT_COMFY_HOST, port: int = DEFAULT_COMFY_PORT) -> dict:
    """Load a prompt JSON file previously exported by ComfyUI and submit it."""
    with open(path, 'r', encoding='utf-8') as f:
        prompt = json.load(f)
    return post_prompt(prompt, host=host, port=port)


def get_queue(host: str = DEFAULT_COMFY_HOST, port: int = DEFAULT_COMFY_PORT) -> dict:
    """Return the current ComfyUI queue (pending and running prompts)."""
    url = f"{comfy_base_url(host, port)}/queue"
    r = requests.get(url, timeout=5)
    r.raise_for_status()
    return r.json()


def get_object_info(host: str = DEFAULT_COMFY_HOST, port: int = DEFAULT_COMFY_PORT) -> dict:
    """
    Return ComfyUI object info (node classes and input metadata).
    Useful for discovering valid COMBO values.
    """
    url = f"{comfy_base_url(host, port)}/object_info"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def get_history(prompt_id: str, host: str = DEFAULT_COMFY_HOST, port: int = DEFAULT_COMFY_PORT) -> dict:
    """Fetch the history entry for a given prompt_id."""
    url = f"{comfy_base_url(host, port)}/history/{prompt_id}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


class ComfyPromptTimeout(TimeoutError):
    """Raised when a Comfy prompt does not finish within the wait budget.

    ``image_infos`` lists output/temp files still in history after the prompt
    was interrupted or deleted. Callers delete those files. They are not a
    successful result and must not be posted.
    """

    def __init__(self, message: str, image_infos: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.image_infos = list(image_infos or [])


class PromptCancelResult:
    """Outcome of trying to stop a prompt that hit the wait budget.

    ``completed`` means history already had output images, so the caller
    should return them and must not treat the wait as a timeout.
    ``cancelled`` means a pending delete or a running interrupt was sent.
    ``images`` are successful outputs when ``completed`` is true, otherwise
    leftover output/temp files to delete.
    """

    def __init__(self, images: List[Dict[str, Any]], completed: bool, cancelled: bool):
        self.images = images
        self.completed = completed
        self.cancelled = cancelled


def _history_entry(hist: dict, prompt_id: str) -> Optional[dict]:
    if not isinstance(hist, dict):
        return None
    entry = hist.get(prompt_id)
    if isinstance(entry, dict):
        return entry
    return None


def _execution_error_text(entry: dict, prompt_id: str) -> Optional[str]:
    """User-facing Comfy execution_error text, or None when the entry has none."""
    status = entry.get("status") or {}
    messages = status.get("messages") or []
    for msg in messages:
        if not isinstance(msg, list) or len(msg) < 2:
            continue
        msg_type, msg_payload = msg[0], msg[1]
        if msg_type != "execution_error" or not isinstance(msg_payload, dict):
            continue

        node_id = msg_payload.get("node_id")
        node_type = msg_payload.get("node_type")
        exception_message = msg_payload.get("exception_message", "Unknown ComfyUI execution error")
        exception_type = msg_payload.get("exception_type")

        details = [f"ComfyUI execution_error for prompt_id={prompt_id}"]
        if node_id is not None:
            details.append(f"node_id={node_id}")
        if node_type:
            details.append(f"node_type={node_type}")
        if exception_type:
            details.append(f"exception_type={exception_type}")
        return f"{', '.join(details)}: {exception_message}"
    return None


def _images_from_entry(entry: dict) -> List[Dict[str, Any]]:
    """Output and temp images recorded on one history entry."""
    images: List[Dict[str, Any]] = []
    outputs = entry.get("outputs") or {}
    if not isinstance(outputs, dict):
        return images
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for out in node_out.get("images") or []:
            if isinstance(out, dict) and out.get("type") in ("output", "temp"):
                images.append(out)
    return images


def _load_history_entry(prompt_id: str, host: str, port: int) -> Optional[dict]:
    try:
        hist = get_history(prompt_id, host=host, port=port)
    except Exception as exc:
        log.warning("Could not read ComfyUI history for %s: %s", prompt_id, exc)
        return None
    return _history_entry(hist, prompt_id)


def _queue_prompt_ids(queue: dict, key: str) -> set:
    """prompt_ids in queue_running or queue_pending.

    ComfyUI 0.45 queue items are [number, prompt_id, prompt, extra, outputs].
    """
    found = set()
    items = queue.get(key) or []
    if not isinstance(items, list):
        return found
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 2 and item[1] is not None:
            found.add(str(item[1]))
    return found


def _prompt_queue_flags(prompt_id: str, host: str, port: int) -> Optional[Tuple[bool, bool]]:
    """Return (running, pending) for prompt_id, or None if the queue cannot be read."""
    try:
        queue = get_queue(host=host, port=port)
    except Exception as exc:
        log.warning("Could not read ComfyUI queue while cancelling %s: %s", prompt_id, exc)
        return None
    if not isinstance(queue, dict):
        return False, False
    pid = str(prompt_id)
    running = pid in _queue_prompt_ids(queue, "queue_running")
    pending = pid in _queue_prompt_ids(queue, "queue_pending")
    return running, pending


def _post_comfy(path: str, payload: Optional[dict], host: str, port: int) -> None:
    url = f"{comfy_base_url(host, port)}{path}"
    kwargs: Dict[str, Any] = {"timeout": 10}
    if payload is not None:
        kwargs["json"] = payload
    response = requests.post(url, **kwargs)
    response.raise_for_status()


def interrupt_or_delete_prompt(
    prompt_id: str,
    host: str = DEFAULT_COMFY_HOST,
    port: int = DEFAULT_COMFY_PORT,
    grace_sec: float = PROMPT_CANCEL_GRACE_SEC,
    poll_interval: float = 0.5,
) -> PromptCancelResult:
    """Stop a prompt that is still queued, or return images if it already finished.

    Pending prompts are removed with POST /queue ``{"delete": [prompt_id]}``.
    A running prompt is stopped with POST /interrupt. The interrupt body
    includes ``prompt_id`` so ComfyUI 0.45 only stops that job.

    If history already has output or temp images and no execution error, those
    images are returned as success and nothing is cancelled. After a cancel,
    this polls until the prompt leaves the queue or ``grace_sec`` elapses.
    Images that show up after the cancel are leftovers, not a success.
    """
    entry = _load_history_entry(prompt_id, host, port)
    if entry is not None and _execution_error_text(entry, prompt_id) is None:
        finished = _images_from_entry(entry)
        if finished:
            log.info(
                "Comfy prompt %s already had %s image(s) at timeout; not interrupting",
                prompt_id,
                len(finished),
            )
            return PromptCancelResult(finished, completed=True, cancelled=False)

    def _success_or_leftovers() -> Optional[PromptCancelResult]:
        """Images already on disk. Success only if this call did not cancel."""
        done = _load_history_entry(prompt_id, host, port)
        if done is None:
            return None
        images = _images_from_entry(done)
        if images and not cancelled and _execution_error_text(done, prompt_id) is None:
            log.info(
                "Comfy prompt %s already had %s image(s) at timeout; not interrupting",
                prompt_id,
                len(images),
            )
            return PromptCancelResult(images, completed=True, cancelled=False)
        return PromptCancelResult(images, completed=False, cancelled=cancelled)

    pause = poll_interval if poll_interval and poll_interval > 0 else 0.25
    deadline = time.time() + max(0.0, float(grace_sec))
    cancelled = False
    sent_delete = False
    sent_interrupt = False
    leftovers: List[Dict[str, Any]] = []
    saw_running = False
    saw_pending = False

    while True:
        flags = _prompt_queue_flags(prompt_id, host, port)
        if flags is not None:
            running, pending = flags
            saw_running = saw_running or running
            saw_pending = saw_pending or pending
            if pending and not sent_delete:
                log.info("Deleting pending Comfy prompt %s", prompt_id)
                cancelled = True
                try:
                    _post_comfy("/queue", {"delete": [prompt_id]}, host, port)
                    sent_delete = True
                except Exception as exc:
                    log.warning("Failed to delete pending Comfy prompt %s: %s", prompt_id, exc)
            if running and not sent_interrupt:
                # Targeted interrupt: a global POST /interrupt would cancel
                # whatever else started if this prompt finished in the gap.
                log.info("Interrupting running Comfy prompt %s", prompt_id)
                cancelled = True
                try:
                    _post_comfy("/interrupt", {"prompt_id": prompt_id}, host, port)
                    sent_interrupt = True
                except Exception as exc:
                    log.warning("Failed to interrupt Comfy prompt %s: %s", prompt_id, exc)
            if not running and not pending:
                found = _success_or_leftovers()
                if found is not None and found.completed:
                    return found
                if found is not None:
                    leftovers = found.images
                break
        elif not cancelled:
            break

        if time.time() >= deadline:
            found = _success_or_leftovers()
            if found is not None and found.completed:
                return found
            if found is not None:
                leftovers = found.images
            if cancelled and (saw_running or saw_pending):
                log.warning(
                    "Comfy prompt %s still queued after %.0fs cancel grace",
                    prompt_id,
                    grace_sec,
                )
            break
        time.sleep(pause)

    if cancelled and not leftovers:
        done = _load_history_entry(prompt_id, host, port)
        if done is not None:
            leftovers = _images_from_entry(done)
    return PromptCancelResult(leftovers, completed=False, cancelled=cancelled)


def free_memory(host: str = "127.0.0.1", port: int = 8188, unload_models: bool = True, free_memory: bool = True):
    url = f"http://{host}:{port}/free"
    payload = {
        "unload_models": unload_models,
        "free_memory": free_memory
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Called /free: models unloaded / memory freed")
    except Exception as e:
        log.error(f"Failed to call /free endpoint: {e}")


def wait_for_result(
    prompt_id: str,
    host: str = DEFAULT_COMFY_HOST,
    port: int = DEFAULT_COMFY_PORT,
    timeout_sec: float = 600.0,
    poll_interval: float = 1.0,
    cancel_grace_sec: float = PROMPT_CANCEL_GRACE_SEC,
) -> List[Dict[str, Any]]:
    """
    Poll ComfyUI history until the prompt completes or timeout.

    Returns a list of image info dicts, each containing filename/subfolder/type.

    When the budget expires and the prompt is still running or pending, it is
    interrupted or deleted and this waits until it leaves the queue (or
    ``cancel_grace_sec`` elapses) before raising ``ComfyPromptTimeout``.
    Output images already in history at the timeout are returned as success
    and the prompt is not cancelled. Images written after a cancel are
    attached to the timeout as ``image_infos`` so the caller can delete them.
    """
    start = time.time()
    last_error: Optional[Exception] = None
    while time.time() - start < timeout_sec:
        try:
            hist = get_history(prompt_id, host=host, port=port)
            # history response structure: {prompt_id: { "outputs": {node_id: {"images": [...]}}}}
            entry = _history_entry(hist, prompt_id)
            if entry is not None:
                # Surface execution_error instead of waiting out the timeout.
                # The loop still stores it and keeps polling, matching the
                # previous "remember the last error" behavior.
                error_text = _execution_error_text(entry, prompt_id)
                if error_text:
                    raise RuntimeError(error_text)

                images = _images_from_entry(entry)
                if images:
                    return images
        except Exception as e:
            last_error = e

        time.sleep(poll_interval)

    if not prompt_id:
        if last_error:
            raise last_error
        raise ComfyPromptTimeout("Timed out waiting for ComfyUI result for prompt_id=None")

    try:
        result = interrupt_or_delete_prompt(
            prompt_id,
            host=host,
            port=port,
            grace_sec=cancel_grace_sec,
            poll_interval=poll_interval,
        )
    except Exception as exc:
        log.warning("Failed to cancel ComfyUI prompt %s after timeout: %s", prompt_id, exc)
        if last_error:
            raise last_error
        raise ComfyPromptTimeout(
            f"Timed out waiting for ComfyUI result for prompt_id={prompt_id}"
        ) from exc

    if result.completed and result.images:
        return result.images
    # A finished execution_error is not a queued job. Keep that error when
    # nothing was cancelled. A prompt that was still queued is a timeout,
    # even if an earlier poll also stored a transient error.
    if last_error and not result.cancelled:
        raise last_error
    raise ComfyPromptTimeout(
        f"Timed out waiting for ComfyUI result for prompt_id={prompt_id}",
        image_infos=result.images,
    )


def get_image_bytes(
    filename: str,
    subfolder: str = "",
    image_type: str = "output",
    host: str = DEFAULT_COMFY_HOST,
    port: int = DEFAULT_COMFY_PORT,
) -> bytes:
    """
    Download image bytes from ComfyUI's view endpoint given a file descriptor.
    """
    params = {
        "filename": filename,
        "subfolder": subfolder,
        "type": image_type,
    }
    url = f"{comfy_base_url(host, port)}/view"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.content


def upload_image(
    image_bytes: bytes,
    filename: str = "discord_edit.png",
    *,
    host: str = DEFAULT_COMFY_HOST,
    port: int = DEFAULT_COMFY_PORT,
    overwrite: bool = True,
) -> dict:
    """
    Upload an image into ComfyUI's input folder via POST /upload/image.

    Returns a dict: {name, subfolder, type, load_name}
    where load_name is what LoadImage expects (subfolder/name or name).
    Caller should delete via delete_media_file when done (privacy).
    """
    url = f"{comfy_base_url(host, port)}/upload/image"
    # Comfy expects multipart field "image"; optional overwrite flag.
    files = {
        "image": (filename, image_bytes, "application/octet-stream"),
    }
    data = {"overwrite": "true" if overwrite else "false"}
    r = requests.post(url, files=files, data=data, timeout=120)
    r.raise_for_status()
    payload = r.json()
    # Typical response: {"name": "foo.png", "subfolder": "", "type": "input"}
    name = payload.get("name") or filename
    subfolder = (payload.get("subfolder") or "").replace("\\", "/").strip("/")
    media_type = payload.get("type") or "input"
    load_name = f"{subfolder}/{name}" if subfolder else name
    return {
        "name": name,
        "subfolder": subfolder,
        "type": media_type,
        "load_name": load_name,
    }


def _candidate_comfy_roots() -> List[str]:
    """Roots where Comfy may store input/output (bot install + Desktop Documents)."""
    roots: List[str] = []
    try:
        roots.append(default_comfy_root())
    except Exception:
        pass
    docs = os.path.join(os.path.expanduser("~"), "Documents", "ComfyUI")
    if os.path.isdir(docs) and docs not in roots:
        roots.append(docs)
    # Dedupe preserving order
    seen = set()
    out = []
    for r in roots:
        ar = os.path.abspath(r)
        if ar not in seen and os.path.isdir(ar):
            seen.add(ar)
            out.append(ar)
    return out


def resolve_media_path(
    filename: str,
    subfolder: str = "",
    image_type: str = "output",
) -> Optional[str]:
    """Resolve a Comfy input/output/temp file on disk, if present."""
    if not filename:
        return None
    # load_name may be "sub/file.png" with empty subfolder arg
    if not subfolder and ("/" in filename or "\\" in filename):
        filename = filename.replace("\\", "/")
        parts = filename.rsplit("/", 1)
        if len(parts) == 2:
            subfolder, filename = parts[0], parts[1]
    folder = image_type if image_type in ("input", "output", "temp") else "output"
    subfolder = (subfolder or "").replace("\\", "/").strip("/")
    for root in _candidate_comfy_roots():
        base = os.path.join(root, folder)
        path = os.path.join(base, subfolder, filename) if subfolder else os.path.join(base, filename)
        if os.path.isfile(path):
            return path
    return None


def delete_media_file(
    filename: str,
    subfolder: str = "",
    image_type: str = "output",
) -> bool:
    """
    Delete a ComfyUI media file from disk (privacy: do not retain user/bot images).

    Returns True if a file was removed.
    """
    path = resolve_media_path(filename, subfolder=subfolder, image_type=image_type)
    if not path:
        log.debug(
            "delete_media_file: not found type=%s subfolder=%s name=%s",
            image_type,
            subfolder,
            filename,
        )
        return False
    try:
        os.remove(path)
        log.info("Deleted Comfy media file (privacy): %s", path)
        return True
    except OSError as e:
        log.warning("Failed to delete Comfy media file %s: %s", path, e)
        return False


def delete_media_files(file_infos: List[dict]) -> int:
    """Delete many {filename, subfolder, type} dicts; return count removed."""
    removed = 0
    for info in file_infos or []:
        if not isinstance(info, dict):
            continue
        name = info.get("filename") or info.get("name")
        if not name:
            continue
        if delete_media_file(
            name,
            subfolder=info.get("subfolder", "") or "",
            image_type=info.get("type", "output") or "output",
        ):
            removed += 1
    return removed


def inspect_safetensors_keys(path: str) -> list:
    """Return a list of keys present in a safetensors file (requires `safetensors` installed)."""
    try:
        from safetensors import safe_open
    except Exception as e:
        raise RuntimeError('safetensors not installed') from e

    keys = []
    with safe_open(path, framework='pt') as f:
        keys = list(f.keys())
    return keys
