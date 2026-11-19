"""Alert state, settings, and health alert persistence."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import support  # noqa: F401

import health_alerts
import settings
from alert_state import MAX_EDIT_FAILURES, WEEK, AlertState


class AlertStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "sent.json"

    def message(self, **changes):
        entry = {"at": 100.0, "entity": "notify.chat", "message_id": 7, "position": [47.0, 19.0],
                 "inside": True, "photo": False}
        entry.update(changes)
        return entry

    def test_missing_file_is_empty(self):
        state, problem = AlertState.load(self.path)
        self.assertEqual((state.sent, state.muted, problem), ({}, {}, None))

    def test_corrupt_file_is_set_aside(self):
        self.path.write_text("{truncated", encoding="utf-8")
        state, problem = AlertState.load(self.path)
        self.assertEqual(state.sent, {})
        self.assertIsNotNone(problem)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.path.parent.glob("sent.json.corrupt-*"))), 1)

    def test_version_one_file_is_migrated(self):
        self.path.write_text(json.dumps({"Predicted landing/A": self.message(), "Estimated/B": 50}), encoding="utf-8")
        state, _ = AlertState.load(self.path)
        self.assertEqual(state.sent["Estimated/B"], 50.0)
        self.assertEqual(state.sent["Predicted landing/A"]["edit_failures"], 0)
        state.save()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["version"], 2)

    def test_damaged_message_record_keeps_its_timestamp(self):
        damaged = self.message()
        del damaged["position"]
        self.path.write_text(json.dumps({"k": damaged, "bad": "text", "worse": {"entity": "x"}}), encoding="utf-8")
        state, _ = AlertState.load(self.path)
        self.assertEqual(state.sent, {"k": 100.0})

    def test_round_trip_with_muted(self):
        state = AlertState(self.path, {"k": self.message()}, {"m": {"at": 5.0, "line": "held"}})
        state.save()
        loaded, _ = AlertState.load(self.path)
        self.assertEqual(loaded.muted, {"m": {"at": 5.0, "line": "held"}})
        self.assertTrue(loaded.editable("k", "notify.chat"))
        self.assertFalse(loaded.editable("k", "notify.other"))

    def test_edits_are_abandoned_after_repeated_failures(self):
        state = AlertState(self.path, {"k": self.message(edit_failures=0)})
        results = [state.edit_failed("k") for _ in range(MAX_EDIT_FAILURES)]
        self.assertEqual(results, [False] * (MAX_EDIT_FAILURES - 1) + [True])
        self.assertFalse(state.editable("k", "notify.chat"))

    def test_prune_drops_week_old_entries(self):
        state = AlertState(self.path, {"old": 0.0, "new": self.message(at=WEEK)}, {"m": {"at": 0.0, "line": ""}})
        state.prune(WEEK + 1)
        self.assertEqual(list(state.sent), ["new"])
        self.assertEqual(state.muted, {})


class SettingsTests(unittest.TestCase):
    BASE = {"zone": "zone.home", "radius_km": 30, "archive_radius_km": 0, "telegram_entity": " notify.chat ",
            "timezone": "Europe/Budapest"}

    def test_defaults(self):
        parsed = settings.parse(self.BASE)
        self.assertEqual(parsed.telegram_entity, "notify.chat")
        self.assertEqual(parsed.archive_radius_km, 30)
        self.assertEqual(parsed.tiles, {"style": settings.DEFAULT_STYLE})
        self.assertFalse(parsed.is_quiet())
        self.assertEqual(parsed.telemetry_retention_days, 0)

    def test_rejects_bad_values(self):
        for change in ({"telegram_entity": "telegram.chat"}, {"timezone": "Mars/Base"},
                       {"dnd_enabled": True, "dnd_from": "10pm"}, {"radius_km": "30"},
                       {"telemetry_retention_days": -1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                settings.parse({**self.BASE, **change})

    def test_quiet_hours(self):
        parsed = settings.parse({**self.BASE, "dnd_enabled": True, "dnd_from": "22:00", "dnd_to": "07:00"})
        tz = ZoneInfo("Europe/Budapest")
        self.assertTrue(parsed.is_quiet(datetime(2026, 11, 8, 23, tzinfo=tz)))
        self.assertFalse(parsed.is_quiet(datetime(2026, 11, 8, 12, tzinfo=tz)))

    def test_custom_tiles_need_attribution(self):
        url = "https://tiles.example.org/{z}/{x}/{y}.png"
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(settings.parse({**self.BASE, "map_tile_url": url}).custom_tile_url)
        parsed = settings.parse({**self.BASE, "map_tile_url": url, "map_tile_attribution": "© <Example>"})
        self.assertEqual(parsed.custom_tile_url, url)
        self.assertEqual(parsed.tiles["attribution"], "© &lt;Example&gt;")


class HealthAlertTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(health_alerts, "STATE", Path(directory.name) / "health.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_incident_is_reported_once_until_recovered(self):
        sent = []
        health = health_alerts.HealthAlerts(True)
        for _ in range(3):
            health.fail("Feed", "down")
        health.flush(sent.append, immediate=True)
        self.assertEqual(len(sent), 1)
        self.assertIn("Feed: down", sent[0])
        health.fail("Feed", "down")
        health.flush(sent.append, immediate=True)
        self.assertEqual(len(sent), 1)
        health.healthy("Feed")
        for _ in range(3):
            health.fail("Feed", "down again")
        health.flush(sent.append, immediate=True)
        self.assertEqual(len(sent), 2)

    def test_failed_delivery_stays_pending_across_restarts(self):
        health = health_alerts.HealthAlerts(True)
        health.fail("Zone", "missing", threshold=1)

        def broken(message):
            raise OSError("offline")
        with self.assertLogs(level="WARNING"):
            health.flush(broken, immediate=True)
        restarted = health_alerts.HealthAlerts(True)
        self.assertEqual(restarted.pending, {"Zone": "missing"})


if __name__ == "__main__":
    unittest.main()
