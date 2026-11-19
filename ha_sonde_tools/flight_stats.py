"""Whole-track statistics for each archived flight.

Running maxima are folded into ``flight_summaries`` as fixes are written (archive_storage). Values
that need the whole ordered track, such as track length, the straight-line distance between the
first and last fix, the burst time, receivers, and frequency, are recomputed here for flights
marked dirty by new fixes. Results stay in the summary when telemetry retention removes the fixes.
"""
import logging
import math
import sqlite3
import time
from collections import Counter
from contextlib import closing

from flight_distance import distance, position
from health_alerts import error_detail

GAP_SECONDS = 300  # Track segments across longer reception gaps are not counted.
BURST_DROP_M = 1000  # A burst counts as observed with this much climb before and fall after it.
REFRESH_INTERVAL_SECONDS = 60
REFRESH_BATCH = 100
WRITE_TIMEOUT = 30


def _number(value, low, high):
    return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


def bearing(start, end):
    lat1, lat2 = math.radians(start[0]), math.radians(end[0])
    delta = math.radians(end[1] - start[1])
    angle = math.atan2(math.sin(delta) * math.cos(lat2),
                       math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta))
    return (math.degrees(angle) + 360) % 360


def compute(db, serial):
    """Return the derived statistics of one serial from its archived fixes, or None without fixes."""
    points, track, previous = [], 0.0, None
    ascent, fastest, frequencies, subtypes = None, None, Counter(), Counter()
    for observed, lat, lon, alt, vertical, speed, frequency, subtype in db.execute("""
        SELECT observed, lat, lon, alt, vel_v, vel_h, tx_frequency, subtype FROM telemetry_fixes
        WHERE serial = ? ORDER BY observed
    """, (serial,)):
        point = position(lat, lon)
        if point is None:
            continue
        alt = _number(alt, -500, 50000)
        if previous is not None and observed - previous[0] <= GAP_SECONDS:
            track += distance(previous[1], point)
        previous = observed, point
        points.append((observed, point, alt))
        vertical = _number(vertical, 0.01, 50)
        if vertical is not None and (ascent is None or vertical > ascent):
            ascent = vertical
        speed = _number(speed, 0, 150)
        if speed is not None and alt is not None and (fastest is None or speed > fastest[0]):
            fastest = speed, alt
        frequency = _number(frequency, 300, 2000)
        if frequency is not None:
            frequencies[round(frequency, 3)] += 1
        if isinstance(subtype, str) and subtype.strip():
            subtypes[subtype.strip()] += 1
    if not points:
        return None
    first, last = points[0], points[-1]
    heights = [(observed, alt) for observed, _, alt in points if alt is not None]
    burst = None
    if heights:
        top = max(range(len(heights)), key=lambda index: heights[index][1])
        peak = heights[top][1]
        climbed = any(alt < peak - BURST_DROP_M for _, alt in heights[:top])
        fell = any(alt < peak - BURST_DROP_M for _, alt in heights[top + 1:])
        burst = heights[top][0] if climbed and fell else None
    receivers = db.execute("""
        SELECT count(DISTINCT report.uploader) FROM telemetry_reports AS report
        JOIN telemetry_fixes AS fix ON fix.id = report.fix_id WHERE fix.serial = ?
    """, (serial,)).fetchone()[0]
    straight = distance(first[1], last[1])
    return {
        "first_lat": first[1][0], "first_lon": first[1][1], "first_alt": first[2],
        "last_lat": last[1][0], "last_lon": last[1][1], "last_alt": last[2],
        "track_km": round(track, 3), "straight_km": round(straight, 3),
        "bearing": round(bearing(first[1], last[1]), 1) if straight >= 0.05 else None,
        "burst_observed": burst, "max_ascent": ascent, "speed_alt": fastest[1] if fastest else None,
        "receivers": receivers, "frequency": frequencies.most_common(1)[0][0] if frequencies else None,
        "subtype": subtypes.most_common(1)[0][0] if subtypes else None,
    }


def refresh(database, limit=REFRESH_BATCH, serial=None):
    """Recompute dirty flights (or one serial); returns how many summaries were updated."""
    done = 0
    with closing(sqlite3.connect(database, timeout=WRITE_TIMEOUT)) as db:
        if serial is None:
            pending = db.execute("""
                SELECT serial, fixes, last_observed FROM flight_summaries WHERE dirty = 1
                ORDER BY last_observed DESC LIMIT ?
            """, (limit,)).fetchall()
        else:
            pending = db.execute("SELECT serial, fixes, last_observed FROM flight_summaries "
                                 "WHERE serial = ? AND dirty = 1", (serial.upper(),)).fetchall()
        for name, fixes, last_observed in pending:
            stats = compute(db, name)
            with db:
                # The guard leaves the flight dirty when new fixes arrived while it was computed.
                if stats is None:
                    db.execute("UPDATE flight_summaries SET dirty = 0 WHERE serial = ? AND fixes = ? "
                               "AND last_observed = ?", (name, fixes, last_observed))
                    continue
                columns = ", ".join(f"{column} = :{column}" for column in stats)
                done += db.execute(f"""
                    UPDATE flight_summaries SET {columns}, dirty = 0
                    WHERE serial = :serial AND fixes = :fixes AND last_observed = :last_observed
                """, {**stats, "serial": name, "fixes": fixes, "last_observed": last_observed}).rowcount
    return done


def run(database, health=None):
    """Background loop: keep derived statistics current, at most one batch per minute."""
    while True:
        try:
            updated = refresh(database)
            if updated:
                logging.debug("Flight statistics refreshed for %s flights", updated)
            if health:
                health.healthy("Flight statistics")
        except sqlite3.Error as error:
            logging.warning("Flight statistics: %s", error)
            if health:
                health.fail("Flight statistics", f"Could not update flight statistics ({error_detail(error)}).")
        time.sleep(REFRESH_INTERVAL_SECONDS)
