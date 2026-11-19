import json
import unittest
from datetime import datetime, time, timezone

import support

from detection import forecasts, in_quiet_hours, parse_clock, point, prepare_samples, timestamp

NOW = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc).timestamp()


def forecast_row(serial="S1", issued_offset=-60, eta_offset=900, landed=0, lat=47.0, lon=19.0, as_text=True):
    issued = datetime.fromtimestamp(NOW + issued_offset, timezone.utc).isoformat()
    path = [{"time": NOW, "lat": 46.0, "lon": 18.0}, {"time": NOW + eta_offset, "lat": lat, "lon": lon}]
    return {"vehicle": serial, "time": issued, "landed": landed, "data": json.dumps(path) if as_text else path,
            "latitude": 46.0, "longitude": 18.0}


class PointTests(unittest.TestCase):
    def test_normalizes_longitude(self):
        self.assertEqual(point(10, 350), (10.0, -10.0))

    def test_rejects_invalid_latitude(self):
        with self.assertRaises(ValueError):
            point(91, 0)

    def test_timestamp_requires_zone(self):
        with self.assertRaises(ValueError):
            timestamp("2026-11-08T01:00:00")
        self.assertEqual(timestamp("2026-11-08T01:00:00Z"), NOW)


class ForecastTests(unittest.TestCase):
    def test_yields_newest_fresh_forecast(self):
        rows = [forecast_row(issued_offset=-300, lat=46.5), forecast_row(issued_offset=-60, lat=47.0)]
        self.assertEqual(list(forecasts(rows, NOW)), [("S1", (47.0, 19.0), NOW + 900)])

    def test_skips_stale_landed_and_past_eta(self):
        rows = [forecast_row("OLD", issued_offset=-1300), forecast_row("LANDED", landed=1),
                forecast_row("PAST", eta_offset=-10, issued_offset=-30)]
        self.assertEqual(list(forecasts(rows, NOW)), [])

    def test_accepts_decoded_path_and_reports_errors(self):
        errors = []
        broken = forecast_row("BROKEN")
        broken["data"] = "not json"
        rows = [forecast_row(as_text=False), broken, {"vehicle": "X"}]
        result = list(forecasts(rows, NOW, errors=errors))
        self.assertEqual([serial for serial, *_ in result], ["S1"])
        self.assertEqual(len(errors), 2)

    def test_save_callback_receives_every_fresh_forecast(self):
        saved = []
        rows = [forecast_row(issued_offset=-300), forecast_row(issued_offset=-60)]
        list(forecasts(rows, NOW, save=lambda *args: saved.append(args[:3])))
        self.assertEqual(len(saved), 2)


class PrepareSamplesTests(unittest.TestCase):
    def test_filters_by_radius_window_and_validity(self):
        center = (46.34, 20.41)
        batch = [support.telemetry_row(), support.telemetry_row(lat=48.0),
                 support.telemetry_row(when="2026-11-08T05:00:00Z"), {"serial": "BAD"}]
        start = timestamp("2026-11-08T00:00:00Z")
        end = timestamp("2026-11-08T02:00:00Z")
        samples, invalid = prepare_samples(batch, center, 30, start, end)
        self.assertEqual([(serial, uploader) for serial, _, uploader, _ in samples], [("X1722353", "TEST1")])
        self.assertEqual(invalid, 1)

    def test_rejects_non_finite_values(self):
        samples, invalid = prepare_samples([support.telemetry_row(alt=float("nan"))], (46.34, 20.41), 30)
        self.assertEqual((samples, invalid), ([], 1))


class QuietHoursTests(unittest.TestCase):
    def test_overnight_window(self):
        start, end = time(22), time(7)
        self.assertTrue(in_quiet_hours(time(23, 30), start, end))
        self.assertTrue(in_quiet_hours(time(0, 0), start, end))
        self.assertTrue(in_quiet_hours(time(6, 59), start, end))
        self.assertFalse(in_quiet_hours(time(7, 0), start, end))
        self.assertFalse(in_quiet_hours(time(21, 59), start, end))

    def test_daytime_window_and_empty_window(self):
        self.assertTrue(in_quiet_hours(time(13), time(12), time(14)))
        self.assertFalse(in_quiet_hours(time(14), time(12), time(14)))
        self.assertFalse(in_quiet_hours(time(12), time(12), time(12)))

    def test_parse_clock_is_strict(self):
        self.assertEqual(parse_clock("07:05", "dnd_to"), time(7, 5))
        for value in ("7:05", "07:05:00", "25:00", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_clock(value, "dnd_to")


if __name__ == "__main__":
    unittest.main()
