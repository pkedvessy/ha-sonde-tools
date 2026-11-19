"""SQLite archive: storage round trip, retention, compaction, and page rendering."""
import json
import sqlite3
import time
import unittest
from contextlib import closing
from zoneinfo import ZoneInfo

import support

import landing_history
import maintenance
from detection import map_url, prepare_samples


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.database, _ = support.temp_database(self)
        self.center = (46.34, 20.41)

    def store(self, *rows):
        samples, invalid = prepare_samples(rows, self.center, 50)
        self.assertEqual(invalid, 0)
        return landing_history.record_telemetry(samples)

    def test_telemetry_round_trip_preserves_extra_fields(self):
        row = support.telemetry_row(xdata="0501", custom={"nested": [1, True, None]}, sats="12")
        self.assertEqual(self.store(row), 1)
        self.assertEqual(self.store(row), 0)  # Duplicate reports are ignored.
        exported = json.loads(landing_history.read_telemetry("x1722353"))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["custom"], {"nested": [1, True, None]})
        self.assertEqual(exported[0]["sats"], 12)  # Known counters are normalized to numbers.
        self.assertIsNone(exported[0]["sondehub_prediction"])

    def test_retention_removes_old_telemetry_and_forecasts_only(self):
        self.store(support.telemetry_row(when="2020-01-01T00:00:00Z", uploader_callsign="A"),
                   support.telemetry_row(when="2020-01-01T00:00:01Z", uploader_callsign="B"),
                   support.telemetry_row(when="2026-11-08T01:00:00Z", custom=1))
        landing_history.record("X1722353", "Predicted landing", 1.0, 2.0, self.center, 0.0, None)
        old = 1577836800.0
        landing_history.record_forecasts([
            ("X1722353", old, old + 600, 46.3, 20.4, [{"lat": 46.3, "lon": 20.4}], True, "X1722353", old - 1200),
        ])
        removed = maintenance.prune(self.database, time.time() - 365 * 86400, batch=1)
        self.assertEqual(removed, (2, 1))
        self.assertEqual(support.count(self.database, "telemetry_fixes"), 1)
        self.assertEqual(support.count(self.database, "telemetry_reports"), 1)
        self.assertEqual(support.count(self.database, "telemetry_fields"), 1)
        self.assertEqual(support.count(self.database, "telemetry_receivers"), 1)
        self.assertEqual(support.count(self.database, "forecast_points"), 0)
        self.assertEqual(support.count(self.database, "events"), 1)

    def test_compaction_runs(self):
        self.store(support.telemetry_row())
        maintenance.compact(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_maintenance_rejects_overlapping_compaction(self):
        worker = maintenance.Maintenance(self.database, 0, health=None)
        worker.request_compact()
        with self.assertRaises(ValueError):
            worker.request_compact()

    def test_pages_render_with_bundled_scripts(self):
        self.store(support.telemetry_row())
        worker = maintenance.Maintenance(self.database, 30, health=None)
        tz = ZoneInfo("UTC")
        for tab, marker in (("history", "sonde_tools_flight"), ("map", 'id="map-data"'),
                            ("flight", 'id="map-data"'), ("scorecard", "<section"),
                            ("backfill", "Compact database"), ("diagnostics", "Run integration checks")):
            with self.subTest(tab=tab):
                query = {"tab": [tab], "serial": ["X1722353"]}
                page = landing_history.history_page(query, self.center, 30, tz, map_url, 20, "token",
                                                    maintenance=worker)
                self.assertIn(marker, page)
                self.assertTrue(page.rstrip().endswith("</html>"))
        self.assertTrue(all(source.strip() for source in landing_history.SCRIPTS.values()))


if __name__ == "__main__":
    unittest.main()


class WebServerTests(unittest.TestCase):
    def setUp(self):
        support.temp_database(self)
        self.alive = True
        self.server = landing_history.serve(lambda: None, 30, ZoneInfo("UTC"), map_url, lambda serials: [], 30,
                                            lambda serial: 0, heartbeat=lambda: self.alive, port=0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        from urllib.error import HTTPError
        from urllib.request import urlopen
        try:
            with urlopen(self.base + path, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            error.close()
            return error.code, b""

    def test_health_endpoint_follows_heartbeat(self):
        self.assertEqual(self.get("/health"), (200, b"ok"))
        self.alive = False
        self.assertEqual(self.get("/health")[0], 503)

    def test_pages_are_limited_to_ingress(self):
        self.assertEqual(self.get("/")[0], 403)
