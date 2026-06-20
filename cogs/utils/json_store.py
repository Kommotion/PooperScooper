"""Thread-safe JSON file reads/writes using an exclusive lock file."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT = 10.0
DEFAULT_LOCK_POLL = 0.05


class LockedJsonFile:
    def __init__(
        self,
        path: str | Path,
        *,
        default: Callable[[], Any] | Any = dict,
        indent: int | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._default_factory = default if callable(default) else (lambda: default)
        self.indent = indent
        self.lock_timeout = lock_timeout

    def _acquire_lock(self) -> None:
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock for {self.path}")
                time.sleep(DEFAULT_LOCK_POLL)

    def _release_lock(self) -> None:
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Failed to release lock %s: %s", self.lock_path, e)

    def read(self) -> Any:
        self._acquire_lock()
        try:
            if not self.path.is_file():
                return self._default_factory()
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            log.error("%s is corrupted; returning default value.", self.path)
            return self._default_factory()
        finally:
            self._release_lock()

    def write(self, data: Any) -> None:
        self._acquire_lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=self.indent)
            os.replace(tmp_path, self.path)
        finally:
            self._release_lock()

    def update(self, mutator: Callable[[Any], Any]) -> Any:
        self._acquire_lock()
        try:
            if self.path.is_file():
                try:
                    with open(self.path, encoding="utf-8") as f:
                        data = json.load(f)
                except json.JSONDecodeError:
                    log.error("%s is corrupted; resetting to default.", self.path)
                    data = self._default_factory()
            else:
                data = self._default_factory()

            data = mutator(data)
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=self.indent)
            os.replace(tmp_path, self.path)
            return data
        finally:
            self._release_lock()