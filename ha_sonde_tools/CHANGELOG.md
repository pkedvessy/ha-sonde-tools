# Changelog

## 0.15.2

### Added
- Records can be filtered to a single month of a year. The month view charts flights and landings by day.

## 0.15.1

### Added
- `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password`, and `mqtt_ssl` connect the dashboard sensor and live entities to an external MQTT broker. Before, only a broker app advertised by the Supervisor (such as Mosquitto) was found.

## 0.15.0

### Added
- **Atmospheric sounding** on the Flight page: a Skew-T log-P diagram of temperature and dew point, a wind speed and direction profile, and a hodograph, with the tropopause, freezing level, burst altitude, and strongest wind. Pressure is derived from GPS altitude for sondes without a pressure sensor.
- **Records** tab: year-in-review statistics, flight records, recovery rate, a flight calendar, monthly and hourly charts, and sonde types.
- `live_entities`: MQTT device trackers for each nearby sonde and its landing point (shown on the Map card), plus `binary_sensor.sonde_tools_incoming`, `sensor.sonde_tools_active_sondes`, and `sensor.sonde_tools_next_landing`.
- `follow_persons`: landings near followed people are alerted too, and the message names who the landing is closest to.
- Flight summaries: per-serial statistics (time span, fix count, highest altitude, fastest wind and descent, coldest reading, type) are saved as telemetry arrives and kept by telemetry retention. Records, the header count, and recovery syncing use them, and the Flight page shows them, also after the raw fixes are removed. Existing archives are summarized once on upgrade.

### Changed
- The dashboard sensor and live entities share one MQTT setup module.

## 0.14.0

### Added
- `telemetry_retention_days` deletes raw GPS telemetry and forecast paths older than the chosen age. Landing events are kept.
- **Compact database** under **Backfill** returns freed archive space to the disk.
- `dnd_summary`: after quiet hours, one Telegram message lists the alerts that were held back.
- The dashboard sensor uses MQTT discovery when the MQTT integration is available, so it keeps a unique ID and history across Home Assistant restarts.
- Supervisor watchdog: the app is restarted if its polling loop stops for 10 minutes.

### Fixed
- An unreadable `sent.json` no longer stops the app. It is set aside, and a health alert is sent.
- A forecast message that can no longer be edited (for example, deleted in Telegram) is no longer retried every minute for a week.
- A damaged alert state entry can no longer stop the polling loop.
- Map rendering for alerts is limited to 30 seconds, and map tiles are cached between alerts.

### Changed
- Internal restructuring: detection logic, settings, alert state, archive maintenance, and the dashboard sensor are separate modules; web page scripts live in `web/`. Tests import the production code directly.
- Added CI (unit tests, ruff, JavaScript syntax, and Docker builds for amd64 and aarch64).

## 0.13.0

- Telegram landing alerts include a rendered map image.
