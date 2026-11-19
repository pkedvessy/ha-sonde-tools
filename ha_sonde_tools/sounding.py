"""Atmospheric sounding from archived sonde telemetry: Skew-T, wind profile, and hodograph.

Radiosondes measure temperature, humidity and (some types) pressure, and their GPS motion is the
wind. Readings are averaged into altitude bins from one flight phase, so the charts show the
vertical structure of the atmosphere the sonde measured.
"""
import math
from html import escape

BIN_M = 100
G = 9.80665
RD = 287.05
KAPPA = 0.2857
TEMP_COLOR = "#d1372c"
DEW_COLOR = "#0f8a4b"
WIND_COLOR = "#2465d0"
COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def _value(value, low, high):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and low <= value <= high else None


def dew_point(temp, humidity):
    """Magnus formula over water, in °C."""
    if temp is None or humidity is None or humidity <= 0:
        return None
    gamma = math.log(min(humidity, 100) / 100) + 17.62 * temp / (243.12 + temp)
    return 243.12 * gamma / (17.62 - gamma)


def isa_pressure(altitude):
    """International Standard Atmosphere pressure in hPa (troposphere and lower stratosphere)."""
    if altitude <= 11000:
        return 1013.25 * (1 - 2.25577e-5 * altitude) ** 5.25588
    return 226.32 * math.exp(-G * (altitude - 11000) / (RD * 216.65))


def compass(degrees):
    return COMPASS[round(degrees / 22.5) % 16]


def _bins(rows):
    bins = {}
    for alt, temp, humidity, pressure, speed, heading in rows:
        bins.setdefault(int(alt // BIN_M), []).append((alt, temp, humidity, pressure, speed, heading))
    return bins


def _mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def levels_from(rows):
    """Average fixes into altitude bins; returns levels ordered from the ground up."""
    levels = []
    for _, samples in sorted(_bins(rows).items()):
        alt = _mean([sample[0] for sample in samples])
        temp = _mean([sample[1] for sample in samples])
        humidity = _mean([sample[2] for sample in samples])
        pressure = _mean([sample[3] for sample in samples])
        winds = [(sample[4], sample[5]) for sample in samples if sample[4] is not None and sample[5] is not None]
        u = v = None
        if winds:
            # Average wind as a vector; the sonde moves toward its heading, so this is where the air goes.
            u = sum(speed * math.sin(math.radians(heading)) for speed, heading in winds) / len(winds)
            v = sum(speed * math.cos(math.radians(heading)) for speed, heading in winds) / len(winds)
        levels.append({"alt": alt, "temp": temp, "dew": dew_point(temp, humidity), "humidity": humidity,
                       "pressure": pressure, "u": u, "v": v})
    return levels


def fill_pressure(levels):
    """Use measured pressure when most levels have it; otherwise integrate hydrostatically."""
    measured = sum(level["pressure"] is not None for level in levels)
    if levels and measured >= 0.8 * len(levels):
        for previous, level in zip(levels, levels[1:]):
            if level["pressure"] is None:
                level["pressure"] = previous["pressure"]
        if levels[0]["pressure"] is None:
            levels[0]["pressure"] = next(level["pressure"] for level in levels if level["pressure"] is not None)
        return "sensor"
    previous = None
    for level in levels:
        if previous is None:
            level["pressure"] = isa_pressure(level["alt"])
        else:
            temps = [t for t in (previous["temp"], level["temp"]) if t is not None]
            kelvin = (sum(temps) / len(temps) + 273.15) if temps else 288.15 - 6.5 * min(level["alt"], 11000) / 1000
            level["pressure"] = previous["pressure"] * math.exp(-G * (level["alt"] - previous["alt"]) / (RD * kelvin))
        previous = level
    return "derived"


def read(db, serial):
    """Read and summarize one flight's sounding, or None without usable fixes."""
    rows = []
    for alt, temp, humidity, pressure, speed, heading in db.execute("""
        SELECT alt, temp, humidity, pressure, vel_h, heading FROM telemetry_fixes
        WHERE serial = ? ORDER BY observed
    """, (serial.upper(),)):
        alt = _value(alt, -500, 50000)
        if alt is None:
            continue
        rows.append((alt, _value(temp, -110, 60), _value(humidity, 0, 100), _value(pressure, 1, 1100),
                     _value(speed, 0, 150), _value(heading, 0, 360)))
    if len(rows) < 5:
        return None
    burst = max(range(len(rows)), key=lambda index: rows[index][0])
    ascent, descent = rows[:burst + 1], rows[burst:]
    phase, selected = "ascent", ascent
    if len(_bins(descent)) > max(len(_bins(ascent)), 9):
        phase, selected = "descent", descent
    levels = levels_from(selected)
    if len(levels) < 5:
        return None
    source = fill_pressure(levels)
    return {"phase": phase, "levels": levels, "pressure_source": source, "burst": rows[burst][0],
            "burst_observed": burst < len(rows) - 1 and burst > 0}


def tropopause(levels):
    """WMO lapse-rate tropopause: lowest level above 500 hPa where the lapse rate drops to 2 K/km
    and stays at or below 2 K/km on average over the next 2 km."""
    temps = [level for level in levels if level["temp"] is not None]
    for index, level in enumerate(temps[:-1]):
        if level["pressure"] > 500:
            continue
        above = [other for other in temps[index + 1:] if other["alt"] - level["alt"] <= 2000]
        if not above or temps[-1]["alt"] - level["alt"] < 2000:
            return None
        if all((level["temp"] - other["temp"]) / ((other["alt"] - level["alt"]) / 1000) <= 2 for other in above):
            return level
    return None


def freezing_level(levels):
    temps = [level for level in levels if level["temp"] is not None]
    if not temps or temps[0]["temp"] <= 0:
        return None
    for lower, upper in zip(temps, temps[1:]):
        if lower["temp"] > 0 >= upper["temp"]:
            fraction = lower["temp"] / (lower["temp"] - upper["temp"])
            return lower["alt"] + fraction * (upper["alt"] - lower["alt"])
    return None


def summary(data):
    levels = data["levels"]
    winds = [(math.hypot(level["u"], level["v"]), level) for level in levels if level["u"] is not None]
    temps = [level for level in levels if level["temp"] is not None]
    result = {"tropopause": tropopause(levels), "freezing": freezing_level(levels),
              "coldest": min(temps, key=lambda level: level["temp"]) if temps else None,
              "jet": max(winds, key=lambda item: item[0]) if winds else None}
    return result


def _wind_from(level):
    # Meteorological direction: where the wind blows from.
    return (math.degrees(math.atan2(level["u"], level["v"])) + 180) % 360


def skew_t(data, notes):
    levels = data["levels"]
    lowest = min(level["pressure"] for level in levels)
    bottom_p = 1050
    top_p = 100 if lowest >= 90 else 10
    left, right, top, bottom = 50, 590, 15, 455
    width, height = right - left, bottom - top
    span = math.log(bottom_p / top_p)
    t_min, t_max = -40, 50

    def y(p):
        return bottom - math.log(bottom_p / p) / span * height

    def x(temp, p):
        return left + (temp - t_min) / (t_max - t_min) * width + (bottom - y(p))

    svg = [f'<svg class="sounding-chart" viewBox="0 0 640 490" role="img" aria-label="Skew-T log-P diagram">'
           '<title>Skew-T log-P diagram of temperature and dew point</title>'
           f'<defs><clipPath id="skewt-clip"><rect x="{left}" y="{top}" width="{width}" height="{height}"/></clipPath></defs>'
           f'<rect x="{left}" y="{top}" width="{width}" height="{height}" fill="none" stroke="currentColor" opacity=".35"/>'
           '<g clip-path="url(#skewt-clip)">']
    for temp in range(-150, 60, 10):
        svg.append(f'<path d="M{x(temp, bottom_p):.1f} {y(bottom_p):.1f}L{x(temp, top_p):.1f} {y(top_p):.1f}" '
                   f'stroke="currentColor" opacity="{.3 if temp == 0 else .12}"/>')
    pressures = [p for p in (1000, 850, 700, 500, 400, 300, 200, 150, 100, 70, 50, 30, 20, 10) if p >= top_p]
    steps = [bottom_p * (top_p / bottom_p) ** (i / 40) for i in range(41)]
    for theta in range(250, 560, 10):
        path = "M" + "L".join(f"{x(theta * (p / 1000) ** KAPPA - 273.15, p):.1f} {y(p):.1f}" for p in steps)
        svg.append(f'<path d="{path}" fill="none" stroke="#d97706" stroke-dasharray="3 4" opacity=".28"/>')
    for p in pressures:
        svg.append(f'<path d="M{left} {y(p):.1f}H{right}" stroke="currentColor" opacity=".15"/>')
    for key, color, label in (("temp", TEMP_COLOR, "Temperature"), ("dew", DEW_COLOR, "Dew point")):
        points, segments = [], []
        for level in levels:
            if level[key] is None:
                if points:
                    segments.append(points)
                points = []
                continue
            points.append(f"{x(level[key], level['pressure']):.1f} {y(level['pressure']):.1f}")
        if points:
            segments.append(points)
        path = " ".join("M" + "L".join(segment) for segment in segments if len(segment) > 1)
        if path:
            svg.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linejoin="round">'
                       f'<title>{label}</title></path>')
    info = notes
    for key, color, text in (("tropopause", "#7851c8", "Tropopause"), ("freezing", "#2465d0", "0 °C")):
        value = info.get(key)
        if value is None:
            continue
        alt = value["alt"] if isinstance(value, dict) else value
        p = min(levels, key=lambda level: abs(level["alt"] - alt))["pressure"]
        svg.append(f'<path d="M{left} {y(p):.1f}H{right}" stroke="{color}" stroke-width="1.5" stroke-dasharray="7 4"/>'
                   f'<text x="{right - 6}" y="{y(p) - 5:.1f}" text-anchor="end" fill="{color}">{text} · {alt / 1000:.1f} km</text>')
    svg.append("</g>")
    for p in pressures:
        svg.append(f'<text x="{left - 6}" y="{y(p) + 4:.1f}" text-anchor="end">{p}</text>')
    for temp in range(-40, 51, 10):
        svg.append(f'<text x="{x(temp, bottom_p):.1f}" y="{bottom + 15}" text-anchor="middle">{temp}</text>')
    for km in range(0, 50, 5 if top_p == 10 else 2):
        nearest = min(levels, key=lambda level: abs(level["alt"] - km * 1000))
        if abs(nearest["alt"] - km * 1000) <= 300:
            svg.append(f'<text x="{right + 6}" y="{y(nearest["pressure"]) + 4:.1f}">{km} km</text>')
    svg.append(f'<text x="{left}" y="{bottom + 32}">hPa (left) · °C (bottom, skewed 45°) · '
               'dashed orange: dry adiabats</text></svg>')
    return "".join(svg)


def wind_profile(data, notes):
    winds = [level for level in data["levels"] if level["u"] is not None]
    if len(winds) < 3:
        return ""
    top_km = max(level["alt"] for level in winds) / 1000
    fastest = max(math.hypot(level["u"], level["v"]) for level in winds)
    maximum = max(10, math.ceil(fastest / 10) * 10)
    left, right, top, bottom = 50, 330, 15, 455

    def x(speed):
        return left + speed / maximum * (right - left)

    def y(alt):
        return bottom - alt / 1000 / max(top_km, 1) * (bottom - top)

    svg = ['<svg class="sounding-chart" viewBox="0 0 400 490" role="img" aria-label="Wind speed and direction by altitude">'
           '<title>Wind speed and direction by altitude</title>'
           f'<rect x="{left}" y="{top}" width="{right - left}" height="{bottom - top}" fill="none" stroke="currentColor" opacity=".35"/>']
    for speed in range(0, maximum + 1, 10):
        svg.append(f'<path d="M{x(speed):.1f} {top}V{bottom}" stroke="currentColor" opacity=".12"/>'
                   f'<text x="{x(speed):.1f}" y="{bottom + 15}" text-anchor="middle">{speed}</text>')
    step = 5 if top_km > 12 else 2 if top_km > 4 else 1
    for km in range(0, int(top_km) + 1, step):
        svg.append(f'<path d="M{left} {y(km * 1000):.1f}H{right}" stroke="currentColor" opacity=".12"/>'
                   f'<text x="{left - 6}" y="{y(km * 1000) + 4:.1f}" text-anchor="end">{km}</text>')
    path = "M" + "L".join(f"{x(math.hypot(level['u'], level['v'])):.1f} {y(level['alt']):.1f}" for level in winds)
    svg.append(f'<path d="{path}" fill="none" stroke="{WIND_COLOR}" stroke-width="2.2" stroke-linejoin="round"/>')
    # Direction arrows (pointing where the air moves), about every 500 m.
    spacing = max(500, top_km * 1000 / 30)
    next_alt = winds[0]["alt"]
    for level in winds:
        if level["alt"] < next_alt:
            continue
        next_alt = level["alt"] + spacing
        speed = math.hypot(level["u"], level["v"])
        if speed < 0.5:
            continue
        angle = math.degrees(math.atan2(level["u"], level["v"]))
        direction = _wind_from(level)
        title = escape(f"{level['alt'] / 1000:.1f} km · {speed:.1f} m/s ({speed * 3.6:.0f} km/h) from "
                       f"{direction:.0f}° {compass(direction)}")
        svg.append(f'<g transform="translate({right + 35} {y(level["alt"]):.1f}) rotate({angle:.0f})">'
                   f'<title>{title}</title><path d="M0 7V-7M-4 -2L0 -7L4 -2" fill="none" stroke="{WIND_COLOR}" '
                   'stroke-width="1.6" stroke-linecap="round"/></g>')
    jet = notes.get("jet")
    if jet:
        speed, level = jet
        svg.append(f'<circle cx="{x(speed):.1f}" cy="{y(level["alt"]):.1f}" r="4" fill="{WIND_COLOR}"/>'
                   f'<text x="{min(x(speed), right - 4) - 6:.1f}" y="{y(level["alt"]) - 8:.1f}" text-anchor="end">'
                   f'{speed:.0f} m/s</text>')
    svg.append(f'<text x="{left}" y="{bottom + 32}">km (left) · m/s (bottom) · arrows: air motion, north up</text></svg>')
    return "".join(svg)


def altitude_color(fraction):
    """Blue near the ground through amber to red at the top of the profile."""
    stops = ((0, (36, 101, 208)), (0.5, (217, 119, 6)), (1, (209, 55, 44)))
    for (f0, c0), (f1, c1) in zip(stops, stops[1:]):
        if fraction <= f1:
            t = (fraction - f0) / (f1 - f0) if f1 > f0 else 0
            return "#" + "".join(f"{round(a + (b - a) * t):02x}" for a, b in zip(c0, c1))
    return "#d1372c"


def hodograph(data):
    winds = [level for level in data["levels"] if level["u"] is not None]
    if len(winds) < 3:
        return ""
    fastest = max(math.hypot(level["u"], level["v"]) for level in winds)
    maximum = max(10, math.ceil(fastest / 10) * 10)
    size, middle = 300, 160
    scale = size / 2 / maximum
    top_alt = max(level["alt"] for level in winds) or 1
    svg = ['<svg class="sounding-chart sounding-hodograph" viewBox="0 0 320 340" role="img" '
           'aria-label="Hodograph of wind vectors coloured by altitude"><title>Hodograph</title>']
    for ring in range(10, maximum + 1, 10):
        svg.append(f'<circle cx="{middle}" cy="{middle}" r="{ring * scale:.1f}" fill="none" stroke="currentColor" opacity=".15"/>'
                   f'<text x="{middle + ring * scale * .71 + 2:.1f}" y="{middle - ring * scale * .71 - 2:.1f}" opacity=".7">{ring}</text>')
    svg.append(f'<path d="M{middle - size / 2} {middle}H{middle + size / 2}M{middle} {middle - size / 2}V{middle + size / 2}" '
               'stroke="currentColor" opacity=".25"/>'
               f'<text x="{middle}" y="8" text-anchor="middle">N</text><text x="{middle + size / 2 + 4}" y="{middle + 4}">E</text>')
    previous = None
    for level in winds:
        point = middle + level["u"] * scale, middle - level["v"] * scale
        if previous is not None:
            color = altitude_color(level["alt"] / top_alt)
            svg.append(f'<path d="M{previous[0]:.1f} {previous[1]:.1f}L{point[0]:.1f} {point[1]:.1f}" '
                       f'stroke="{color}" stroke-width="2.4" stroke-linecap="round"/>')
        previous = point
    marked = set()
    for level in winds:
        km = int(level["alt"] // 5000) * 5
        if km and km not in marked and level["alt"] >= km * 1000:
            marked.add(km)
            px, py = middle + level["u"] * scale, middle - level["v"] * scale
            svg.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="currentColor"/>'
                       f'<text x="{px + 5:.1f}" y="{py - 4:.1f}">{km} km</text>')
    svg.append('<text x="10" y="335">Wind vectors in m/s, blue (low) → red (high)</text></svg>')
    return "".join(svg)


def chart(data):
    if not data:
        return ""
    notes = summary(data)
    levels = data["levels"]
    facts = [f"{'Ascent' if data['phase'] == 'ascent' else 'Descent'} profile · {len(levels)} levels "
             f"from {levels[0]['alt'] / 1000:.1f} to {levels[-1]['alt'] / 1000:.1f} km"]
    if data["burst_observed"]:
        facts.append(f"Burst altitude {data['burst'] / 1000:.2f} km")
    ground = next((level for level in levels if level["temp"] is not None), None)
    if ground:
        facts.append(f"Lowest level {ground['temp']:.1f} °C at {ground['alt']:.0f} m")
    if notes["freezing"] is not None:
        facts.append(f"Freezing level {notes['freezing']:.0f} m")
    trop = notes["tropopause"]
    if trop:
        facts.append(f"Tropopause {trop['alt'] / 1000:.1f} km · {trop['pressure']:.0f} hPa · {trop['temp']:.1f} °C")
    if notes["coldest"]:
        facts.append(f"Coldest {notes['coldest']['temp']:.1f} °C at {notes['coldest']['alt'] / 1000:.1f} km")
    if notes["jet"]:
        speed, level = notes["jet"]
        direction = _wind_from(level)
        facts.append(f"Strongest wind {speed:.0f} m/s ({speed * 3.6:.0f} km/h) from {compass(direction)} "
                     f"at {level['alt'] / 1000:.1f} km")
    has_temp = any(level["temp"] is not None for level in levels)
    pressure_note = ("Pressure from the sonde sensor." if data["pressure_source"] == "sensor" else
                     "This sonde reports no pressure, so pressure is derived from GPS altitude and temperature.")
    charts = (skew_t(data, notes) if has_temp else "") + wind_profile(data, notes) + hodograph(data)
    if not charts:
        return ""
    legend = (f'<div class="legend"><span style="color:{TEMP_COLOR}">Temperature</span>'
              f'<span style="color:{DEW_COLOR}">Dew point</span><span style="color:{WIND_COLOR}">Wind</span>'
              '<span style="color:#7851c8">Tropopause</span></div>') if has_temp else ""
    return ('<section class="flight-panel"><h3>Atmospheric sounding</h3>'
            "<p>What this sonde measured on its way through the atmosphere, averaged in "
            f"{BIN_M} m altitude steps. {pressure_note} Wind is the sonde's GPS drift.</p>"
            '<ul class="sounding-facts">' + "".join(f"<li>{escape(fact)}</li>" for fact in facts) + "</ul>"
            + legend + f'<div class="sounding-grid">{charts}</div>'
            "<p>Only archived fixes are used; flights that launched outside the archive radius may show the "
            "descent only. Dew point uses the reported relative humidity.</p></section>")
