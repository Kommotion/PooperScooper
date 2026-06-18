import asyncio
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LAVALINK_DIR = PROJECT_ROOT / "lavalink"
TEMPLATE_NAME = "application.yml.template"
GENERATED_CONFIG_NAME = "application.yml"
JAR_NAME = "Lavalink.jar"
WRAPPER_NAME = "yt-dlp-wrapper.bat"


@dataclass
class LavalinkSettings:
    enabled: bool
    managed: bool
    auto_start: bool
    kill_existing: bool
    host: str
    port: int
    password: str
    java_executable: str
    directory: Path
    ytdlp_executable: Optional[str]
    spotify_country_code: str
    deezer_arl: str
    startup_timeout_seconds: int


def _nested_get(credentials: dict, *keys, default=None):
    value = credentials
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def load_lavalink_settings(credentials: dict) -> LavalinkSettings:
    """Build Lavalink settings from config.json (flat or nested lavalink block)."""
    nested = credentials.get("lavalink")
    if isinstance(nested, dict):
        enabled = nested.get("enabled", True)
        managed = nested.get("managed", True)
        auto_start = nested.get("auto_start", True)
        kill_existing = nested.get("kill_existing", managed)
        host = nested.get("host", "localhost")
        port = int(nested.get("port", 2333))
        password = nested.get("password", "changeme")
        java_executable = nested.get("java_executable", "java")
        directory = nested.get("directory", "lavalink")
        ytdlp_executable = nested.get("ytdlp_executable")
        spotify_country_code = nested.get("spotify_country_code", "US")
        deezer_arl = nested.get("deezer_arl", "")
        startup_timeout_seconds = int(nested.get("startup_timeout_seconds", 90))
    else:
        enabled = credentials.get("lavalink_enabled", True)
        managed = credentials.get("lavalink_managed", True)
        auto_start = credentials.get("lavalink_auto_start", True)
        kill_existing = credentials.get("lavalink_kill_existing", managed)
        host = credentials.get("lavalink_host", "localhost")
        port = int(credentials.get("lavalink_port", 2333))
        password = credentials.get("lavalink_password", "changeme")
        java_executable = credentials.get("lavalink_java", "java")
        directory = credentials.get("lavalink_dir", "lavalink")
        ytdlp_executable = credentials.get("ytdlp_executable")
        spotify_country_code = credentials.get("spotify_country_code", "US")
        deezer_arl = credentials.get("deezer_arl", "")
        startup_timeout_seconds = int(credentials.get("lavalink_startup_timeout", 90))

    lavalink_dir = Path(directory)
    if not lavalink_dir.is_absolute():
        lavalink_dir = PROJECT_ROOT / lavalink_dir

    return LavalinkSettings(
        enabled=enabled,
        managed=managed,
        auto_start=auto_start,
        kill_existing=kill_existing,
        host=host,
        port=port,
        password=password,
        java_executable=java_executable,
        directory=lavalink_dir,
        ytdlp_executable=ytdlp_executable,
        spotify_country_code=spotify_country_code,
        deezer_arl=deezer_arl,
        startup_timeout_seconds=startup_timeout_seconds,
    )


def _resolve_ytdlp(settings: LavalinkSettings) -> Optional[str]:
    if settings.ytdlp_executable:
        path = Path(settings.ytdlp_executable)
        if path.is_file():
            return str(path.resolve())
        log.warning("Configured ytdlp_executable not found: %s", settings.ytdlp_executable)
    found = shutil.which("yt-dlp")
    return found


def _write_ytdlp_wrapper(lavalink_dir: Path, ytdlp_executable: str) -> Path:
    wrapper_path = lavalink_dir / WRAPPER_NAME
    if sys.platform == "win32":
        content = (
            "@echo off\r\n"
            "set PYTHONWARNINGS=ignore\r\n"
            "set PYTHONIOENCODING=utf-8\r\n"
            f'"{ytdlp_executable}" %*\r\n'
        )
        wrapper_path.write_text(content, encoding="ascii")
        return wrapper_path

    wrapper_path = lavalink_dir / "yt-dlp-wrapper.sh"
    content = (
        "#!/usr/bin/env bash\n"
        "export PYTHONWARNINGS=ignore\n"
        "export PYTHONIOENCODING=utf-8\n"
        f'exec "{ytdlp_executable}" "$@"\n'
    )
    wrapper_path.write_text(content, encoding="utf-8")
    wrapper_path.chmod(0o755)
    return wrapper_path


def _yaml_path_for_lavalink(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def write_lavalink_config(settings: LavalinkSettings, credentials: dict) -> Path:
    """Generate application.yml from template + config.json (never commit secrets)."""
    template_path = settings.directory / TEMPLATE_NAME
    if not template_path.is_file():
        raise FileNotFoundError(f"Lavalink template not found: {template_path}")

    spotify_client_id = credentials.get("spotify_client_id", "")
    spotify_secret = credentials.get("spotify_secret", "")
    if not spotify_client_id or not spotify_secret:
        log.warning("Spotify credentials missing in config.json; Spotify music resolution may fail.")

    ytdlp_executable = _resolve_ytdlp(settings)
    if not ytdlp_executable:
        raise FileNotFoundError(
            "yt-dlp not found. Install yt-dlp or set lavalink.ytdlp_executable in config.json."
        )

    wrapper_path = _write_ytdlp_wrapper(settings.directory, ytdlp_executable)
    ytdlp_path = _yaml_path_for_lavalink(wrapper_path)

    deezer_enabled = "true" if settings.deezer_arl else "false"
    template = template_path.read_text(encoding="utf-8")
    rendered = (
        template.replace("{{SPOTIFY_CLIENT_ID}}", spotify_client_id)
        .replace("{{SPOTIFY_SECRET}}", spotify_secret)
        .replace("{{SPOTIFY_COUNTRY_CODE}}", settings.spotify_country_code)
        .replace("{{LAVALINK_PASSWORD}}", settings.password)
        .replace("{{LAVALINK_PORT}}", str(settings.port))
        .replace("{{YTDLP_PATH}}", ytdlp_path)
        .replace("{{DEEZER_ARL}}", settings.deezer_arl)
        .replace("deezer: false", f"deezer: {deezer_enabled}")
    )

    output_path = settings.directory / GENERATED_CONFIG_NAME
    output_path.write_text(rendered, encoding="utf-8")
    log.info("Wrote Lavalink config to %s", output_path)
    return output_path


def _lavalink_http_ready(host: str, port: int, password: Optional[str] = None) -> bool:
    for path in ("/version", "/"):
        try:
            request = Request(f"http://{host}:{port}{path}")
            if password:
                request.add_header("Authorization", password)
            with urlopen(request, timeout=2) as response:
                if response.status < 500:
                    return True
        except (URLError, OSError, TimeoutError):
            continue
    return False


def _port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _lavalink_reachable(host: str, port: int) -> bool:
    return _lavalink_http_ready(host, port) or _port_listening(host, port)


def _subprocess_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _pids_listening_on_port(port: int) -> set[int]:
    """Return PIDs bound to the given TCP listen port."""
    pids: set[int] = set()
    if sys.platform == "win32":
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
        )
        needle = f":{port}"
        for line in result.stdout.splitlines():
            upper = line.upper()
            if needle not in line or "LISTENING" not in upper:
                continue
            parts = line.split()
            if not parts:
                continue
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                continue
        return pids

    for command in (
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        ["fuser", f"{port}/tcp"],
    ):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                creationflags=_subprocess_flags(),
            )
        except FileNotFoundError:
            continue
        if result.returncode != 0 and not result.stdout.strip():
            continue
        for token in re.split(r"[\s,]+", result.stdout.strip()):
            if not token:
                continue
            try:
                pids.add(int(token))
            except ValueError:
                continue
        if pids:
            break
    return pids


def _kill_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
        )
        return result.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        log.warning("No permission to terminate pid=%s", pid)
        return False
    return True


async def _wait_for_port_free(host: str, port: int, timeout_seconds: int = 15) -> bool:
    for _ in range(timeout_seconds):
        if not _port_listening(host, port):
            return True
        await asyncio.sleep(1)
    return not _port_listening(host, port)


class LavalinkServerManager:
    """Starts and stops a local Lavalink process when configured as managed."""

    def __init__(self, settings: LavalinkSettings, credentials: dict):
        self.settings = settings
        self.credentials = credentials
        self._process: Optional[subprocess.Popen] = None
        self._started_by_bot = False

    @property
    def running(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        return _lavalink_reachable(self.settings.host, self.settings.port)

    @property
    def started_by_bot(self) -> bool:
        return self._started_by_bot and self.running

    async def kill_existing_on_port(self) -> list[int]:
        """Terminate any process listening on the configured Lavalink port."""
        port = self.settings.port
        pids = _pids_listening_on_port(port)
        if self._process is not None and self._process.poll() is None:
            pids.add(self._process.pid)

        killed: list[int] = []
        for pid in sorted(pids):
            if pid == os.getpid():
                continue
            log.info("Killing existing process on port %s (pid=%s)", port, pid)
            if _kill_pid(pid):
                killed.append(pid)
            else:
                log.warning("Failed to kill pid=%s on port %s", pid, port)

        if killed:
            host = self.settings.host
            if not await _wait_for_port_free(host, port):
                log.warning("Port %s still in use after killing %s", port, killed)
        return killed

    async def prepare(self) -> None:
        if not self.settings.enabled or not self.settings.managed:
            return
        jar_path = self.settings.directory / JAR_NAME
        if not jar_path.is_file():
            raise FileNotFoundError(
                f"{jar_path} not found. Run lavalink/setup.ps1 once to download Lavalink.jar."
            )
        write_lavalink_config(self.settings, self.credentials)

    async def start(self, *, wait: bool = True) -> bool:
        if not self.settings.enabled:
            log.info("Lavalink disabled in config; skipping server start.")
            return False

        host = self.settings.host
        port = self.settings.port

        if not self.settings.managed:
            if _lavalink_reachable(host, port):
                log.info("Using external Lavalink at %s:%s", host, port)
                self._started_by_bot = False
                return True
            log.warning(
                "Lavalink is not reachable and lavalink.managed is false; start Lavalink externally."
            )
            return False

        if self.settings.kill_existing and _lavalink_reachable(host, port):
            killed = await self.kill_existing_on_port()
            if killed:
                log.info(
                    "Cleared port %s (killed pids: %s); starting fresh bot-managed Lavalink.",
                    port,
                    ", ".join(str(pid) for pid in killed),
                )
            elif _port_listening(host, port):
                log.warning(
                    "Port %s is still in use but no owning PID was found; spawn may fail.",
                    port,
                )
            self._process = None
            self._started_by_bot = False
        elif _lavalink_http_ready(host, port):
            log.info("Lavalink already running at %s:%s (reusing)", host, port)
            self._started_by_bot = False
            return True
        elif _port_listening(host, port):
            log.warning(
                "Port %s is in use but Lavalink did not respond to health checks; "
                "assuming an existing instance and skipping spawn.",
                port,
            )
            self._started_by_bot = False
            return True

        await self.prepare()
        jar_path = self.settings.directory / JAR_NAME
        command = [self.settings.java_executable, "-jar", str(jar_path)]

        log_file = self.settings.directory / "logs" / "bot-managed.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_file.open("a", encoding="utf-8")
        log_handle.write(f"\n--- Lavalink spawn {datetime.now(timezone.utc).isoformat()} ---\n")
        log_handle.flush()

        log.info("Starting Lavalink: %s (cwd=%s)", " ".join(command), self.settings.directory)
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._process = subprocess.Popen(
            command,
            cwd=str(self.settings.directory),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        self._started_by_bot = True

        if wait:
            ready = await self.wait_until_ready()
            if not ready:
                process_alive = self._process is not None and self._process.poll() is None
                if process_alive and _port_listening(host, port):
                    log.warning(
                        "Lavalink health check failed but the process is still listening on %s:%s; leaving it running.",
                        host,
                        port,
                    )
                    ready = True
                else:
                    await self.stop()
            if ready:
                log_handle.flush()
            return ready
        return True

    async def wait_until_ready(self) -> bool:
        host = self.settings.host
        port = self.settings.port
        timeout = self.settings.startup_timeout_seconds
        log.info("Waiting up to %ss for Lavalink at %s:%s", timeout, host, port)

        for _ in range(timeout):
            if self._process is not None and self._process.poll() is not None:
                exit_code = self._process.returncode
                self._process = None
                self._started_by_bot = False
                if _lavalink_reachable(host, port) and not self.settings.kill_existing:
                    log.warning(
                        "Bot-managed Lavalink exited (code %s) but %s:%s is up; using existing instance.",
                        exit_code,
                        host,
                        port,
                    )
                    return True
                log.error(
                    "Lavalink process exited early with code %s. "
                    "If port %s is taken, stop the other Lavalink/Java process or change lavalink.port in config.json. "
                    "See lavalink/logs/bot-managed.log",
                    exit_code,
                    port,
                )
                return False
            if _lavalink_http_ready(host, port, self.settings.password):
                log.info("Lavalink is ready at %s:%s", host, port)
                return True
            if self._started_by_bot and _port_listening(host, port):
                log.info("Lavalink is listening at %s:%s", host, port)
                return True
            await asyncio.sleep(1)

        if self._process is not None and self._process.poll() is None and _port_listening(host, port):
            log.warning("Timed out on Lavalink HTTP health check, but %s:%s is listening; proceeding.", host, port)
            return True

        log.error("Timed out waiting for Lavalink at %s:%s", host, port)
        return False

    async def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            log.info("Stopping Lavalink process (pid=%s)", self._process.pid)
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("Lavalink did not exit cleanly; killing process.")
                self._process.kill()
                self._process.wait(timeout=5)

        self._process = None
        self._started_by_bot = False

        if self.settings.managed and self.settings.kill_existing:
            host = self.settings.host
            port = self.settings.port
            if _port_listening(host, port):
                killed = await self.kill_existing_on_port()
                if killed:
                    log.info(
                        "Stopped Lavalink on port %s (killed pids: %s)",
                        port,
                        ", ".join(str(pid) for pid in killed),
                    )

    def status_summary(self) -> str:
        if not self.settings.enabled:
            return "disabled"
        if self.running:
            origin = "bot-managed" if self.started_by_bot else "external"
            return f"online ({origin}) @ {self.settings.host}:{self.settings.port}"
        return "offline"