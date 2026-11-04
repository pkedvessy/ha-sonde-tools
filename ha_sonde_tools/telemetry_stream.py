import gzip
import json
import logging
import time
from threading import Lock, Thread
from urllib.parse import urlsplit
from urllib.request import urlopen
from uuid import uuid4

import paho.mqtt.client as mqtt


class TelemetryStream:
    def __init__(self, api, archive_batch):
        self.api = api
        self.archive_batch = archive_batch
        self.lock = Lock()
        self.rows = {}
        self.ready = False
        self.last_batch = None
        Thread(target=self.run, daemon=True).start()

    def drain(self):
        with self.lock:
            rows, self.rows = self.rows, {}
            healthy = self.ready and self.last_batch is not None and time.monotonic() - self.last_batch <= 120
            return healthy, rows

    def on_connect(self, client, userdata, flags, reason, properties):
        if not reason.is_failure:
            client.subscribe("batch")
        else:
            client.disconnect()

    def on_subscribe(self, client, userdata, mid, reasons, properties):
        with self.lock:
            self.ready = bool(reasons) and all(not reason.is_failure for reason in reasons)
        if self.ready:
            logging.info("Connected to SondeHub WebSocket telemetry")
        else:
            client.disconnect()

    def on_message(self, client, userdata, message):
        try:
            body = message.payload
            if body.startswith(bytes((31, 139))):
                body = gzip.decompress(body)
            batch = json.loads(body)
            if not isinstance(batch, list):
                raise ValueError("Expected a telemetry batch")
            self.archive_batch(batch)
            with self.lock:
                for row in batch:
                    if isinstance(row, dict) and isinstance(row.get("serial"), str) and isinstance(row.get("datetime"), str):
                        self.last_batch = time.monotonic()
                        serial = row["serial"]
                        if serial not in self.rows or row["datetime"] > self.rows[serial]["datetime"]:
                            self.rows[serial] = row
        except (OSError, EOFError, ValueError) as exc:
            logging.warning("SondeHub WebSocket telemetry: %s", exc)

    def run(self):
        delay = 5
        while True:
            try:
                with urlopen(f"{self.api}/sondes/websocket", timeout=20) as response:
                    url = urlsplit(response.read().decode().strip())
                if url.scheme != "wss" or not url.hostname:
                    raise ValueError("Expected a secure SondeHub WebSocket URL")
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="websockets",
                                     client_id=f"ha-sonde-tools-{uuid4().hex}", reconnect_on_failure=False)
                client.tls_set()
                client.ws_set_options(path=url.path + (f"?{url.query}" if url.query else ""))
                client.on_connect = self.on_connect
                client.on_subscribe = self.on_subscribe
                client.on_message = self.on_message
                client.connect(url.hostname, url.port or 443)
                client.loop_forever()
            except (OSError, ValueError, mqtt.WebsocketConnectionError) as exc:
                logging.warning("SondeHub WebSocket connection failed (%s); retrying", type(exc).__name__)
            finally:
                with self.lock:
                    delay = 5 if self.ready else min(delay * 2, 120)
                    self.ready = False
                    self.last_batch = None
            time.sleep(delay)
