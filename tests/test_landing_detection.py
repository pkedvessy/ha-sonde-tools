"""Run with: python -m unittest discover -s tests."""
import ast
import math
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone

# Load the production detector without starting MQTT, HA, or application services.
ROOT = Path(__file__).resolve().parents[1] / "ha_sonde_tools"
sys.path.insert(0, str(ROOT))
from field_tools import distance
source = ast.parse((ROOT / "notifier.py").read_text(encoding="utf-8"))
functions = [node for node in source.body if isinstance(node, ast.FunctionDef)
             and node.name in ("timestamp", "point", "landings")]
namespace = dict(math=math, datetime=datetime, distance=distance,
                 SIGNAL_LOSS_MAX_ALTITUDE_M=500, logging=__import__("logging"))
exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / "notifier.py"), "exec"), namespace)
landings = namespace["landings"]


class LandingDetectionTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 11, 8, 1, 6, 47, tzinfo=timezone.utc).timestamp()
        self.row = dict(datetime="2026-11-08T01:06:47Z", lat=46.33959, lon=20.4069,
                        alt=157.02147, vel_v=-16.70299, vel_h=3.98718, heading=343.09175)

    def replay(self, delay, eta_delay, altitude=None):
        row = dict(self.row)
        if altitude is not None:
            row["alt"] = altitude
        forecast = {"X1722353": (self.at-60, self.at+eta_delay, (row["lat"], row["lon"]))}
        return list(landings({"X1722353": row}, {}, self.at+delay, None, forecast))

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


if __name__ == "__main__":
    unittest.main()
