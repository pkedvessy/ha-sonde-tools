import gzip
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from statistics import median
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def distance(center, position):
    lat, lon, plat, plon = map(math.radians, (*center, *position))
    h = math.sin((plat - lat) / 2) ** 2 + math.cos(lat) * math.cos(plat) * math.sin((plon - lon) / 2) ** 2
    return 12742 * math.asin(min(1, math.sqrt(h)))


def coverage(database, ceiling, days, now):
    with closing(sqlite3.connect(database)) as db:
        rows = db.execute("""
            WITH recent AS (
                SELECT serial, observed,
                       round(CAST(lat AS REAL) * 20) / 20 AS lat,
                       round(CAST(lon AS REAL) * 20) / 20 AS lon,
                       CAST(alt AS REAL) AS altitude,
                       max(observed) OVER (PARTITION BY serial) AS latest
                FROM telemetry WHERE observed >= ?
            )
            SELECT lat, lon, count(DISTINCT serial),
                   count(DISTINCT CASE WHEN observed = latest AND latest < ? THEN serial END)
            FROM recent WHERE altitude <= ? AND altitude >= -500
            GROUP BY lat, lon ORDER BY count(DISTINCT serial) DESC LIMIT 2000
        """, (now - days * 86400, now - 180, ceiling)).fetchall()
    return [{"position": [lat, (lon + 180) % 360 - 180], "count": heard, "lastHeard": last,
             "label": f"{heard} sonde serials heard · {last} last heard here"}
            for lat, lon, heard, last in rows]


def scorecard(database):
    buckets = {60: [], 30: [], 10: []}
    with closing(sqlite3.connect(database)) as db:
        recoveries = db.execute("""
            SELECT r.serial, r.latitude, r.longitude, max(e.event_time) FROM recovery AS r
            JOIN events AS e ON e.serial = r.serial AND e.kind = 'Estimated landing after signal loss'
            WHERE r.status = 'Recovered' AND r.latitude IS NOT NULL AND r.longitude IS NOT NULL
              AND e.event_time BETWEEN r.reported - 604800 AND r.reported + 21600
            GROUP BY r.serial
        """).fetchall()
        for serial, lat, lon, touchdown in recoveries:
            for lead, errors in buckets.items():
                target = touchdown - lead * 60
                forecast = db.execute("""
                    SELECT latitude, longitude FROM forecasts
                    WHERE serial = ? AND issued BETWEEN ? AND ?
                    ORDER BY issued DESC LIMIT 1
                """, (serial, target - 1200, target)).fetchone()
                if forecast:
                    errors.append(distance((lat, lon), forecast))
    return [(lead, len(errors), median(errors) if errors else None) for lead, errors in buckets.items()]


def fetch_json(url):
    request = Request(url, headers={"User-Agent": "HA-Sonde-Tools/0.12.2"})
    with urlopen(request, timeout=10) as response:
        data = response.read()
        if data.startswith(bytes((31, 139))):
            data = gzip.decompress(data)
        return json.loads(data)


@lru_cache(maxsize=2)
def launch_sites(hour):
    return fetch_json("https://api.v2.sondehub.org/sites")


def upcoming_launches(center, now):
    stations = []
    for serial, site in launch_sites(int(now.timestamp() // 3600)).items():
        try:
            lon, lat = map(float, site["position"])
            windows = []
            for value in site["times"]:
                day, hour, minute = map(int, value.split(":"))
                if day != 0 or not (0 <= hour < 24 and 0 <= minute < 60):
                    continue
                nominal = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if nominal <= now:
                    nominal += timedelta(days=1)
                windows.append(nominal)
            if windows:
                stations.append((distance(center, (lat, lon)), serial, site, min(windows)))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(stations, key=lambda entry: entry[0])[:8]


@lru_cache(maxsize=32)
def launch_forecast(serial, nominal, hour):
    site = launch_sites(hour)[serial]
    lon, lat = site["position"]
    start = datetime.fromisoformat(nominal) - timedelta(minutes=45)
    query = urlencode({
        "launch_latitude": lat, "launch_longitude": float(lon) % 360,
        "launch_altitude": site.get("alt", 0), "launch_datetime": start.isoformat(),
        "ascent_rate": site.get("ascent_rate") or 5, "burst_altitude": site.get("burst_altitude") or 30000,
        "descent_rate": site.get("descent_rate") or 6, "profile": "standard_profile",
    })
    result = fetch_json("https://tawhiri.v2.sondehub.org/api/v1/?" + query)
    end = result["prediction"][-1]["trajectory"][-1]
    return [end["latitude"], (end["longitude"] + 180) % 360 - 180], end["datetime"]


def project(position, metres, heading):
    lat, lon = map(math.radians, position)
    bearing = math.radians(heading)
    arc = metres / 6371000
    end_lat = math.asin(math.sin(lat) * math.cos(arc) + math.cos(lat) * math.sin(arc) * math.cos(bearing))
    end_lon = lon + math.atan2(math.sin(bearing) * math.sin(arc) * math.cos(lat),
                              math.cos(arc) - math.sin(lat) * math.sin(end_lat))
    return math.degrees(end_lat), (math.degrees(end_lon) + 180) % 360 - 180


def terrain_corridor(database, serial):
    with closing(sqlite3.connect(database)) as db:
        rows = db.execute("""
            SELECT observed, lat, lon, alt, vel_v, vel_h, heading FROM telemetry WHERE serial = ?
            ORDER BY observed DESC, uploader LIMIT 60
        """, (serial.upper(),)).fetchall()
    if not rows:
        return []
    motions = []
    for observed, _, _, _, vertical, horizontal, direction in rows:
        if rows[0][0] - observed > 60 or any(observed == fix[0] for fix in motions):
            continue
        try:
            descent, speed, heading = -float(vertical), float(horizontal), float(direction)
            if 1 <= descent <= 100 and 0 <= speed <= 100 and 0 <= heading <= 360:
                motions.append((observed, descent, speed, heading))
                if len(motions) == 5:
                    break
        except (KeyError, TypeError, ValueError):
            continue
    if not motions or motions[0][0] != rows[0][0]:
        return []
    _, lat, lon, alt, *_ = rows[0]
    position = float(lat), float(lon)
    altitude = float(alt)
    if not math.isfinite(altitude):
        return []
    routes = [[project(position, speed * seconds, heading) for seconds in range(0, 121, 10)]
              for _, _, speed, heading in motions]
    points = [point for route in routes for point in route]
    query = urlencode({"latitude": ",".join(f"{lat:.5f}" for lat, _ in points),
                       "longitude": ",".join(f"{lon:.5f}" for _, lon in points)})
    elevations = fetch_json("https://api.open-meteo.com/v1/elevation?" + query)["elevation"]
    if len(elevations) != len(points):
        raise ValueError("Incomplete elevation response")
    corridor = []
    for index, (route, (_, descent, _, _)) in enumerate(zip(routes, motions)):
        gaps = [altitude - descent * step * 10 - float(elevations[index * len(route) + step])
                for step in range(len(route))]
        for step in range(1, len(route)):
            if gaps[step] <= 0 < gaps[step - 1]:
                fraction = gaps[step - 1] / (gaps[step - 1] - gaps[step])
                _, _, speed, heading = motions[index]
                corridor.append(route[:step] + [project(position, speed * (step - 1 + fraction) * 10, heading)])
                break
    return corridor
