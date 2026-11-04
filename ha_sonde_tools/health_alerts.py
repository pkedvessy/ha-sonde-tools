import json
import logging
import time
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError

STATE = Path("/data/health.json")


def error_detail(error):
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    return type(error).__name__


class HealthAlerts:
    def __init__(self, enabled):
        self.enabled = enabled
        self.lock = Lock()
        self.failures = {}
        self.pending = {}
        self.reported = set()
        self.retry_at = 0
        if enabled and STATE.exists():
            try:
                data = json.loads(STATE.read_text(encoding="utf-8"))
                self.pending = {key: value for key, value in data.get("pending", {}).items()
                                if isinstance(key, str) and isinstance(value, str)}
                self.reported = {key for key in data.get("reported", []) if isinstance(key, str)}
            except (OSError, TypeError, ValueError, AttributeError):
                logging.warning("Unable to load health alert state; using memory")

    def _save(self):
        try:
            temp = STATE.with_suffix(".tmp")
            temp.write_text(json.dumps({"pending": self.pending, "reported": sorted(self.reported)}),
                            encoding="utf-8")
            temp.replace(STATE)
        except OSError:
            logging.warning("Unable to save health alert state; using memory")

    def fail(self, component, detail, threshold=3):
        if not self.enabled:
            return
        with self.lock:
            count = self.failures.get(component, 0) + 1
            self.failures[component] = count
            if count >= threshold and component not in self.reported and component not in self.pending:
                self.pending[component] = detail
                logging.warning("Health incident queued: %s: %s", component, detail)
                self._save()

    def healthy(self, component):
        if not self.enabled:
            return
        with self.lock:
            self.failures.pop(component, None)
            if component in self.reported:
                self.reported.remove(component)
                self._save()

    def flush(self, send, immediate=False):
        if not self.enabled:
            return
        now = time.monotonic()
        with self.lock:
            if not self.pending or (not immediate and now < self.retry_at):
                return
            batch = dict(self.pending)
            self.retry_at = now + 60
        message = "Sonde Tools · health alert\n" + "\n".join(
            f"• {component}: {detail}" for component, detail in batch.items()
        ) + "\nSee the app log for details."
        try:
            send(message)
        except (OSError, EOFError, TypeError, ValueError, RuntimeError) as error:
            logging.warning("Health alert delivery pending (%s)", error_detail(error))
            return
        with self.lock:
            for component in batch:
                self.pending.pop(component, None)
                if component in self.failures:
                    self.reported.add(component)
            self._save()
