"""Shared test setup: makes the app modules importable and isolates /data paths."""
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "ha_sonde_tools"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


def temp_database(test):
    """Point landing_history at a fresh database for one test case and create the schema."""
    import landing_history
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    original = landing_history.DATABASE
    landing_history.DATABASE = str(Path(directory.name) / "history.sqlite3")
    test.addCleanup(setattr, landing_history, "DATABASE", original)
    landing_history.initialize()
    return landing_history.DATABASE, Path(directory.name)


def count(database, table):
    with closing(sqlite3.connect(database)) as db:
        return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def telemetry_row(serial="X1722353", when="2026-11-08T01:06:47Z", lat=46.33959, lon=20.4069, **extra):
    row = dict(serial=serial, datetime=when, lat=lat, lon=lon, alt=157.02, vel_v=-16.7, vel_h=3.98,
               heading=343.09, uploader_callsign="TEST1", frame=1234)
    row.update(extra)
    return row
