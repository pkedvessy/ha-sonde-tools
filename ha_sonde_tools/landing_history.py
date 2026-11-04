import json
import logging
import math
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html import escape
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import field_tools
from app_version import APP_VERSION
import archive_storage as archive
from health_alerts import error_detail

DATABASE = "/data/history.sqlite3"
INTERFACE_CSS = Path(__file__).with_name("interface.css").read_text(encoding="utf-8")
WEEK = 7 * 86400
COLORS = {
    "Predicted landing": "#2465d0",
    "Estimated landing after signal loss": "#008678",
    "Last GPS fix": "#a6590f",
}


ICONS = {
    "brand": '<path d="M12 3a6 6 0 0 0-6 6c0 4 4 7 6 9 2-2 6-5 6-9a6 6 0 0 0-6-6ZM10 18h4l1 3H9l1-3Z"/><path d="M3 17h3m12 0h3M3 12h2m14 0h2M10 7l2-2"/>',
    "history": '<path d="M4 11a8 8 0 1 1 2 6m-2-6V5m0 6h6m2-4v5l3 2"/>',
    "map": '<path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15m6-12v15"/>',
    "flight": '<path d="M3 17c4 0 5-7 9-7h4m0 0-3-3m3 3-3 3M4 20h16M20 5v9"/>',
    "recovery": '<path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><path d="m9 10 2 2 4-4"/>',
    "heatmap": '<path d="M3 3h5v5H3zm6.5 0h5v5h-5zM16 3h5v5h-5zM3 9.5h5v5H3zm6.5 0h5v5h-5zM16 9.5h5v5h-5zM3 16h5v5H3zm6.5 0h5v5h-5zM16 16h5v5h-5z"/>',
    "radius": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3"/>',
    "bell": '<path d="M5 17h14l-2-3V9A5 5 0 0 0 7 9v5l-2 3Zm5 3h4"/>',
    "coverage": '<circle cx="12" cy="12" r="2"/><path d="M6 6a8 8 0 0 0 0 12M18 6a8 8 0 0 1 0 12M3 3a12 12 0 0 0 0 18M21 3a12 12 0 0 1 0 18"/>',
    "outlook": '<path d="M5 19h14M8 15l8-8m-5 0h5v5M4 11V5h6"/>',
    "scorecard": '<path d="M4 4v16h16M8 16v-4m4 4V9m4 7V6"/>',
    "backfill": '<path d="M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4"/>',
    "diagnostics": '<path d="M3 12h4l3-6 4 12 3-6h4M4 4h16v16H4z"/>',
    "database": '<ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v7c0 2 3 3 7 3s7-1 7-3V5M5 12v7c0 2 3 3 7 3s7-1 7-3v-7"/>',
}


def icon(name):
    return ('<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            + ICONS[name] + '</svg>')


def initialize():
    with closing(sqlite3.connect(DATABASE)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                serial TEXT NOT NULL, kind TEXT NOT NULL,
                recorded REAL NOT NULL, event_time REAL NOT NULL,
                latitude REAL NOT NULL, longitude REAL NOT NULL,
                distance_km REAL NOT NULL, last_lat REAL, last_lon REAL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS events_serial ON events(serial, kind, recorded)")
        db.execute("CREATE INDEX IF NOT EXISTS events_recorded ON events(recorded)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS recovery (
                serial TEXT PRIMARY KEY, status TEXT NOT NULL, checked REAL NOT NULL, reported REAL,
                latitude REAL, longitude REAL
            )
        """)


        archive.initialize(db)

        db.execute("""
            CREATE TABLE IF NOT EXISTS forecasts (
                serial TEXT NOT NULL, issued REAL NOT NULL, eta REAL NOT NULL,
                latitude REAL NOT NULL, longitude REAL NOT NULL,
                PRIMARY KEY (serial, issued)
            ) WITHOUT ROWID
        """)


def record_telemetry(samples):
    if samples:
        with closing(sqlite3.connect(DATABASE)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return archive.write_packets(db, samples)
    return 0


def record_forecasts(samples):
    if not samples:
        return
    with closing(sqlite3.connect(DATABASE)) as db, db:
        for serial, issued, eta, latitude, longitude, route, nearby, _, since in samples:
            inserted = db.execute("""
                INSERT OR IGNORE INTO forecasts (serial, issued, eta, latitude, longitude)
                SELECT ?, ?, ?, ?, ? WHERE ? OR EXISTS (
                    SELECT 1 FROM telemetry WHERE serial = ? AND observed >= ?
                )
            """, (serial, issued, eta, latitude, longitude, nearby, serial, since)).rowcount
            if inserted or db.execute("""
                SELECT 1 FROM forecasts AS f WHERE serial=? AND issued=?
                AND NOT EXISTS (SELECT 1 FROM forecast_points AS p WHERE p.serial=f.serial AND p.issued=f.issued)
            """, (serial, issued)).fetchone():
                archive.record_path(db, serial, issued, route)


def read_telemetry(serial):
    with closing(sqlite3.connect(DATABASE)) as db:
        db.row_factory = sqlite3.Row
        serial = serial.upper()
        extras = archive.packet_extras(db, serial)
        rows = db.execute(f"""
            SELECT fix.serial, fix.id AS fix_id, report.uploader, f.issued, f.eta, f.latitude, f.longitude,
                   {archive.packet_select()}
            FROM telemetry_fixes AS fix
            JOIN telemetry_reports AS report ON report.fix_id = fix.id
            JOIN telemetry_receivers AS receiver ON receiver.id = report.receiver_id
            LEFT JOIN forecasts AS f ON f.serial = fix.serial AND f.issued = (
                SELECT max(issued) FROM forecasts
                WHERE serial = fix.serial AND issued BETWEEN fix.observed - 1200 AND fix.observed
            )
            AND f.eta >= fix.observed
            WHERE fix.serial = ? ORDER BY fix.observed, report.uploader
        """, (serial,))
        samples = []
        for row in rows:
            sample = archive.packet_json(row, extras)
            issued, eta, lat, lon = row["issued"], row["eta"], row["latitude"], row["longitude"]
            sample["sondehub_prediction"] = (
                {"issued_at": datetime.fromtimestamp(issued, timezone.utc).isoformat(),
                 "estimated_landing_at": datetime.fromtimestamp(eta, timezone.utc).isoformat(),
                 "latitude": lat, "longitude": lon}
                if issued is not None else None
            )
            samples.append(sample)
        return json.dumps(samples, separators=(",", ":")).encode()

def fix_details(lat, lon, alt, speed, observed, now):
    return {
        "position": [float(lat), (float(lon) + 180) % 360 - 180],
        "observed": observed, "altitude": alt, "verticalSpeed": speed,
        "live": -30 <= now - observed <= 180,
    }


def read_latest(serial):
    with closing(sqlite3.connect(DATABASE)) as db:
        row = db.execute("""
            SELECT lat, lon, alt, vel_v, observed FROM telemetry WHERE serial = ?
            ORDER BY observed DESC, uploader LIMIT 1
        """, (serial.upper(),)).fetchone()
        forecast = db.execute("""
            SELECT eta FROM forecasts WHERE serial = ? ORDER BY issued DESC LIMIT 1
        """, (serial.upper(),)).fetchone()
    fix = fix_details(*row, time.time()) if row else None
    if fix:
        fix["forecastEta"] = forecast[0] if forecast else None
    return fix


def read_tracks(query, exact=False):
    days = {"7": 7, "30": 30, "365": 365, "0": 0}.get(query.get("days", ["30"])[0], 30)
    serial = query.get("serial", [""])[0][:80]
    hide_recovered = query.get("hide_recovered") == ["1"]
    now = time.time()
    since = now - days * 86400 if days else 0
    with closing(sqlite3.connect(DATABASE)) as db:
        rows = db.execute("""
            WITH sampled AS (
                SELECT t.serial, t.observed, t.uploader,
                       row_number() OVER (
                           PARTITION BY t.serial, CAST(t.observed / 60 AS INTEGER)
                           ORDER BY t.observed DESC, t.uploader
                       ) AS rank
                FROM telemetry AS t LEFT JOIN recovery AS r ON r.serial = t.serial
                WHERE t.observed >= ?
                  AND CASE WHEN ? THEN t.serial = upper(?) ELSE instr(t.serial, upper(?)) > 0 END
                  AND (NOT ? OR r.status IS NULL OR r.status != 'Recovered'
                       OR r.reported IS NULL OR r.reported < t.observed - 21600)
            ), selected AS (
                SELECT serial, observed, uploader FROM sampled
                WHERE rank = 1 ORDER BY observed DESC, serial LIMIT 12001
            )
            SELECT t.serial, t.observed, t.lat, t.lon, t.alt, t.vel_v FROM selected
            JOIN telemetry AS t USING (serial, observed, uploader)
            ORDER BY t.observed DESC, t.serial
        """, (since, exact, serial, serial, hide_recovered)).fetchall()
    tracks = {}
    for serial, observed, lat, lon, alt, speed in reversed(rows[:12000]):
        fix = fix_details(lat, lon, alt, speed, observed, now)
        position = fix["position"]
        track = tracks.setdefault(serial, {"serial": serial, "segments": [], "latest": None})
        latest = track["latest"]
        if (latest is None or observed - latest["observed"] > 300
                or abs(position[1] - latest["position"][1]) > 180):
            track["segments"].append([])
        track["segments"][-1].append(position)
        track["latest"] = fix
        if exact:
            track.setdefault("profile", []).append(fix)
    return {"tracks": list(tracks.values()), "limited": len(rows) > 12000}


def read_flight(serial, tz):
    serial = serial.upper()
    tracks = read_tracks({"serial": [serial], "days": ["0"]}, exact=True)
    with closing(sqlite3.connect(DATABASE)) as db:
        rows = db.execute("""
            SELECT issued, eta, latitude, longitude, EXISTS (
                SELECT 1 FROM forecast_points AS p WHERE p.serial = f.serial AND p.issued = f.issued
            )
            FROM forecasts AS f WHERE serial = ? ORDER BY issued
        """, (serial,))
        forecasts = [{
            "issued": issued,
            "label": datetime.fromtimestamp(issued, tz).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "eta": datetime.fromtimestamp(eta, tz).strftime("%Y-%m-%d %H:%M %Z"),
            "position": [lat, lon], "hasPath": has_path,
        } for issued, eta, lat, lon, has_path in rows]
        recovery = db.execute("""
            SELECT latitude, longitude, reported FROM recovery
            WHERE serial = ? AND status = 'Recovered' AND latitude IS NOT NULL AND longitude IS NOT NULL
        """, (serial,)).fetchone()
    latest = tracks["tracks"][0]["latest"]["observed"] if tracks["tracks"] else 0
    if forecasts:
        latest = max(latest, forecasts[-1]["issued"])
    position = list(recovery[:2]) if recovery and recovery[2] >= latest - 21600 else None
    if position and forecasts:
        # Score the forecast series nearest the recovery, without crossing a gap between flights.
        eligible = [snapshot for snapshot in forecasts if recovery[2] - 7 * 86400 <= snapshot["issued"] <= recovery[2] + 21600]
        scored = set()
        for snapshot in reversed(eligible):
            if scored and min(scored) - snapshot["issued"] > 7200:
                break
            scored.add(snapshot["issued"])
        forecasts = [dict(snapshot, score=snapshot["issued"] in scored) for snapshot in forecasts]
    return {"serial": serial, "tracks": tracks["tracks"], "limited": tracks["limited"],
            "forecasts": forecasts, "recovery": position, "latest": read_latest(serial)}


def read_forecast_path(serial, issued):
    with closing(sqlite3.connect(DATABASE)) as db:
        key = serial.upper(), issued
        points = db.execute("""
            SELECT latitude, longitude FROM forecast_points WHERE serial=? AND issued=? ORDER BY step
        """, key).fetchall()
    segments = []
    for latitude, longitude in points:
        try:
            lat, lon = float(latitude), float(longitude)
            if not (-90 <= lat <= 90 and -180 <= lon <= 360):
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        lon = (lon + 180) % 360 - 180
        if not segments or abs(segments[-1][-1][1] - lon) > 180:
            segments.append([])
        segments[-1].append([lat, lon])
    return segments


def sync_recoveries(fetch_reports, health=None):
    while True:
        try:
            now = time.time()
            with closing(sqlite3.connect(DATABASE)) as db:
                serials = [row[0] for row in db.execute("""
                    SELECT DISTINCT events.serial FROM events
                    LEFT JOIN recovery USING (serial)
                    WHERE recovery.checked IS NULL OR recovery.checked < ?
                    ORDER BY recovery.checked LIMIT 25
                """, (now - 1800,))]
            if serials:
                reports = fetch_reports(serials)
                if not isinstance(reports, list):
                    raise ValueError("Expected a list of SondeHub recovery reports")
                statuses = dict.fromkeys(serials, ("No recovery reported", None, None, None))
                for report in reports:
                    serial = report.get("serial")
                    if serial not in statuses:
                        continue
                    try:
                        date = datetime.fromisoformat(report["datetime"])
                        reported = (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
                    except (KeyError, TypeError, ValueError, OverflowError):
                        continue
                    if statuses[serial][1] is not None and reported < statuses[serial][1]:
                        continue
                    if report.get("recovered") is True:
                        try:
                            lat, lon = float(report["lat"]), float(report["lon"])
                            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                                raise ValueError
                        except (KeyError, TypeError, ValueError):
                            lat = lon = None
                        statuses[serial] = "Recovered", reported, lat, lon
                    elif report.get("planned") is True:
                        statuses[serial] = "Recovery planned", reported, None, None
                    elif report.get("recovered") is False:
                        statuses[serial] = "Attempt unsuccessful", reported, None, None
                with closing(sqlite3.connect(DATABASE)) as db, db:
                    db.executemany("""
                        INSERT OR REPLACE INTO recovery (serial, status, checked, reported, latitude, longitude)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, ((serial, status, now, reported, lat, lon)
                          for serial, (status, reported, lat, lon) in statuses.items()))
                if health:
                    health.healthy("Recovery reports")
        except (OSError, ValueError, TypeError, AttributeError, EOFError, sqlite3.Error) as exc:
            logging.warning("SondeHub recovery sync: %s", exc)
            if health:
                health.fail("Recovery reports", f"Could not refresh recovery reports ({error_detail(exc)}).")
        time.sleep(60)


def record(serial, kind, now, when, position, distance_km, last_fix):
    with closing(sqlite3.connect(DATABASE)) as db, db:
        db.execute("""
            INSERT INTO events
                (serial, kind, recorded, event_time, latitude, longitude, distance_km, last_lat, last_lon)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM events WHERE serial = ? AND kind = ? AND recorded > ?
            )
        """, (serial, kind, now, when, *position, distance_km, *(last_fix or (None, None)),
              serial, kind, now - WEEK))


def read_events(days, serial, page, hide_recovered=False):
    since = time.time() - days * 86400 if days else 0
    with closing(sqlite3.connect(DATABASE)) as db:
        db.row_factory = sqlite3.Row
        return db.execute("""
            SELECT events.*, CASE
                WHEN recovery.checked = 0 THEN NULL
                WHEN recovery.reported < events.recorded - 21600 THEN 'No recovery reported for this flight'
                ELSE recovery.status
            END AS status
            FROM events LEFT JOIN recovery USING (serial)
            WHERE recorded >= ? AND instr(upper(events.serial), upper(?)) > 0
                AND (NOT ? OR recovery.status IS NULL OR recovery.status != 'Recovered'
                     OR recovery.reported IS NULL OR recovery.reported < events.recorded - 21600)
            ORDER BY recorded DESC, id DESC LIMIT ? OFFSET ?
        """, (since, serial, hide_recovered, 101 if page else -1, (page - 1) * 100 if page else 0)).fetchall()


def latest_event_positions(rows, include_gps=True):
    updated = []
    now = time.time()
    with closing(sqlite3.connect(DATABASE)) as db:
        for original in rows:
            row = dict(original)
            serial = row["serial"].upper()
            start, end = row["recorded"] - 21600, row["recorded"] + 21600
            if include_gps:
                fix = db.execute("""
                    SELECT lat, lon, alt, vel_v, observed FROM telemetry
                    WHERE serial = ? AND observed BETWEEN ? AND ?
                    ORDER BY observed DESC, uploader LIMIT 1
                """, (serial, start, end)).fetchone()
                row["latest_gps"] = fix_details(*fix, now) if fix else None
            if row["kind"] == "Predicted landing":
                forecast = db.execute("""
                    SELECT latitude, longitude, eta, issued FROM forecasts
                    WHERE serial = ? AND issued BETWEEN ? AND ?
                    ORDER BY issued DESC LIMIT 1
                """, (serial, row["recorded"] - 1200, end)).fetchone()
                if forecast:
                    row["latitude"], row["longitude"], row["event_time"], row["forecast_issued"] = forecast
            updated.append(row)
    return updated


def recovery_shortlist(query, tz, center):
    now = time.time()
    ranks = {"Estimated landing after signal loss": 0, "Last GPS fix": 1, "Predicted landing": 2}
    candidates = {}
    with closing(sqlite3.connect(DATABASE)) as db:
        live = {row[0] for row in db.execute("SELECT DISTINCT serial FROM telemetry WHERE observed >= ?", (now - 180,))}
    rows = read_events(30, "", None, hide_recovered=True)
    if center is not None:
        rows = latest_event_positions(rows, include_gps=False)
    for row in rows:
        if row["event_time"] > now or row["serial"].upper() in live:
            continue
        serial = row["serial"]
        prior = candidates.get(serial)
        if prior is None or (row["recorded"] >= prior["recorded"] - 21600
                             and ranks[row["kind"]] < ranks[prior["kind"]]):
            candidates[serial] = row
    nearest = query.get("sort") == ["distance"]
    items_with_distance = [
        (row, field_tools.distance(center, (row["latitude"], row["longitude"])) if center is not None else row["distance_km"])
        for row in candidates.values()
    ]
    rows = sorted(items_with_distance, key=lambda entry: entry[1] if nearest else -entry[0]["event_time"])[:20]
    filters = {key: query[key][0] for key in ("days", "serial", "hide_recovered", "page") if key in query}
    links = " · ".join(
        f'<a href="?{escape(urlencode({**filters, "sort": value}))}">{label}</a>'
        for value, label in (("recent", "Most recent"), ("distance", "Nearest first"))
    )
    items = []
    for row, km in rows:
        lat, lon = row["latitude"], row["longitude"]
        google = escape(urlencode({"api": 1, "query": f"{lat},{lon}"}))
        serial = quote(row["serial"], safe="")
        date = datetime.fromtimestamp(row["event_time"], tz).strftime("%Y-%m-%d %H:%M %Z")
        items.append(
            f'<li><a href="?tab=flight&amp;serial={serial}">{escape(row["serial"])}</a> · '
            f'<a href="?tab=recovery&amp;serial={serial}">Recovery Mode</a> · '
            f'{km:.1f} km · {escape(row["kind"])} · {date} · '
            f'{escape(row["status"] or "Recovery not checked")} · '
            f'<a href="https://www.google.com/maps/search/?{google}" target="_blank" rel="noopener noreferrer">Google Maps</a></li>'
        )
    return (f'<section class="card"><h2 class="section-title">{icon("recovery")}Recovery shortlist</h2>'
            "<p>Recent candidates with no confirmed recovery "
            "for this flight. Availability is unconfirmed.</p><p>" + links + "</p>"
            + ('<ol class="recovery-list">' + "".join(items) + "</ol>" if items else "<p>No recent candidates.</p>") + "</section>")


def recovery_view(serial, terrain, tz, map_url, tiles):
    latest = read_latest(serial)
    rows = [row for row in read_events(0, serial, None) if row["serial"].upper() == serial.upper()]
    reference = max([row["recorded"] for row in rows] + ([latest["observed"]] if latest else [0]))
    rows = [row for row in rows if row["recorded"] >= reference - 21600]
    target = next((row for row in rows if row["kind"] == "Estimated landing after signal loss"), None)
    if target is None:
        with closing(sqlite3.connect(DATABASE)) as db:
            forecast = db.execute("""
                SELECT 'Predicted landing' AS kind, latitude, longitude,
                       NULL AS last_lat, NULL AS last_lon
                FROM forecasts WHERE serial = ? AND issued >= ? ORDER BY issued DESC LIMIT 1
            """, (serial.upper(), reference - 21600))
            forecast.row_factory = sqlite3.Row
            target = forecast.fetchone()
        if target is None:
            target = next((row for row in rows if row["kind"] == "Predicted landing"), None)
    positions = []
    if target:
        positions.append((target["kind"], [target["latitude"], target["longitude"]]))
    if latest:
        positions.append(("Last GPS fix", latest["position"]))
    elif target and target["last_lat"] is not None:
        positions.append(("Last GPS fix", [target["last_lat"], target["last_lon"]]))
    elif rows and rows[0]["kind"] == "Last GPS fix":
        positions.append(("Last GPS fix", [rows[0]["latitude"], rows[0]["longitude"]]))
    heading = f"<h2>Recovery Mode · {escape(serial.upper())}</h2>"
    if not positions:
        return heading + "<p>No saved position for this sonde.</p>"
    links = []
    for label, (lat, lon) in positions:
        directions = escape(urlencode({"api": 1, "destination": f"{lat},{lon}"}))
        links.append(f'<h3>{escape(label)}</h3><p>{lat:.6f}, {lon:.6f}</p>'
                     f'<a class="navigate" href="https://www.google.com/maps/dir/?{directions}" '
                     'target="_blank" rel="noopener noreferrer">Navigate with Google Maps</a>')
    terrain_link = escape(urlencode({"tab": "recovery", "serial": serial, "terrain": 1}))
    corridor_view = f'<p><a href="?{terrain_link}">Estimate terrain corridor</a></p>'
    if terrain:
        try:
            routes = field_tools.terrain_corridor(DATABASE, serial)
            corridor_view += (
                "<p>Terrain estimate from recent descent speeds and headings. Each line is one projected route "
                "to the ground, using Open-Meteo elevation. GPS and terrain altitude references can differ.</p>"
                + full_map([], None, 1, tz, map_url, 1, tiles, corridor=routes)
                if routes else "<p>No terrain intersection could be estimated from the saved descent.</p>"
            )
        except (OSError, KeyError, TypeError, ValueError, OverflowError) as exc:
            logging.warning("Terrain corridor for %s: %s", serial, exc)
            corridor_view += "<p>Terrain estimate unavailable. The navigation links still work.</p>"
    data = json.dumps(positions[0][1])
    return heading + "".join(links) + corridor_view + f"""
<p><button id="location-button" type="button">Show distance and bearing</button></p>
<p id="location-result" role="status">Location access is optional and stays in this browser.</p>
<script type="application/json" id="recovery-target">{data}</script>
""" + """
<script>
(() => {
    const button = document.getElementById("location-button");
    const result = document.getElementById("location-result");
    const target = JSON.parse(document.getElementById("recovery-target").textContent);
    let watch = null;
    function stop() {
        if (watch === null) return;
        navigator.geolocation.clearWatch(watch);
        watch = null;
        button.textContent = "Show distance and bearing";
    }
    button.addEventListener("click", () => {
        if (watch !== null) {
            stop();
            result.textContent = "Location sharing stopped.";
            return;
        }
        if (!navigator.geolocation || !window.isSecureContext) {
            result.textContent = "Location requires HTTPS and browser permission. Navigation links remain available.";
            return;
        }
        button.textContent = "Stop using location";
        watch = navigator.geolocation.watchPosition(({coords}) => {
            const rad = Math.PI / 180;
            const a = coords.latitude * rad;
            const b = target[0] * rad;
            const lon = (target[1] - coords.longitude) * rad;
            const h = Math.sin((b - a) / 2) ** 2 + Math.cos(a) * Math.cos(b) * Math.sin(lon / 2) ** 2;
            const km = 12742 * Math.asin(Math.min(1, Math.sqrt(h)));
            const bearing = Math.atan2(Math.sin(lon) * Math.cos(b),
                Math.cos(a) * Math.sin(b) - Math.sin(a) * Math.cos(b) * Math.cos(lon));
            result.textContent = (km < 1 ? Math.round(km * 1000) + " m" : km.toFixed(2) + " km") +
                " · bearing " + ((Math.round(bearing / rad) + 360) % 360) + "° · GPS ±" +
                Math.round(coords.accuracy) + " m";
        }, () => {
            stop();
            result.textContent = "Location unavailable. Check browser permissions or use the navigation link.";
        }, {enableHighAccuracy: true, maximumAge: 10000, timeout: 15000});
    });
    window.addEventListener("pagehide", stop);
})();
</script>"""


def landing_heatmap(query, tz):
    kind = query.get("heat_kind", ["estimated"])[0]
    labels = {"estimated": "Estimated landing", "predicted": "Predicted landing", "recovered": "Reported recovery"}
    if kind not in labels:
        kind = "estimated"
    month = query.get("month", [""])[0]
    start, end = 0, 253402300799
    if re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", month):
        date = datetime.fromisoformat(month + "-01").replace(tzinfo=tz)
        start = date.timestamp()
        end = date.replace(year=date.year + (date.month == 12), month=date.month % 12 + 1).timestamp()
    else:
        month = ""
    with closing(sqlite3.connect(DATABASE)) as db:
        if kind == "recovered":
            points = db.execute("""
                SELECT r.latitude, r.longitude FROM recovery AS r
                WHERE r.status = 'Recovered' AND r.latitude IS NOT NULL AND r.longitude IS NOT NULL
                  AND r.reported >= ? AND r.reported < ?
                  AND EXISTS (SELECT 1 FROM events AS e WHERE e.serial = r.serial
                              AND r.reported >= e.recorded - 21600)
            """, (start, end))
        else:
            event = "Estimated landing after signal loss" if kind == "estimated" else "Predicted landing"
            points = db.execute("""
                SELECT latitude, longitude FROM events
                WHERE kind = ? AND event_time >= ? AND event_time < ?
            """, (event, start, end))
        counts = {}
        for lat, lon in points:
            cell = (round(lat * 20) / 20, round(lon * 20) / 20)
            counts[cell] = counts.get(cell, 0) + 1
    cells = [{"position": point, "count": count} for point, count in counts.items()]
    options = "".join(f'<option value="{value}" {"selected" if kind == value else ""}>{label}</option>'
                      for value, label in labels.items())
    controls = (f'<form method="get"><input type="hidden" name="tab" value="heatmap">'
                f'<label>Location type <select name="heat_kind">{options}</select></label>'
                f'<label>Month <input type="month" name="month" value="{escape(month)}"></label>'
                '<button>Show density</button></form>')
    return controls, cells


def coverage_view(query, center, radius, tz, map_url, notify_radius, tiles):
    ceiling = {"250": 250, "500": 500, "1000": 1000}.get(query.get("ceiling", ["500"])[0], 500)
    days = {"7": 7, "30": 30, "90": 90}.get(query.get("coverage_days", ["30"])[0], 30)
    cells = field_tools.coverage(DATABASE, ceiling, days, time.time())
    altitude_options = "".join(f'<option {"selected" if value == ceiling else ""}>{value}</option>'
                               for value in (250, 500, 1000))
    period_options = "".join(f'<option value="{value}" {"selected" if value == days else ""}>{value} days</option>'
                             for value in (7, 30, 90))
    return (
        '<h2>Receiver coverage radar</h2><p>Counts are distinct sonde serials per cell, from all receivers. '
        'Altitude uses the reported GPS height. A red outline marks cells containing the last saved fix '
        'of one or more sondes.</p>'
        '<form class="form-row" method="get"><input type="hidden" name="tab" value="coverage">'
        f'<label>GPS altitude ceiling (m)<select name="ceiling">{altitude_options}</select></label>'
        f'<label>Period<select name="coverage_days">{period_options}</select></label>'
        '<button>Show coverage</button></form>'
        + (full_map([], center, radius, tz, map_url, notify_radius, tiles, heatmap=cells)
           if cells else "<p>No archived fixes match this altitude and period.</p>")
    )


def outlook_view(query, center, tz, map_url):
    heading = "<h2>Next launches</h2>"
    if center is None:
        return heading + "<p>Waiting for the configured zone center.</p>"
    now = datetime.now(timezone.utc)
    try:
        launches = field_tools.upcoming_launches(center, now)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        logging.warning("Launch schedule unavailable: %s", exc)
        return heading + "<p>SondeHub launch schedules are temporarily unavailable.</p>"
    selected = query.get("site", [""])[0]
    rows = []
    for km, serial, site, nominal in launches:
        name = escape(site.get("station_name", serial))
        link = escape(urlencode({"tab": "outlook", "site": serial}))
        eta = nominal.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
        rows.append(f'<li><strong>{name}</strong> · {km:.0f} km · {eta} · <a href="?{link}">Estimate landing</a></li>')
    result = ""
    match = next((entry for entry in launches if entry[1] == selected), None)
    if match:
        _, serial, site, nominal = match
        try:
            position, when = field_tools.launch_forecast(serial, nominal.isoformat(), int(now.timestamp() // 3600))
            result = (f'<p><strong>{escape(site.get("station_name", serial))}</strong> · '
                      f'Estimated touchdown {field_tools.distance(center, position):.0f} km from zone · '
                      f'{escape(datetime.fromisoformat(when).astimezone(tz).strftime("%Y-%m-%d %H:%M %Z"))} · '
                      f'<a href="{escape(map_url(position))}">Open estimated point</a></p>')
        except (OSError, KeyError, TypeError, ValueError, IndexError) as exc:
            logging.warning("Launch prediction for %s: %s", serial, exc)
            result = "<p>Could not generate a landing estimate for this launch window.</p>"
    return (heading + "<p>Nominal daily launch windows from the nearest reporting stations. "
            "Estimates use a launch time 45 minutes before the nominal window and published or default flight parameters.</p>"
            + result + ('<ul class="recovery-list">' + "".join(rows) + "</ul>" if rows else "<p>No scheduled launches found.</p>"))


def scorecard_view():
    rows = []
    for lead, count, error in field_tools.scorecard(DATABASE):
        label = f"{error:.2f} km" if error is not None else "—"
        rows.append(f"<tr><td>{lead} min</td><td>{count}</td><td>{label}</td></tr>")
    return ("<h2>Forecast scorecard</h2><p>Median distance to the reported recovery point, grouped by lead time "
            "before the estimated touchdown. A flight contributes only if a forecast was saved within "
            "20 minutes before the lead-time cutoff.</p>"
            "<table><thead><tr><th>Lead time</th><th>Flights</th><th>Median error</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>")


def map_svg(rows, center, radius):
    if center is None:
        return "<p>The map will appear when Home Assistant resolves the configured zone.</p>"
    lat, lon = map(math.radians, center)
    markers = []
    outside = 0
    for row in rows:
        plat, plon = map(math.radians, (row["latitude"], row["longitude"]))
        delta = plon - lon
        bearing = math.atan2(math.sin(delta) * math.cos(plat),
                             math.cos(lat) * math.sin(plat) - math.sin(lat) * math.cos(plat) * math.cos(delta))
        km = field_tools.distance(center, (row["latitude"], row["longitude"]))
        if km > radius + 0.001:
            outside += 1
            continue
        x = 180 + 150 * km / radius * math.sin(bearing)
        y = 180 - 150 * km / radius * math.cos(bearing)
        label = escape(f'{row["serial"]} · {row["kind"]} · {row["status"] or "Recovery not checked"}')
        color = "#818894" if row["status"] == "Recovered" else COLORS[row["kind"]]
        markers.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="{color}">'
                       f'<title>{label}</title></circle>')
    return (
        '<svg class="radar-fallback" viewBox="0 0 360 360" role="img" aria-label="Landing positions relative to the current zone">'
        '<circle cx="180" cy="180" r="150" fill="none" stroke="currentColor" opacity=".45"/>'
        '<path d="M30 180h300 M180 30v300" stroke="currentColor" opacity=".2"/>'
        '<text x="180" y="21" text-anchor="middle" fill="currentColor">N</text>'
        '<circle cx="180" cy="180" r="3" fill="currentColor"/>'
        + "".join(markers) + "</svg>"
        + f"<p>{radius:g} km radius · {radius * 2:g} km diameter. "
          f"{outside} of this page’s records fall outside the current circle.</p>"
    )


def map_events(rows, timezone, map_url, compact=False):
    events = []
    gps_markers = set()
    for row in rows:
        position = row["latitude"], row["longitude"]
        events.append({
            "position": position,
            "serial": row["serial"],
            "mapUrl": map_url(position),
            "kind": row["kind"],
            "status": row["status"] or "Recovery not checked",
            "color": "#818894" if row["status"] == "Recovered" else COLORS[row["kind"]],
            "time": datetime.fromtimestamp(row["event_time"], timezone).strftime("%Y-%m-%d %H:%M %Z"),
        })
        if compact:
            fix = row.get("latest_gps")
            if fix and (row["serial"], fix["observed"]) not in gps_markers:
                gps_markers.add((row["serial"], fix["observed"]))
                events.append({
                    "position": fix["position"], "serial": row["serial"], "kind": "Latest saved GPS fix",
                    "mapUrl": map_url(fix["position"]), "status": "", "color": "#c82568",
                    "time": datetime.fromtimestamp(fix["observed"], timezone).strftime("%Y-%m-%d %H:%M %Z"),
                })
    return events


def full_map(rows, center, radius, timezone, map_url, notify_radius, tiles=None, compact=False, flight=None, heatmap=None, corridor=None):
    if flight and not (flight["tracks"] or flight["forecasts"]):
        return "<p>No saved telemetry or forecasts for this serial.</p>"
    if center is None and not rows and not flight and not heatmap and not corridor:
        return "<p>The map will appear when Home Assistant resolves the configured zone.</p>"
    events = map_events(rows, timezone, map_url, compact)
    data = json.dumps({"center": center, "radius": radius * 1000, "notifyRadius": notify_radius * 1000, "events": events, "tiles": tiles, "compact": compact, "flight": flight, "heatmap": heatmap, "corridor": corridor}).replace("<", "\\u003c")
    fallback = map_svg(rows, center, radius) if compact else ""
    map_class = "landing-map--compact" if compact else "landing-map--full"
    controls = (
        '<p><label>Forecast snapshot <input id="forecast-snapshot" type="range" min="0" '
        f'max="{len(flight["forecasts"]) - 1}" value="{len(flight["forecasts"]) - 1}"></label> '
        '<output id="forecast-caption" for="forecast-snapshot" aria-live="polite"></output></p>'
        if flight and flight["forecasts"] else "<p>No saved forecasts for this flight.</p>" if flight else ""
    )
    flight_panels = """
<section class="flight-panel" aria-label="Flight data">
<h3>Flight status</h3><p id="flight-card" role="status"></p>
<h3>Altitude profile</h3>
<p><button id="replay-button" type="button" hidden>Play flight</button>
<output id="replay-time" aria-label="Playback time"></output></p>
<svg id="altitude-profile" viewBox="0 0 640 160" aria-label="Observed altitude over time" role="img"></svg>
<label id="profile-control">Observed fix <input id="profile-index" type="range" min="0" value="0"></label>
<output id="profile-caption" for="profile-index"></output>
</section>
""" if flight else ""
    vector_assets = """
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/maplibre-gl@5.6.1/dist/maplibre-gl.css"
 integrity="sha384-Nq6PQ+9vJPvw7U/VfDELyrWoGQMsy0gi6QShhaSrGzkpF5KkM40csg2leky+YMTd" crossorigin="">
<script src="https://cdn.jsdelivr.net/npm/maplibre-gl@5.6.1/dist/maplibre-gl.js"
 integrity="sha384-/L1njH4bbgNt9Uk3HwJ272N9fxJzRBQCxhtwGkZiqgl+Nxpq2ETUNZhNMNV1RgyW" crossorigin=""></script>
<script src="https://cdn.jsdelivr.net/npm/@maplibre/maplibre-gl-leaflet@0.1.3/leaflet-maplibre-gl.js"
 integrity="sha384-LIxE/QjpJKC2A91yD40ZisdYtFbgAjl58jqpo9/MUZNgwhqsTfzwrTlqv6nDdzzB" crossorigin=""></script>
""" if tiles and tiles.get("style") else ""
    return f"""
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
 integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<p id="basemap-status" role="status"></p>
<p id="track-status" role="status"></p>
{controls}
<div id="full-map" class="{map_class}" aria-label="Interactive landing map">{fallback}</div>
{flight_panels}
<script type="application/json" id="map-data">{data}</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
 integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
{vector_assets}
""" + """
<script>
(() => {
    const container = document.getElementById("full-map");
    if (!window.L) {
        if (!container.children.length) {
            container.textContent = "The interactive map could not load. The History tab still has map links.";
        }
        return;
    }
    const {center, radius, notifyRadius, events, tiles, compact, flight, heatmap, corridor} = JSON.parse(document.getElementById("map-data").textContent);
    container.replaceChildren();
    const start = flight?.tracks[0]?.latest.position || flight?.forecasts.at(-1)?.position || center || heatmap?.[0]?.position || corridor?.[0]?.[0] || events[0].position;
    const map = L.map(container, {preferCanvas: true, scrollWheelZoom: !compact}).setView(start, 10);
    const status = document.getElementById("basemap-status");
    const unavailable = "Basemap unavailable. Event markers and search circles are still shown.";
    if (tiles && tiles.style) {
        try {
            if (!L.maplibreGL || !window.maplibregl) throw new Error("MapLibre unavailable");
            const layer = L.maplibreGL({
                style: tiles.style,
                attributionControl: {customAttribution: '<a href="https://openfreemap.org/">OpenFreeMap</a> · ' +
                    '<a href="https://openmaptiles.org/">OpenMapTiles</a> · ' +
                    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}
            }).addTo(map);
            const vectorMap = layer.getMaplibreMap();
            vectorMap.on("error", () => { status.textContent = unavailable; });
            vectorMap.on("load", () => { status.textContent = ""; });
        } catch (error) {
            status.textContent = unavailable;
        }
    } else if (tiles) {
        const layer = L.tileLayer(tiles.url, {
            maxZoom: 19, attribution: tiles.attribution, referrerPolicy: "strict-origin-when-cross-origin"
        });
        layer.once("tileerror", () => {
            layer.remove();
            status.textContent = unavailable;
        });
        layer.addTo(map);
    } else {
        status.textContent = "No basemap configured. Select an event marker to open its street map.";
    }
    const bounds = L.latLngBounds(events.map(event => event.position));
    if (center && !flight) {
        const circle = L.circle(center, {radius, weight: 2, fillOpacity: 0.06}).addTo(map);
        bounds.extend(circle.getBounds());
        if (notifyRadius !== radius) {
            const notify = L.circle(center, {radius: notifyRadius, dashArray: "6 5", fill: false}).addTo(map);
            bounds.extend(notify.getBounds());
        }
    }
    if (bounds.isValid()) map.fitBounds(bounds, {padding: [24, 24], maxZoom: 15});
    document.addEventListener("DOMContentLoaded", () => {
        map.invalidateSize({pan: false});
        if (compact && bounds.isValid()) map.fitBounds(bounds, {padding: [24, 24], maxZoom: 15});
    }, {once: true});
    function sondeMarker(position, color, glyph, popup, destination) {
        const badge = document.createElement("div");
        badge.className = "map-pin-badge";
        badge.style.setProperty("--pin-color", color);
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("fill", "none");
        svg.setAttribute("stroke", "currentColor");
        svg.setAttribute("stroke-width", "2");
        svg.setAttribute("stroke-linecap", "round");
        svg.setAttribute("stroke-linejoin", "round");
        const path = document.createElementNS(svg.namespaceURI, "path");
        const shapes = {
            sonde: "M12 3a6 6 0 0 0-6 6c0 4 4 7 6 9 2-2 6-5 6-9a6 6 0 0 0-6-6ZM10 18h4l1 3H9l1-3Z",
            target: "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 5v6m-3-3h6",
            found: "m5 12 4 4L19 6",
            fix: "M19 10c0 4-7 10-7 10S5 14 5 10a7 7 0 1 1 14 0Zm-7-2v4",
        };
        path.setAttribute("d", shapes[glyph]);
        svg.append(path);
        badge.append(svg);
        const pin = L.divIcon({className: "map-pin", html: badge, iconSize: [28, 28], iconAnchor: [14, 14], popupAnchor: [0, -12]});
        return L.marker(position, {icon: pin, title: popup.textContent}).bindPopup(popup).addTo(destination);
    }

    function appendPositionLinks(popup, serial, position, osmUrl) {
        const links = document.createElement("div");
        const googleQuery = new URLSearchParams({api: "1", query: position.join(",")});
        const destinations = [
            ["SondeHub", "https://sondehub.org/" + encodeURIComponent(serial)],
            ["Google Maps", "https://www.google.com/maps/search/?" + googleQuery],
            ["Flight", "?" + new URLSearchParams({tab: "flight", serial})],
            ["Recovery Mode", "?" + new URLSearchParams({tab: "recovery", serial})]
        ];
        if (osmUrl) destinations.push(["OpenStreetMap", osmUrl]);
        for (const [label, url] of destinations) {
            const link = document.createElement("a");
            link.href = url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = label;
            links.append(link, document.createTextNode(" "));
        }
        popup.append(links);
    }

    function showAltitudeProfile(track) {
        const svg = document.getElementById("altitude-profile");
        const slider = document.getElementById("profile-index");
        const caption = document.getElementById("profile-caption");
        const fixes = track?.profile.filter(fix => fix.altitude != null && Number.isFinite(Number(fix.altitude))) || [];
        if (!fixes.length) {
            svg.hidden = true;
            document.getElementById("profile-control").hidden = true;
            caption.textContent = "No archived altitude data.";
            return;
        }
        const first = fixes[0];
        const last = fixes.at(-1);
        const peak = fixes.reduce((highest, fix) => Number(fix.altitude) > Number(highest.altitude) ? fix : highest);
        const low = fixes.reduce((min, fix) => Math.min(min, Number(fix.altitude)), Number(first.altitude));
        const x = fix => 24 + 592 * (fix.observed - first.observed) / Math.max(1, last.observed - first.observed);
        const y = fix => 134 - 108 * (Number(fix.altitude) - low) / Math.max(1, Number(peak.altitude) - low);
        const make = (tag, attrs) => {
            const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
            for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
            svg.append(element);
            return element;
        };
        make("path", {d: "M24 20V134H616", stroke: "currentColor", opacity: ".35", fill: "none"});
        let path = "";
        let previous = null;
        for (const fix of fixes) {
            const gap = previous && fix.observed - previous.observed > 300;
            path += (previous && !gap ? "L" : "M") + x(fix) + "," + y(fix);
            if (gap) make("line", {x1: x(fix), x2: x(fix), y1: 24, y2: 134,
                stroke: "currentColor", opacity: ".35", "stroke-dasharray": "4 4"});
            previous = fix;
        }
        make("path", {d: path, stroke: "#7851c8", "stroke-width": 3, fill: "none"});
        const peakIndex = fixes.indexOf(peak);
        const labels = [
            [peak, fixes.slice(peakIndex + 1).some(fix => Number(fix.verticalSpeed) < -1) ? "Possible burst" : "Highest fix"],
            [last, "Last fix"]
        ];
        for (const [fix, label] of labels) {
            make("circle", {cx: x(fix), cy: y(fix), r: 4, fill: "#7851c8"});
            const text = make("text", {x: x(fix), y: fix === last ? 155 : 15, fill: "currentColor",
                "text-anchor": x(fix) > 480 ? "end" : "start", "font-size": 12});
            text.textContent = label;
        }
        const cursor = make("circle", {r: 5, fill: "#c82568"});
        const marker = L.circleMarker(last.position, {color: "#c82568", radius: 6}).addTo(map);
        slider.max = fixes.length - 1;
        slider.value = fixes.length - 1;
        const selectFix = index => {
            const fix = fixes[index];
            if (!fix) {
                marker.remove();
                cursor.setAttribute("visibility", "hidden");
                caption.textContent = "No observed fix yet";
                return;
            }
            if (!map.hasLayer(marker)) marker.addTo(map);
            marker.setLatLng(fix.position);
            cursor.removeAttribute("visibility");
            cursor.setAttribute("cx", x(fix));
            cursor.setAttribute("cy", y(fix));
            caption.textContent = new Date(fix.observed * 1000).toLocaleString() + " · " + fix.altitude + " m";
        };
        slider.addEventListener("input", () => selectFix(Number(slider.value)));
        svg.addEventListener("click", event => {
            const bounds = svg.getBoundingClientRect();
            const clicked = (event.clientX - bounds.left) * 640 / bounds.width;
            const best = fixes.reduce((index, fix, i) => Math.abs(x(fix) - clicked) < Math.abs(x(fixes[index]) - clicked) ? i : index, 0);
            slider.value = best;
            selectFix(best);
        });
        selectFix(fixes.length - 1);
        return {fixes, burst: labels[0][1] === "Possible burst" ? peak.observed : null,
            select(index) { slider.value = Math.max(0, index); selectFix(index); }};
    }

    if (flight) {
        let latest = flight.latest;
        function showFlightCard() {
            const card = document.getElementById("flight-card");
            if (!latest) {
                card.textContent = "No observed GPS fix.";
                return;
            }
            const now = Date.now() / 1000;
            const age = Math.max(0, Math.round(now - latest.observed));
            const speed = latest.verticalSpeed == null ? null : Number(latest.verticalSpeed);
            const motion = speed == null || !Number.isFinite(speed) ? "Motion unknown" :
                speed > 1 ? "Ascending" : speed < -1 ? "Descending" : "Low vertical speed";
            const parts = [age <= 180 ? motion : "Last known · " + motion];
            if (latest.altitude != null) parts.push(latest.altitude + " m altitude");
            if (speed != null && Number.isFinite(speed)) parts.push(speed.toFixed(1) + " m/s vertical");
            if (center) parts.push((map.distance(center, latest.position) / 1000).toFixed(1) + " km from zone center");
            if (latest.forecastEta > now) parts.push("ETA " + new Date(latest.forecastEta * 1000).toLocaleString());
            parts.push("GPS fix " + (age < 120 ? age + " s" : Math.floor(age / 60) + " min") + " ago");
            card.textContent = parts.join(" · ");
        }
        showFlightCard();
        window.setInterval(showFlightCard, 1000);
        let refreshing = false;
        window.setInterval(async () => {
            if (document.hidden || refreshing) return;
            refreshing = true;
            try {
                const query = new URLSearchParams({latest: "1", serial: flight.serial});
                const response = await fetch("?" + query, {cache: "no-store"});
                if (response.ok) {
                    latest = await response.json();
                    showFlightCard();
                }
            } catch (error) {
                // Keep the last fix and let its age continue to update.
            } finally {
                refreshing = false;
            }
        }, 30000);

        const observed = L.latLngBounds([]);
        const track = flight.tracks[0];
        if (track) {
            for (const segment of track.segments) {
                L.polyline(segment, {color: "#7851c8", weight: 3}).addTo(map);
                observed.extend(L.latLngBounds(segment));
            }
            const popup = document.createElement("div");
            popup.textContent = "Last observed GPS fix";
            appendPositionLinks(popup, flight.serial, track.latest.position);
            L.circleMarker(track.latest.position, {color: "#7851c8"}).bindPopup(popup).addTo(map);
            if (flight.limited) {
                document.getElementById("track-status").textContent = "Showing the newest 12,000 observed route points.";
            }
        }
        const profile = showAltitudeProfile(track);
        if (flight.recovery) {
            const popup = document.createElement("div");
            popup.textContent = "Reported recovery";
            appendPositionLinks(popup, flight.serial, flight.recovery);
            sondeMarker(flight.recovery, "#008678", "found", popup, map);
            observed.extend(flight.recovery);
        }
        if (observed.isValid()) map.fitBounds(observed, {padding: [24, 24], maxZoom: 15});
        const slider = document.getElementById("forecast-snapshot");
        let selectForecast = () => {};
        if (slider) {
            const caption = document.getElementById("forecast-caption");
            const predicted = L.layerGroup().addTo(map);
            const drift = L.layerGroup().addTo(map);
            const spread = L.layerGroup().addTo(map);
            L.control.layers({}, {"Forecast spread": spread}).addTo(map);
            const scoring = flight.recovery ? flight.forecasts.filter(forecast => forecast.score) : [];
            const bestError = scoring.reduce((best, forecast) =>
                Math.min(best, map.distance(forecast.position, flight.recovery)), Infinity);
            let selected = 0;
            async function showForecast(fit) {
                const request = ++selected;
                const index = Number(slider.value);
                const forecast = flight.forecasts[index];
                caption.textContent = forecast.label + " · ETA " + forecast.eta;
                drift.clearLayers();
                spread.clearLayers();
                const earlier = [];
                for (let i = index - 1; i >= 0; i--) {
                    if (flight.forecasts[i + 1].issued - flight.forecasts[i].issued > 7200) break;
                    earlier.unshift(flight.forecasts[i]);
                }
                for (const snapshot of earlier) {
                    const dot = L.circleMarker(snapshot.position, {radius: 3, color: "#2465d0", opacity: 0.45, fillOpacity: 0.35});
                    const label = document.createElement("span");
                    label.textContent = snapshot.label;
                    dot.bindTooltip(label).addTo(drift);
                }
                const recent = earlier.find(snapshot => forecast.issued - snapshot.issued <= 1200);
                const recentPositions = earlier.filter(snapshot => forecast.issued - snapshot.issued <= 1200);
                if (recentPositions.length) {
                    const spreadRadius = recentPositions.reduce((largest, snapshot) =>
                        Math.max(largest, map.distance(snapshot.position, forecast.position)), 0);
                    if (spreadRadius > 0) {
                        L.circle(forecast.position, {radius: spreadRadius, color: "#2465d0", weight: 1,
                            fillOpacity: 0.12}).bindTooltip("Forecast spread (not a confidence area)").addTo(spread);
                        caption.textContent += " · Spread " + (spreadRadius / 1000).toFixed(2) + " km";
                    }
                }
                if (recent) {
                    const delta = map.distance(recent.position, forecast.position);
                    const radians = Math.PI / 180;
                    const a = recent.position[0] * radians;
                    const b = forecast.position[0] * radians;
                    const lon = (forecast.position[1] - recent.position[1]) * radians;
                    const bearing = Math.atan2(Math.sin(lon) * Math.cos(b),
                        Math.cos(a) * Math.sin(b) - Math.sin(a) * Math.cos(b) * Math.cos(lon));
                    const direction = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][
                        (Math.round(bearing / radians / 45) + 8) % 8];
                    const minutes = Math.max(1, Math.round((forecast.issued - recent.issued) / 60));
                    caption.textContent += " · Drift " + (delta / 1000).toFixed(1) + " km" +
                        (delta >= 50 ? " " + direction : "") + " over " + minutes + " min";
                }
                if (forecast.score) {
                    caption.textContent += " · " + (map.distance(forecast.position, flight.recovery) / 1000).toFixed(2) +
                        " km from reported recovery (best " + (bestError / 1000).toFixed(2) + " km)";
                }
                predicted.clearLayers();
                const popup = document.createElement("div");
                popup.textContent = "Predicted landing · " + forecast.eta;
                appendPositionLinks(popup, flight.serial, forecast.position);
                sondeMarker(forecast.position, "#2465d0", "target", popup, predicted);
                const visible = L.latLngBounds([forecast.position]);
                if (observed.isValid()) visible.extend(observed);
                if (forecast.hasPath) {
                    try {
                        const query = new URLSearchParams({path: "1", serial: flight.serial, issued: forecast.issued});
                        const response = await fetch("?" + query);
                        if (!response.ok) throw new Error("Forecast path unavailable");
                        const segments = await response.json();
                        if (request !== selected) return;
                        for (const segment of segments) {
                            L.polyline(segment, {color: "#2465d0", weight: 3, dashArray: "6 5"}).addTo(predicted);
                            visible.extend(L.latLngBounds(segment));
                        }
                    } catch (error) {
                        if (request !== selected) return;
                        caption.textContent += " · Route unavailable";
                    }
                } else {
                    caption.textContent += " · Landing point only";
                }
                if (fit) map.fitBounds(visible, {padding: [24, 24], maxZoom: 15});
            }
            selectForecast = index => {
                if (index < 0) {
                    selected++;
                    predicted.clearLayers();
                    drift.clearLayers();
                    spread.clearLayers();
                    caption.textContent = "No forecast issued yet";
                } else {
                    slider.value = index;
                    showForecast(false);
                }
            };
            slider.addEventListener("input", () => showForecast(false));
            showForecast(true);
        }

        const moments = [...new Set([...(profile?.fixes.map(fix => fix.observed) || []),
                                     ...flight.forecasts.map(forecast => forecast.issued)])].sort((a, b) => a - b);
        const play = document.getElementById("replay-button");
        const replayTime = document.getElementById("replay-time");
        if (moments.length > 1) {
            play.hidden = false;
            let timer = null;
            let step = 0;
            let fixIndex = -2;
            let forecastIndex = -2;
            function pause(label) {
                window.clearInterval(timer);
                timer = null;
                play.textContent = label;
            }
            function advance() {
                const at = moments[step++];
                replayTime.textContent = new Date(at * 1000).toLocaleString();
                if (profile) {
                    const index = profile.fixes.findLastIndex(fix => fix.observed <= at);
                    if (index !== fixIndex) profile.select(index);
                    fixIndex = index;
                }
                const index = flight.forecasts.findLastIndex(forecast => forecast.issued <= at);
                if (index !== forecastIndex) selectForecast(index);
                forecastIndex = index;
                if (step === moments.length) {
                    pause("Replay flight");
                } else if (at === profile?.burst) {
                    pause("Resume after possible burst");
                }
            }
            play.addEventListener("click", () => {
                if (timer !== null) {
                    pause("Resume flight");
                    return;
                }
                if (step === moments.length) step = 0;
                fixIndex = -2;
                forecastIndex = -2;
                play.textContent = "Pause flight";
                timer = window.setInterval(advance, 500);
                advance();
            });
        }
    }
    if (corridor) {
        const footprint = L.latLngBounds([]);
        for (const route of corridor) {
            L.polyline(route, {color: "#008678", weight: 3, opacity: 0.65}).addTo(map);
            L.circleMarker(route.at(-1), {color: "#008678", radius: 4}).addTo(map);
            footprint.extend(L.latLngBounds(route));
        }
        L.circleMarker(corridor[0][0], {color: "#c82568", radius: 6}).bindTooltip("Last GPS fix").addTo(map);
        map.fitBounds(footprint, {padding: [24, 24], maxZoom: 17});
    }
    if (heatmap) {
        const largest = heatmap.reduce((max, cell) => Math.max(max, cell.count), 1);
        for (const cell of heatmap) {
            const weight = Math.sqrt(cell.count / largest);
            const popup = document.createElement("span");
            popup.textContent = cell.label || cell.count + (cell.count === 1 ? " location" : " locations") + " in this grid cell";
            L.circle(cell.position, {radius: 2800, stroke: Boolean(cell.lastHeard), color: "#b32e37", weight: 2, fillColor: weight > 0.65 ? "#d43b36" : "#f39a23",
                fillOpacity: 0.16 + 0.64 * weight}).bindPopup(popup).addTo(map);
        }
        if (heatmap.length && !center) {
            map.fitBounds(L.latLngBounds(heatmap.map(cell => cell.position)), {padding: [24, 24], maxZoom: 12});
        }
    }
    const landingMarkers = L.layerGroup().addTo(map);
    let displayedEvents;
    renderLandingMarkers(events);

    if (!compact && !flight && !heatmap && !corridor) {
        const trackStatus = document.getElementById("track-status");
        const routes = L.layerGroup().addTo(map);
        const livePositions = L.layerGroup().addTo(map);
        const query = new URLSearchParams(window.location.search);
        query.set("tracks", "1");
        query.set("map_events", "1");
        let updating = false;
        async function refreshTracks() {
            if (updating || document.hidden) return;
            updating = true;
            try {
                const response = await fetch("?" + query.toString(), {cache: "no-store"});
                if (!response.ok) throw new Error("Track request failed");
                const {tracks, limited, events: freshEvents} = await response.json();
                if (freshEvents) renderLandingMarkers(freshEvents);
                const count = document.getElementById("map-event-count");
                if (count && freshEvents) count.textContent = freshEvents.length;
                routes.clearLayers();
                livePositions.clearLayers();
                let liveCount = 0;
                for (const track of tracks) {
                    const label = document.createElement("span");
                    label.textContent = track.serial;
                    for (const segment of track.segments) {
                        if (segment.length < 2) continue;
                        L.polyline(segment, {color: "#7851c8", weight: 3, opacity: 0.8})
                            .bindTooltip(label.cloneNode(true)).addTo(routes);
                    }
                    if (!track.latest.live) continue;
                    liveCount++;
                    const popup = document.createElement("div");
                    const time = new Date(track.latest.observed * 1000).toLocaleString();
                    const altitude = track.latest.altitude == null ? "" : " · " + track.latest.altitude + " m";
                    popup.textContent = track.serial + " · GPS fix " + time + altitude;
                    appendPositionLinks(popup, track.serial, track.latest.position);
                    sondeMarker(track.latest.position, "#c82568", "sonde", popup, livePositions);
                }
                trackStatus.textContent = liveCount + " live positions · " + tracks.length + " archived tracks" +
                    (limited ? " · Showing the newest 12,000 track points. Narrow the filters for older points." : "");
            } catch (error) {
                livePositions.clearLayers();
                trackStatus.textContent = "Track refresh failed. Displayed routes may be out of date.";
            } finally {
                updating = false;
            }
        }
        L.control.layers({}, {"Observed routes": routes, "Live positions": livePositions}).addTo(map);
        refreshTracks();
        window.setInterval(refreshTracks, 30000);
        document.addEventListener("visibilitychange", refreshTracks);
    }

    function renderLandingMarkers(items) {
        const signature = JSON.stringify(items);
        if (signature === displayedEvents) return;
        displayedEvents = signature;
        landingMarkers.clearLayers();
        for (const event of items) {
            const popup = document.createElement("div");
            const title = document.createElement("strong");
            title.textContent = event.serial;
            popup.append(title);
            for (const value of [event.kind, event.status, event.time].filter(Boolean)) {
                const line = document.createElement("div");
                line.textContent = value;
                popup.append(line);
            }
            appendPositionLinks(popup, event.serial, event.position, event.mapUrl);
            const glyph = event.status === "Recovered" ? "found" : event.kind === "Predicted landing" ? "target" :
                event.kind === "Latest saved GPS fix" ? "sonde" : "fix";
            sondeMarker(event.position, event.color, glyph, popup, landingMarkers);
        }
    }
})();
</script>"""


@lru_cache(maxsize=4)
def archive_count(database, minute):
    with closing(sqlite3.connect(database)) as db:
        return db.execute("SELECT count(DISTINCT serial) FROM telemetry").fetchone()[0]


def archive_footprint(database):
    size = 0
    for path in (Path(database), Path(f"{database}-wal")):
        try:
            size += path.stat().st_size
        except FileNotFoundError:
            continue
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GiB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MiB"
    return f"{size / 1024:.0f} KiB"


def backfill_view(backfill, center, radius, token):
    heading = "<h2>Archive Backfill</h2><p>Fill gaps in known flights or discover older flights from launch stations.</p>"
    if backfill is None:
        return heading + "<p>The background worker is unavailable.</p>"
    job = backfill.status()
    content = heading
    active = job and job["state"] in ("Discovering", "Ready", "Running", "Paused")
    if job:
        state, settings, counts, sources = (job[key] for key in ("state", "settings", "counts", "sources"))
        mode = "Known flights" if settings["mode"] == "known" else "Launch station discovery"
        stations = (" · Stations " + escape(", ".join(settings["stations"]))) if settings["stations"] else ""
        content += (f'<div class="flight-panel"><h3>{escape(state)}</h3>'
                    f'<p>{mode} · {settings["first"]} to {settings["last"]} UTC · '
                    f'{settings["radius"]:g} km around {settings["center"][0]:.4f}, {settings["center"][1]:.4f}{stations}</p>'
                    f'<p>{sum(counts.values()):,} candidate flights · {counts.get("Done", 0):,} completed · '
                    f'{counts.get("Queued", 0):,} queued · {counts.get("Failed", 0):,} failed</p>'
                    f'<p>{job["added"]:,} new samples · {job["duplicates"]:,} duplicates skipped</p>')
        if sources:
            content += (f'<p>Station months: {sources.get("Done", 0)} checked · {sources.get("Queued", 0)} queued · '
                        f'{sources.get("Failed", 0)} failed. Flight counts grow as stations are scanned.</p>')
        if state == "Discovering":
            content += "<p>Scanning the station index for this date range. Full tracks will be downloaded only after you start the backfill.</p>"
        if state == "Ready" and settings["mode"] == "stations":
            content += "<p>The preview counts indexed flights from the selected stations. The archive radius is applied to the full GPS tracks during import.</p>"
        actions = {"Discovering": (("cancel", "Cancel scan"),),
                   "Ready": (("resume", "Start backfill"), ("cancel", "Cancel")),
                   "Running": (("pause", "Pause"), ("cancel", "Cancel")),
                   "Paused": (("resume", "Resume"), ("cancel", "Cancel")),
                   "Complete with errors": (("retry", "Retry failed downloads"),)}.get(state, ())
        if state == "Ready" and sources.get("Failed"):
            actions = (("rescan", "Retry station scan"),) + actions
        if actions:
            content += f'<form class="form-row" method="post"><input type="hidden" name="token" value="{escape(token)}">'
            content += "".join(f'<button name="backfill_action" value="{action}">{label}</button>' for action, label in actions)
            content += "</form>"
        if job["errors"]:
            content += "<h3>Failed requests (up to 20)</h3><ul>" + "".join(
                f'<li>{escape(row["item"])} · {escape(row["error"])}</li>' for row in job["errors"]
            ) + "</ul>"
        if state in ("Discovering", "Running"):
            content += ('<p><a href="?tab=backfill">Refresh progress</a></p>'
                        '<script>window.setTimeout(() => { if (!document.hidden) location.reload(); }, 10000);</script>')
        content += "</div>"
    if active:
        return content
    if center is None:
        return content + "<p>Waiting for the configured zone center.</p>"
    try:
        sites = field_tools.launch_sites(int(time.time() // 3600))
        nearby = []
        for key, site in sites.items():
            try:
                lon, lat = map(float, site["position"])
                nearby.append((field_tools.distance(center, (lat, lon)), key, site.get("station_name", key)))
            except (KeyError, TypeError, ValueError):
                continue
        nearby = sorted(nearby)[:16]
        selected = [key for km, key, _ in nearby if km <= radius][:8] or [key for _, key, _ in nearby[:1]]
        station_list = "".join(
            f'<label class="checkbox"><input type="checkbox" name="stations" value="{escape(key)}" {"checked" if key in selected else ""}> '
            f'{escape(name)} · {km:.0f} km</label>'
            for km, key, name in nearby
        )
    except (OSError, EOFError, AttributeError, ValueError) as error:
        logging.warning("Backfill launch station list: %s", error)
        nearby = []
        station_list = "<p>Station list unavailable. Known-flight backfill is still available.</p>"
    today = datetime.now(timezone.utc).date()
    return content + f"""
<p>The preview uses your current archive radius of <strong>{radius:g} km</strong>. Select up to 366 UTC days.
Imported GPS samples appear in Flight, Full map, and Coverage.</p>
<form class="form-row backfill-form" method="post">
<input type="hidden" name="token" value="{escape(token)}">
<input type="hidden" name="backfill_action" value="prepare">
<label>Mode<select name="mode"><option value="stations" {"selected" if nearby else "disabled"}>Discover flights by launch station</option>
<option value="known" {"" if nearby else "selected"}>Fill gaps in known flights (local serials only)</option></select></label>
<label>First day<input type="date" name="first" required value="{today - timedelta(days=30)}" max="{today}"></label>
<label>Last day<input type="date" name="last" required value="{today}" max="{today}"></label>
<button>Prepare preview</button>
<details {"open" if nearby else ""}><summary>Launch stations (choose up to eight for discovery)</summary>
<div class="station-list">{station_list}</div></details>
</form>"""


def diagnostics_view(token, results):
    checks = "".join(
        f'<tr><td>{escape(name)}</td><td><strong>{escape(status)}</strong></td><td>{escape(detail)}</td></tr>'
        for name, status, detail in (results or ())
    )
    report = ("<div class='table'><table><thead><tr><th>Check</th><th>Status</th><th>Details</th></tr></thead>"
              f"<tbody>{checks}</tbody></table></div>") if checks else ""
    return (
        f'<h2 class="section-title">{icon("diagnostics")}Diagnostics</h2>'
        '<p>Check the data feed and local archive, or send a real test message to the configured Telegram chat.</p>'
        f'<form class="form-row" method="post"><input type="hidden" name="token" value="{escape(token)}">'
        '<button name="diagnostic_action" value="checks">Run integration checks</button>'
        '<button name="diagnostic_action" value="telegram">Send Telegram test</button></form>'
        + report
    )


def history_page(query, center, radius, timezone, map_url, notify_radius=None, import_token="", tiles=None, backfill=None, diagnostic_results=None):
    notify_radius = radius if notify_radius is None else notify_radius
    period = query.get("days", ["30"])[0]
    periods = {"7": "7 days", "30": "30 days", "365": "1 year", "0": "All history"}
    if period not in periods:
        period = "30"
    serial = query.get("serial", [""])[0][:80]
    page_text = query.get("page", ["1"])[0]
    page = max(1, int(page_text)) if page_text.isascii() and page_text.isdecimal() and len(page_text) <= 9 else 1
    hide_recovered = query.get("hide_recovered") == ["1"]
    active_tab = query.get("tab", ["history"])[0]
    if active_tab not in ("history", "map", "flight", "recovery", "heatmap", "coverage", "outlook", "scorecard", "backfill", "diagnostics"):
        active_tab = "history"
    show_map = active_tab == "map"
    show_flight = active_tab == "flight"
    show_recovery = active_tab == "recovery"
    show_heatmap = active_tab == "heatmap"
    exact_serial = show_flight or show_recovery
    analytical = active_tab in ("heatmap", "coverage", "outlook", "scorecard", "backfill", "diagnostics")
    filters = {"days": period, "serial": serial, "hide_recovered": int(hide_recovered)}
    tabs = []
    for tab, label in (("history", "History"), ("map", "Full map"), ("flight", "Flight"), ("recovery", "Recovery Mode"), ("heatmap", "Heatmap"), ("coverage", "Coverage"), ("outlook", "Launches"), ("scorecard", "Scorecard"), ("backfill", "Backfill"), ("diagnostics", "Diagnostics")):
        href = escape(urlencode({**filters, "tab": tab}))
        current = ' aria-current="page"' if tab == active_tab else ""
        tabs.append(f'<a href="?{href}"{current}>{icon(tab)}{label}</a>')
    results = [] if exact_serial or analytical else read_events(int(period), serial, None if show_map else page, hide_recovered)
    rows = latest_event_positions(results[:100]) if active_tab == "history" else []
    options = "".join(f'<option value="{key}" {"selected" if key == period else ""}>{label}</option>'
                      for key, label in periods.items())
    date_filter = "" if exact_serial else f'<label>Recorded within <select name="days">{options}</select></label>'
    recovery_filter = "" if exact_serial else (
        '<label class="checkbox"><input type="checkbox" name="hide_recovered" value="1" '
        f'{"checked" if hide_recovered else ""}> Hide recovered</label>'
    )
    entries = []
    for row in rows:
        date = datetime.fromtimestamp(row["event_time"], timezone).strftime("%Y-%m-%d %H:%M %Z")
        last = ""
        if row["last_lat"] is not None:
            last = f' · <a href="{escape(map_url((row["last_lat"], row["last_lon"])))}">Last GPS fix</a>'
        fix = row.get("latest_gps")
        gps = ""
        if fix:
            observed = datetime.fromtimestamp(fix["observed"], timezone).strftime("%Y-%m-%d %H:%M %Z")
            gps = f'<br><a href="{escape(map_url(fix["position"]))}">Latest GPS fix</a> · {observed}'
        issued = row.get("forecast_issued")
        forecast = ""
        if issued is not None:
            forecast = f'<br><small>Forecast issued {datetime.fromtimestamp(issued, timezone):%Y-%m-%d %H:%M %Z}</small>'
        entries.append(
            f'<tr><td data-label="Event time">{date}{forecast}</td><td data-label="Serial"><a href="https://sondehub.org/{quote(row["serial"], safe="")}">'
            f'{escape(row["serial"])}</a> · <a href="?tab=flight&amp;serial={quote(row["serial"], safe="")}">Flight</a>'
            f'</td><td data-label="Type"><span class="chip">{icon("radius" if row["kind"] == "Predicted landing" else "recovery")}{escape(row["kind"])}</span></td>'
            f'<td data-label="Recovery">{escape(row["status"] or "Recovery not checked")}</td>'
            f'<td data-label="Distance">{row["distance_km"]:.1f} km</td>'
            f'<td data-label="Position"><a href="{escape(map_url((row["latitude"], row["longitude"])))}">Map</a>{last}{gps}</td></tr>'
        )
    navigation = []
    for target, label in (() if show_map else ((page - 1, "Previous"), (page + 1, "Next"))):
        if target >= 1 and (target < page or len(results) > 100):
            href = escape(urlencode({"days": period, "serial": serial, "page": target,
                                     "hide_recovered": int(hide_recovered)}))
            navigation.append(f'<a href="?{href}">{label}</a>')
    legend = "".join(
        f'<span class="legend-item"><i class="swatch" style="--swatch:{color}"></i>{label}</span>'
        for label, color in COLORS.items()
    )
    overview = f"""
<div class="history-grid"><section class="card"><h2 class="section-title">{icon("map")}Area map</h2>
<p>Shows the latest saved GPS fixes and forecasts for flights on this page. Reload to see new samples.</p>
{full_map(rows, center, radius, timezone, map_url, notify_radius, tiles, compact=True)}
<div class="legend">{legend}<span class="legend-item"><i class="swatch" style="--swatch:#818894"></i>Recovered</span><span class="legend-item"><i class="swatch" style="--swatch:#c82568"></i>Latest GPS fix</span></div>
</section>
{recovery_shortlist(query, timezone, center)}</div>
<section class="card"><h2 class="section-title">{icon("history")}Events</h2>
<p>Distances use the zone at the time of recording. Recovery status comes from SondeHub reports.</p>
<div class="table"><table><thead><tr><th>Event time</th><th>Serial</th><th>Type</th>
<th>Recovery</th><th>Distance</th><th>Position</th></tr></thead>
<tbody>{"".join(entries) or '<tr><td class="empty" colspan="6">No events match these filters.</td></tr>'}</tbody></table></div>
<nav class="pager" aria-label="Event pages">{' '.join(navigation)}</nav></section>""" if active_tab == "history" else ""
    content = (f'<h2 class="section-title">{icon("map")}Full map</h2><p><span id="map-event-count">{len(results)}</span> matching events</p><div class="legend">{legend}'
               '<span class="legend-item"><i class="swatch" style="--swatch:#818894"></i>Recovered</span>'
               '<span class="legend-item"><i class="swatch" style="--swatch:#7851c8"></i>Observed route</span>'
               '<span class="legend-item"><i class="swatch" style="--swatch:#c82568"></i>Live position</span></div>'
               + full_map(latest_event_positions(results, include_gps=False), center, radius, timezone, map_url,
                          notify_radius, tiles)) if show_map else overview
    if show_flight:
        content = "<h2>Flight</h2><p>Enter an exact sonde serial to compare observed and predicted routes.</p>"
        if serial:
            flight = read_flight(serial.strip(), timezone)
            content = ("<h2>Flight " + escape(flight["serial"]) + "</h2>"
                       "<p>Purple shows observed GPS fixes. Dashed blue shows the selected forecast, and faint blue dots show earlier touchdown estimates. Green marks a reported recovery when available.</p>"
                       + full_map([], center, radius, timezone, map_url, notify_radius, tiles, flight=flight))
    if show_recovery:
        content = recovery_view(serial.strip(), query.get("terrain") == ["1"], timezone, map_url, tiles) if serial.strip() else "<h2>Recovery Mode</h2><p>Enter a sonde serial to open its recovery view.</p>"
    if show_heatmap:
        controls, cells = landing_heatmap(query, timezone)
        count = sum(cell["count"] for cell in cells)
        content = (f"<h2>Landing heatmap</h2>{controls}<p>{count:,} locations. Darker cells contain more records.</p>"
                   + full_map([], center, radius, timezone, map_url, notify_radius, tiles, heatmap=cells))
    if active_tab == "coverage":
        content = coverage_view(query, center, radius, timezone, map_url, notify_radius, tiles)
    if active_tab == "outlook":
        content = outlook_view(query, center, timezone, map_url)
    if active_tab == "scorecard":
        content = scorecard_view()
    if active_tab == "backfill":
        content = backfill_view(backfill, center, radius, import_token)
    if active_tab == "diagnostics":
        content = diagnostics_view(import_token, diagnostic_results)
    serial_form = "" if analytical else f"""<form class="form-row card" method="get"><input type="hidden" name="tab" value="{active_tab}">{date_filter}
<label>{"Exact sonde serial" if exact_serial else "Sonde serial"} <input name="serial" value="{escape(serial)}" maxlength="80"></label>
{recovery_filter}
<button>{"Show sonde" if exact_serial else "Apply filters"}</button></form>"""
    try:
        count = f"{archive_count(DATABASE, int(time.time() // 60)):,} archived sondes"
        footprint = archive_footprint(DATABASE) + " database"
    except (OSError, sqlite3.Error):
        if active_tab != "diagnostics":
            raise
        count, footprint = "Archive unavailable", ""
    view = content if active_tab == "history" else f'<section class="card">{content}</section>'
    panel_link = """
<script>
try {
    if (window.parent !== window && !location.search) {
        const serial = new URLSearchParams(window.parent.location.search).get("sonde_tools_flight");
        if (serial && /^[A-Za-z0-9_-]{1,80}$/.test(serial)) {
            location.replace("?" + new URLSearchParams({tab: "flight", serial}));
        }
    }
} catch (error) {
    // An iframe on another origin cannot inspect its parent.
}
</script>
""" if active_tab == "history" else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sonde Tools · {active_tab.title()}</title>
<style>{INTERFACE_CSS}</style>{panel_link}</head>
<body><div class="shell">
<header class="hero">
  <div class="brand"><div class="brand-mark">{icon("brand")}</div>
    <div><div class="eyebrow">Radiosonde tracker</div><h1>Sonde Tools</h1>
      <p>Flight tracks, forecasts, and recoveries.</p></div></div>
  <div class="hero-stats"><div class="metrics" aria-label="Configured radii">
    <div class="metric">{icon("radius")}<div><small>Archive radius</small><strong>{radius:g} km</strong></div></div>
    <div class="metric">{icon("bell")}<div><small>Telegram radius</small><strong>{notify_radius:g} km</strong></div></div>
  </div>
  <div class="archive-stats">{icon("database")}<strong>{count}</strong>
    <span title="SQLite database and write-ahead log">{footprint}</span></div>
  </div>
</header>
<nav class="tabs" aria-label="SondeHub views">{"".join(tabs)}</nav>
<main class="workspace">
{serial_form}
{view}
<details class="card toolbox">
<summary>{icon("database")}Telemetry import and export</summary>
<div class="tool-forms">
<form class="form-row" method="get"><input type="hidden" name="download" value="telemetry">
<label>Download saved telemetry <input name="serial" placeholder="Exact sonde serial" required maxlength="80"></label>
<button>Download JSON</button></form>
<form class="form-row" method="post"><input type="hidden" name="token" value="{escape(import_token)}">
<label>Fetch from SondeHub <input name="serial" placeholder="Exact sonde serial" required maxlength="80"></label>
<button>Fetch and archive</button></form>
</div></details>
</main>
<footer class="app-footer">Sonde Tools <span>v{escape(APP_VERSION)}</span></footer>
</div></body></html>"""


def serve(center, radius, timezone, map_url, fetch_reports, notify_radius, import_telemetry, tiles=None, health=None, backfill=None, diagnostics=None):
    import_token = token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.client_address[0] != "172.30.32.2":
                self.send_error(403)
                return
            if urlsplit(self.path).path != "/":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2048:
                    raise ValueError
                form = parse_qs(self.rfile.read(size).decode())
            except (UnicodeError, ValueError):
                self.send_error(400, "Invalid import form")
                return
            token = form.get("token", [""])[0]
            if not token.isascii() or not compare_digest(token, import_token):
                self.send_error(403, "Refresh the history page before importing")
                return
            diagnostic_action = form.get("diagnostic_action", [""])[0]
            if diagnostic_action:
                if diagnostics is None or diagnostic_action not in ("checks", "telegram"):
                    self.send_error(400, "Unknown diagnostic action")
                    return
                results = diagnostics(diagnostic_action)
                body = history_page({"tab": ["diagnostics"]}, center(), radius, timezone, map_url,
                                    notify_radius, import_token, tiles, backfill, results).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            action = form.get("backfill_action", [""])[0]
            if action and backfill:
                try:
                    if action == "prepare":
                        backfill.prepare(form, center(), radius)
                    else:
                        backfill.action(action)
                except ValueError as error:
                    self.send_error(400, str(error))
                    return
                except sqlite3.Error as error:
                    if health:
                        health.fail("Backfill database", f"Could not change backfill state ({error_detail(error)}).", threshold=1)
                    self.send_error(503, "Backfill state is temporarily unavailable")
                    return
                self.send_response(303)
                self.send_header("Location", "?tab=backfill")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            serial = form.get("serial", [""])[0].strip().upper()
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,79}", serial):
                self.send_error(400, "Enter a valid sonde serial")
                return

            try:
                added = import_telemetry(serial)
                message = f"Added {added:,} new samples for {serial}."
                if not added:
                    message += " In-range samples may already be saved, or this flight had no in-range samples."
                status = 200
            except HTTPError as exc:
                status = 404 if exc.code == 404 else 502
                message = ("No serial archive is available from SondeHub."
                           if status == 404 else f"SondeHub returned HTTP {exc.code}.")
            except (OSError, EOFError, ValueError, sqlite3.Error) as exc:
                logging.warning("SondeHub import for %s failed: %s", serial, exc)
                status = 502
                message = "The import failed. Check the app log and try again."

            download = escape(urlencode({"download": "telemetry", "serial": serial}))
            body = (f'<!doctype html><html lang="en"><meta charset="utf-8"><title>Telemetry import</title>'
                    f'<h1>Telemetry import</h1><p>{escape(message)}</p>'
                    f'<p><a href="?">Back to history</a> · <a href="?{download}">Download saved JSON</a></p>'
                    f'<footer>Sonde Tools v{escape(APP_VERSION)}</footer></html>').encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.client_address[0] != "172.30.32.2":
                self.send_error(403)
                return
            url = urlsplit(self.path)
            if url.path != "/":
                self.send_error(404)
                return
            try:
                query = parse_qs(url.query)
                latest = query.get("latest") == ["1"]
                forecast_path = query.get("path") == ["1"]
                tracks = query.get("tracks") == ["1"]
                download = query.get("download") == ["telemetry"] and not tracks
                if latest:
                    serial = query.get("serial", [""])[0][:80]
                    body = json.dumps(read_latest(serial)).encode()
                elif forecast_path:
                    try:
                        issued = float(query.get("issued", [""])[0])
                        serial = query.get("serial", [""])[0][:80]
                        if not serial or not math.isfinite(issued):
                            raise ValueError
                    except ValueError:
                        self.send_error(400, "A serial and forecast issue time are required")
                        return
                    body = json.dumps(read_forecast_path(serial, issued)).encode()
                elif tracks:
                    data = read_tracks(query)
                    if query.get("map_events") == ["1"]:
                        days = {"7": 7, "30": 30, "365": 365, "0": 0}.get(query.get("days", ["30"])[0], 30)
                        serial = query.get("serial", [""])[0][:80]
                        hide_recovered = query.get("hide_recovered") == ["1"]
                        rows = read_events(days, serial, None, hide_recovered)
                        data["events"] = map_events(latest_event_positions(rows, include_gps=False), timezone, map_url)
                    body = json.dumps(data).encode()
                elif download:
                    serial = query.get("serial", [""])[0].strip()
                    if not serial or len(serial) > 80:
                        self.send_error(400, "An exact sonde serial is required")
                        return
                    body = read_telemetry(serial)
                else:
                    body = history_page(query, center(), radius, timezone, map_url, notify_radius, import_token, tiles, backfill).encode()
            except sqlite3.Error as error:
                if health:
                    health.fail("History database", f"Could not read local history ({error_detail(error)}).", threshold=1)
                logging.exception("Unable to read landing history")
                self.send_error(503, "History is temporarily unavailable")
                return
            self.send_response(200)
            if health:
                health.healthy("History database")
            if tracks or forecast_path or latest:
                self.send_header("Content-Type", "application/json")
            elif download:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", "attachment; filename=sondehub-telemetry.json")
            else:
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("", 8099), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    Thread(target=sync_recoveries, args=(fetch_reports, health), daemon=True).start()
