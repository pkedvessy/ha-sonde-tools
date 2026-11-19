"""Validated app options, parsed once from the Supervisor's options.json."""
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from detection import in_quiet_hours, parse_clock

OPTIONS = Path("/data/options.json")
DEFAULT_STYLE = "https://tiles.openfreemap.org/styles/liberty"


@dataclass(frozen=True)
class Settings:
    zone: str
    radius_km: float
    archive_radius_km: float
    telegram_entity: str
    timezone: ZoneInfo
    startup_test: bool = False
    debug: bool = False
    dashboard_sensor: bool = False
    health_alerts: bool = True
    quiet_hours: tuple = None
    dnd_summary: bool = True
    ground_elevation_m: float = None
    telegram_map_image: bool = True
    tiles: dict = None
    custom_tile_url: str = None
    custom_tile_attribution: str = None
    telemetry_retention_days: int = 0
    live_entities: bool = False
    follow_persons: tuple = ()

    def is_quiet(self, now=None):
        if self.quiet_hours is None:
            return False
        current = (now or datetime.now(self.timezone)).time()
        return in_quiet_hours(current, *self.quiet_hours)


def _number(cfg, key, default=None, minimum=None, maximum=None):
    value = cfg.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number.")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return value


def _tiles(cfg):
    """Return (web map tiles, custom raster URL, custom attribution)."""
    tiles = {"style": DEFAULT_STYLE} if cfg.get("map_default_basemap", True) else None
    url = (cfg.get("map_tile_url") or "").strip()
    attribution = (cfg.get("map_tile_attribution") or "").strip()
    if not url:
        return tiles, None, None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or not attribution or not all(
            part in url for part in ("{z}", "{x}", "{y}")):
        logging.warning("Custom basemap ignored: configure an HTTPS tile URL with {z}, {x}, {y} and attribution.")
        return tiles, None, None
    return {"url": url, "attribution": escape(attribution)}, url, attribution


def parse(cfg):
    entity = str(cfg.get("telegram_entity", "")).strip()
    if not entity.startswith("notify."):
        raise ValueError("Set telegram_entity to a Telegram notify.* entity.")
    zone = str(cfg.get("zone", "zone.home")).strip()
    if not zone:
        raise ValueError("Set zone to a Home Assistant zone entity.")
    try:
        tz = ZoneInfo(cfg.get("timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown timezone {cfg.get('timezone')!r}.") from None
    radius = _number(cfg, "radius_km", 30, 0.1, 500)
    archive_radius = _number(cfg, "archive_radius_km", 0, 0, 500) or radius
    quiet = None
    if cfg.get("dnd_enabled", False):
        quiet = (parse_clock(cfg.get("dnd_from", "22:00"), "dnd_from"),
                 parse_clock(cfg.get("dnd_to", "07:00"), "dnd_to"))
    tiles, custom_url, custom_attribution = _tiles(cfg)
    retention = _number(cfg, "telemetry_retention_days", 0, 0, 3650)
    follow = cfg.get("follow_persons") or []
    if not isinstance(follow, list) or not all(isinstance(item, str) for item in follow):
        raise ValueError("follow_persons must be a list of person.* entity IDs.")
    follow = tuple(dict.fromkeys(item.strip() for item in follow if item.strip()))
    invalid = [item for item in follow if not item.startswith(("person.", "device_tracker."))]
    if invalid:
        raise ValueError(f"follow_persons accepts person.* or device_tracker.* entities, not {invalid[0]!r}.")
    return Settings(
        zone=zone, radius_km=radius, archive_radius_km=archive_radius, telegram_entity=entity, timezone=tz,
        startup_test=bool(cfg.get("startup_test", False)), debug=bool(cfg.get("debug", False)),
        dashboard_sensor=bool(cfg.get("dashboard_sensor", False)),
        health_alerts=bool(cfg.get("health_alerts", True)),
        quiet_hours=quiet, dnd_summary=bool(cfg.get("dnd_summary", True)),
        ground_elevation_m=_number(cfg, "ground_elevation_m", None, -400, 9000),
        telegram_map_image=bool(cfg.get("telegram_map_image", True)),
        tiles=tiles, custom_tile_url=custom_url, custom_tile_attribution=custom_attribution,
        telemetry_retention_days=int(retention),
        live_entities=bool(cfg.get("live_entities", False)), follow_persons=follow,
    )


def load(path=OPTIONS):
    return parse(json.loads(Path(path).read_text(encoding="utf-8")))
