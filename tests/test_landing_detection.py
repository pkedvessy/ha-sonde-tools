"""Run with: python -m unittest discover -s tests."""
import unittest
from datetime import datetime, timezone

import support  # noqa: F401  (adds the app directory to sys.path)

from detection import landings


class LandingDetectionTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 11, 8, 1, 6, 47, tzinfo=timezone.utc).timestamp()
        self.row = dict(datetime="2026-11-08T01:06:47Z", lat=46.33959, lon=20.4069,
                        alt=157.02147, vel_v=-16.70299, vel_h=3.98718, heading=343.09175)

    def replay(self, delay, eta_delay, altitude=None, ground=None):
        row = dict(self.row)
        if altitude is not None:
            row["alt"] = altitude
        forecast = {"X1722353": (self.at-60, self.at+eta_delay, (row["lat"], row["lon"]))}
        return list(landings({"X1722353": row}, {}, self.at+delay, ground, forecast))

    def test_future_eta_does_not_block_low_altitude_fallback(self):
        for phase in range(60):
            with self.subTest(phase=phase):
                matches = self.replay(180+phase, 600)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0][3], None)

    def test_future_eta_still_waits_three_minutes(self):
        self.assertEqual(self.replay(179, 600), [])

    def test_high_altitude_does_not_use_early_fallback(self):
        self.assertEqual(self.replay(180, 600, altitude=17000), [])

    def test_forecast_touchdown_after_eta(self):
        result = self.replay(60, 10)
        self.assertEqual(result[0][-1], "Likely touchdown")

    def test_no_forecast_fallback_is_unchanged(self):
        result = list(landings({"X1722353": self.row}, {}, self.at+180, None, {}))
        self.assertEqual(len(result), 1)

    def test_ground_elevation_projects_touchdown(self):
        result = list(landings({"X1722353": self.row}, {}, self.at+180, 100, {}))
        serial, position, when, last = result[0]
        self.assertAlmostEqual(last[0], self.row["lat"])
        self.assertAlmostEqual(last[1], self.row["lon"])
        self.assertNotEqual(position, last)
        self.assertAlmostEqual(when - self.at, (157.02147 - 100) / 16.70299, places=3)

    def test_stale_fix_is_forgotten(self):
        history = {}
        list(landings({"X1722353": self.row}, history, self.at+30, None, {}))
        self.assertIn("X1722353", history)
        list(landings({}, history, self.at+601, None, {}))
        self.assertNotIn("X1722353", history)

    def test_ascending_sonde_is_ignored(self):
        row = dict(self.row, vel_v=5)
        self.assertEqual(list(landings({"X1722353": row}, {}, self.at+300, None, {})), [])


if __name__ == "__main__":
    unittest.main()
