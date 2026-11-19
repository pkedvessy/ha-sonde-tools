"""Shared MQTT broker lookup and client setup for Home Assistant discovery publishers."""
from uuid import uuid4

from app_version import APP_VERSION

SUPERVISOR = "http://supervisor"
DEVICE = {"identifiers": ["ha_sonde_tools"], "name": "Sonde Tools", "manufacturer": "Sonde Tools",
          "sw_version": APP_VERSION}


def broker(request, token):
    """Return the Supervisor's MQTT service details. Raises OSError/ValueError/EOFError."""
    service = request(f"{SUPERVISOR}/services/mqtt", token, timeout=10)
    data = service.get("data") if isinstance(service, dict) else None
    if not isinstance(data, dict) or not data.get("host"):
        raise ValueError("No MQTT service is registered")
    return data


def start(data, name, will_topic, on_connect, on_disconnect, on_message=None):
    """Create a client with a retained offline will, connect in the background, and start its loop."""
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"ha-sonde-tools-{name}-{uuid4().hex[:12]}")
    if data.get("username"):
        client.username_pw_set(data["username"], data.get("password"))
    if data.get("ssl"):
        client.tls_set()
    client.will_set(will_topic, "offline", retain=True)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    if on_message is not None:
        client.on_message = on_message
    client.reconnect_delay_set(5, 120)
    client.connect_async(data["host"], int(data.get("port") or 1883))
    client.loop_start()
    return client
