"""Persistent record of sent Telegram alerts and alerts muted by quiet hours."""
import json
import logging
import math
import time
from pathlib import Path

WEEK = 7 * 86400
MAX_EDIT_FAILURES = 3


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _sent_entry(value):
    """Return a valid entry, downgrade a damaged message record to its timestamp, or None."""
    if _number(value):
        return float(value)
    if not isinstance(value, dict) or not _number(value.get("at")):
        return None
    position = value.get("position")
    valid = (isinstance(value.get("entity"), str) and isinstance(value.get("message_id"), int)
             and not isinstance(value.get("message_id"), bool)
             and isinstance(position, (list, tuple)) and len(position) == 2 and all(map(_number, position))
             and isinstance(value.get("inside"), bool))
    if not valid:
        return float(value["at"])
    failures = value.get("edit_failures", 0)
    return {"at": float(value["at"]), "entity": value["entity"], "message_id": value["message_id"],
            "position": [float(position[0]), float(position[1])], "inside": value["inside"],
            "photo": bool(value.get("photo", False)),
            "edit_failures": failures if isinstance(failures, int) and failures >= 0 else 0}


def _muted_entry(value):
    if isinstance(value, dict) and _number(value.get("at")) and isinstance(value.get("line"), str):
        return {"at": float(value["at"]), "line": value["line"]}
    return None


class AlertState:
    def __init__(self, path, sent=None, muted=None):
        self.path = Path(path)
        self.sent = sent or {}
        self.muted = muted or {}

    @classmethod
    def load(cls, path):
        """Load state; a damaged file is set aside and replaced by an empty state.

        Returns (state, problem) where problem is None or a short description.
        """
        path = Path(path)
        if not path.exists():
            return cls(path), None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Alert state is not a JSON object")
        except (OSError, UnicodeError, ValueError) as error:
            backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                path.replace(backup)
            except OSError:
                backup = None
            logging.warning("Alert state %s is unreadable (%s); starting empty%s", path, error,
                            f", saved a copy as {backup.name}" if backup else "")
            return cls(path), "The saved alert history was unreadable and has been reset."
        if data.get("version") == 2:
            raw_sent, raw_muted = data.get("sent", {}), data.get("muted", {})
        else:
            raw_sent, raw_muted = data, {}  # Version 1 stored the sent map at the top level.
        sent = {}
        for key, value in (raw_sent.items() if isinstance(raw_sent, dict) else ()):
            entry = _sent_entry(value)
            if isinstance(key, str) and entry is not None:
                sent[key] = entry
        muted = {}
        for key, value in (raw_muted.items() if isinstance(raw_muted, dict) else ()):
            entry = _muted_entry(value)
            if isinstance(key, str) and entry is not None:
                muted[key] = entry
        return cls(path, sent, muted), None

    @staticmethod
    def sent_at(value):
        return value["at"] if isinstance(value, dict) else value

    def prune(self, now, max_age=WEEK):
        self.sent = {key: value for key, value in self.sent.items() if now - self.sent_at(value) < max_age}
        self.muted = {key: value for key, value in self.muted.items() if now - value["at"] < max_age}

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 2, "sent": self.sent, "muted": self.muted}), encoding="utf-8")
        temp.replace(self.path)

    def editable(self, key, entity):
        entry = self.sent.get(key)
        return (isinstance(entry, dict) and entry["entity"] == entity
                and entry["edit_failures"] < MAX_EDIT_FAILURES)

    def edit_failed(self, key):
        """Count a failed edit. Returns True when edits for this message are now abandoned."""
        entry = self.sent[key]
        entry["edit_failures"] += 1
        return entry["edit_failures"] >= MAX_EDIT_FAILURES
