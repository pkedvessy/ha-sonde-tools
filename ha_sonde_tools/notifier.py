import gzip
import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import landing_history
import live_entities
import settings as app_settings
from alert_state import AlertState
from app_version import USER_AGENT
from backfill import Backfill, fetch_archive
from dashboard import Dashboard
from detection import forecasts, landings, map_url, point, prepare_samples
from field_tools import distance
from health_alerts import HealthAlerts, error_detail
from maintenance import Maintenance
from telemetry_stream import TelemetryStream

API = "https://api.v2.sondehub.org"
HA = "http://supervisor/core/api"
STATE = Path("/data/sent.json")
POLL_INTERVAL_SECONDS = 60
MAP_RENDER_TIMEOUT_SECONDS = 30
# The watchdog restarts the app when the poll loop has not completed for this long.
STALL_SECONDS = 10 * POLL_INTERVAL_SECONDS
DELIVERY_ERRORS = (OSError, EOFError, TypeError, ValueError, RuntimeError)


def request(url, token=None, payload=None, timeout=20, method=None):
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    with urlopen(Request(url, body, headers, method=method), timeout=timeout) as response:
        body = response.read()
        logging.debug("HTTP %s %s (%s bytes, encoding=%s)",
                      response.status, url.split("?")[0], len(body),
                      response.headers.get("Content-Encoding", "identity"))
        if response.headers.get("Content-Encoding", "").lower() == "gzip" or body.startswith(bytes((31, 139))):
            body = gzip.decompress(body)
        return json.loads(body)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ["SUPERVISOR_TOKEN"]
    try:
        cfg = json.loads(app_settings.OPTIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    if cfg.get("debug", False):
        logging.getLogger().setLevel(logging.DEBUG)
    health = HealthAlerts(cfg.get("health_alerts", True))
    entity = str(cfg.get("telegram_entity", "")).strip()
    notifier = None
    try:
        notifier = Notifier(app_settings.parse(cfg), token, health)
        notifier.run()
    except Exception as error:
        health.fail("Notifier stopped", f"Startup or runtime failure ({error_detail(error)}).", threshold=1)
        quiet = notifier.settings.is_quiet() if notifier else False
        if entity.startswith("notify.") and not quiet:
            health.flush(lambda message: send_telegram(token, entity, message), immediate=True)
        raise


def send_telegram(token, entity, message, photo_url=None):
    if photo_url:
        response = request(f"{HA}/services/telegram_bot/send_photo?return_response", token, {
            "entity_id": entity, "url": photo_url, "caption": f"🎈 {message}", "parse_mode": "plain_text",
        }, timeout=60)
    else:
        response = request(f"{HA}/services/telegram_bot/send_message?return_response", token, {
            "entity_id": entity, "message": f"🎈 {message}", "parse_mode": "plain_text",
            "disable_web_page_preview": True,
        })
    result = response.get("service_response") if isinstance(response, dict) else None
    chats = result.get("chats") if isinstance(result, dict) else None
    if not isinstance(chats, list) or not chats or not isinstance(chats[0], dict):
        raise RuntimeError("Home Assistant did not confirm Telegram delivery.")
    return chats


class Notifier:
    def __init__(self, settings, token, health):
        self.settings = settings
        self.token = token
        self.health = health
        self.entity = settings.telegram_entity
        self.notify_radius = settings.radius_km
        self.archive_radius = settings.archive_radius_km
        self.center = None
        self.stream = None
        self.bootstrap = True
        self.history = {}
        self.predicted = {}
        self.touchdowns = {}
        self.centers = []
        self.zone_name = "home"
        self.live = None
        self.dashboard = None
        self.dashboard_state = None
        self.dashboard_updated = 0
        self.test_pending = settings.startup_test
        self.previous_quiet = False
        self.started = time.monotonic()
        self.heartbeat = None
        self.renderer = self.images = None
        self.render_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="map-render")

        self.state, problem = AlertState.load(STATE)
        if problem:
            self.health.fail("Alert state", f"{problem} Some alerts may be sent twice.", threshold=1)

    # ----- Delivery -----

    def send(self, message, photo_url=None):
        return send_telegram(self.token, self.entity, message, photo_url)

    def quiet(self):
        return self.settings.is_quiet()

    def save_alerts(self):
        try:
            self.state.save()
            self.health.healthy("Alert state")
        except OSError as error:
            logging.exception("Unable to save alert state")
            self.health.fail("Alert state", f"Could not save notification state ({error_detail(error)}).", threshold=1)

    # ----- Startup -----

    def start_services(self):
        try:
            landing_history.initialize()
        except (OSError, sqlite3.Error) as error:
            self.health.fail("Local archive", f"Database initialization failed ({error_detail(error)}).", threshold=1)
            raise
        self.backfill = Backfill(landing_history.DATABASE, prepare_samples, self.health)
        self.backfill.start()
        self.maintenance = Maintenance(landing_history.DATABASE, self.settings.telemetry_retention_days, self.health)
        self.maintenance.start()
        self.health.healthy("Local archive")
        if self.settings.telegram_map_image:
            try:
                import map_image
                self.map_image = map_image
                self.renderer = map_image.MapRenderer(self.settings.custom_tile_url,
                                                      self.settings.custom_tile_attribution)
                self.images = map_image.ImageServer()
            except (ImportError, OSError) as error:
                logging.warning("Telegram map images disabled: %s", error)
                self.renderer = self.images = None
        if self.settings.dashboard_sensor:
            self.dashboard = Dashboard(request, self.token, self.health)
        if self.settings.live_entities:
            self.live = live_entities.LiveEntities(request, self.token, self.health)
        landing_history.serve(
            lambda: self.center, self.archive_radius, self.settings.timezone, map_url, self.recovery_reports,
            self.notify_radius, self.import_telemetry, self.settings.tiles, health=self.health,
            backfill=self.backfill, diagnostics=self.diagnostics, maintenance=self.maintenance,
            heartbeat=self.alive)

    def alive(self):
        """True while the poll loop keeps completing; drives the Supervisor watchdog."""
        last = self.heartbeat if self.heartbeat is not None else self.started
        return time.monotonic() - last < STALL_SECONDS

    # ----- Archive callbacks -----

    def archive_batch(self, batch, importing=False):
        center = self.center
        if center is None:
            if importing:
                raise ValueError("Wait for Home Assistant to resolve the configured zone before importing.")
            return 0
        samples, invalid = prepare_samples(batch, center, self.archive_radius)
        if not importing:
            if invalid:
                self.health.fail("Telemetry parsing", f"{invalid} telemetry records could not be decoded in the latest batch.")
            elif batch:
                self.health.healthy("Telemetry parsing")
        try:
            added = landing_history.record_telemetry(samples)
            if samples:
                self.health.healthy("Raw telemetry archive")
            return added
        except sqlite3.Error as error:
            self.health.fail("Raw telemetry archive", f"Could not save GPS samples ({error_detail(error)}).", threshold=1)
            if importing:
                raise
            logging.exception("Unable to archive telemetry batch")
            return 0

    def import_telemetry(self, serial):
        return self.archive_batch(fetch_archive(serial), importing=True)

    @staticmethod
    def recovery_reports(serials):
        query = urlencode({"serial": ",".join(serials), "last": 0})
        return request(f"{API}/recovered?{query}")

    # ----- Map images -----

    def render(self, *args, **kwargs):
        """Render a map with an overall deadline, so slow tile servers cannot stall polling."""
        future = self.render_pool.submit(self.renderer.render, *args, **kwargs)
        try:
            return future.result(timeout=MAP_RENDER_TIMEOUT_SECONDS)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError(f"Map rendering exceeded {MAP_RENDER_TIMEOUT_SECONDS} s") from None

    def map_photo(self, serial, kind, position, last_fix, center):
        if self.renderer is None:
            return None
        try:
            track = self.map_image.read_track(landing_history.DATABASE, serial, time.time() - 1800)
        except sqlite3.Error as error:
            logging.debug("Map track for %s unavailable: %s", serial, error)
            track = []
        try:
            png = self.render(position, last_fix, track, center, kind)
        except (OSError, ValueError) as error:
            logging.warning("Map image for %s could not be rendered: %s", serial, error)
            return None
        return self.images.publish(png)

    def send_alert(self, message, serial, kind, position, last_fix, center):
        """Send with a map image when possible; fall back to a text message."""
        photo_url = self.map_photo(serial, kind, position, last_fix, center)
        if photo_url:
            try:
                return self.send(message, photo_url), True
            except TimeoutError:
                raise  # The photo may still arrive; a text fallback could duplicate the alert.
            except (OSError, ValueError, RuntimeError) as error:
                logging.warning("Telegram map photo for %s failed, sending text: %s", serial, error)
            finally:
                self.images.discard(photo_url)
        return self.send(message), False

    # ----- Diagnostics -----

    def diagnostics(self, action):
        if action == "telegram":
            if self.quiet():
                return [("Telegram", "Waiting", "Do Not Disturb is active. Try again after quiet hours.")]
            try:
                self.send("Sonde Tools · diagnostics test message")
            except DELIVERY_ERRORS as error:
                logging.warning("Diagnostics Telegram test: %s", error)
                return [("Telegram", "Failed", error_detail(error))]
            return [("Telegram", "Passed", "Home Assistant confirmed delivery to the configured chat.")]
        if action == "telegram_map":
            return self.diagnose_map()
        return self.diagnose_integrations()

    def diagnose_map(self):
        if self.renderer is None:
            reason = ("telegram_map_image is disabled." if not self.settings.telegram_map_image
                      else "The map renderer could not start. Check the app log.")
            return [("Telegram map", "Failed", reason)]
        if self.center is None:
            return [("Telegram map", "Waiting", "The configured zone has not been resolved yet.")]
        if self.quiet():
            return [("Telegram map", "Waiting", "Do Not Disturb is active. Try again after quiet hours.")]
        try:
            png = self.render(self.center, label="Test position · zone center")
        except (OSError, ValueError) as error:
            logging.warning("Diagnostics map render: %s", error)
            return [("Map rendering", "Failed", error_detail(error))]
        results = [("Map rendering", "Passed", f"Rendered a {len(png) / 1024:.0f} KB PNG from map tiles.")]
        photo_url = self.images.publish(png)
        try:
            self.send("Sonde Tools · diagnostics map test\n"
                      f"Zone center {map_url(self.center)}", photo_url)
            results.append(("Telegram map", "Passed", "Home Assistant downloaded the image and confirmed delivery."))
        except DELIVERY_ERRORS as error:
            logging.warning("Diagnostics Telegram map test: %s", error)
            results.append(("Telegram map", "Failed", error_detail(error)))
        finally:
            self.images.discard(photo_url)
        return results

    def diagnose_integrations(self):
        results = []
        ready, age = self.stream.status() if self.stream is not None else (False, None)
        if ready and age is not None and age <= 120:
            results.append(("SondeHub WebSocket", "Passed", f"Last valid GPS batch arrived {age:.0f} seconds ago."))
        else:
            results.append(("SondeHub WebSocket", "Waiting", "No recent valid GPS batch. Check the app log for reconnects."))
        try:
            self.resolve_zone(timeout=6)
            results.append(("Home Assistant zone", "Passed", f"{self.settings.zone} has valid coordinates."))
        except (OSError, EOFError, KeyError, TypeError, ValueError) as error:
            results.append(("Home Assistant zone", "Failed", error_detail(error)))
        try:
            rows = request(f"{API}/predictions?vehicles=", timeout=8)
            if not isinstance(rows, list):
                raise ValueError("Expected a forecast list")
            errors = []
            usable = list(forecasts(rows, time.time(), errors=errors))
            status = "Check" if errors else "Passed"
            results.append(("SondeHub forecasts", status,
                            f"{len(rows)} records, {len(usable)} current forecasts, {len(errors)} parse errors."))
        except (OSError, EOFError, TypeError, ValueError) as error:
            results.append(("SondeHub forecasts", "Failed", error_detail(error)))
        try:
            if not Path(landing_history.DATABASE).is_file():
                raise sqlite3.OperationalError("Archive file is missing")
            with closing(sqlite3.connect(landing_history.DATABASE, timeout=2)) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("SELECT id FROM telemetry_fixes LIMIT 1").fetchone()
                db.rollback()
            results.append(("Local archive", "Passed", "SQLite is readable and a write transaction can start."))
        except sqlite3.Error as error:
            results.append(("Local archive", "Failed", error_detail(error)))
        if self.dashboard is not None:
            mode = self.dashboard.mode
            connected = mode == "rest" or self.dashboard.connected
            results.append(("Dashboard sensor", "Passed" if connected else "Waiting",
                            "Published with MQTT discovery." if mode == "mqtt" and connected else
                            "Waiting for the MQTT broker." if mode == "mqtt" else
                            "Published with the REST API (MQTT integration not found)."))
        if self.live is not None:
            results.append(("Live sonde entities",
                            "Passed" if self.live.connected else "Failed" if self.live.client is None else "Waiting",
                            "Published with MQTT discovery." if self.live.connected else
                            "The MQTT integration was not found." if self.live.client is None else
                            "Waiting for the MQTT broker."))
        if self.settings.follow_persons:
            people = self.resolve_people()
            located = ", ".join(name for name, _ in people) or "nobody"
            results.append(("Followed people", "Passed" if len(people) == len(self.settings.follow_persons) else "Check",
                            f"{len(people)} of {len(self.settings.follow_persons)} have a location ({located})."))
        return results

    # ----- Alerts -----

    def alert(self, kind, candidate, center, now):
        serial, position, when, *last_fix = candidate
        key = f"{kind}/{serial}"
        if last_fix and last_fix[0] is None:
            kind = "Signal lost during descent"
        details = f"Last GPS fix: {map_url(last_fix[0])}\n" if last_fix and last_fix[0] else ""
        title = last_fix[1] if len(last_fix) > 1 else kind
        if kind != "Predicted landing":
            self.touchdowns[serial.upper()] = (when, position, title)
        zone_km = distance(center, position)
        if zone_km <= self.archive_radius:
            archive_kind = "Last GPS fix" if kind == "Signal lost during descent" else kind
            try:
                landing_history.record(serial, archive_kind, now, when, position, zone_km,
                                       last_fix[0] if last_fix else None)
                self.health.healthy("Landing event archive")
            except sqlite3.Error as error:
                logging.exception("Unable to archive event for %s", serial)
                self.health.fail("Landing event archive", f"Could not save landing events ({error_detail(error)}).", threshold=1)
        km, near, near_point = self.closest(center, position)
        where = f" from {near}" if len(self.centers) > 1 else ""
        inside = km <= self.notify_radius
        previous = self.state.sent.get(key)
        tz = self.settings.timezone
        eta = datetime.fromtimestamp(when, tz).strftime("%Y-%m-%d %H:%M %Z")
        if self.quiet():
            if inside and previous is None and key not in self.state.muted:
                self.state.muted[key] = {"at": now, "line": f"{title} · {serial} · {km:.1f} km{where} · {eta}\n{map_url(position)}"}
                self.save_alerts()
            logging.debug("%s / %s muted by quiet hours", kind, serial)
            return
        message = (f"{title} · {serial}\nDistance {km:.1f} km{where}\nTime {eta}\n"
                   f"{map_url(position)}\n{details}https://sondehub.org/{quote(serial, safe='')}")
        if isinstance(previous, dict):
            self.update_forecast_message(key, previous, message, position, inside, serial)
            return
        if not inside:
            logging.debug("%s / %s skipped Telegram: %.1f km is outside the notification radius", kind, serial, km)
            return
        if previous is not None:
            logging.debug("%s / %s skipped: already alerted", kind, serial)
            return
        chats, photo = self.send_alert(message, serial, title, position, last_fix[0] if last_fix else None, near_point)
        self.health.healthy("Telegram delivery")
        message_id = chats[0].get("message_id")
        self.state.muted.pop(key, None)
        self.state.sent[key] = now
        if kind == "Predicted landing" and isinstance(message_id, int):
            self.state.sent[key] = {"at": now, "entity": self.entity, "message_id": message_id,
                                    "position": list(position), "inside": inside, "photo": photo, "edit_failures": 0}
        self.save_alerts()
        logging.info("Alert sent: %s / %s", kind, serial)

    def closest(self, center, position):
        """Return (km, name, point) of the zone or followed person closest to a landing position."""
        centers = self.centers or [(self.zone_name, center)]
        return min((distance(point, position), name, point) for name, point in centers)

    def update_forecast_message(self, key, previous, message, position, inside, serial):
        if not self.state.editable(key, self.entity):
            return
        moved = distance(previous["position"], position)
        crossed = previous["inside"] != inside
        if not (crossed or moved >= 1):
            return
        status = ("Entered" if inside else "Left") + " the Telegram radius" if crossed else (
            "Inside the Telegram radius" if inside else "Outside the Telegram radius"
        )
        text = f"🎈 {message}\nForecast updated · {status} · moved {moved:.1f} km"
        try:
            if previous["photo"]:
                # Telegram cannot turn a photo caption into a text message, and
                # Home Assistant has no media edit service, so only the caption changes.
                request(f"{HA}/services/telegram_bot/edit_caption", self.token, {
                    "entity_id": self.entity, "message_id": previous["message_id"],
                    "caption": f"{text}\nMap shows the first forecast position.",
                })
            else:
                request(f"{HA}/services/telegram_bot/edit_message", self.token, {
                    "entity_id": self.entity, "message_id": previous["message_id"], "message": text,
                    "parse_mode": "plain_text", "disable_web_page_preview": True,
                })
        except DELIVERY_ERRORS:
            abandoned = self.state.edit_failed(key)
            self.save_alerts()
            if abandoned:
                logging.warning("Forecast message for %s could not be edited repeatedly; it may have been "
                                "deleted. Further updates for this flight are skipped.", serial)
            raise
        previous.update(position=list(position), inside=inside, edit_failures=0)
        self.health.healthy("Telegram delivery")
        self.save_alerts()
        logging.info("Forecast message updated: %s / %s", serial, status)

    def send_quiet_summary(self):
        """After quiet hours, report the alerts that were held back, in one message."""
        if not self.state.muted:
            return
        if not self.settings.dnd_summary:
            self.state.muted.clear()
            self.save_alerts()
            return
        held = sorted(self.state.muted.items(), key=lambda item: item[1]["at"])
        lines = [entry["line"] for _, entry in held[:10]]
        if len(held) > 10:
            lines.append(f"…and {len(held) - 10} more. See the history page.")
        try:
            self.send("Sonde Tools · during quiet hours\n" + "\n".join(lines))
        except DELIVERY_ERRORS as error:
            logging.warning("Quiet hours summary pending (%s)", error_detail(error))
            self.health.fail("Telegram delivery", f"Quiet hours summary failed ({error_detail(error)}).", threshold=1)
            return
        self.health.healthy("Telegram delivery")
        for key, entry in held:
            # Treat held alerts as delivered so the summary is not followed by duplicates.
            self.state.sent.setdefault(key, entry["at"])
        self.state.muted.clear()
        self.save_alerts()
        logging.info("Quiet hours summary sent (%s alerts)", len(held))

    # ----- Polling -----

    def resolve_zone(self, timeout=20):
        zone = request(f"{HA}/states/{quote(self.settings.zone, safe='.')}", self.token, timeout=timeout)["attributes"]
        name = zone.get("friendly_name")
        if isinstance(name, str) and name.strip():
            self.zone_name = name.strip()
        return point(zone["latitude"], zone["longitude"])

    def resolve_people(self):
        """Return [(name, position)] for followed people with a known GPS location."""
        people, failed = [], False
        for entity in self.settings.follow_persons:
            try:
                state = request(f"{HA}/states/{quote(entity, safe='.')}", self.token, timeout=10)
                attributes = state["attributes"]
                if attributes.get("latitude") is None or attributes.get("longitude") is None:
                    logging.debug("Followed %s has no location (state %s)", entity, state.get("state"))
                    continue
                name = attributes.get("friendly_name") or entity.split(".", 1)[1]
                people.append((str(name), point(attributes["latitude"], attributes["longitude"])))
            except (OSError, EOFError, KeyError, TypeError, ValueError, AttributeError) as error:
                logging.warning("Followed person %s: %s", entity, error)
                self.health.fail("Followed people", f"Cannot read {entity} ({error_detail(error)}).")
                failed = True
        if self.settings.follow_persons and not failed:
            self.health.healthy("Followed people")
        return people

    def load_forecasts(self):
        try:
            rows = request(f"{API}/predictions?vehicles=")
            if not isinstance(rows, list):
                raise ValueError("Expected a list of forecasts")
            self.health.healthy("SondeHub forecasts")
            return rows
        except (OSError, EOFError, ValueError) as exc:
            logging.warning("SondeHub (Predicted landing): %s", exc)
            self.health.fail("SondeHub forecasts", f"Could not load landing forecasts ({error_detail(exc)}).")
            return None

    def load_telemetry(self, center):
        ready, streamed = self.stream.drain()
        if ready:
            self.health.healthy("Live telemetry stream")
        else:
            self.health.fail("Live telemetry stream", "No valid WebSocket batches are arriving. REST snapshots cannot capture every GPS fix.")
        if self.bootstrap or not ready:
            query = urlencode({"lat": center[0], "lon": center[1],
                               "distance": round(max(self.notify_radius, self.archive_radius) * 1000) + 12000,
                               "last": 600})
            try:
                nearby = request(f"{API}/sondes?{query}")
                if not isinstance(nearby, dict) or any(not isinstance(row, dict) for row in nearby.values()):
                    raise ValueError("Expected a telemetry object keyed by serial")
                self.archive_batch(nearby.values())
                for serial, row in nearby.items():
                    if serial not in streamed or row.get("datetime", "") > streamed[serial]["datetime"]:
                        streamed[serial] = row
                self.bootstrap = False
                self.health.healthy("REST telemetry")
            except (OSError, EOFError, AttributeError, ValueError) as exc:
                logging.warning("SondeHub telemetry fallback: %s", exc)
                self.health.fail("REST telemetry", f"Could not load fallback GPS data ({error_detail(exc)}).")
                if not ready:
                    return None
        if ready and not self.bootstrap:
            self.health.healthy("REST telemetry")
        return streamed

    def process(self, center, now, forecast_rows, streamed):
        snapshots = []
        forecast_errors = []

        def save_forecast(serial, issued, eta, position, row, path):
            nearby = distance(center, position) <= self.archive_radius
            if not nearby:
                try:
                    origin = point(row["latitude"], row["longitude"])
                    nearby = distance(center, origin) <= self.archive_radius
                except (KeyError, TypeError, ValueError):
                    pass
            serial = serial.upper()
            if serial not in self.predicted or issued > self.predicted[serial][0]:
                self.predicted[serial] = issued, eta, position
            snapshots.append((serial, issued, eta, *position, path, nearby, serial, issued - 1200))

        sources = [
            ("Predicted landing", forecast_rows, lambda rows: forecasts(rows, now, save_forecast, forecast_errors)),
            ("Estimated landing after signal loss", streamed,
             lambda rows: landings(rows, self.history, now, self.settings.ground_elevation_m, self.predicted)),
        ]
        for kind, rows, candidates in sources:
            if rows is None:
                continue
            try:
                matches = list(candidates(rows))
                self.health.healthy(f"{kind} processing")
                if kind == "Predicted landing":
                    self.archive_forecasts(snapshots, forecast_errors)
                logging.debug("%s: %s records, %s candidates", kind, len(rows), len(matches))
                for candidate in matches:
                    try:
                        self.alert(kind, candidate, center, now)
                    except (OSError, EOFError, KeyError, TypeError, ValueError, RuntimeError) as exc:
                        logging.warning("Alert delivery failed (%s): %s", candidate[0], exc)
                        self.health.fail("Telegram delivery", f"A sonde alert or edit failed ({error_detail(exc)}).", threshold=1)
            except (OSError, EOFError, AttributeError, TypeError, ValueError) as exc:
                logging.warning("SondeHub (%s): %s", kind, exc)
                self.health.fail(f"{kind} processing", f"Could not process SondeHub data ({error_detail(exc)}).")

    def archive_forecasts(self, snapshots, forecast_errors):
        if forecast_errors:
            self.health.fail("Forecast parsing", f"{len(forecast_errors)} forecasts could not be decoded in the latest poll.")
        else:
            self.health.healthy("Forecast parsing")
        try:
            landing_history.record_forecasts(snapshots)
            if snapshots:
                self.health.healthy("Forecast archive")
        except sqlite3.Error as error:
            self.health.fail("Forecast archive", f"Could not save forecast paths ({error_detail(error)}).", threshold=1)
            logging.exception("Unable to archive forecast snapshots")

    def update_dashboard(self, center, now):
        if self.dashboard is None:
            return
        tz = self.settings.timezone
        active = []
        for serial, (observed, row) in self.history.items():
            if not -30 <= now - observed <= 180:
                continue
            try:
                km = distance(center, point(row["lat"], row["lon"]))
                if km <= self.archive_radius:
                    active.append((km, serial, observed, row))
            except (KeyError, TypeError, ValueError):
                continue
        state = {"state": "No active sonde", "attributes": {
            "friendly_name": "Nearest active radiosonde", "icon": "mdi:weather-windy",
        }}
        if active:
            km, serial, observed, row = min(active, key=lambda item: item[0])
            forecast = self.predicted.get(serial.upper())
            state["state"] = serial
            state["attributes"].update(
                distance_km=round(km, 1), altitude_m=row.get("alt"),
                gps_fix=datetime.fromtimestamp(observed, tz).isoformat(),
                forecast_eta=datetime.fromtimestamp(forecast[1], tz).isoformat() if forecast and forecast[1] > now else None,
                sondehub_url=f"https://sondehub.org/{quote(serial, safe='')}",
            )
        if state != self.dashboard_state or now - self.dashboard_updated >= 300:
            try:
                self.dashboard.publish(state)
                self.dashboard_state, self.dashboard_updated = state, now
            except (OSError, ValueError, EOFError) as exc:
                logging.warning("Home Assistant dashboard sensor: %s", exc)
                self.health.fail("Home Assistant dashboard sensor", f"Could not publish sensor state ({error_detail(exc)}).")

    def update_live(self, now):
        self.touchdowns = {serial: entry for serial, entry in self.touchdowns.items() if now - entry[0] <= 3 * 3600}
        if self.live is None:
            return
        try:
            state = live_entities.snapshot(self.history, self.predicted, self.touchdowns, self.centers,
                                           self.archive_radius, self.notify_radius, now)
            self.live.update(state, now)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            logging.warning("Live sonde entities: %s", exc)
            self.health.fail("Live sonde entities", f"Could not publish entity states ({error_detail(exc)}).")

    def poll(self):
        """Run one polling cycle. Returns False when the zone could not be resolved."""
        muted = self.quiet()
        if muted != self.previous_quiet:
            logging.info("Telegram quiet hours %s", "started" if muted else "ended")
            self.previous_quiet = muted
        try:
            center = self.resolve_zone()
            if center != self.center:
                logging.debug("Polling every %s seconds within %.1f km of %s (%.6f, %.6f)",
                              POLL_INTERVAL_SECONDS, self.notify_radius, self.settings.zone, *center)
                self.center = center
            if self.stream is None:
                self.stream = TelemetryStream(API, self.archive_batch)
            self.health.healthy("Home Assistant zone")
        except (OSError, EOFError, KeyError, TypeError, ValueError, RuntimeError) as error:
            logging.warning("Home Assistant zone: %s", error)
            self.health.fail("Home Assistant zone", f"Cannot resolve the configured zone ({error_detail(error)}).")
            return False
        self.centers = [(self.zone_name, center)] + self.resolve_people()
        if self.test_pending and not muted:
            try:
                self.send("Sonde Tools · test message")
                self.test_pending = False
                self.health.healthy("Telegram delivery")
            except DELIVERY_ERRORS as error:
                logging.warning("Telegram test message: %s", error)
                self.health.fail("Telegram delivery", f"Test message failed ({error_detail(error)}).", threshold=1)

        forecast_rows = self.load_forecasts()
        now = time.time()
        self.predicted = {serial: forecast for serial, forecast in self.predicted.items() if now - forecast[0] <= 1800}
        self.state.prune(now)
        streamed = self.load_telemetry(center)
        self.process(center, now, forecast_rows, streamed)
        if not muted:
            # After processing, so alerts that are still current go out in full and leave the summary.
            self.send_quiet_summary()
        self.update_dashboard(center, now)
        self.update_live(now)
        self.health.healthy("Notifier stopped")
        return True

    def run(self):
        self.start_services()
        while True:
            self.poll()
            self.heartbeat = time.monotonic()
            if not self.quiet():
                self.health.flush(self.send)
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
