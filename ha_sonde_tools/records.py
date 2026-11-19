"""Records and year-in-review statistics from the local archive."""
import re
import sqlite3
from calendar import monthrange
from contextlib import closing
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import quote

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
FULL_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
FLIGHT_COLOR = "#7851c8"
LANDING_COLOR = "#008678"


def period(year, tz, month=None):
    """Return (start, end) timestamps for a calendar year or month in the app time zone, or all time."""
    if year is None:
        return 0, 253402300799
    if month is None:
        start = datetime(year, 1, 1, tzinfo=tz)
        return start.timestamp(), start.replace(year=year + 1).timestamp()
    start = datetime(year, month, 1, tzinfo=tz)
    end = start.replace(year=year + 1, month=1) if month == 12 else start.replace(month=month + 1)
    return start.timestamp(), end.timestamp()


def available_years(db, tz):
    first, last = db.execute("SELECT min(first_observed), max(first_observed) FROM flight_summaries").fetchone()
    event_first, event_last = db.execute("SELECT min(event_time), max(event_time) FROM events").fetchone()
    stamps = [value for value in (first, last, event_first, event_last) if value is not None]
    if not stamps:
        return []
    low, high = (datetime.fromtimestamp(value, tz).year for value in (min(stamps), max(stamps)))
    return list(range(high, low - 1, -1))


def flights(db, start, end):
    """Per-flight summaries, kept when telemetry retention deletes the raw fixes.

    A flight belongs to the period of its first archived fix.
    """
    return db.execute("""
        SELECT serial, first_observed, last_observed, fixes, max_alt, max_speed, min_vertical, min_temp, type
        FROM flight_summaries WHERE first_observed >= ? AND first_observed < ?
    """, (start, end)).fetchall()


def read(database, year, tz, month=None):
    with closing(sqlite3.connect(database)) as db:
        years = available_years(db, tz)
        start, end = period(year, tz, month)
        rows = flights(db, start, end)
        events = db.execute("""
            SELECT e.serial, e.kind, e.event_time, e.distance_km, r.status, r.reported, e.recorded
            FROM events AS e LEFT JOIN recovery AS r USING (serial)
            WHERE e.event_time >= ? AND e.event_time < ?
        """, (start, end)).fetchall()
    return {"years": years, "year": year, "month": month, "flights": rows, "events": events}


def _best(rows, index, pick=max):
    candidates = [row for row in rows if row[index] is not None]
    return pick(candidates, key=lambda row: row[index]) if candidates else None


def records(data, tz):
    rows = data["flights"]
    result = []

    def add(label, row, value, when):
        if row is not None:
            result.append((label, value, row[0], datetime.fromtimestamp(when, tz).strftime("%Y-%m-%d")))

    row = _best(rows, 4)
    add("Highest altitude", row, row and f"{row[4] / 1000:.2f} km", row and row[1])
    row = _best(rows, 5)
    add("Fastest wind ride", row, row and f"{row[5]:.0f} m/s · {row[5] * 3.6:.0f} km/h", row and row[1])
    row = _best(rows, 6, min)
    add("Fastest descent", row, row and f"{-row[6]:.0f} m/s", row and row[1])
    row = _best(rows, 7, min)
    add("Coldest reading", row, row and f"{row[7]:.1f} °C", row and row[1])
    durations = [(row[0], row[2] - row[1], row[1]) for row in rows if row[3] > 1]
    if durations:
        serial, seconds, when = max(durations, key=lambda item: item[1])
        add("Longest archived track", (serial,), f"{seconds / 3600:.1f} h", when)
    landed = [event for event in data["events"] if event[1] != "Predicted landing"] or data["events"]
    if landed:
        event = min(landed, key=lambda item: item[3])
        add("Closest landing", event, f"{event[3]:.2f} km", event[2])
    return result


def recovery_stats(data):
    serials = {}
    for serial, _, _, _, status, reported, recorded in data["events"]:
        # A recovery counts only when reported for this flight, not an earlier reuse of the serial.
        current = status if status and (reported is None or reported >= recorded - 21600) else None
        if serials.get(serial) != "Recovered":
            serials[serial] = current
    recovered = sum(status == "Recovered" for status in serials.values())
    attempted = sum(status == "Attempt unsuccessful" for status in serials.values())
    return len(serials), recovered, attempted


def bar_chart(labels, series, label):
    """Grouped bars: series is a list of (name, color, values)."""
    # Counts are integers, so round the scale up to a multiple of the four grid steps.
    peak = max([1] + [value for _, _, values in series for value in values])
    maximum = 4 * -(-peak // 4)
    left, bottom, height = 40, 170, 150
    width = max(1, len(labels))
    slot = 640 / width
    bar = slot * 0.8 / len(series)
    svg = [f'<svg class="review-chart" viewBox="0 0 700 200" role="img" aria-label="{escape(label)}"><title>{escape(label)}</title>']
    for step in range(5):
        value = maximum * step / 4
        y = bottom - height * step / 4
        svg.append(f'<path d="M{left} {y:.1f}H{left + 640}" stroke="currentColor" opacity=".13"/>'
                   f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{value:.0f}</text>')
    for index, name in enumerate(labels):
        x0 = left + index * slot + slot * 0.1
        for offset, (series_name, color, values) in enumerate(series):
            value = values[index]
            h = height * value / maximum
            svg.append(f'<rect x="{x0 + offset * bar:.1f}" y="{bottom - h:.1f}" width="{max(bar - 1, 1):.1f}" '
                       f'height="{h:.1f}" rx="2" fill="{color}"><title>{escape(f"{name} · {series_name}: {value}")}</title></rect>')
        if width <= 24 or index % 2 == 0:
            svg.append(f'<text x="{left + (index + .5) * slot:.1f}" y="{bottom + 15}" text-anchor="middle">{escape(str(name))}</text>')
    svg.append("</svg>")
    return "".join(svg)


def calendar(year, counts):
    """GitHub-style day grid: one column per week, Monday at the top."""
    first = date(year, 1, 1)
    offset = first.weekday()
    peak = max(counts.values(), default=0)
    cell = 12
    svg = [f'<svg class="review-chart calendar-chart" viewBox="0 0 {30 + 54 * (cell + 2)} {7 * (cell + 2) + 30}" '
           f'role="img" aria-label="Flights per day in {year}"><title>Flights per day in {year}</title>']
    for index, name in enumerate(("Mon", "", "Wed", "", "Fri", "", "")):
        if name:
            svg.append(f'<text x="0" y="{20 + index * (cell + 2) + cell - 2}">{name}</text>')
    day = first
    while day.year == year:
        position = (day - first).days + offset
        week, weekday = divmod(position, 7)
        count = counts.get(day, 0)
        level = 0 if not count else min(4, 1 + int(3 * count / peak)) if peak > 1 else 2
        x, y = 30 + week * (cell + 2), 20 + weekday * (cell + 2)
        svg.append(f'<rect class="day" data-level="{level}" x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2">'
                   f'<title>{day:%Y-%m-%d}: {count} flight{"s" if count != 1 else ""}</title></rect>')
        if day.day == 1:
            svg.append(f'<text x="{x}" y="12">{MONTHS[day.month - 1]}</text>')
        day += timedelta(days=1)
    svg.append("</svg>")
    return "".join(svg)


def flight_panel(database, serial, tz):
    """Summary facts for one flight; the only record left once retention removed its fixes."""
    with closing(sqlite3.connect(database)) as db:
        row = db.execute("""
            SELECT first_observed, last_observed, fixes, max_alt, max_speed, min_vertical, min_temp, type,
                   EXISTS (SELECT 1 FROM telemetry_fixes WHERE serial = s.serial)
            FROM flight_summaries AS s WHERE serial = ?
        """, (serial.upper(),)).fetchone()
    if row is None:
        return ""
    first, last, fixes, altitude, speed, vertical, temp, kind, has_fixes = row
    stamp = lambda value: datetime.fromtimestamp(value, tz).strftime("%Y-%m-%d %H:%M %Z")  # noqa: E731
    facts = [f"{stamp(first)} to {stamp(last)}", f"{fixes:,} GPS fixes"]
    if kind:
        facts.append(f"Type {kind}")
    if altitude is not None:
        facts.append(f"Highest {altitude / 1000:.2f} km")
    if speed is not None:
        facts.append(f"Fastest wind {speed:.0f} m/s")
    if vertical is not None:
        facts.append(f"Fastest descent {-vertical:.0f} m/s")
    if temp is not None:
        facts.append(f"Coldest {temp:.1f} °C")
    note = ("" if has_fixes else
            "<p>The raw GPS fixes were removed by telemetry retention. This summary was kept.</p>")
    return ('<section class="flight-panel"><h3>Flight summary</h3>' + note + '<ul class="sounding-facts">'
            + "".join(f"<li>{escape(fact)}</li>" for fact in facts) + "</ul></section>")


def view(database, query, tz):
    text = query.get("year", [""])[0]
    year = int(text) if re.fullmatch(r"(19|20)\d\d", text) else None
    month_text = query.get("month", [""])[0]
    # A month only narrows a chosen year; with "All time" it is ignored.
    month = int(month_text) if re.fullmatch(r"[1-9]|1[0-2]", month_text) else None
    if year is None and not text:
        with closing(sqlite3.connect(database)) as db:
            years = available_years(db, tz)
        year = years[0] if years else None
    if year is None:
        month = None
    data = read(database, year, tz, month)
    options = '<option value="all">All time</option>' + "".join(
        f'<option value="{value}" {"selected" if value == year else ""}>{value}</option>' for value in data["years"])
    month_options = '<option value="">All months</option>' + "".join(
        f'<option value="{index}" {"selected" if index == month else ""}>{name}</option>'
        for index, name in enumerate(FULL_MONTHS, 1))
    title = ("all time" if year is None else str(year) if month is None else f"{FULL_MONTHS[month - 1]} {year}")
    heading = ('<h2>Records and year in review</h2>'
               '<form class="form-row" method="get"><input type="hidden" name="tab" value="records">'
               f'<label>Year <select name="year">{options}</select></label>'
               f'<label>Month <select name="month">{month_options}</select></label>'
               f'<button>Show</button></form><p>Showing <strong>{escape(title)}</strong>.'
               + (" Choose a year to filter by month." if year is None else "") + "</p>")
    rows = data["flights"]
    if not rows and not data["events"]:
        return heading + "<p>No archived flights for this period yet.</p>"
    flights_total = len(rows)
    fixes = sum(row[3] for row in rows)
    landings, recovered, attempted = recovery_stats(data)
    rate = f"{recovered / landings:.0%}" if landings else "—"
    tiles = [("Flights archived", f"{flights_total:,}", "distinct serials with GPS fixes"),
             ("GPS fixes", f"{fixes:,}", "saved positions"),
             ("Landings near you", f"{landings:,}", "flights with a landing event"),
             ("Recovered", f"{recovered:,}", f"{rate} of nearby landings" + (f" · {attempted} unsuccessful" if attempted else ""))]
    stats = '<div class="records-grid">' + "".join(
        f'<div class="record"><small>{escape(label)}</small><strong>{value}</strong><span>{escape(note)}</span></div>'
        for label, value, note in tiles) + "</div>"
    record_tiles = '<h3>Records</h3><div class="records-grid">' + "".join(
        f'<div class="record"><small>{escape(label)}</small><strong>{escape(value)}</strong>'
        f'<span><a href="?tab=flight&amp;serial={quote(serial, safe="")}">{escape(serial)}</a> · {when}</span></div>'
        for label, value, serial, when in records(data, tz)) + "</div>"

    first_seen = [datetime.fromtimestamp(row[1], tz) for row in rows]
    landing_days = {}
    for serial, _, when, *_ in data["events"]:
        moment = datetime.fromtimestamp(when, tz)
        landing_days.setdefault(serial, moment)
    if month is not None:
        length = monthrange(year, month)[1]
        labels = list(range(1, length + 1))
        flight_counts = [sum(moment.day == day for moment in first_seen) for day in labels]
        landing_counts = [sum(moment.day == day for moment in landing_days.values()) for day in labels]
        unit = "day"
    elif year is not None:
        labels = list(MONTHS)
        flight_counts = [sum(moment.month == value for moment in first_seen) for value in range(1, 13)]
        landing_counts = [sum(moment.month == value for moment in landing_days.values()) for value in range(1, 13)]
        unit = "month"
    else:
        unit = "year"
        years = sorted({moment.year for moment in first_seen} | {moment.year for moment in landing_days.values()})
        labels = years
        flight_counts = [sum(moment.year == value for moment in first_seen) for value in years]
        landing_counts = [sum(moment.year == value for moment in landing_days.values()) for value in years]
    timeline = (f"<h3>Flights and landings by {unit}</h3>"
                f'<div class="legend"><span style="color:{FLIGHT_COLOR}">Flights archived</span>'
                f'<span style="color:{LANDING_COLOR}">Landings near you</span></div>'
                + bar_chart(labels, [("Flights", FLIGHT_COLOR, flight_counts),
                                     ("Landings", LANDING_COLOR, landing_counts)], "Flights and landings over time"))
    hours = [sum(moment.hour == hour for moment in landing_days.values()) for hour in range(24)]
    hourly = ("<h3>Landing time of day</h3><p>Local time of each nearby landing. Launch schedules make clear peaks.</p>"
              + bar_chart([f"{hour:02d}" for hour in range(24)], [("Landings", LANDING_COLOR, hours)],
                          "Landings by hour of day")) if landing_days else ""
    days = {}
    for moment in first_seen:
        days[moment.date()] = days.get(moment.date(), 0) + 1
    busiest = max(days.items(), key=lambda item: item[1]) if days else None
    calendar_view = ""
    if month is not None and busiest:
        calendar_view = f"<p>Busiest day: {busiest[0]:%Y-%m-%d} with {busiest[1]} flights.</p>"
    elif year is not None and rows:
        calendar_view = (f"<h3>Flight calendar {year}</h3><p>Flights by the day of their first archived fix."
                         + (f" Busiest day: {busiest[0]:%Y-%m-%d} with {busiest[1]} flights." if busiest else "")
                         + "</p>" + calendar(year, days))
    types = {}
    for row in rows:
        types[row[8] or "Unknown"] = types.get(row[8] or "Unknown", 0) + 1
    type_list = ("<h3>Sonde types</h3><ul class=\"sounding-facts\">" + "".join(
        f"<li>{escape(str(name))} · {count:,}</li>" for name, count in sorted(types.items(), key=lambda item: -item[1]))
        + "</ul>") if types else ""
    return (heading + stats + record_tiles + timeline + calendar_view + hourly + type_list
            + "<p>Records use flight summaries saved as fixes arrive inside the archive radius, so a flight's true "
              "maximum may be outside it. Summaries are kept when telemetry retention removes the raw fixes.</p>")
