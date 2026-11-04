from pathlib import Path


manifest = Path(__file__).with_name("config.yaml").read_text(encoding="utf-8")
APP_VERSION = next(line.split(":", 1)[1].strip().strip("\"'")
                   for line in manifest.splitlines() if line.startswith("version:"))
USER_AGENT = f"HA-Sonde-Tools/{APP_VERSION}"
