# Sonde Tools for Home Assistant

Get a Telegram message when a weather radiosonde is predicted to land near a Home Assistant zone. A second alert can follow when reception ends during a low-altitude descent. Each message includes the sonde serial, distance from the zone center, event time, and links to OpenStreetMap and SondeHub.

Sonde Tools is an independent Home Assistant app. It uses public telemetry, forecasts, and recovery reports from [SondeHub](https://sondehub.org/) and your existing Home Assistant Telegram Bot integration. A local radio receiver is optional.

## Install on Home Assistant OS

1. Set up the [Telegram Bot integration](https://www.home-assistant.io/integrations/telegram_bot/) and allow the chat you want to receive alerts. Copy the chat's `notify.*` entity ID from the integration.
2. In Home Assistant, open **Settings → Apps → App store**. Open the three-dot menu, select **Repositories**, and add `https://github.com/pkedvessy/ha-sonde-tools`.
3. Install **Sonde Tools**. The app supports Home Assistant OS on 64-bit x86 and ARM.
4. In the app's **Configuration** tab, set `telegram_entity` to the entity ID from step 1. Set `radius_km` to your search radius. The default is 30 km around `zone.home`.
5. Enable `startup_test`, save, and start the app. Once the test message arrives, disable `startup_test`, save, and restart.

For all settings, alert rules, and troubleshooting, see the [configuration guide](ha_sonde_tools/DOCS.md). You can also read it in the app's **Documentation** tab.

Sonde Tools uses the ha_sonde_tools app ID and starts with its own settings and archive. If you ran the previous app, copy any settings you want to reuse and stop it before starting Sonde Tools to avoid duplicate alerts. Use Backfill to populate the new archive.

## What the alerts tell you

- **Predicted landing** means a fresh forecast places the landing point inside your radius. It can arrive while the sonde is still outside the circle. Forecasts can shift as SondeHub receives new data.
- **Estimated landing after signal loss** projects the final descent to your configured ground elevation and checks the estimated touchdown against your radius. If a projection is unavailable, **Signal lost during descent** uses the last GPS fix.

## Browse landing history

Open **Sonde Tools** from the Home Assistant sidebar or the app's Web UI. The app keeps a local archive of in-range events with a map, date filters, and serial search. History starts collecting when the app runs.

## Reporting a problem

Include the installed version, sonde serial, approximate event time with time zone, and the relevant log lines with `debug` enabled. Remove your zone coordinates and any chat details before posting publicly.
