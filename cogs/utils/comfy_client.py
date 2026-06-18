import os
import time
import uuid
import json
import logging
import subprocess
from enum import StrEnum
from typing import Optional, List, Dict, Any

import requests

log = logging.getLogger(__name__)


DEFAULT_COMFY_HOST = "127.0.0.1"
DEFAULT_COMFY_PORT = 8188
READINESS_TIMEOUT_SEC = 120.0
READINESS_POLL_INTERVAL_SEC = 2.0


def comfy_base_url(host: str = DEFAULT_COMFY_HOST, port: int = DEFAULT_COMFY_PORT) -> str:
    return f"http://{host}:{port}"


def is_comfy_running(host: str = DEFAULT_COMFY_HOST, port: int = DEFAULT_COMFY_PORT, timeout: float = 2.0) -> bool:
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
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'ComfyUI_New'))


def comfy_log_path(comfy_root: Optional[str] = None) -> str:
    root = comfy_root or default_comfy_root()
    return os.path.join(root, "comfyui_bot.log")


def wait_until_ready(
    host: str = DEFAULT_COMFY_HOST,
    port: int = DEFAULT_COMFY_PORT,
    timeout_sec: float = READINESS_TIMEOUT_SEC,
    poll_interval_sec: float = READINESS_POLL_INTERVAL_SEC,
) -> bool:
    """Poll /system_stats until ComfyUI responds or timeout."""
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
        host: str = DEFAULT_COMFY_HOST,
        port: int = DEFAULT_COMFY_PORT,
        extra_args: Optional[list] = None,
        readiness_timeout_sec: float = READINESS_TIMEOUT_SEC,
    ) -> ComfySource:
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
        running = is_comfy_running(host=self._host, port=self._port)
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
            "host": self._host,
            "port": self._port,
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


def ensure_comfy_running(**kwargs) -> ComfySource:
    return manager.ensure_comfy_running(**kwargs)


def get_comfy_status() -> Dict[str, Any]:
    return manager.get_status()


def shutdown_if_bot_started() -> None:
    manager.shutdown_if_bot_started()


def start_comfy(comfy_root: str = None, host: str = DEFAULT_COMFY_HOST, port: int = DEFAULT_COMFY_PORT, extra_args: Optional[list] = None) -> Optional[subprocess.Popen]:
    """
    Start ComfyUI as a background process. Returns subprocess.Popen or None on error.

    comfy_root: path to the ComfyUI folder (where `main.py` lives). If None the code will try to find it relative to this file.
    extra_args: list of additional CLI args to pass, e.g. ['--disable-auto-launch', '--fp8_e4m3fn-unet']
    """
    if comfy_root is None:
        comfy_root = default_comfy_root()

    main_py = os.path.join(comfy_root, 'main.py')
    if not os.path.isfile(main_py):
        log.error(f"ComfyUI main.py not found at: {main_py}")
        return None

    cmd = [os.sys.executable, main_py, '--port', str(port), '--disable-auto-launch', '--dont-print-server']
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
) -> List[Dict[str, Any]]:
    """
    Poll ComfyUI history until the prompt completes or timeout.

    Returns a list of image info dicts, each containing filename/subfolder/type.
    """
    start = time.time()
    last_error: Optional[Exception] = None
    while time.time() - start < timeout_sec:
        try:
            hist = get_history(prompt_id, host=host, port=port)
            # history response structure: {prompt_id: { "outputs": {node_id: [images...] } } }
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get("status") or {}
                messages = status.get("messages") or []

                # Fail fast when Comfy reports execution_error so callers can surface
                # the actual node + error text instead of waiting for timeout.
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

                    raise RuntimeError(f"{', '.join(details)}: {exception_message}")

                outputs = entry.get("outputs") or {}
                images: List[Dict[str, Any]] = []
                # outputs: { node_id: { "images": [ {filename, subfolder, type}, ...] } }
                for node_out in outputs.values():
                    if not isinstance(node_out, dict):
                        continue
                    for out in node_out.get("images") or []:
                        if isinstance(out, dict) and out.get("type") in ("output", "temp"):
                            images.append(out)
                if images:
                    return images
        except Exception as e:
            last_error = e

        time.sleep(poll_interval)

    if last_error:
        raise last_error
    raise TimeoutError(f"Timed out waiting for ComfyUI result for prompt_id={prompt_id}")


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
