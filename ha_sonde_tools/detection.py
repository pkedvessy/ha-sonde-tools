"""Pure landing-detection logic: no network, database, or Home Assistant access.

Everything here is deterministic given its inputs so it can be unit tested directly.
"""
import json
import logging
import math
from datetime import datetime
from datetime import time as wall_time

from field_tools import distance

SIGNAL_LOSS_MAX_ALTITUDE_M = 500


def point(lat, lon):
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 360):
        raise ValueError("Invalid coordinates")
    return lat, (lon + 180) % 360 - 180


def timestamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Missing time zone")
    return dt.timestamp()


def map_url(position):
    lat, lon = position
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=16/{lat:.6f}/{lon:.6f}"


def forecasts(rows, now, save=None, errors=None):
    """Yield (serial, landing position, ETA) for the newest fresh, unlanded forecast of each sonde."""
    newest = {}
    fresh = []
    for row in rows:
        try:
            serial = row["vehicle"]
            issued = timestamp(row["time"])
            if not isinstance(serial, str) or not serial:
                raise ValueError("Invalid forecast serial")
            if not -60 <= now - issued <= 1200:
                continue
            fresh.append((serial, issued, row))
            if serial not in newest or issued > newest[serial][0]:
                newest[serial] = issued, row
        except (KeyError, TypeError, ValueError, OverflowError):
            if errors is not None:
                errors.append(1)
            continue
    for serial, issued, row in fresh:
        try:
            if row.get("landed") in (1, "1"):
                continue
            path = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            end = path[-1]
            if "time" in end:
                eta = float(end["time"])
                position = point(end["lat"], end["lon"])
            else:
                eta = timestamp(end["datetime"])
                position = point(end["latitude"], end["longitude"])
            if not math.isfinite(eta) or not issued <= eta <= issued + 86400:
                raise ValueError("Invalid forecast ETA")
            if save is not None:
                save(serial, issued, eta, position, row, path)
            if eta > now and row is newest[serial][1]:
                yield serial, position, eta
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            if errors is not None:
                errors.append(1)
            continue


def landings(rows, history, now, ground, predicted=None):
    """Yield touchdown or signal-loss candidates from live telemetry.

    ``history`` keeps the newest fix per serial between polls and is updated in place.
    """
    predicted = predicted or {}
    for serial, frames in rows.items():
        observations = (frames,) if "datetime" in frames else frames.values()
        for row in observations:
            try:
                observed = timestamp(row["datetime"])
                if -30 <= now - observed <= 600 and observed > history.get(serial, (0, None))[0]:
                    history[serial] = observed, row
            except (KeyError, TypeError, ValueError):
                continue

    for serial, (observed, row) in list(history.items()):
        age = now - observed
        if age > 600:
            del history[serial]
            continue
        try:
            descent = -float(row["vel_v"])
            last = point(row["lat"], row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= descent <= 100:
            continue
        forecast = predicted.get(serial.upper())
        if forecast:
            issued, eta, position = forecast
            km = distance(last, position)
            if (observed - 1200 <= issued <= observed + 60
                    and observed - 120 <= eta <= observed + 600 and km <= 1):
                logging.debug("Touchdown check / %s: %.0f m from forecast, fix age %.0f s, ETA in %.0f s",
                              serial, km * 1000, age, eta - now)
                if age >= 60 and now >= eta:
                    yield serial, position, eta, last, "Likely touchdown"
                    continue
                if age < 180:
                    continue
                # A late ETA must not block the low-altitude signal-loss fallback.
                # Otherwise the final fix can expire before the next poll after ETA.

        try:
            altitude = float(row["alt"])
        except (KeyError, TypeError, ValueError):
            continue
        logging.debug("Signal check / %s: fix age %.0f s, altitude %.0f m, descent %.1f m/s",
                      serial, age, altitude, descent)
        if age < 180 or not math.isfinite(altitude) or altitude > SIGNAL_LOSS_MAX_ALTITUDE_M:
            continue

        try:
            speed = float(row["vel_h"])
            heading = float(row["heading"])
            seconds = (altitude - ground) / descent if ground is not None else math.nan
            if 0 <= seconds <= 120 and 0 <= speed <= 100 and 0 <= heading <= 360:
                lat, lon = map(math.radians, last)
                bearing = math.radians(heading)
                arc = speed * seconds / 6371000
                end_lat = math.asin(math.sin(lat) * math.cos(arc)
                                    + math.cos(lat) * math.sin(arc) * math.cos(bearing))
                end_lon = lon + math.atan2(math.sin(bearing) * math.sin(arc) * math.cos(lat),
                                           math.cos(arc) - math.sin(lat) * math.sin(end_lat))
                position = math.degrees(end_lat), (math.degrees(end_lon) + 180) % 360 - 180
                yield serial, position, observed + seconds, last
                continue
        except (KeyError, TypeError, ValueError):
            pass
        yield serial, last, observed, None


def prepare_samples(batch, center, archive_radius, start=None, end=None):
    """Validate telemetry rows and keep those inside the archive radius (and optional time window)."""
    samples = []
    invalid = 0
    for row in batch:
        try:
            serial = row["serial"]
            if not isinstance(serial, str) or not serial:
                raise ValueError("Invalid telemetry serial")
            position = point(row["lat"], row["lon"])
            observed = timestamp(row["datetime"])
            if (start is not None and observed < start) or (end is not None and observed >= end):
                continue
            if distance(center, position) > archive_radius:
                continue
            uploader = str(row.get("uploader_callsign") or "")
            json.dumps(row, allow_nan=False)
            samples.append((serial.upper(), observed, uploader, row))
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    return samples, invalid


def parse_clock(value, option):
    """Parse a strict 24-hour HH:MM option value."""
    try:
        parsed = wall_time.fromisoformat(value)
        if parsed.strftime("%H:%M") != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{option} must be a 24-hour time in HH:MM format.") from None
    return parsed


def in_quiet_hours(current, start, end):
    if start > end:
        return current >= start or current < end
    return start <= current < end
