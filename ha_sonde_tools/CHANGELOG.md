# Changelog

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
