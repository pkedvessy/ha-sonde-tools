import gzip
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, time as wall_time
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import landing_history
from backfill import Backfill, fetch_archive
from field_tools import distance
from health_alerts import HealthAlerts, error_detail
from telemetry_stream import TelemetryStream

API = "https://api.v2.sondehub.org"
HA = "http://supervisor/core/api"
STATE = Path("/data/sent.json")
WEEK = 7 * 86400


def request(url, token=None, payload=None):
    headers = {"User-Agent": "HA-Sonde-Tools/0.12.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    with urlopen(Request(url, body, headers), timeout=20) as response:
        body = response.read()
        logging.debug("HTTP %s %s (%s bytes, encoding=%s)",
                      response.status, url.split("?")[0], len(body),
                      response.headers.get("Content-Encoding", "identity"))
        if response.headers.get("Content-Encoding", "").lower() == "gzip" or body.startswith(bytes((31, 139))):
            body = gzip.decompress(body)
        return json.loads(body)


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


def forecasts(rows, now, save=None, errors=None):
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


def map_url(position):
    lat, lon = position
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=16/{lat:.6f}/{lon:.6f}"


def landings(rows, history, now, max_alt, ground, predicted=None):
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

        try:
            altitude = float(row["alt"])
        except (KeyError, TypeError, ValueError):
            continue
        logging.debug("Signal check / %s: fix age %.0f s, altitude %.0f m, descent %.1f m/s",
                      serial, age, altitude, descent)
        if age < 180 or not math.isfinite(altitude) or altitude > max_alt:
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


def in_quiet_hours(current, start, end):
    if start > end:
        return current >= start or current < end
    return start <= current < end


def dnd_check(cfg, tz):
    if not cfg.get("dnd_enabled", False):
        return lambda: False
    times = []
    for option, default in (("dnd_from", "22:00"), ("dnd_to", "07:00")):
        value = cfg.get(option, default)
        try:
            parsed = wall_time.fromisoformat(value)
            if parsed.strftime("%H:%M") != value:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"{option} must be a 24-hour time in HH:MM format.") from None
        times.append(parsed)
    start, end = times
    return lambda: in_quiet_hours(datetime.now(tz).time(), start, end)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(Path("/data/options.json").read_text(encoding="utf-8"))
    if cfg.get("debug", False):
        logging.getLogger().setLevel(logging.DEBUG)
    token = os.environ["SUPERVISOR_TOKEN"]
    entity = cfg["telegram_entity"].strip()
    if not entity.startswith("notify."):
        raise ValueError("Set telegram_entity to a Telegram notify.* entity.")
    health = HealthAlerts(cfg.get("health_alerts", True))

    def send(message):
        response = request(f"{HA}/services/telegram_bot/send_message?return_response", token, {
            "entity_id": entity, "message": message, "parse_mode": "plain_text",
            "disable_web_page_preview": True,
        })
        result = response.get("service_response") if isinstance(response, dict) else None
        chats = result.get("chats") if isinstance(result, dict) else None
        if not isinstance(chats, list) or not chats or not isinstance(chats[0], dict):
            raise RuntimeError("Home Assistant did not confirm Telegram delivery.")
        return chats

    quiet = lambda: False
    try:
        tz = ZoneInfo(cfg["timezone"])
        quiet = dnd_check(cfg, tz)
        run(cfg, token, entity, send, health, tz, quiet)
    except Exception as error:
        health.fail("Notifier stopped", f"Startup or runtime failure ({error_detail(error)}).", threshold=1)
        if not quiet():
            health.flush(send, immediate=True)
        raise


def run(cfg, token, entity, send, health, tz, quiet):
    sent = json.loads(STATE.read_text()) if STATE.exists() else {}
    history = {}
    predicted = {}
    notify_radius = cfg["radius_km"]
    archive_radius = cfg.get("archive_radius_km") or notify_radius
    stream = None
    bootstrap = True
    dashboard_state = None
    dashboard_updated = 0
    last_center = None
    try:
        landing_history.initialize()
    except (OSError, sqlite3.Error) as error:
        health.fail("Local archive", f"Database initialization failed ({error_detail(error)}).", threshold=1)
        raise
    backfill = Backfill(landing_history.DATABASE, prepare_samples, health)
    backfill.start()
    health.healthy("Local archive")

    def archive_batch(batch, importing=False):
        center = last_center
        if center is None:
            if importing:
                raise ValueError("Wait for Home Assistant to resolve the configured zone before importing.")
            return 0
        samples, invalid = prepare_samples(batch, center, archive_radius)
        if not importing:
            if invalid:
                health.fail("Telemetry parsing", f"{invalid} telemetry records could not be decoded in the latest batch.")
            elif batch:
                health.healthy("Telemetry parsing")
        try:
            added = landing_history.record_telemetry(samples)
            if samples:
                health.healthy("Raw telemetry archive")
            return added
        except sqlite3.Error as error:
            health.fail("Raw telemetry archive", f"Could not save GPS samples ({error_detail(error)}).", threshold=1)
            if importing:
                raise
            logging.exception("Unable to archive telemetry batch")
            return 0

    def import_telemetry(serial):
        return archive_batch(fetch_archive(serial), importing=True)

    def recovery_reports(serials):
        query = urlencode({"serial": ",".join(serials), "last": 0})
        return request(f"{API}/recovered?{query}")

    tiles = {"style": "https://tiles.openfreemap.org/styles/liberty"} if cfg.get("map_default_basemap", True) else None
    tile_url = cfg.get("map_tile_url", "").strip()
    tile_attribution = cfg.get("map_tile_attribution", "").strip()
    if tile_url:
        try:
            parsed = urlsplit(tile_url)
            if parsed.scheme != "https" or not parsed.hostname or not tile_attribution or not all(
                part in tile_url for part in ("{z}", "{x}", "{y}")
            ):
                raise ValueError
            tiles = {"url": tile_url, "attribution": escape(tile_attribution)}
        except ValueError:
            logging.warning("Custom basemap ignored: configure an HTTPS tile URL with {z}, {x}, {y} and attribution.")
    landing_history.serve(lambda: last_center, archive_radius, tz, map_url, recovery_reports,
                          notify_radius, import_telemetry, tiles, health=health, backfill=backfill)

    def save_alerts():
        temp = STATE.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(sent))
            temp.replace(STATE)
            health.healthy("Alert state")
        except OSError as error:
            logging.exception("Unable to save alert state")
            health.fail("Alert state", f"Could not save notification state ({error_detail(error)}).", threshold=1)

    def alert(kind, candidate, center, now):
        serial, position, when, *last_fix = candidate
        key = f"{kind}/{serial}"
        if last_fix and last_fix[0] is None:
            kind = "Signal lost during descent"
        details = f"Last GPS fix: {map_url(last_fix[0])}\n" if last_fix and last_fix[0] else ""
        km = distance(center, position)
        if km <= archive_radius:
            archive_kind = "Last GPS fix" if kind == "Signal lost during descent" else kind
            try:
                landing_history.record(serial, archive_kind, now, when, position, km, last_fix[0] if last_fix else None)
                health.healthy("Landing event archive")
            except sqlite3.Error as error:
                logging.exception("Unable to archive event for %s", serial)
                health.fail("Landing event archive", f"Could not save landing events ({error_detail(error)}).", threshold=1)
        if quiet():
            logging.debug("%s / %s muted by quiet hours", kind, serial)
            return
        inside = km <= notify_radius
        previous = sent.get(key)
        eta = datetime.fromtimestamp(when, tz).strftime("%Y-%m-%d %H:%M %Z")
        title = last_fix[1] if len(last_fix) > 1 else kind
        message = (f"{title} · {serial}\nDistance {km:.1f} km\nTime {eta}\n"
                   f"{map_url(position)}\n{details}https://sondehub.org/{quote(serial, safe='')}")
        if isinstance(previous, dict):
            moved = distance(previous["position"], position)
            crossed = previous["inside"] != inside
            if previous["entity"] == entity and (crossed or moved >= 1):
                status = ("Entered" if inside else "Left") + " the Telegram radius" if crossed else (
                    "Inside the Telegram radius" if inside else "Outside the Telegram radius"
                )
                request(f"{HA}/services/telegram_bot/edit_message", token, {
                    "entity_id": entity, "message_id": previous["message_id"],
                    "message": f"{message}\nForecast updated · {status} · moved {moved:.1f} km",
                    "parse_mode": "plain_text", "disable_web_page_preview": True,
                })
                health.healthy("Telegram delivery")
                previous.update(position=position, inside=inside)
                save_alerts()
                logging.info("Forecast message updated: %s / %s", serial, status)
            return
        if not inside:
            logging.debug("%s / %s skipped Telegram: %.1f km is outside the notification radius", kind, serial, km)
            return
        if previous is not None:
            logging.debug("%s / %s skipped: already alerted", kind, serial)
            return
        chats = send(message)
        health.healthy("Telegram delivery")
        message_id = chats[0].get("message_id")
        sent[key] = now
        if kind == "Predicted landing" and isinstance(message_id, int):
            sent[key] = {"at": now, "entity": entity, "message_id": message_id,
                         "position": position, "inside": inside}
        save_alerts()
        logging.info("Alert sent: %s / %s", kind, serial)

    test_pending = cfg["startup_test"]
    previous_quiet = False
    while True:
        muted = quiet()
        if muted != previous_quiet:
            logging.info("Telegram quiet hours %s", "started" if muted else "ended")
            previous_quiet = muted
        try:
            zone = request(f"{HA}/states/{quote(cfg['zone'], safe='.')}", token)["attributes"]
            center = point(zone["latitude"], zone["longitude"])
            if center != last_center:
                logging.debug("Polling every %s seconds within %.1f km of %s (%.6f, %.6f)",
                              cfg["poll_seconds"], cfg["radius_km"], cfg["zone"], *center)
                last_center = center
            if stream is None:
                stream = TelemetryStream(API, archive_batch)
            health.healthy("Home Assistant zone")
        except (OSError, EOFError, KeyError, TypeError, ValueError, RuntimeError) as error:
            logging.warning("Home Assistant zone: %s", error)
            health.fail("Home Assistant zone", f"Cannot resolve the configured zone ({error_detail(error)}).")
            if not quiet():
                health.flush(send)
            time.sleep(cfg["poll_seconds"])
            continue
        if test_pending and not quiet():
            try:
                send("Sonde Tools · test message")
                test_pending = False
                health.healthy("Telegram delivery")
            except (OSError, EOFError, TypeError, ValueError, RuntimeError) as error:
                logging.warning("Telegram test message: %s", error)
                health.fail("Telegram delivery", f"Test message failed ({error_detail(error)}).", threshold=1)

        try:
            forecast_rows = request(f"{API}/predictions?vehicles=")
            if not isinstance(forecast_rows, list):
                raise ValueError("Expected a list of forecasts")
            health.healthy("SondeHub forecasts")
        except (OSError, EOFError, ValueError) as exc:
            logging.warning("SondeHub (Predicted landing): %s", exc)
            forecast_rows = None
            health.fail("SondeHub forecasts", f"Could not load landing forecasts ({error_detail(exc)}).")

        now = time.time()
        predicted = {serial: forecast for serial, forecast in predicted.items() if now - forecast[0] <= 1800}
        sent = {key: value for key, value in sent.items()
                if now - (value["at"] if isinstance(value, dict) else value) < WEEK}
        query = urlencode({"lat": center[0], "lon": center[1],
                           "distance": round(max(notify_radius, archive_radius) * 1000) + 12000, "last": 600})
        ready, streamed = stream.drain()
        if ready:
            health.healthy("Live telemetry stream")
        else:
            health.fail("Live telemetry stream", "No valid WebSocket batches are arriving. REST snapshots cannot capture every GPS fix.")
        if bootstrap or not ready:
            try:
                nearby = request(f"{API}/sondes?{query}")
                if not isinstance(nearby, dict) or any(not isinstance(row, dict) for row in nearby.values()):
                    raise ValueError("Expected a telemetry object keyed by serial")
                archive_batch(nearby.values())
                for serial, row in nearby.items():
                    if serial not in streamed or row.get("datetime", "") > streamed[serial]["datetime"]:
                        streamed[serial] = row
                bootstrap = False
                health.healthy("REST telemetry")
            except (OSError, EOFError, AttributeError, ValueError) as exc:
                logging.warning("SondeHub telemetry fallback: %s", exc)
                health.fail("REST telemetry", f"Could not load fallback GPS data ({error_detail(exc)}).")
                if not ready:
                    streamed = None

        if ready and not bootstrap:
            health.healthy("REST telemetry")
        snapshots = []
        forecast_errors = []

        def save_forecast(serial, issued, eta, position, row, path):
            nearby = distance(center, position) <= archive_radius
            if not nearby:
                try:
                    origin = point(row["latitude"], row["longitude"])
                    nearby = distance(center, origin) <= archive_radius
                except (KeyError, TypeError, ValueError):
                    pass
            serial = serial.upper()
            if serial not in predicted or issued > predicted[serial][0]:
                predicted[serial] = issued, eta, position
            snapshots.append((serial, issued, eta, *position, path, nearby, serial, issued - 1200))

        sources = [
            ("Predicted landing", forecast_rows, lambda rows: forecasts(rows, now, save_forecast, forecast_errors)),
            ("Estimated landing after signal loss", streamed,
             lambda rows: landings(rows, history, now, cfg["landing_max_altitude_m"],
                                   cfg.get("ground_elevation_m"), predicted)),
        ]
        for kind, rows, candidates in sources:
            try:
                if rows is None:
                    continue
                matches = list(candidates(rows))
                health.healthy(f"{kind} processing")
                if kind == "Predicted landing":
                    if forecast_errors:
                        health.fail("Forecast parsing", f"{len(forecast_errors)} forecasts could not be decoded in the latest poll.")
                    else:
                        health.healthy("Forecast parsing")
                    try:
                        landing_history.record_forecasts(snapshots)
                        if snapshots:
                            health.healthy("Forecast archive")
                    except sqlite3.Error as error:
                        health.fail("Forecast archive", f"Could not save forecast paths ({error_detail(error)}).", threshold=1)
                        logging.exception("Unable to archive forecast snapshots")
                logging.debug("%s: %s records, %s candidates", kind, len(rows), len(matches))
                for candidate in matches:
                    try:
                        alert(kind, candidate, center, now)
                    except (OSError, ValueError, RuntimeError) as exc:
                        logging.warning("Alert delivery failed (%s): %s", candidate[0], exc)
                        health.fail("Telegram delivery", f"A sonde alert or edit failed ({error_detail(exc)}).", threshold=1)
            except (OSError, EOFError, AttributeError, TypeError, ValueError) as exc:
                logging.warning("SondeHub (%s): %s", kind, exc)
                health.fail(f"{kind} processing", f"Could not process SondeHub data ({error_detail(exc)}).")
        if cfg.get("dashboard_sensor"):
            active = []
            for serial, (observed, row) in history.items():
                if not -30 <= now - observed <= 180:
                    continue
                try:
                    km = distance(center, point(row["lat"], row["lon"]))
                    if km <= archive_radius:
                        active.append((km, serial, observed, row))
                except (KeyError, TypeError, ValueError):
                    continue
            state = {"state": "No active sonde", "attributes": {
                "friendly_name": "Nearest active radiosonde", "icon": "mdi:weather-windy",
            }}
            if active:
                km, serial, observed, row = min(active, key=lambda item: item[0])
                forecast = predicted.get(serial.upper())
                state["state"] = serial
                state["attributes"].update(
                    distance_km=round(km, 1), altitude_m=row.get("alt"),
                    gps_fix=datetime.fromtimestamp(observed, tz).isoformat(),
                    forecast_eta=datetime.fromtimestamp(forecast[1], tz).isoformat() if forecast and forecast[1] > now else None,
                    sondehub_url=f"https://sondehub.org/{quote(serial, safe='')}",
                )
            if state != dashboard_state or now - dashboard_updated >= 300:
                try:
                    request(f"{HA}/states/sensor.sonde_tools_nearest_active", token, state)
                    dashboard_state, dashboard_updated = state, now
                    health.healthy("Home Assistant dashboard sensor")
                except (OSError, ValueError) as exc:
                    logging.warning("Home Assistant dashboard sensor: %s", exc)
                    health.fail("Home Assistant dashboard sensor", f"Could not publish sensor state ({error_detail(exc)}).")
        health.healthy("Notifier stopped")
        if not quiet():
            health.flush(send)
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
