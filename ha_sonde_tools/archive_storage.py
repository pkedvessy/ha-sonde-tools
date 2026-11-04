import hashlib
import math
from datetime import datetime

FIX = (
    "datetime", "frame", "lat", "lon", "alt", "vel_v", "vel_h", "heading", "sats",
    "temp", "humidity", "pressure", "batt", "burst_timer", "tx_frequency",
    "position", "ref_position", "ref_datetime", "manufacturer", "type", "subtype",
    "rs41_mainboard", "rs41_mainboard_fw", "rs41_subframe", "xdata",
)
REPORT = ("time_received", "snr", "rssi", "frequency", "upload_time", "upload_time_delta")
RECEIVER = (
    "uploader_callsign", "uploader_position", "uploader_alt", "uploader_antenna",
    "uploader_radio", "software_name", "software_version",
)
STANDARD = set(FIX + REPORT + RECEIVER + ("serial",))
GPS = ("lat", "lon", "alt", "vel_v", "vel_h", "heading")


def fields(columns):
    # BLOB affinity preserves upstream scalar types, including numeric strings.
    return ", ".join(f"{column} BLOB" for column in columns)


def initialize(db):
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS telemetry_fixes (
            id INTEGER PRIMARY KEY, serial TEXT NOT NULL, observed REAL NOT NULL,
            {fields(FIX)}, UNIQUE (serial, observed)
        )
    """)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS telemetry_receivers (
            id INTEGER PRIMARY KEY, signature BLOB UNIQUE NOT NULL, {fields(RECEIVER)}
        )
    """)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS telemetry_reports (
            fix_id INTEGER NOT NULL REFERENCES telemetry_fixes(id),
            uploader TEXT NOT NULL, receiver_id INTEGER NOT NULL REFERENCES telemetry_receivers(id),
            {fields(REPORT)}, PRIMARY KEY (fix_id, uploader)
        ) WITHOUT ROWID
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_fields (
            fix_id INTEGER NOT NULL, uploader TEXT NOT NULL, path TEXT NOT NULL,
            kind TEXT NOT NULL, value BLOB,
            PRIMARY KEY (fix_id, uploader, path),
            FOREIGN KEY (fix_id, uploader) REFERENCES telemetry_reports(fix_id, uploader)
        ) WITHOUT ROWID
    """)
    normalized = ", ".join(f"f.{key}" for key in GPS)
    db.execute(f"""
        CREATE VIEW IF NOT EXISTS telemetry AS
        SELECT f.serial, f.observed, r.uploader, {normalized}
        FROM telemetry_fixes AS f JOIN telemetry_reports AS r ON r.fix_id = f.id
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS forecast_points (
            serial TEXT NOT NULL, issued REAL NOT NULL, step INTEGER NOT NULL,
            latitude REAL NOT NULL, longitude REAL NOT NULL, altitude REAL, observed REAL,
            PRIMARY KEY (serial, issued, step)
        ) WITHOUT ROWID
    """)


def scalar(value):
    if type(value) in (str, float) or (type(value) is int and -(2**63) <= value < 2**63):
        return value
    return None


def field_path(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def value_nodes(path, value):
    if isinstance(value, dict):
        yield path, "object", None
        for key, item in value.items():
            yield from value_nodes(field_path(path, key), item)
    elif isinstance(value, list):
        yield path, "array", None
        for index, item in enumerate(value):
            yield from value_nodes(field_path(path, index), item)
    elif isinstance(value, bool):
        yield path, "bool", int(value)
    elif value is None:
        yield path, "null", None
    elif type(value) is int and scalar(value) is None:
        yield path, "integer", str(value)
    else:
        yield path, "scalar", value


def write_packets(db, samples):
    fixes = {}
    receivers = {}
    added = 0
    for serial, observed, uploader, row in samples:
        key = serial, observed
        if key not in fixes:
            found = db.execute(f"SELECT id, {', '.join(FIX)} FROM telemetry_fixes WHERE serial=? AND observed=?", key).fetchone()
            if found is None:
                values = tuple(scalar(row.get(name)) for name in FIX)
                result = db.execute(
                    f"INSERT INTO telemetry_fixes (serial, observed, {', '.join(FIX)}) "
                    f"VALUES ({', '.join('?' for _ in range(len(FIX) + 2))})", (*key, *values),
                )
                found = (result.lastrowid, *values)
            fixes[key] = tuple(found)
        fix_id, *canonical = fixes[key]
        profile = tuple(scalar(row.get(name)) for name in RECEIVER)
        signature = hashlib.sha256(repr(profile).encode()).digest()
        if signature not in receivers:
            result = db.execute(
                f"INSERT OR IGNORE INTO telemetry_receivers (signature, {', '.join(RECEIVER)}) "
                f"VALUES ({', '.join('?' for _ in range(len(RECEIVER) + 1))})", (signature, *profile),
            )
            receivers[signature] = (result.lastrowid if result.rowcount
                                  else db.execute("SELECT id FROM telemetry_receivers WHERE signature=?", (signature,)).fetchone()[0])
        report = tuple(scalar(row.get(name)) for name in REPORT)
        result = db.execute(
            f"INSERT OR IGNORE INTO telemetry_reports (fix_id, uploader, receiver_id, {', '.join(REPORT)}) "
            f"VALUES ({', '.join('?' for _ in range(len(REPORT) + 3))})",
            (fix_id, uploader, receivers[signature], *report),
        )
        if not result.rowcount:
            continue
        added += 1
        base = dict(zip(FIX + REPORT + RECEIVER, (*canonical, *report, *profile)))
        base["serial"] = serial
        extras = []
        for name, value in base.items():
            path = field_path("", name)
            if name not in row:
                if value is not None:
                    extras.append((path, "omit", None))
            elif value is None or type(row[name]) is not type(value) or row[name] != value:
                extras.extend(value_nodes(path, row[name]))
        for name in row.keys() - STANDARD:
            extras.extend(value_nodes(field_path("", name), row[name]))
        if extras:
            db.executemany("INSERT INTO telemetry_fields VALUES (?, ?, ?, ?, ?)",
                           ((fix_id, uploader, path, kind, value) for path, kind, value in extras))
    return added


def packet_select():
    return ", ".join(f"{alias}.{key} AS {key}" for alias, columns in
                     (("fix", FIX), ("report", REPORT), ("receiver", RECEIVER)) for key in columns)


def packet_extras(db, serial):
    extras = {}
    for fix_id, uploader, path, kind, value in db.execute("""
        SELECT x.fix_id, x.uploader, x.path, x.kind, x.value
        FROM telemetry_fixes AS f JOIN telemetry_fields AS x ON x.fix_id = f.id
        WHERE f.serial = ? ORDER BY x.fix_id, x.uploader, x.path
    """, (serial,)):
        extras.setdefault((fix_id, uploader), []).append((path, kind, value))
    return extras


def packet_json(row, extras):
    packet = {name: row[name] for name in FIX + REPORT + RECEIVER + ("serial",) if row[name] is not None}
    for path, kind, value in extras.get((row["fix_id"], row["uploader"]), ()):
        keys = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
        if kind == "omit":
            packet.pop(keys[0], None)
            continue
        target = packet
        for key in keys[:-1]:
            target = target[int(key) if isinstance(target, list) else key]
        if kind == "object":
            value = {}
        elif kind == "array":
            value = []
        elif kind == "bool":
            value = bool(value)
        elif kind == "integer":
            value = int(value)
        if isinstance(target, list):
            index = int(keys[-1])
            target.extend([None] * (index + 1 - len(target)))
            target[index] = value
        else:
            target[keys[-1]] = value
    return packet


def record_path(db, serial, issued, path):
    points = []
    for step, sample in enumerate(path or ()):
        try:
            lat = float(sample.get("lat", sample.get("latitude")))
            lon = float(sample.get("lon", sample.get("longitude")))
            if not (-90 <= lat <= 90 and -180 <= lon <= 360):
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        try:
            alt = float(sample.get("alt", sample.get("altitude")))
            alt = alt if math.isfinite(alt) else None
        except (TypeError, ValueError):
            alt = None
        try:
            observed = float(sample["time"]) if "time" in sample else datetime.fromisoformat(sample["datetime"]).timestamp()
            observed = observed if math.isfinite(observed) else None
        except (KeyError, OSError, TypeError, ValueError, OverflowError):
            observed = None
        points.append((serial, issued, step, lat, (lon + 180) % 360 - 180, alt, observed))
    if points:
        db.executemany("INSERT OR IGNORE INTO forecast_points VALUES (?, ?, ?, ?, ?, ?, ?)", points)
