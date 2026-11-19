"""Publishes the optional "nearest active radiosonde" sensor.

With the Home Assistant MQTT integration available through the Supervisor, the sensor is
created by MQTT discovery: it has a unique ID, survives Home Assistant restarts and keeps
history. Without MQTT, the state is written directly with the REST API as before.
"""
import json
import logging
from threading import Lock

import mqtt_link
from health_alerts import error_detail

ENTITY_ID = "sensor.sonde_tools_nearest_active"
BASE = "sonde_tools/nearest_active"
DISCOVERY = "homeassistant/sensor/ha_sonde_tools/nearest_active/config"
CORE = "http://supervisor/core/api"


def discovery_config():
    return {
        "name": "Nearest active radiosonde",
        "unique_id": "ha_sonde_tools_nearest_active",
        "default_entity_id": ENTITY_ID,
        "icon": "mdi:weather-windy",
        "state_topic": f"{BASE}/state",
        "json_attributes_topic": f"{BASE}/attributes",
        "availability_topic": f"{BASE}/availability",
        "device": mqtt_link.DEVICE,
    }


class Dashboard:
    def __init__(self, request, token, health):
        self.request = request
        self.token = token
        self.health = health
        self.lock = Lock()
        self.client = None
        self.connected = False
        self.cleaned = False
        self.last = None
        self.mode = "rest"
        self._start_mqtt()

    def _start_mqtt(self):
        try:
            data = mqtt_link.broker(self.request, self.token)
        except (OSError, ValueError, EOFError) as error:
            logging.info("Dashboard sensor uses the REST API (MQTT unavailable: %s)", error_detail(error))
            return
        try:
            client = mqtt_link.start(data, "dashboard", f"{BASE}/availability", self._on_connect, self._on_disconnect)
        except (OSError, ValueError, TypeError) as error:
            logging.warning("Dashboard MQTT setup failed, using the REST API: %s", error)
            return
        self.client, self.mode = client, "mqtt"
        logging.info("Dashboard sensor uses MQTT discovery at %s", data["host"])

    def _on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            logging.warning("Dashboard MQTT connection refused: %s", reason)
            return
        if not self.cleaned:
            self._remove_rest_state()
            self.cleaned = True
        client.publish(DISCOVERY, json.dumps(discovery_config()), retain=True)
        client.publish(f"{BASE}/availability", "online", retain=True)
        with self.lock:
            self.connected = True
            last = self.last
        if last is not None:
            self._publish_mqtt(last)

    def _remove_rest_state(self):
        """Delete a state left by REST mode, so discovery can claim the same entity ID.

        A REST-written state has the bare friendly name; the discovered entity is prefixed
        with the device name, so an existing MQTT entity is never removed.
        """
        try:
            current = self.request(f"{CORE}/states/{ENTITY_ID}", self.token, timeout=10)
            if current.get("attributes", {}).get("friendly_name") == "Nearest active radiosonde":
                self.request(f"{CORE}/states/{ENTITY_ID}", self.token, method="DELETE", timeout=10)
                logging.info("Removed the REST dashboard sensor state before MQTT discovery")
        except (OSError, ValueError, EOFError, AttributeError):
            pass

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        with self.lock:
            self.connected = False

    def _publish_mqtt(self, state):
        self.client.publish(f"{BASE}/state", state["state"], retain=True)
        attributes = {key: value for key, value in state["attributes"].items()
                      if key not in ("friendly_name", "icon")}
        self.client.publish(f"{BASE}/attributes", json.dumps(attributes), retain=True)

    def publish(self, state):
        """Publish {"state": str, "attributes": dict}. Raises OSError/ValueError on REST failures."""
        with self.lock:
            self.last = state
            connected = self.connected
        if self.mode == "mqtt":
            if connected:
                self._publish_mqtt(state)
                self.health.healthy("Home Assistant dashboard sensor")
            else:
                self.health.fail("Home Assistant dashboard sensor", "The MQTT broker is not connected.")
            return
        self.request(f"{CORE}/states/{ENTITY_ID}", self.token, state)
        self.health.healthy("Home Assistant dashboard sensor")
