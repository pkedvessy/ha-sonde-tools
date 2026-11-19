"""Sounding charts, records page, live MQTT entities, and alerts for followed people."""
import importlib.util
import json
import math
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import support

import landing_history
import live_entities
import records
import settings
import sounding
from detection import map_url, prepare_samples

HOME = (46.34, 20.41)
UTC = ZoneInfo("UTC")
START = datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc).timestamp()
HAS_PAHO = importlib.util.find_spec("paho") is not None


def standard_temperature(alt):
    return 15 - 6.5 * min(alt, 11000) / 1000


def flight_rows(serial="T1000001", pressure=True, top=25000):
    """A synthetic ascent at 5 m/s, then a descent, with a standard atmosphere and a west wind."""
    rows = []
    seconds = 0
    for alt in list(range(100, top, 50)) + list(range(top, 0, -200)):
        ascending = len(rows) < (top - 100) // 50
        when = datetime.fromtimestamp(START + seconds, timezone.utc).isoformat().replace("+00:00", "Z")
        row = support.telemetry_row(serial, when, HOME[0] + seconds / 1e5, HOME[1] + seconds / 1e5,
                                    alt=float(alt), vel_v=5.0 if ascending else -10.0,
                                    vel_h=5 + 30 * math.exp(-((alt - 11000) / 3000) ** 2), heading=90.0,
                                    temp=standard_temperature(alt), humidity=60.0, type="RS41")
        if pressure:
            row["pressure"] = sounding.isa_pressure(alt)
        rows.append(row)
        seconds += 10 if ascending else 20
    return rows


class SoundingTests(unittest.TestCase):
    def setUp(self):
        self.database, _ = support.temp_database(self)

    def store(self, rows):
        samples, invalid = prepare_samples(rows, HOME, 500)
        self.assertEqual(invalid, 0)
        landing_history.record_telemetry(samples)

    def read(self, serial="T1000001"):
        with closing(sqlite3.connect(self.database)) as db:
            return sounding.read(db, serial)

    def test_ascent_profile_finds_tropopause_freezing_level_and_jet(self):
        self.store(flight_rows())
        data = self.read()
        self.assertEqual(data["phase"], "ascent")
        self.assertEqual(data["pressure_source"], "sensor")
        notes = sounding.summary(data)
        self.assertAlmostEqual(notes["tropopause"]["alt"] / 1000, 11, delta=0.3)
        self.assertAlmostEqual(notes["freezing"], 15 / 6.5 * 1000, delta=150)
        speed, level = notes["jet"]
        self.assertAlmostEqual(speed, 35, delta=1)
        self.assertAlmostEqual(level["alt"] / 1000, 11, delta=0.3)
        # The sonde moves east, so the wind blows from the west.
        self.assertAlmostEqual(sounding._wind_from(level), 270, delta=1)
        html = sounding.chart(data)
        self.assertEqual(html.count("<svg"), 3)
        self.assertIn("Tropopause", html)

    def test_pressure_is_derived_without_a_sensor(self):
        self.store(flight_rows(pressure=False))
        data = self.read()
        self.assertEqual(data["pressure_source"], "derived")
        top = data["levels"][-1]
        self.assertAlmostEqual(top["pressure"], sounding.isa_pressure(top["alt"]), delta=top["pressure"] * 0.03)

    def test_dew_point(self):
        self.assertAlmostEqual(sounding.dew_point(20, 50), 9.3, delta=0.1)
        self.assertIsNone(sounding.dew_point(20, 0))

    def test_too_few_fixes_has_no_chart(self):
        self.store(flight_rows()[:3])
        self.assertIsNone(self.read())
        self.assertEqual(sounding.chart(None), "")

    def test_flight_page_includes_sounding(self):
        self.store(flight_rows())
        page = landing_history.history_page({"tab": ["flight"], "serial": ["T1000001"]}, HOME, 30, UTC, map_url)
        self.assertIn("Atmospheric sounding", page)


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.database, _ = support.temp_database(self)

    def test_records_page_lists_highest_flight_and_recovery_rate(self):
        rows = flight_rows("T1000001", top=30000) + flight_rows("T1000002", top=20000)
        samples, _ = prepare_samples(rows, HOME, 500)
        landing_history.record_telemetry(samples)
        landing_history.record("T1000002", "Estimated landing after signal loss", START + 9000, START + 9000,
                               (46.35, 20.42), 1.4, None)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO recovery VALUES ('T1000002', 'Recovered', ?, ?, 46.35, 20.42)",
                       (START + 9500, START + 9500))
        data = records.read(self.database, 2026, UTC)
        found = {label: (value, serial) for label, value, serial, _ in records.records(data, UTC)}
        self.assertEqual(found["Highest altitude"][1], "T1000001")
        self.assertEqual(found["Closest landing"], ("1.40 km", "T1000002"))
        self.assertEqual(records.recovery_stats(data), (1, 1, 0))
        page = landing_history.history_page({"tab": ["records"]}, HOME, 30, UTC, map_url)
        self.assertIn("Flight calendar 2026", page)
        self.assertIn("100%", page)
        self.assertIn("RS41", page)
        all_time = landing_history.history_page({"tab": ["records"], "year": ["all"]}, HOME, 30, UTC, map_url)
        self.assertIn("by year", all_time)

    def test_summaries_survive_retention(self):
        import maintenance
        rows = flight_rows("T1000003", top=28000)
        rows.append(dict(rows[10], alt=99999.0, temp=-500.0))  # Decoding glitch on a new fix: ignored.
        rows[-1]["datetime"] = rows[-1]["datetime"].replace("T11:", "T23:")
        samples, _ = prepare_samples(rows, HOME, 500)
        landing_history.record_telemetry(samples)
        landing_history.record_telemetry(samples)  # Duplicates do not change the summary.
        with closing(sqlite3.connect(self.database)) as db:
            before = db.execute("SELECT * FROM flight_summaries").fetchall()
        self.assertEqual(len(before), 1)
        serial, _, _, fixes, altitude, speed, vertical, temp, kind = before[0]
        self.assertEqual((serial, fixes, kind), ("T1000003", len(rows), "RS41"))
        self.assertAlmostEqual(altitude, 28000)
        self.assertAlmostEqual(speed, 35, delta=1)
        self.assertEqual(vertical, -10)
        self.assertAlmostEqual(temp, -56.5)
        self.assertEqual(maintenance.prune(self.database, START + 86400)[0], len(rows))
        self.assertEqual(support.count(self.database, "flight_summaries"), 1)
        found = {label: value for label, value, *_ in records.records(records.read(self.database, 2026, UTC), UTC)}
        self.assertEqual(found["Highest altitude"], "28.00 km")
        page = landing_history.history_page({"tab": ["flight"], "serial": ["T1000003"]}, HOME, 30, UTC, map_url)
        self.assertIn("removed by telemetry retention", page)
        self.assertIn("Highest 28.00 km", page)
        self.assertEqual(landing_history.archive_count(self.database, -1), 1)

    def test_existing_archive_is_summarized_once_on_upgrade(self):
        samples, _ = prepare_samples(flight_rows("T1000004", top=5000), HOME, 500)
        landing_history.record_telemetry(samples)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TABLE flight_summaries")
            db.execute("DELETE FROM archive_migrations WHERE name = 'flight_summaries_v1'")
        landing_history.initialize()
        landing_history.initialize()
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT serial, fixes FROM flight_summaries").fetchall(),
                             [("T1000004", len(samples))])

    def test_month_filter_narrows_records_and_charts_by_day(self):
        june = flight_rows("T1000005", top=30000)
        july = [dict(row, datetime=row["datetime"].replace("2026-06-", "2026-07-")) for row in flight_rows("T1000006")]
        samples, _ = prepare_samples(june + july, HOME, 500)
        landing_history.record_telemetry(samples)
        self.assertEqual(records.period(2026, UTC, 12),
                         (datetime(2026, 12, 1, tzinfo=UTC).timestamp(), datetime(2027, 1, 1, tzinfo=UTC).timestamp()))
        data = records.read(self.database, 2026, UTC, 7)
        self.assertEqual([row[0] for row in data["flights"]], ["T1000006"])
        page = landing_history.history_page({"tab": ["records"], "year": ["2026"], "month": ["7"]}, HOME, 30, UTC,
                                            map_url)
        self.assertIn("Showing <strong>July 2026</strong>", page)
        self.assertIn("by day", page)
        self.assertIn('<option value="7" selected>July</option>', page)
        self.assertNotIn("Flight calendar", page)
        self.assertIn("T1000006", page)
        self.assertNotIn("T1000005", page)
        # A month without a year (All time) is ignored.
        page = landing_history.history_page({"tab": ["records"], "year": ["all"], "month": ["7"]}, HOME, 30, UTC,
                                            map_url)
        self.assertIn("Showing <strong>all time</strong>", page)
        self.assertIn("T1000005", page)
        # An invalid month falls back to the whole year.
        page = landing_history.history_page({"tab": ["records"], "year": ["2026"], "month": ["13"]}, HOME, 30, UTC,
                                            map_url)
        self.assertIn("Showing <strong>2026</strong>", page)

    def test_empty_archive(self):
        page = landing_history.history_page({"tab": ["records"]}, HOME, 30, UTC, map_url)
        self.assertIn("No archived flights", page)


NOW = 1_800_000_000.0


class LiveEntityTests(unittest.TestCase):
    def snapshot(self, **overrides):
        values = dict(history={"S1": (NOW - 20, support.telemetry_row("S1", lat=46.36, lon=20.41)),
                               "FAR": (NOW - 20, support.telemetry_row("FAR", lat=50.0, lon=10.0))},
                      predicted={"S1": (NOW - 60, NOW + 600, (46.35, 20.41)),
                                 "S2": (NOW - 60, NOW + 900, (46.0, 20.0))},
                      touchdowns={}, centers=[("Home", HOME)], archive_radius=60, notify_radius=30, now=NOW)
        values.update(overrides)
        return live_entities.snapshot(**values)

    def test_snapshot_keeps_nearby_sondes_and_landings(self):
        state = self.snapshot()
        self.assertEqual(set(state["sondes"]), {"S1"})
        self.assertEqual(state["sondes"]["S1"]["phase"], "descending")
        self.assertEqual(set(state["landings"]), {"S1", "S2"})
        self.assertTrue(state["landings"]["S1"]["inside_alert_radius"])
        self.assertFalse(state["landings"]["S2"]["inside_alert_radius"])
        incoming, active, upcoming = live_entities.summary(state, NOW)
        self.assertEqual(incoming[0], "ON")
        self.assertEqual(incoming[1]["serials"], ["S1"])
        self.assertEqual(active, ("1", {"serials": ["S1"]}))
        self.assertEqual(upcoming[1]["serial"], "S1")

    def test_touchdown_estimate_replaces_forecast(self):
        state = self.snapshot(touchdowns={"S1": (NOW - 60, (46.345, 20.41), "Likely touchdown")})
        self.assertEqual(state["landings"]["S1"]["source"], "Likely touchdown")
        incoming, _, upcoming = live_entities.summary(state, NOW)
        self.assertEqual(incoming[0], "ON")
        self.assertEqual(upcoming[1]["serial"], "S2")

    def test_followed_person_brings_far_sondes_into_range(self):
        state = self.snapshot(centers=[("Home", HOME), ("Alex", (50.01, 10.0))])
        self.assertEqual(state["sondes"]["FAR"]["nearest_to"], "Alex")

    @unittest.skipUnless(HAS_PAHO, "paho-mqtt is not installed")
    def test_publisher_adds_lingers_and_removes_trackers(self):
        from health_alerts import HealthAlerts
        client = mock.Mock()
        with mock.patch.object(live_entities.mqtt_link, "broker", return_value={"host": "core-mosquitto"}), \
                mock.patch.object(live_entities.mqtt_link, "start", return_value=client):
            entities = live_entities.LiveEntities(None, "token", HealthAlerts(False))
        entities._on_connect(client, None, None, mock.Mock(is_failure=False), None)
        topics = lambda: {call.args[0]: call.args[1] for call in client.publish.call_args_list}  # noqa: E731
        self.assertIn("homeassistant/binary_sensor/ha_sonde_tools/incoming/config", topics())
        client.publish.reset_mock()
        entities.update(self.snapshot(), NOW)
        published = topics()
        config = json.loads(published["homeassistant/device_tracker/ha_sonde_tools/sonde_s1/config"])
        self.assertEqual(config["source_type"], "gps")
        attributes = json.loads(published["sonde_tools/live/sonde_s1/attributes"])
        self.assertEqual(attributes["latitude"], 46.36)
        self.assertEqual(published["sonde_tools/live/incoming/state"], "ON")
        client.publish.reset_mock()
        empty = {"sondes": {}, "landings": {}}
        entities.update(empty, NOW + 60)
        self.assertEqual(json.loads(topics()["sonde_tools/live/sonde_s1/attributes"])["signal"], "lost")
        client.publish.reset_mock()
        entities.update(empty, NOW + live_entities.LINGER_SECONDS + 120)
        self.assertEqual(topics()["homeassistant/device_tracker/ha_sonde_tools/sonde_s1/config"], "")
        self.assertEqual(entities.trackers, {})

    @unittest.skipUnless(HAS_PAHO, "paho-mqtt is not installed")
    def test_leftover_trackers_from_a_previous_run_are_removed(self):
        from health_alerts import HealthAlerts
        client = mock.Mock()
        with mock.patch.object(live_entities.mqtt_link, "broker", return_value={"host": "h"}), \
                mock.patch.object(live_entities.mqtt_link, "start", return_value=client):
            entities = live_entities.LiveEntities(None, "token", HealthAlerts(False))
        entities._on_connect(client, None, None, mock.Mock(is_failure=False), None)
        entities._on_message(client, None, mock.Mock(
            topic="homeassistant/device_tracker/ha_sonde_tools/sonde_old1/config", payload=b"{}"))
        client.publish.reset_mock()
        entities.update({"sondes": {}, "landings": {}}, NOW)
        removed = {call.args[0] for call in client.publish.call_args_list if call.args[1] == ""}
        self.assertIn("homeassistant/device_tracker/ha_sonde_tools/sonde_old1/config", removed)


class BrokerSettingsTests(unittest.TestCase):
    base = {"telegram_entity": "notify.chat", "timezone": "UTC"}

    def test_external_broker_is_used_without_asking_the_supervisor(self):
        import mqtt_link
        parsed = settings.parse({**self.base, "mqtt_host": " 10.0.0.102 ", "mqtt_username": "sonde",
                                 "mqtt_password": "secret"})
        self.assertEqual(parsed.mqtt_broker, {"host": "10.0.0.102", "port": 1883, "username": "sonde",
                                              "password": "secret", "ssl": False})
        self.assertNotIn("secret", repr(parsed))
        request = mock.Mock(side_effect=AssertionError("Supervisor must not be asked"))
        self.assertEqual(mqtt_link.broker(request, "token", parsed.mqtt_broker)["host"], "10.0.0.102")

    def test_without_host_the_supervisor_service_is_used(self):
        import mqtt_link
        self.assertIsNone(settings.parse(self.base).mqtt_broker)
        request = mock.Mock(return_value={"data": {"host": "core-mosquitto", "port": 1883}})
        self.assertEqual(mqtt_link.broker(request, "token", None)["host"], "core-mosquitto")

    def test_url_style_host_is_rejected(self):
        with self.assertRaises(ValueError):
            settings.parse({**self.base, "mqtt_host": "mqtt://10.0.0.102:1883"})


class FollowSettingsTests(unittest.TestCase):
    def test_follow_persons_are_validated(self):
        base = {"telegram_entity": "notify.chat", "timezone": "UTC"}
        parsed = settings.parse({**base, "follow_persons": ["person.alex", " person.alex ", "device_tracker.phone"]})
        self.assertEqual(parsed.follow_persons, ("person.alex", "device_tracker.phone"))
        with self.assertRaises(ValueError):
            settings.parse({**base, "follow_persons": ["zone.work"]})


@unittest.skipUnless(HAS_PAHO, "paho-mqtt is not installed")
class FollowAlertTests(unittest.TestCase):
    def setUp(self):
        import notifier
        from health_alerts import HealthAlerts
        _, directory = support.temp_database(self)
        patcher = mock.patch.object(notifier, "STATE", directory / "sent.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        cfg = {"telegram_entity": "notify.chat", "timezone": "UTC", "telegram_map_image": False,
               "follow_persons": ["person.alex"]}
        self.app = notifier.Notifier(settings.parse(cfg), "token", HealthAlerts(False))
        self.app.quiet = lambda: False
        self.sent = []
        self.app.send = lambda message, photo=None: self.sent.append(message) or [{"message_id": 1}]
        states = {"person.alex": {"state": "not_home", "attributes": {
            "latitude": 47.5, "longitude": 19.04, "friendly_name": "Alex"}},
            "zone.home": {"state": "0", "attributes": {"latitude": HOME[0], "longitude": HOME[1],
                                                       "friendly_name": "Home"}}}
        request = mock.patch.object(notifier, "request",
                                    side_effect=lambda url, *args, **kwargs: states[url.rsplit("/", 1)[1]])
        request.start()
        self.addCleanup(request.stop)
        self.app.resolve_zone()
        self.app.centers = [(self.app.zone_name, HOME)] + self.app.resolve_people()

    def test_landing_near_followed_person_is_sent_with_their_name(self):
        self.app.alert("Predicted landing", ("S9", (47.52, 19.05), NOW + 600), HOME, NOW)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("from Alex", self.sent[0])
        # Far from the zone, so nothing is archived.
        self.assertEqual(support.count(landing_history.DATABASE, "events"), 0)

    def test_landing_near_home_names_the_zone(self):
        self.app.alert("Estimated landing after signal loss", ("S8", (46.35, 20.41), NOW, (46.36, 20.41)), HOME, NOW)
        self.assertIn("from Home", self.sent[0])
        self.assertIn("S8", self.app.touchdowns)


if __name__ == "__main__":
    unittest.main()
