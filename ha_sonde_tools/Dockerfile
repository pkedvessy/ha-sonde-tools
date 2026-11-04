FROM python:3.13-alpine
RUN apk add --no-cache tzdata
RUN pip install --no-cache-dir paho-mqtt==2.1.0
COPY notifier.py landing_history.py telemetry_stream.py field_tools.py health_alerts.py backfill.py archive_storage.py app_version.py interface.css config.yaml /
CMD ["python", "-u", "/notifier.py"]
