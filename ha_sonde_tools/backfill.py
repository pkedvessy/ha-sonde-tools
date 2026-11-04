import gzip
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from threading import Thread
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree

from app_version import USER_AGENT
from health_alerts import error_detail
import archive_storage as archive

BUCKET = "https://sondehub-history.s3.amazonaws.com"
SERIAL = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,79}")
S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def fetch_archive(serial):
    url = f"{BUCKET}/serial/{quote(serial, safe='')}.json.gz"
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=25) as response:
        with gzip.GzipFile(fileobj=response) as archive:
            limit = 64 * 1024 * 1024
            payload = archive.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("This archive exceeds the 64 MiB import limit.")
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise ValueError("SondeHub returned an unexpected archive format.")
    return (row for row in rows if isinstance(row, dict) and str(row.get("serial", "")).upper() == serial)


def station_serials(prefix, first, last):
    token = None
    serials = set()
    while True:
        params = {"list-type": 2, "prefix": prefix}
        if token:
            params["continuation-token"] = token
        request = Request(f"{BUCKET}/?{urlencode(params)}", headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=25) as response:
            root = ElementTree.fromstring(response.read(2 * 1024 * 1024))
        for content in root.findall(f"{S3}Contents"):
            key = content.findtext(f"{S3}Key", "")
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
            match = re.fullmatch(r"(\d{2})/([A-Za-z0-9_-]+)\.json", suffix)
            if not match:
                continue
            _, year, month = prefix.rstrip("/").rsplit("/", 2)
            try:
                day = date(int(year), int(month), int(match[1]))
            except ValueError:
                continue
            serial = match[2].upper()
            if first <= day <= last and SERIAL.fullmatch(serial):
                serials.add(serial)
        next_token = root.findtext(f"{S3}NextContinuationToken")
        if root.findtext(f"{S3}IsTruncated") != "true":
            return serials
        if not next_token or next_token == token:
            raise ValueError("Invalid SondeHub archive pagination")
        token = next_token
        time.sleep(0.5)


class Backfill:
    def __init__(self, database, prepare_samples, health):
        self.database = database
        self.prepare_samples = prepare_samples
        self.health = health
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS backfill_job (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1), id TEXT NOT NULL,
                    state TEXT NOT NULL, settings TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backfill_sources (
                    prefix TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS backfill_items (
                    serial TEXT PRIMARY KEY, state TEXT NOT NULL,
                    added INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0, error TEXT
                );
            """)
            db.execute("""
                UPDATE backfill_job SET state='Discovering' WHERE state='Ready'
                AND EXISTS (SELECT 1 FROM backfill_sources WHERE state='Queued')
            """)

    def connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        return db

    def prepare(self, form, center, radius):
        if center is None:
            raise ValueError("Wait for Home Assistant to resolve the configured zone.")
        try:
            first = date.fromisoformat(form.get("first", [""])[0])
            last = date.fromisoformat(form.get("last", [""])[0])
        except ValueError:
            raise ValueError("Enter a valid start date and end date.") from None
        if not 0 <= (last - first).days < 366 or not date(2000, 1, 1) <= first <= last <= datetime.now(timezone.utc).date():
            raise ValueError("Choose up to 366 days ending today or earlier.")
        mode = form.get("mode", [""])[0]
        stations = sorted(set(form.get("stations", [])))
        if mode not in ("known", "stations"):
            raise ValueError("Select a backfill mode.")
        if mode == "stations" and (not 1 <= len(stations) <= 8 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", site) for site in stations)):
            raise ValueError("Choose one to eight launch stations.")
        start = datetime.combine(first, datetime.min.time(), timezone.utc).timestamp()
        end = datetime.combine(last + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()
        settings = {"mode": mode, "first": first.isoformat(), "last": last.isoformat(),
                    "start": start, "end": end, "center": center, "radius": radius,
                    "stations": stations if mode == "stations" else []}
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT state FROM backfill_job").fetchone()
            if current and current["state"] in ("Discovering", "Ready", "Running", "Paused"):
                raise ValueError("Finish or cancel the existing job first.")
            db.execute("DELETE FROM backfill_items")
            db.execute("DELETE FROM backfill_sources")
            state = "Discovering" if mode == "stations" else "Ready"
            db.execute("INSERT OR REPLACE INTO backfill_job VALUES (1, ?, ?, ?)",
                       (uuid4().hex, state, json.dumps(settings)))
            if mode == "known":
                rows = db.execute("""
                    SELECT upper(serial) AS serial FROM telemetry WHERE observed >= ? AND observed < ?
                    UNION SELECT upper(serial) FROM events WHERE event_time >= ? AND event_time < ?
                    UNION SELECT upper(serial) FROM forecasts WHERE issued >= ? AND issued < ?
                """, (start, end, start, end, start, end)).fetchall()
                db.executemany("INSERT INTO backfill_items (serial, state) VALUES (?, 'Queued')",
                               ((row["serial"],) for row in rows if SERIAL.fullmatch(row["serial"])))
            else:
                month = first.replace(day=1)
                while month <= last:
                    db.executemany("INSERT INTO backfill_sources VALUES (?, 'Queued', NULL)",
                                   ((f"launchsites/{site}/{month:%Y/%m}/",) for site in stations))
                    month = month.replace(year=month.year + (month.month == 12), month=month.month % 12 + 1)

    def action(self, action):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT state FROM backfill_job").fetchone()
            if not job:
                raise ValueError("There is no backfill job.")
            state = job["state"]
            if action == "resume" and state in ("Ready", "Paused"):
                next_state = "Running"
            elif action == "pause" and state == "Running":
                next_state = "Paused"
            elif action == "cancel" and state in ("Discovering", "Ready", "Paused", "Running"):
                next_state = "Cancelled"
            elif action == "rescan" and state == "Ready":
                db.execute("UPDATE backfill_sources SET state='Queued', error=NULL WHERE state='Failed'")
                next_state = "Discovering"
            elif action == "retry" and state == "Complete with errors":
                db.execute("UPDATE backfill_items SET state='Queued', error=NULL WHERE state='Failed'")
                db.execute("UPDATE backfill_sources SET state='Queued', error=NULL WHERE state='Failed'")
                next_state = "Running"
            else:
                raise ValueError("That action is unavailable in the current job state.")
            db.execute("UPDATE backfill_job SET state=?", (next_state,))

    def status(self):
        with closing(self.connect()) as db:
            job = db.execute("SELECT state, settings FROM backfill_job").fetchone()
            if not job:
                return None
            counts = {row["state"]: row["total"] for row in db.execute(
                "SELECT state, count(*) AS total FROM backfill_items GROUP BY state")}
            added, duplicates = db.execute(
                "SELECT coalesce(sum(added), 0), coalesce(sum(duplicates), 0) FROM backfill_items").fetchone()
            sources = {row["state"]: row["total"] for row in db.execute(
                "SELECT state, count(*) AS total FROM backfill_sources GROUP BY state")}
            errors = db.execute("""
                SELECT serial AS item, error FROM backfill_items WHERE state='Failed'
                UNION ALL SELECT prefix, error FROM backfill_sources WHERE state='Failed' LIMIT 20
            """).fetchall()
        return {"state": job["state"], "settings": json.loads(job["settings"]), "counts": counts,
                "added": added, "duplicates": duplicates, "sources": sources, "errors": errors}

    def step(self):
        with closing(self.connect()) as db:
            job = db.execute("SELECT id, state, settings FROM backfill_job WHERE state IN ('Discovering', 'Running')").fetchone()
            if not job:
                return False
            source = db.execute("SELECT prefix FROM backfill_sources WHERE state='Queued' ORDER BY prefix LIMIT 1").fetchone()
            item = None
            if job["state"] == "Running" and not source:
                item = db.execute("SELECT serial FROM backfill_items WHERE state='Queued' ORDER BY serial LIMIT 1").fetchone()
        settings = json.loads(job["settings"])
        error = None
        try:
            if source:
                serials = station_serials(source["prefix"], date.fromisoformat(settings["first"]),
                                         date.fromisoformat(settings["last"]))
            elif item:
                samples, _ = self.prepare_samples(fetch_archive(item["serial"]), settings["center"],
                                                  settings["radius"], settings["start"], settings["end"])
        except (OSError, EOFError, TypeError, ValueError, KeyError, ElementTree.ParseError) as exc:
            error = error_detail(exc)
            logging.warning("Backfill download for %s: %s", (source or item)[0], error)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT state FROM backfill_job WHERE id=?", (job["id"],)).fetchone()
            if not current or current["state"] not in ("Discovering", "Running", "Paused"):
                return True
            if source:
                if not error:
                    db.executemany("INSERT OR IGNORE INTO backfill_items (serial, state) VALUES (?, 'Queued')",
                                   ((serial,) for serial in serials))
                db.execute("UPDATE backfill_sources SET state=?, error=? WHERE prefix=?",
                           ("Failed" if error else "Done", error, source["prefix"]))
            elif item:
                if error:
                    db.execute("UPDATE backfill_items SET state='Failed', error=? WHERE serial=?", (error, item["serial"]))
                else:
                    added = archive.write_packets(db, samples)
                    db.execute("UPDATE backfill_items SET state='Done', added=?, duplicates=?, error=NULL WHERE serial=?",
                               (added, len(samples) - added, item["serial"]))
            elif current["state"] == "Discovering":
                db.execute("UPDATE backfill_job SET state='Ready'")
            elif current["state"] == "Running":
                failures = db.execute("""
                    SELECT 1 FROM backfill_items WHERE state='Failed'
                    UNION ALL SELECT 1 FROM backfill_sources WHERE state='Failed' LIMIT 1
                """).fetchone()
                db.execute("UPDATE backfill_job SET state=?", ("Complete with errors" if failures else "Complete",))
        return True

    def start(self):
        def run():
            while True:
                try:
                    working = self.step()
                    if working:
                        self.health.healthy("Backfill database")
                except sqlite3.Error as exc:
                    logging.warning("Backfill database: %s", exc)
                    self.health.fail("Backfill database", f"Could not save backfill progress ({error_detail(exc)}).", threshold=1)
                    working = False
                time.sleep(1 if working else 5)
        Thread(target=run, daemon=True).start()
