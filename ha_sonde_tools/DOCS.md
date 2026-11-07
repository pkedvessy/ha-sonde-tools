# Sonde Tools

Sonde Tools uses public SondeHub radiosonde forecasts and telemetry to send Telegram alerts around a Home Assistant zone. It runs on Home Assistant OS on amd64 and aarch64.

## Set up delivery

1. In your [Telegram Bot integration](https://www.home-assistant.io/integrations/telegram_bot/), find the `notify.*` entity for the allowed chat that should receive alerts.
2. In the app's **Configuration** tab, enter that entity as `telegram_entity`. Choose the zone and radius. The radius is measured from the zone's coordinates and is independent of the zone's own radius.
3. Enable `startup_test`, save, and start or restart the app. When the test message arrives, disable `startup_test`, save, and restart. The test verifies Telegram delivery.

For a local installation, copy the `ha_sonde_tools` directory into `/addons`, reload the app store, and install **Sonde Tools** under local apps.

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `telegram_entity` | Empty | Required. The Telegram Bot `notify.*` entity for the destination chat. |
| `zone` | `zone.home` | Home Assistant zone entity used as the circle's center. |
| `radius_km` | `30` | Telegram alert radius in kilometers. Accepts 0.1 to 500. |
| `archive_radius_km` | `0` | Local history radius in kilometers. Use 0 to follow `radius_km`, or enter a positive radius up to 500. |
| `ground_elevation_m` | Unset | Optional approximate ground elevation near the expected touchdown, in the same vertical reference as the GPS altitude. |
| `timezone` | `Europe/Budapest` | IANA time zone for Telegram timestamps, such as `Europe/London` or `America/Denver`. |
| `startup_test` | `false` | Sends a test message at startup. |
| `debug` | `false` | Enables detailed polling and alert logs. |

## Alert rules

**Predicted landing.** The app uses the final point of a fresh SondeHub forecast and checks whether the forecast landing time is still in the future. An alert is sent when that endpoint is within the configured radius, even if the sonde is currently outside it. The message time is the forecast ETA.

**Likely touchdown.** When the last GPS fix shows descent within 1 km of a recent predicted touchdown, the app waits until the ETA has passed and the fix is at least 60 seconds old. A recent WebSocket batch or a successful REST telemetry snapshot is required. The alert uses the predicted point for the radius check and includes the last GPS fix. The fallback alone uses a 500 m GPS altitude ceiling.

**Estimated landing after signal loss.** Without a suitable nearby forecast, after three minutes without a newer GPS fix, the app checks that the last fix showed descent and was at or below 500 m GPS altitude. With a configured ground elevation, it projects the last reported descent speed, horizontal speed, and heading to the ground for up to two minutes. The alert checks the estimated point against the radius and includes both map points. If a projection is unavailable, **Signal lost during descent** uses the last GPS fix for the radius check and map link. Terrain variations and changes in wind can shift the actual touchdown.

The app remembers successful alerts for seven days across restarts. The two alert types are tracked independently for each sonde serial.

## Troubleshooting

- **The app stops at startup.** Verify that `telegram_entity` starts with `notify.` and matches the exact entity ID shown in the Telegram Bot integration.
- **The startup test arrives but a flight produces no alerts.** Enable `debug`, save, and restart. Check the **Log** tab for HTTP status, record and candidate counts, distance or duplicate checks, and the sonde's altitude, fix age, and descent speed. A candidate is evaluated against your radius before delivery.
- **No update appears.** Refresh the app store and check the installed version on the app page. For a local installation, replace the files in the local app directory with the updated source before refreshing.

## Location data

The app uses the SondeHub MQTT-over-WebSocket batch feed for live telemetry. It requests a nearby REST snapshot on startup and while disconnected, sending the zone center and a search radius 12 km wider than the larger configured radius. Forecast and recovery lookups use REST. The notifier checks forecasts and evaluates alerts on a fixed 60-second cycle. Live GPS samples continue to arrive and archive over WebSocket between checks. Debug logging includes the resolved zone coordinates. Alert messages include the forecast or observed position via the map link.

## Local history

Open the app's **Web UI** from its Home Assistant page. Enable **Show in sidebar** to open it directly as **Sonde Tools**.

The history records predicted landings, estimated landings after signal loss, and last GPS fixes within the archive radius, independently of Telegram delivery. Each event type is recorded once per sonde serial in a rolling seven-day window. The app keeps the archive in its local data directory across restarts and updates. It starts collecting after installation or upgrade, with no automatic import of earlier flights.

Filter by recording period or search by sonde serial. The area map plots events on the current page relative to the configured zone and radius. Open a row's map link for a street map or its serial link for SondeHub. Estimated landings also include a separate link to the last observed fix.

### Recovery reports

The **Recovery** column distinguishes recovered sondes, planned recoveries, unsuccessful attempts, and sondes with no report. Recovered markers appear gray. Select **Hide recovered** to remove them from the table and map. A missing report means the recovery status is unconfirmed. To handle serials reused on later flights, the map and table ignore recovery reports more than six hours older than the event or GPS fix. The app checks reports in batches and caches them locally. Archived sonde serials are sent to SondeHub when checking their recovery status.

### Full map

Use the **Full map** tab for a zoomable street map with the archive circle and a dashed Telegram circle when the radii differ and all events matching your filters. Select a marker to see the event, recovery status, and links to SondeHub and Google Maps. Google Maps opens the coordinates of the selected marker. The **History** tab shows a compact street map behind the area radar and retains the event table. Both tabs share the date, serial, and recovery filters.

The full map uses the OpenFreeMap street basemap by default. The browser loads map assets and tiles directly, and vector rendering requires WebGL. Set map_default_basemap to false to disable it. To use a custom raster basemap, set map_tile_url to an HTTPS template containing {z}, {x}, and {y}, and set map_tile_attribution to the attribution required by the provider. A valid custom basemap takes precedence. When tiles are unavailable, event markers and search circles remain visible, and each event can still be opened in OpenStreetMap. The archive remains on your Home Assistant server.

### Raw telemetry archive

The app stores the complete fields of each received telemetry sample whose GPS position is inside the archive radius. It keeps one sample per serial, GPS timestamp, and reporting receiver. The archive includes in-range samples from the live stream and the REST snapshots available during startup or outages. It starts collecting after upgrade and does not backfill gaps in reception.

Enter an exact serial in **Download raw telemetry** to export a chronological JSON array. A sonde can have archived telemetry even if it never generated a landing event. Raw telemetry is kept until the app data is removed, so storage grows with coverage, traffic, and the archive radius.

### Import historical telemetry

Enter an exact serial under **Fetch and archive from SondeHub**. The app fetches the public SondeHub serial archive and stores samples whose GPS positions fall inside the current archive radius. The result shows how many new samples were saved and links to the combined local JSON export. Repeat imports deduplicate against live and previously imported samples. Imports add raw telemetry only. They do not replay alerts or create landing events.

A flight must be available in the SondeHub serial archive. Each import is limited to 64 MiB of decompressed JSON.

### Forecasts in telemetry exports

The app stores forecast snapshots separately from GPS samples. Each exported sample includes sondehub_prediction with the landing latitude, longitude, forecast issue time, and estimated landing time when a matching snapshot exists. The export uses the latest saved forecast issued at or before the fix, up to 20 minutes earlier, with an ETA at or after the fix. Otherwise the field is null. Imports do not reconstruct old forecasts from observed telemetry.

### Live positions and observed routes

The **Full map** refreshes live positions, observed routes, and landing markers every 30 seconds while the tab is visible. Predicted landing markers show the newest saved forecast associated with their archived event. A live marker shows the latest archived GPS fix when it is no more than three minutes old. Select it to open the sonde in SondeHub or the GPS fix in Google Maps. Routes use up to one archived fix per minute and break at gaps over five minutes. The map respects the history filters and displays up to 12,000 route points. Download raw telemetry for the full sample resolution.

### Compare forecasts over a flight

Open the **Flight** tab and enter an exact sonde serial, or follow the **Flight** link from a history row or map popup. The purple route shows archived GPS fixes. Move the forecast slider to compare successive dashed blue routes and their predicted touchdown markers. The timestamp beside the slider shows when the forecast was issued and its ETA.

Full forecast routes are saved as they arrive. Importing GPS history does not supply missing historical forecasts.

### Flight analysis and recovery shortlist

- **Touchdown drift** shows earlier predicted touchdown points and the distance and direction the forecast moved over the available snapshots in the last 20 minutes.
- **Altitude profile** plots archived GPS altitude. Select a point or move the slider to locate that fix on the map. Dashes mark reception gaps. A possible burst is inferred from the highest captured fix and subsequent descent.
- **Flight status** shows the latest fix age, ascent or descent, altitude, vertical speed, distance from the zone center, and a future forecast ETA when available. The card fetches updates every 30 seconds while visible. Reload the Flight view for newly archived routes and forecast snapshots.
- **Recovery shortlist** shows up to 20 candidates from the last 30 days, sorted by recency or distance. It excludes recently heard sondes and reported recoveries. Each result identifies the source of its location and links to Google Maps. The shortlist prefers saved touchdown estimates and last-fix events over predictions. Forecast-only entries use the latest saved forecast for the flight. The displayed distance and nearest-first ordering use the same coordinates.
- **Forecast accuracy** marks the reported recovery coordinates when they match the latest flight. The forecast slider shows the selected touchdown error and the best error in that forecast series. This measures distance to the reported recovery location.

### Recovery Mode, heatmap, and replay

- **Recovery Mode** opens from map popups and the recovery shortlist. It shows the estimated or predicted touchdown and the last GPS fix, with Google Maps directions. Distance and bearing use optional browser geolocation over HTTPS. The app does not receive or store your phone location.
- **Forecast spread** draws a shaded circle around the selected touchdown, sized by the farthest forecast endpoint in the preceding 20 minutes. It shows recent forecast variation.
- **Play flight** advances the observed position, altitude cursor, and forecast together on the recorded timeline. Playback pauses at a possible burst and at the end.
- **Heatmap** groups local positions into geographic cells. Choose estimated touchdowns, predictions, or reported recoveries and optionally select a month.
- **Telegram forecast edits** update the original prediction alert when the forecast moves at least 1 km from the last message position or crosses the notification radius. The first message must have been sent by a version that stored its Telegram message ID.

The History area map and event table show the latest saved GPS fix and forecast associated with each displayed flight at page load. Reload the page to pick up new samples. Distances in the table and the landing heatmap use the original archived events.

## Coverage, launch outlook, and scorecard

- **Coverage** maps archived fixes below 250, 500, or 1,000 m GPS altitude. Each cell counts distinct sonde serials heard by the SondeHub receiver network. Red outlines show where a sonde was last heard in the saved data. These are reception observations rather than a prediction of coverage.
- **Launches** lists the next nominal daily windows from the nearest SondeHub stations. Select **Estimate landing** to request a Tawhiri forecast for that window. The start time is set 45 minutes before the published time.
- **Scorecard** shows the median distance to reported recovery at 60, 30, and 10 minutes before estimated touchdown. Each row includes its sample count. It needs a saved touchdown estimate, a recovery with coordinates, and forecasts from the corresponding lead time.
- **Recovery Mode** offers an on-demand terrain corridor using Open-Meteo elevation and recent descent vectors. Opening the corridor sends projected coordinates to Open-Meteo. The GPS and terrain altitude references can differ, so use the result as a search estimate.

## Home Assistant dashboard card

Enable dashboard_sensor in the app configuration and restart. Add a **Markdown** card in manual YAML mode. Replace YOUR_SIDEBAR_PATH with the path shown in your browser when you open the Sonde Tools sidebar panel, without the leading slash.

```yaml
type: markdown
title: Nearest radiosonde
entity_id: sensor.sonde_tools_nearest_active
content: |
  {% set id = 'sensor.sonde_tools_nearest_active' %}
  {% set serial = states(id) %}
  {% set fix = as_timestamp(state_attr(id, 'gps_fix'), 0) %}
  {% if fix and as_timestamp(now()) - fix <= 180 %}
  **{{ serial }}** · {{ state_attr(id, 'distance_km') }} km away · Altitude {{ state_attr(id, 'altitude_m') | float(0) | round(0) }} m
  {% set eta = state_attr(id, 'forecast_eta') %}
  {% if eta %}ETA {{ as_local(as_datetime(eta)).strftime('%H:%M') }}{% endif %}
  [Flight view](/YOUR_SIDEBAR_PATH?sonde_tools_flight={{ serial | urlencode }}) · [SondeHub]({{ state_attr(id, 'sondehub_url') }})
  {% else %}
  No recent radiosonde fixes in the archive area.
  {% endif %}
```

## Telegram health alerts

The app sends a health alert to the configured Telegram chat when a database write fails, a fatal error stops the notifier, or it detects three consecutive failures of a monitored service or parser. It monitors SondeHub forecast and telemetry inputs, recovery report sync, the Home Assistant zone, and the optional dashboard sensor. A failed Telegram send is also recorded and can be reported when delivery works again.

The app groups pending incidents into one message, suppresses repeats until the affected check succeeds again, and retries failed health alert delivery while running. Pending incidents are kept across restarts when the app can write its state file. If Home Assistant or Telegram is down, messages can only arrive after delivery recovers. Startup errors that prevent the app from reading its Telegram configuration are visible in the app log.

Set health_alerts to false to disable these operational messages.

## Archive Backfill and storage

Open **Backfill** and choose a UTC date range of up to 366 days. **Fill gaps in known flights** finds serials already present in local telemetry, events, or forecasts. **Discover flights by launch station** checks the public SondeHub index for up to eight selected stations. Flights from other or unindexed stations may be missed. Known-flight previews count only serials already saved locally. For station discovery, the app scans the selected station months in the background and shows the indexed candidate count before downloading any full tracks. Nearby stations are preselected when available. Start the job when the preview is ready.

The worker checks one request at a time and saves each completed flight and its counts in SQLite. Pause stops after the current request, Resume continues, and Cancel keeps completed samples. A running job resumes after an app restart. Retry failed downloads after a job finishes with errors. The job keeps the zone and archive radius from its preview, stores only fixes inside that circle and UTC date range, and skips existing samples. It does not replay alerts, recreate historical forecasts, or add landing events.

The header shows the number of distinct archived sonde serials and the approximate SQLite footprint, including the write-ahead log. Reload the page to update the footprint. The serial count is cached for up to a minute.

### Archive format

New telemetry uses separate SQLite tables for GPS fixes, receiver metadata, and reception reports. Extra fields and per-receiver differences are stored as typed values. Forecast route points use columns for position, altitude, and time. JSON downloads are reconstructed from these tables. Telemetry columns declare REAL for measurements, INTEGER for counters, and TEXT for metadata; only binary receiver signatures and generic extension values use BLOB. Existing tables are rebuilt transactionally on upgrade, preserving IDs and relationships. Known numeric measurements and counters (including GPS coordinates, altitude, speed, temperature, signal levels, and frame numbers) use numeric values even when the source sends strings. Identifiers, timestamps, encoded payloads, and invalid numeric strings remain unchanged. Downloads therefore return normalized numbers for these measurement fields. Existing archives are converted once at startup in a transaction. Freed database pages can be reused; the file size does not necessarily decrease immediately.

## Do not disturb

Enable dnd_enabled and set dnd_from and dnd_to in 24-hour HH:MM format. The schedule repeats daily in the app timezone. For example, 22:00 to 07:00 mutes Telegram starting at 22:00 and resumes it at 07:00. Matching start and end times leave notifications enabled.

The app keeps collecting telemetry and recording landing events during quiet hours. It skips sonde messages and forecast edits while muted. A sonde may still trigger an alert after quiet hours if it still qualifies at the next check. Health alerts stay queued, and a configured startup test waits until quiet hours end.

## Diagnostics

Open **Diagnostics** to run integration checks for the SondeHub WebSocket and forecast API, Home Assistant zone, and local SQLite archive. **Send Telegram test** sends a real message to the configured chat and reports the outcome. The test respects Do Not Disturb. These checks do not create flight events.

## Event pagination

The History events table defaults to 25 rows per page. Choose 25, 50, or 100 with **Events per page**. Previous and Next retain the active filters and page size; applying filters returns to the first page. SQLite counts matching events separately and fetches only the requested page for the table and compact map. The Full map continues to show all matching events.

## Flight distance chart

The Flight page plots travelled horizontal ground-track distance, remaining forecast route distance, and straight-line distance from the first archived GPS fix to predicted landing, against elapsed time. The first archived fix may be after launch. Calculations use every valid archived GPS fix, not the sampled map route. Gaps over five minutes are excluded, so travelled distance is a lower bound. Each forecast is plotted at its issue time; missing paths do not become zero-distance estimates. Hover over chart points for values. Reload to include new data.
