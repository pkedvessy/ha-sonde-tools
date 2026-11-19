"""Archive retention and on-demand compaction, run on a background thread."""
import logging
import sqlite3
import time
from contextlib import closing
from threading import Event, Lock, Thread

from health_alerts import error_detail

PRUNE_INTERVAL_SECONDS = 6 * 3600
FIRST_PRUNE_DELAY_SECONDS = 600
BATCH = 5000


def prune(database, cutoff, batch=BATCH):
    """Delete raw telemetry and forecast paths older than ``cutoff``. Landing events are kept.

    Work is split into short transactions so live archiving is never blocked for long.
    Returns (telemetry fixes removed, forecasts removed).
    """
    fixes = forecasts = 0
    oldest = "SELECT id FROM telemetry_fixes WHERE observed < ? ORDER BY id LIMIT ?"
    while True:
        with closing(sqlite3.connect(database, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(f"DELETE FROM telemetry_fields WHERE fix_id IN ({oldest})", (cutoff, batch))
            db.execute(f"DELETE FROM telemetry_reports WHERE fix_id IN ({oldest})", (cutoff, batch))
            removed = db.execute(f"DELETE FROM telemetry_fixes WHERE id IN ({oldest})", (cutoff, batch)).rowcount
        fixes += removed
        if removed < batch:
            break
    with closing(sqlite3.connect(database, timeout=30)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM forecast_points WHERE issued < ?", (cutoff,))
        forecasts = db.execute("DELETE FROM forecasts WHERE issued < ?", (cutoff,)).rowcount
        if fixes:
            db.execute("""
                DELETE FROM telemetry_receivers
                WHERE id NOT IN (SELECT DISTINCT receiver_id FROM telemetry_reports)
            """)
    return fixes, forecasts


def compact(database):
    with closing(sqlite3.connect(database, timeout=60, isolation_level=None)) as db:
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class Maintenance:
    def __init__(self, database, retention_days, health):
        self.database = database
        self.retention_days = retention_days
        self.health = health
        self.lock = Lock()
        self.wake = Event()
        self.compact_requested = False
        self.running = None
        self.last = None  # (finished at, summary)

    def status(self):
        with self.lock:
            return {"retention_days": self.retention_days, "running": self.running, "last": self.last,
                    "compact_requested": self.compact_requested}

    def request_compact(self):
        with self.lock:
            if self.running or self.compact_requested:
                raise ValueError("Archive maintenance is already in progress.")
            self.compact_requested = True
        self.wake.set()

    def _task(self, name, work):
        with self.lock:
            self.running = name
        try:
            summary = work()
            self.health.healthy("Archive maintenance")
        except sqlite3.Error as error:
            logging.warning("Archive maintenance (%s): %s", name, error)
            self.health.fail("Archive maintenance", f"{name} failed ({error_detail(error)}).", threshold=1)
            summary = f"{name} failed ({error_detail(error)})"
        with self.lock:
            self.running = None
            self.last = time.time(), summary

    def _prune(self):
        cutoff = time.time() - self.retention_days * 86400
        fixes, forecasts = prune(self.database, cutoff)
        if fixes or forecasts:
            logging.info("Archive retention removed %s GPS fixes and %s forecasts older than %s days",
                         fixes, forecasts, self.retention_days)
        return f"Retention removed {fixes:,} GPS fixes and {forecasts:,} forecasts"

    def _compact(self):
        started = time.monotonic()
        compact(self.database)
        return f"Compaction finished in {time.monotonic() - started:.0f} s"

    def start(self):
        def run():
            next_prune = time.monotonic() + FIRST_PRUNE_DELAY_SECONDS
            while True:
                self.wake.wait(timeout=max(1, next_prune - time.monotonic()) if self.retention_days else None)
                self.wake.clear()
                with self.lock:
                    compact_now, self.compact_requested = self.compact_requested, False
                if self.retention_days and (compact_now or time.monotonic() >= next_prune):
                    self._task("Retention", self._prune)
                    next_prune = time.monotonic() + PRUNE_INTERVAL_SECONDS
                if compact_now:
                    self._task("Compaction", self._compact)
        Thread(target=run, daemon=True, name="archive-maintenance").start()
