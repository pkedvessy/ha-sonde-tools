"""Static PNG maps for Telegram landing alerts.

Home Assistant's telegram_bot.send_photo downloads the image from a URL, so the
rendered PNG is held in memory and served once from a small internal HTTP server
that is reachable only on the Supervisor network (the port is not published).
"""
import io
import logging
import math
import socket
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from secrets import token_urlsafe
from threading import Lock, Thread
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

from app_version import USER_AGENT
from field_tools import distance

OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
PORT = 8098
WIDTH, HEIGHT = 800, 600
TILE = 256
MIN_ZOOM, MAX_ZOOM = 10, 14
PADDING = 90
IMAGE_TTL_SECONDS = 600

TRACK = (122, 59, 182)
LAST_FIX = (166, 89, 15)
LANDING = (210, 38, 48)
HOME = (36, 101, 208)


def pixel(position, zoom):
    lat, lon = position
    lat = max(-85.0511, min(85.0511, lat))
    scale = TILE * 2 ** zoom
    x = (lon + 180) / 360 * scale
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * scale
    return x, y


def fit_zoom(points):
    for zoom in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        xs, ys = zip(*(pixel(p, zoom) for p in points))
        if max(xs) - min(xs) <= WIDTH - 2 * PADDING and max(ys) - min(ys) <= HEIGHT - 2 * PADDING:
            return zoom
    return MIN_ZOOM


def read_track(database, serial, since):
    with closing(sqlite3.connect(database, timeout=2)) as db:
        rows = db.execute("""
            SELECT observed, lat, lon FROM telemetry
            WHERE serial = ? AND observed >= ? AND lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY observed
        """, (serial.upper(), since)).fetchall()
    track, last = [], None
    for observed, lat, lon in rows:
        if observed != last:
            track.append((float(lat), (float(lon) + 180) % 360 - 180))
            last = observed
    return track


class MapRenderer:
    def __init__(self, tile_url=None, attribution=None):
        self.tile_url = tile_url or OSM_TILES
        self.attribution = attribution if tile_url else OSM_ATTRIBUTION
        try:
            self.font = ImageFont.load_default(size=13)
        except TypeError:
            self.font = ImageFont.load_default()

    def tile(self, zoom, x, y):
        count = 2 ** zoom
        if not 0 <= y < count:
            return None
        url = (self.tile_url.replace("{z}", str(zoom)).replace("{x}", str(x % count))
               .replace("{y}", str(y)).replace("{s}", "a").replace("{r}", ""))
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=10) as response:
            return Image.open(io.BytesIO(response.read())).convert("RGB")

    def render(self, landing, last_fix=None, track=(), home=None, label=""):
        points = [landing] + ([last_fix] if last_fix else [])
        points += [p for p in track[-1:] if distance(landing, p) <= 5]
        zoom = fit_zoom(points)
        xs, ys = zip(*(pixel(p, zoom) for p in points))
        left = (min(xs) + max(xs)) / 2 - WIDTH / 2
        top = (min(ys) + max(ys)) / 2 - HEIGHT / 2

        image = Image.new("RGB", (WIDTH, HEIGHT), (232, 232, 228))
        first_x, first_y = math.floor(left / TILE), math.floor(top / TILE)
        last_x, last_y = math.floor((left + WIDTH) / TILE), math.floor((top + HEIGHT) / TILE)
        cells = [(x, y) for x in range(first_x, last_x + 1) for y in range(first_y, last_y + 1)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            tiles = list(pool.map(lambda cell: self._safe_tile(zoom, *cell), cells))
        if not any(tiles):
            raise OSError("No map tiles could be downloaded")
        for (x, y), tile in zip(cells, tiles):
            if tile is not None:
                image.paste(tile, (round(x * TILE - left), round(y * TILE - top)))

        draw = ImageDraw.Draw(image, "RGBA")
        project = lambda p: (pixel(p, zoom)[0] - left, pixel(p, zoom)[1] - top)

        if len(track) > 1:
            draw.line([project(p) for p in track], fill=TRACK + (230,), width=4, joint="curve")
        if last_fix:
            self._dashed(draw, project(last_fix), project(landing), LANDING + (220,))
        if home:
            hx, hy = project(home)
            if -20 <= hx <= WIDTH + 20 and -20 <= hy <= HEIGHT + 20:
                self._dot(draw, (hx, hy), HOME, 8)
            else:
                self._edge_arrow(draw, (hx, hy), HOME)
        if last_fix:
            self._dot(draw, project(last_fix), LAST_FIX, 8)
        self._pin(draw, project(landing))

        legend = [(LANDING, label or "Landing")]
        if last_fix:
            legend.append((LAST_FIX, "Last GPS fix"))
        if len(track) > 1:
            legend.append((TRACK, "Observed track"))
        if home:
            legend.append((HOME, "Home"))
        self._legend(draw, legend)
        self._attribution(draw)

        buffer = io.BytesIO()
        image.save(buffer, "PNG", optimize=True)
        return buffer.getvalue()

    def _safe_tile(self, zoom, x, y):
        try:
            return self.tile(zoom, x, y)
        except (OSError, ValueError) as error:
            logging.debug("Map tile %s/%s/%s failed: %s", zoom, x, y, error)
            return None

    @staticmethod
    def _dot(draw, center, color, radius):
        x, y = center
        draw.ellipse((x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2), fill=(255, 255, 255))
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)

    @staticmethod
    def _pin(draw, tip):
        x, y = tip
        head = (x, y - 30)
        draw.polygon([(x, y), (x - 11, y - 24), (x + 11, y - 24)], fill=(255, 255, 255))
        draw.ellipse((head[0] - 14, head[1] - 14, head[0] + 14, head[1] + 14), fill=(255, 255, 255))
        draw.polygon([(x, y - 3), (x - 9, y - 24), (x + 9, y - 24)], fill=LANDING)
        draw.ellipse((head[0] - 12, head[1] - 12, head[0] + 12, head[1] + 12), fill=LANDING)
        draw.ellipse((head[0] - 5, head[1] - 5, head[0] + 5, head[1] + 5), fill=(255, 255, 255))

    @staticmethod
    def _dashed(draw, start, end, color, dash=10, gap=7):
        length = math.dist(start, end)
        if length < 1:
            return
        dx, dy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
        position = 0
        while position < length:
            stop = min(position + dash, length)
            draw.line([(start[0] + dx * position, start[1] + dy * position),
                       (start[0] + dx * stop, start[1] + dy * stop)], fill=color, width=3)
            position = stop + gap

    @staticmethod
    def _edge_arrow(draw, target, color):
        cx, cy = WIDTH / 2, HEIGHT / 2
        angle = math.atan2(target[1] - cy, target[0] - cx)
        reach = min((WIDTH / 2 - 16) / max(abs(math.cos(angle)), 1e-9),
                    (HEIGHT / 2 - 16) / max(abs(math.sin(angle)), 1e-9))
        tip = (cx + math.cos(angle) * reach, cy + math.sin(angle) * reach)
        wing = lambda turn: (tip[0] - 28 * math.cos(angle + turn), tip[1] - 28 * math.sin(angle + turn))
        outline = [tip, wing(0.5), wing(-0.5)]
        draw.polygon(outline, fill=color, outline=(255, 255, 255), width=3)

    def _legend(self, draw, items):
        line = 20
        width = 24 + max(draw.textlength(text, font=self.font) for _, text in items) + 12
        draw.rounded_rectangle((10, 10, 10 + width, 18 + line * len(items)), radius=6, fill=(255, 255, 255, 225))
        for index, (color, text) in enumerate(items):
            y = 18 + index * line
            draw.ellipse((20, y + 2, 32, y + 14), fill=color)
            draw.text((40, y), text, fill=(30, 30, 30), font=self.font)

    def _attribution(self, draw):
        text = self.attribution or ""
        if not text:
            return
        width = draw.textlength(text, font=self.font)
        draw.rectangle((WIDTH - width - 12, HEIGHT - 22, WIDTH, HEIGHT), fill=(255, 255, 255, 210))
        draw.text((WIDTH - width - 6, HEIGHT - 19), text, fill=(40, 40, 40), font=self.font)


class ImageServer:
    """Serves each rendered PNG under an unguessable one-time path for Home Assistant to fetch."""

    def __init__(self, port=PORT):
        self.port = port
        self.images = {}
        self.lock = Lock()
        self.host = socket.gethostname()
        images, lock = self.images, self.lock

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                token = self.path.removeprefix("/map/").removesuffix(".png")
                with lock:
                    entry = images.pop(token, None)
                if entry is None or entry[1] < time.time():
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(entry[0])))
                self.end_headers()
                self.wfile.write(entry[0])

            def log_message(self, format, *args):
                logging.debug("Map image server: " + format, *args)

        server = ThreadingHTTPServer(("", port), Handler)
        Thread(target=server.serve_forever, daemon=True).start()

    def publish(self, png):
        token = token_urlsafe(24)
        now = time.time()
        with self.lock:
            for key in [key for key, (_, expires) in self.images.items() if expires < now]:
                del self.images[key]
            self.images[token] = png, now + IMAGE_TTL_SECONDS
        return f"http://{self.host}:{self.port}/map/{token}.png"

    def discard(self, url):
        with self.lock:
            self.images.pop(url.rsplit("/", 1)[-1].removesuffix(".png"), None)
