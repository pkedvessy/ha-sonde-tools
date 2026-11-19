"""Live Home Assistant entities over MQTT discovery.

* ``device_tracker`` entities for each nearby sonde's live position and its landing point,
  so they appear on the standard Home Assistant map card.
* ``binary_sensor`` "Sonde incoming": on while a landing is expected inside the Telegram radius.
* ``sensor`` "Active sondes" and "Next landing" (timestamp) for automations and dashboards.

Trackers are removed again an hour after their sonde was last updated.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import quote

import mqtt_link
from field_tools import distance
from health_alerts import error_detail

BASE = "sonde_tools/live"
AVAILABILITY = f"{BASE}/availability"
DISCOVERY = "homeassistant/{component}/ha_sonde_tools/{key}/config"
TRACKER_DISCOVERY = "homeassistant/device_tracker/ha_sonde_tools/+/config"
LINGER_SECONDS = 3600
LIVE_SECONDS = 180
MAX_SONDES = 20


def slug(serial):
    return re.sub(r"[^a-z0-9_]", "_", serial.lower())


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else None


def nearest(centers, position):
    """Return (km, center name) for the closest center."""
    return min((distance(point, position), name) for name, point in centers)


def snapshot(history, predicted, touchdowns, centers, archive_radius, notify_radius, now):
    """Build entity states from the notifier's live data.

    history: serial -> (observed, telemetry row); predicted: serial -> (issued, eta, position);
    touchdowns: serial -> (when, position, label); centers: [(name, (lat, lon))], first is the zone.
    """
    sondes, landings = {}, {}
    if not centers:
        return {"sondes": sondes, "landings": landings}
    for serial, (observed, row) in history.items():
        try:
            position = float(row["lat"]), float(row["lon"])
            km, near = nearest(centers, position)
        except (KeyError, TypeError, ValueError):
            continue
        if km > archive_radius:
            continue
        vertical = _number(row.get("vel_v"))
        phase = ("descending" if vertical is not None and vertical < -1 else
                 "ascending" if vertical is not None and vertical > 1 else "floating")
        sondes[serial.upper()] = {
            "latitude": position[0], "longitude": position[1], "gps_accuracy": 25,
            "altitude_m": _number(row.get("alt")), "vertical_speed_ms": vertical,
            "horizontal_speed_ms": _number(row.get("vel_h")), "heading": _number(row.get("heading")),
            "phase": phase, "signal": "live" if now - observed <= LIVE_SECONDS else "lost",
            "distance_km": round(km, 1), "nearest_to": near, "last_fix": iso(observed),
            "sondehub_url": f"https://sondehub.org/{quote(serial, safe='')}",
        }
    candidates = {}
    for serial, (issued, eta, position) in predicted.items():
        if eta >= now - 1800:
            candidates[serial.upper()] = {"eta": eta, "position": position, "source": "forecast",
                                          "accuracy": 1000, "issued": issued}
    for serial, (when, position, label) in touchdowns.items():
        if when >= now - 3 * 3600:
            candidates[serial.upper()] = {"eta": when, "position": position, "source": label,
                                          "accuracy": 250 if label != "Signal lost during descent" else 100,
                                          "issued": None}
    for serial, landing in candidates.items():
        km, near = nearest(centers, landing["position"])
        if km > archive_radius and serial not in sondes:
            continue
        landings[serial] = {
            "latitude": landing["position"][0], "longitude": landing["position"][1],
            "gps_accuracy": landing["accuracy"], "eta": iso(landing["eta"]), "source": landing["source"],
            "forecast_issued": iso(landing["issued"]), "distance_km": round(km, 1), "nearest_to": near,
            "inside_alert_radius": km <= notify_radius, "_eta": landing["eta"],
            "sondehub_url": f"https://sondehub.org/{quote(serial, safe='')}",
        }
    if len(sondes) > MAX_SONDES:
        keep = sorted(sondes, key=lambda serial: sondes[serial]["distance_km"])[:MAX_SONDES]
        sondes = {serial: sondes[serial] for serial in keep}
    if len(landings) > MAX_SONDES:
        keep = sorted(landings, key=lambda serial: landings[serial]["distance_km"])[:MAX_SONDES]
        landings = {serial: landings[serial] for serial in keep}
    return {"sondes": sondes, "landings": landings}


def summary(state, now):
    """Return the (incoming, active count, next landing) entity payloads."""
    landings = state["landings"]
    incoming = sorted((entry["_eta"], serial, entry) for serial, entry in landings.items()
                      if entry["inside_alert_radius"] and entry["_eta"] >= now - 1800)
    upcoming = sorted((entry["_eta"], serial, entry) for serial, entry in landings.items() if entry["_eta"] > now)
    active = [serial for serial, entry in state["sondes"].items() if entry["signal"] == "live"]
    incoming_attributes = {
        "serials": [serial for _, serial, _ in incoming],
        "nearest_km": min((entry["distance_km"] for *_, entry in incoming), default=None),
        "next_eta": iso(incoming[0][0]) if incoming else None,
    }
    next_attributes = {"serial": upcoming[0][1], "distance_km": upcoming[0][2]["distance_km"],
                       "inside_alert_radius": upcoming[0][2]["inside_alert_radius"]} if upcoming else {}
    return (("ON" if incoming else "OFF", incoming_attributes),
            (str(len(active)), {"serials": sorted(active)}),
            (iso(upcoming[0][0]) if upcoming else "None", next_attributes))


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if number == number else None


SENSORS = (
    ("binary_sensor", "incoming", {"name": "Sonde incoming", "icon": "mdi:parachute"}),
    ("sensor", "active_sondes", {"name": "Active sondes", "icon": "mdi:airballoon", "state_class": "measurement",
                                 "unit_of_measurement": "sondes"}),
    ("sensor", "next_landing", {"name": "Next landing", "icon": "mdi:map-marker-down", "device_class": "timestamp"}),
)


def sensor_config(component, key, extra):
    return {**extra, "unique_id": f"ha_sonde_tools_{key}", "default_entity_id": f"{component}.sonde_tools_{key}",
            "state_topic": f"{BASE}/{key}/state", "json_attributes_topic": f"{BASE}/{key}/attributes",
            "availability_topic": AVAILABILITY, "device": mqtt_link.DEVICE}


def tracker_config(kind, serial):
    key = f"{kind}_{slug(serial)}"
    return key, {
        "name": f"{serial} landing" if kind == "landing" else serial,
        "unique_id": f"ha_sonde_tools_{key}", "default_entity_id": f"device_tracker.sonde_{key}",
        "icon": "mdi:map-marker-down" if kind == "landing" else "mdi:airballoon",
        "json_attributes_topic": f"{BASE}/{key}/attributes", "source_type": "gps",
        "availability_topic": AVAILABILITY, "device": mqtt_link.DEVICE,
    }


class LiveEntities:
    def __init__(self, request, token, health, broker=None):
        self.health = health
        self.lock = Lock()
        self.connected = False
        self.client = None
        self.trackers = {}  # key -> {"attributes": dict, "updated": float, "published": dict | None}
        self.retained = set()  # Tracker keys found on the broker from earlier runs.
        self.last_sensors = None
        try:
            data = mqtt_link.broker(request, token, broker)
            self.client = mqtt_link.start(data, "live", AVAILABILITY, self._on_connect, self._on_disconnect,
                                          self._on_message)
            logging.info("Live sonde entities use MQTT discovery at %s", data["host"])
        except (OSError, ValueError, EOFError, TypeError) as error:
            logging.warning("Live sonde entities need the MQTT integration: %s", error_detail(error))
            health.fail("Live sonde entities", f"MQTT is unavailable ({error_detail(error)}).", threshold=1)

    def _on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            logging.warning("Live entities MQTT connection refused: %s", reason)
            return
        for component, key, extra in SENSORS:
            client.publish(DISCOVERY.format(component=component, key=key),
                           json.dumps(sensor_config(component, key, extra)), retain=True)
        client.subscribe(TRACKER_DISCOVERY)
        client.publish(AVAILABILITY, "online", retain=True)
        with self.lock:
            self.connected = True
            self.last_sensors = None
            for tracker in self.trackers.values():
                tracker["published"] = None  # Republish everything after a reconnect.

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        with self.lock:
            self.connected = False

    def _on_message(self, client, userdata, message):
        key = message.topic.split("/")[-2]
        if message.payload and (key.startswith("sonde_") or key.startswith("landing_")):
            with self.lock:
                self.retained.add(key)

    def update(self, state, now=None):
        if self.client is None:
            return
        now = time.time() if now is None else now
        with self.lock:
            if not self.connected:
                self.health.fail("Live sonde entities", "The MQTT broker is not connected.")
                return
            current = {}
            for kind, entries in (("sonde", state["sondes"]), ("landing", state["landings"])):
                for serial, attributes in entries.items():
                    key, config = tracker_config(kind, serial)
                    current[key] = ({name: value for name, value in attributes.items() if not name.startswith("_")},
                                    config)
            for key, (attributes, config) in current.items():
                tracker = self.trackers.get(key)
                if tracker is None:
                    self.client.publish(DISCOVERY.format(component="device_tracker", key=key), json.dumps(config),
                                        retain=True)
                    tracker = self.trackers[key] = {"published": None}
                    self.retained.discard(key)
                tracker["attributes"], tracker["updated"] = attributes, now
            for key, tracker in list(self.trackers.items()):
                if key in current:
                    continue
                if now - tracker["updated"] > LINGER_SECONDS:
                    self._remove(key)
                    del self.trackers[key]
                elif key.startswith("sonde_") and tracker["attributes"].get("signal") != "lost":
                    tracker["attributes"] = dict(tracker["attributes"], signal="lost")
            for key in list(self.retained):
                # Left over from a previous run and not active now.
                if key not in self.trackers:
                    self._remove(key)
                self.retained.discard(key)
            for key, tracker in self.trackers.items():
                if tracker["published"] != tracker["attributes"]:
                    self.client.publish(f"{BASE}/{key}/attributes", json.dumps(tracker["attributes"]), retain=True)
                    tracker["published"] = tracker["attributes"]
            sensors = summary(state, now)
            if sensors != self.last_sensors:
                for (_, key, _), (value, attributes) in zip(SENSORS, sensors):
                    self.client.publish(f"{BASE}/{key}/state", value, retain=True)
                    self.client.publish(f"{BASE}/{key}/attributes", json.dumps(attributes), retain=True)
                self.last_sensors = sensors
        self.health.healthy("Live sonde entities")

    def _remove(self, key):
        self.client.publish(DISCOVERY.format(component="device_tracker", key=key), "", retain=True)
        self.client.publish(f"{BASE}/{key}/attributes", "", retain=True)
