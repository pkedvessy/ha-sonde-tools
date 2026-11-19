"""Alert decisions in the notifier, with Telegram and Home Assistant replaced by fakes."""
import importlib.util
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import support

import settings

HAS_PAHO = importlib.util.find_spec("paho") is not None
if HAS_PAHO:
    import notifier
    from health_alerts import HealthAlerts

HOME = (46.34, 20.41)
NOW = 1_800_000_000.0


@unittest.skipUnless(HAS_PAHO, "paho-mqtt is not installed")
class NotifierAlertTests(unittest.TestCase):
    def setUp(self):
        _, directory = support.temp_database(self)
        patcher = mock.patch.object(notifier, "STATE", directory / "sent.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.quiet = False
        cfg = {"zone": "zone.home", "radius_km": 30, "telegram_entity": "notify.chat", "timezone": "UTC",
               "telegram_map_image": False}
        self.app = notifier.Notifier(settings.parse(cfg), "token", HealthAlerts(False))
        self.app.quiet = lambda: self.quiet
        self.sent = []
        self.app.send = lambda message, photo=None: self.sent.append(message) or [{"message_id": len(self.sent)}]
        self.requests = []
        request = mock.patch.object(notifier, "request", side_effect=self.fake_request)
        request.start()
        self.addCleanup(request.stop)
        self.fail_edits = False

    def fake_request(self, url, token=None, payload=None, **kwargs):
        self.requests.append((url, payload))
        if self.fail_edits:
            raise OSError("HTTP 400")
        return {}

    def predicted(self, lat=46.40, now=NOW):
        self.app.alert("Predicted landing", ("S1", (lat, HOME[1]), now + 600), HOME, now)

    def test_predicted_landing_is_sent_once_then_edited(self):
        self.predicted()
        self.predicted()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.requests, [])
        self.predicted(lat=46.50)  # Moved more than 1 km.
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0][0].endswith("/edit_message"))
        self.assertIn("moved", self.requests[0][1]["message"])

    def test_outside_radius_is_archived_but_not_sent(self):
        self.app.archive_radius = 100
        self.predicted(lat=47.2)
        self.assertEqual(self.sent, [])
        self.assertEqual(support.count(notifier.landing_history.DATABASE, "events"), 1)

    def test_failed_edits_are_abandoned(self):
        self.predicted()
        self.fail_edits = True
        for step in range(2):
            with self.assertRaises(OSError):
                self.predicted(lat=46.50 + step / 10)
        with self.assertRaises(OSError), self.assertLogs(level="WARNING") as logs:
            self.predicted(lat=46.70)
        self.assertIn("could not be edited repeatedly", logs.output[0])
        self.requests.clear()
        self.predicted(lat=47.0)
        self.assertEqual(self.requests, [])

    def test_quiet_hours_hold_alerts_for_a_summary(self):
        self.quiet = True
        self.app.alert("Estimated landing after signal loss", ("S2", (46.35, 20.40), NOW, (46.36, 20.40)), HOME, NOW)
        self.assertEqual(self.sent, [])
        self.assertIn("Estimated landing after signal loss/S2", self.app.state.muted)
        self.quiet = False
        self.app.send_quiet_summary()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("during quiet hours", self.sent[0])
        self.assertIn("S2", self.sent[0])
        # The held alert counts as delivered, so it is not sent again.
        self.app.alert("Estimated landing after signal loss", ("S2", (46.35, 20.40), NOW, (46.36, 20.40)), HOME, NOW)
        self.assertEqual(len(self.sent), 1)

    def test_current_alert_after_quiet_hours_replaces_its_summary_line(self):
        self.quiet = True
        self.predicted()
        self.quiet = False
        self.predicted()
        self.app.send_quiet_summary()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("during quiet hours", self.sent[0])

    def test_corrupt_state_file_does_not_stop_startup(self):
        notifier.STATE.write_text("not json", encoding="utf-8")
        with self.assertLogs(level="WARNING"):
            app = notifier.Notifier(self.app.settings, "token", HealthAlerts(False))
        self.assertEqual(app.state.sent, {})

    def test_dashboard_state_lists_nearest_live_sonde(self):
        published = []
        self.app.dashboard = mock.Mock(publish=published.append)
        when = datetime.fromtimestamp(NOW - 20, ZoneInfo("UTC")).isoformat()
        self.app.history = {"S3": (NOW - 20, support.telemetry_row("S3", when, 46.35, 20.41))}
        self.app.update_dashboard(HOME, NOW)
        self.assertEqual(published[0]["state"], "S3")
        self.assertEqual(published[0]["attributes"]["distance_km"], 1.1)

    def test_watchdog_heartbeat(self):
        self.assertTrue(self.app.alive())
        self.app.heartbeat = self.app.started - notifier.STALL_SECONDS - 1
        self.assertFalse(self.app.alive())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_PAHO, "paho-mqtt is not installed")
class DashboardTests(unittest.TestCase):
    def test_falls_back_to_rest_without_mqtt(self):
        import dashboard
        calls = []

        def request(url, token=None, payload=None, **kwargs):
            calls.append((url, payload))
            if url.endswith("/services/mqtt"):
                raise OSError("HTTP 400")
            return {}
        with self.assertLogs(level="INFO"):
            board = dashboard.Dashboard(request, "token", HealthAlerts(False))
        self.assertEqual(board.mode, "rest")
        board.publish({"state": "S1", "attributes": {}})
        self.assertEqual(calls[-1], (f"{dashboard.CORE}/states/{dashboard.ENTITY_ID}", {"state": "S1", "attributes": {}}))

    def test_discovery_config_keeps_entity_id(self):
        import dashboard
        config = dashboard.discovery_config()
        self.assertEqual(config["default_entity_id"], "sensor.sonde_tools_nearest_active")
        self.assertTrue(config["state_topic"].startswith(dashboard.BASE))
