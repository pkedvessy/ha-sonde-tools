import hashlib
import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

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


# Only measurements and counters: identifiers and encoded payloads stay textual.
NUMERIC = frozenset((
    "frame", "lat", "lon", "alt", "vel_v", "vel_h", "heading", "sats",
    "temp", "humidity", "pressure", "batt", "burst_timer", "tx_frequency",
    "snr", "rssi", "frequency", "upload_time_delta", "uploader_alt",
))
COUNTERS = frozenset(("frame", "sats", "burst_timer"))
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def numeric(value):
    if type(value) is not str or not NUMBER.fullmatch(value.strip()):
        return value
    try:
        exact = Decimal(value.strip())
        if exact == exact.to_integral_value():
            if -(2**63) <= exact < 2**63:
                return int(exact)
            return value
        result = float(exact)
        return result if math.isfinite(result) and result != 0 else value
    except (InvalidOperation, ValueError, OverflowError):
        return value


def normalize(row):
    result = dict(row)
    for key, value in row.items():
        if key in NUMERIC:
            value = numeric(value)
            if key not in COUNTERS and type(value) in (int, float):
                value = float(value)
        elif key in STANDARD and type(value) in (str, int, float):
            value = str(value)
        result[key] = value
    return result


def normalize_existing(db):
    db.execute("CREATE TABLE IF NOT EXISTS archive_migrations (name TEXT PRIMARY KEY)")
    name = "numeric_measurements_v1"
    if db.execute("SELECT 1 FROM archive_migrations WHERE name=?", (name,)).fetchone():
        return
    db.create_function("archive_numeric", 1, numeric)
    try:
        for table, columns in (("telemetry_fixes", FIX), ("telemetry_reports", REPORT),
                               ("telemetry_receivers", RECEIVER)):
            for column in columns:
                if column in NUMERIC:
                    db.execute(f"UPDATE {table} SET {column}=archive_numeric({column}) "
                               f"WHERE typeof({column})='text'")
        for column in NUMERIC:
            db.execute("UPDATE telemetry_fields SET value=archive_numeric(value) "
                       "WHERE path=? AND kind='scalar' AND typeof(value)='text'", ("/" + column,))
        # Receiver signatures must match the normalized profiles used by new writes.
        for receiver in db.execute(f"SELECT id, {', '.join(RECEIVER)} FROM telemetry_receivers").fetchall():
            receiver_id, *profile = receiver
            signature = hashlib.sha256(repr(tuple(profile)).encode()).digest()
            existing = db.execute("SELECT id FROM telemetry_receivers WHERE signature=?", (signature,)).fetchone()
            if existing and existing[0] != receiver_id:
                db.execute("UPDATE telemetry_reports SET receiver_id=? WHERE receiver_id=?", (existing[0], receiver_id))
                db.execute("DELETE FROM telemetry_receivers WHERE id=?", (receiver_id,))
            else:
                db.execute("UPDATE telemetry_receivers SET signature=? WHERE id=?", (signature, receiver_id))
        db.execute("INSERT INTO archive_migrations VALUES (?)", (name,))
    finally:
        db.create_function("archive_numeric", 1, None)


def fields(columns):
    return ", ".join(f"{column} {column_type(column)}" for column in columns)


def column_type(column):
    return "INTEGER" if column in COUNTERS else "REAL" if column in NUMERIC else "TEXT"


def typed_schema(db):
    tables = ("telemetry_fixes", "telemetry_receivers", "telemetry_reports", "telemetry_fields")
    if all(row[2] == column_type(row[1]) for table, columns in
           ((tables[0], FIX), (tables[1], RECEIVER), (tables[2], REPORT))
           for row in db.execute(f"PRAGMA table_info({table})") if row[1] in columns):
        return
    # Rebuild related tables together, preserving IDs and foreign-key relationships.
    db.execute("SAVEPOINT typed_archive")
    try:
        db.execute("DROP VIEW IF EXISTS telemetry")
        for table in tables:
            sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            for original in tables:
                sql = re.sub(r"\b" + original + r"\b", original + "_typed", sql)
            for column in FIX + RECEIVER + REPORT:
                sql = re.sub(r"\b" + column + r"\s+BLOB\b", column + " " + column_type(column), sql)
            db.execute(sql)
            db.execute(f"INSERT INTO {table}_typed SELECT * FROM {table}")
        for table in reversed(tables):
            db.execute(f"DROP TABLE {table}")
        for table in tables:
            db.execute(f"ALTER TABLE {table}_typed RENAME TO {table}")
        # REAL/TEXT affinity can change Python scalar types used by signatures.
        db.execute("UPDATE telemetry_receivers SET signature=randomblob(32)")
        for receiver in db.execute(f"SELECT id, {', '.join(RECEIVER)} FROM telemetry_receivers").fetchall():
            receiver_id, *profile = receiver
            signature = hashlib.sha256(repr(tuple(profile)).encode()).digest()
            existing = db.execute("SELECT id FROM telemetry_receivers WHERE signature=?", (signature,)).fetchone()
            if existing:
                db.execute("UPDATE telemetry_reports SET receiver_id=? WHERE receiver_id=?", (existing[0], receiver_id))
                db.execute("DELETE FROM telemetry_receivers WHERE id=?", (receiver_id,))
            else:
                db.execute("UPDATE telemetry_receivers SET signature=? WHERE id=?", (signature, receiver_id))
        db.execute("INSERT OR IGNORE INTO archive_migrations VALUES ('explicit_column_types_v1')")
        db.execute("RELEASE typed_archive")
    except Exception:
        db.execute("ROLLBACK TO typed_archive")
        db.execute("RELEASE typed_archive")
        raise


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
    normalize_existing(db)
    typed_schema(db)
    initialize_summaries(db)
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


# Plausible ranges: anything outside is a decoding error and must not become a record.
SUMMARY_LIMITS = {"alt": (-500, 50000), "vel_h": (0, 150), "vel_v": (-150, 0), "temp": (-110, 60)}


def summary_sql(column):
    low, high = SUMMARY_LIMITS[column]
    return f"CASE WHEN {column} BETWEEN {low} AND {high} THEN {column} END"


def initialize_summaries(db):
    """Per-serial flight statistics that outlive telemetry retention."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS flight_summaries (
            serial TEXT PRIMARY KEY, first_observed REAL NOT NULL, last_observed REAL NOT NULL,
            fixes INTEGER NOT NULL, max_alt REAL, max_speed REAL, min_vertical REAL, min_temp REAL, type TEXT
        ) WITHOUT ROWID
    """)
    db.execute("CREATE INDEX IF NOT EXISTS flight_summaries_first ON flight_summaries(first_observed)")
    name = "flight_summaries_v1"
    if not db.execute("SELECT 1 FROM archive_migrations WHERE name=?", (name,)).fetchone():
        # One-time fill from the fixes still present; fixes already removed by retention cannot be recovered.
        db.execute(f"""
            INSERT OR REPLACE INTO flight_summaries
                (serial, first_observed, last_observed, fixes, max_alt, max_speed, min_vertical, min_temp, type)
            SELECT serial, min(observed), max(observed), count(*), max({summary_sql("alt")}),
                   max({summary_sql("vel_h")}), min({summary_sql("vel_v")}), min({summary_sql("temp")}), max(type)
            FROM telemetry_fixes GROUP BY serial
        """)
        db.execute("INSERT INTO archive_migrations VALUES (?)", (name,))
    # Whole-track statistics, recomputed in the background by flight_stats. New columns default to
    # dirty = 1, so existing flights are filled in gradually after an upgrade.
    existing = {row[1] for row in db.execute("PRAGMA table_info(flight_summaries)")}
    for column, kind in DERIVED_COLUMNS:
        if column not in existing:
            db.execute(f"ALTER TABLE flight_summaries ADD COLUMN {column} {kind}")
    db.execute("CREATE INDEX IF NOT EXISTS flight_summaries_dirty ON flight_summaries(dirty) WHERE dirty = 1")


DERIVED_COLUMNS = (
    ("first_lat", "REAL"), ("first_lon", "REAL"), ("first_alt", "REAL"),
    ("last_lat", "REAL"), ("last_lon", "REAL"), ("last_alt", "REAL"),
    ("track_km", "REAL"), ("straight_km", "REAL"), ("bearing", "REAL"),
    ("burst_observed", "REAL"), ("max_ascent", "REAL"), ("speed_alt", "REAL"),
    ("receivers", "INTEGER"), ("frequency", "REAL"), ("subtype", "TEXT"),
    ("dirty", "INTEGER NOT NULL DEFAULT 1"),
)


def _limited(value, column):
    low, high = SUMMARY_LIMITS[column]
    return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


def _merge(current, new, pick):
    values = [value for value in (current, new) if value is not None]
    return pick(values) if values else None


def summarize_fix(summaries, serial, observed, values):
    """Fold one newly inserted fix into the pending per-serial summary."""
    row = dict(zip(FIX, values))
    entry = summaries.setdefault(serial, [observed, observed, 0, None, None, None, None, None])
    entry[0], entry[1] = min(entry[0], observed), max(entry[1], observed)
    entry[2] += 1
    entry[3] = _merge(entry[3], _limited(row["alt"], "alt"), max)
    entry[4] = _merge(entry[4], _limited(row["vel_h"], "vel_h"), max)
    entry[5] = _merge(entry[5], _limited(row["vel_v"], "vel_v"), min)
    entry[6] = _merge(entry[6], _limited(row["temp"], "temp"), min)
    if entry[7] is None and isinstance(row["type"], str) and row["type"]:
        entry[7] = row["type"]


def write_summaries(db, summaries):
    db.executemany("""
        INSERT INTO flight_summaries (serial, first_observed, last_observed, fixes, max_alt, max_speed,
                                      min_vertical, min_temp, type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (serial) DO UPDATE SET
            first_observed = min(first_observed, excluded.first_observed),
            last_observed = max(last_observed, excluded.last_observed),
            fixes = fixes + excluded.fixes,
            max_alt = CASE WHEN max_alt IS NULL OR excluded.max_alt > max_alt THEN excluded.max_alt ELSE max_alt END,
            max_speed = CASE WHEN max_speed IS NULL OR excluded.max_speed > max_speed
                             THEN excluded.max_speed ELSE max_speed END,
            min_vertical = CASE WHEN min_vertical IS NULL OR excluded.min_vertical < min_vertical
                                THEN excluded.min_vertical ELSE min_vertical END,
            min_temp = CASE WHEN min_temp IS NULL OR excluded.min_temp < min_temp THEN excluded.min_temp ELSE min_temp END,
            type = coalesce(type, excluded.type),
            dirty = 1
    """, ((serial, *entry) for serial, entry in summaries.items()))


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
    summaries = {}
    for serial, observed, uploader, row in samples:
        row = normalize(row)
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
                summarize_fix(summaries, serial, observed, values)
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
    if summaries:
        write_summaries(db, summaries)
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
