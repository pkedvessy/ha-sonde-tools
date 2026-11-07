"""Archived horizontal flight distances, in kilometres."""
import math
from html import escape


def distance(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(math.radians(b[1]-a[1])/2)**2
    return 12742.0176 * math.asin(math.sqrt(min(1, max(0, h))))


def position(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 360:
            return lat, (lon+180)%360-180
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def read_distances(db, serial):
    actual, previous, origin, start, total, gaps = [], None, None, None, 0.0, 0
    for observed, lat, lon in db.execute("SELECT observed, lat, lon FROM telemetry_fixes WHERE serial=? ORDER BY observed", (serial,)):
        point = position(lat, lon)
        if point is None:
            continue
        if start is None:
            start, origin = observed, point
        gap = previous is not None and observed-previous[0] > 300
        if previous and not gap:
            total += distance(previous[1], point)
        gaps += int(gap)
        actual.append((observed, total, gap))
        previous = observed, point
    if start is None:
        return None
    forecasts = []
    fix_index = 0
    paths = iter(db.execute("SELECT issued, latitude, longitude, observed FROM forecast_points WHERE serial=? ORDER BY issued, step", (serial,)))
    pending = next(paths, None)
    for issued, lat, lon in db.execute("SELECT issued, latitude, longitude FROM forecasts WHERE serial=? ORDER BY issued", (serial,)):
        while pending is not None and pending[0] < issued:
            pending = next(paths, None)
        prior, length, count = None, 0.0, 0
        while pending is not None and pending[0] == issued:
            _, plat, plon, at = pending
            point = position(plat, plon)
            if point is not None and (at is None or at >= issued):
                if prior is not None:
                    length += distance(prior, point)
                prior, count = point, count+1
            pending = next(paths, None)
        landing = position(lat, lon)
        if issued >= start and landing is not None:
            while fix_index + 1 < len(actual) and actual[fix_index + 1][0] <= issued:
                fix_index += 1
            remaining = length if count >= 2 else None
            estimated_total = actual[fix_index][1] + remaining if remaining is not None else None
            forecasts.append((issued, remaining, distance(origin, landing), estimated_total))
    return dict(start=start, actual=actual, forecasts=forecasts, gaps=gaps)


def chart(data):
    if not data:
        return '<p>No archived GPS fixes to calculate flight distance.</p>'
    start = data['start']
    series = [(data['actual'], 1, '#7851c8', 'Travelled', False),
              (data['forecasts'], 1, '#2465d0', 'Estimated remaining', True),
              (data['forecasts'], 2, '#008678', 'Departure to predicted landing', False),
              (data['forecasts'], 3, '#d97706', 'Estimated total flight length', False)]
    end = max([p[0] for p in data['actual']] + [p[0] for p in data['forecasts']])
    maximum = max(1, *(p[key] for rows, key, *_ in series for p in rows if p[key] is not None))
    x = lambda at: 55+640*(at-start)/max(1,end-start)
    y = lambda km: 200-170*km/maximum
    svg = ['<svg class="distance-chart" viewBox="0 0 720 245" role="img" aria-label="Flight distance in kilometres by elapsed flight time"><title>Flight distances over elapsed time</title>']
    for i in range(5):
        km, at = maximum*i/4, start+(end-start)*i/4
        svg.append(f'<path d="M55 {y(km)}H695" stroke="currentColor" opacity=".15"/><text x="48" y="{y(km)+4}" text-anchor="end">{km:.1f}</text><text x="{x(at)}" y="220" text-anchor="middle">{(at-start)/60:.0f}</text>')
    svg.append('<text x="55" y="16">km</text><text x="375" y="240" text-anchor="middle">Minutes from first archived GPS fix</text>')
    for rows, key, color, label, dashed in series:
        # Reduce SVG size only after calculating distances from every archived fix.
        stride = max(1, math.ceil(len(rows)/1500))
        selected = [p for i,p in enumerate(rows) if i%stride==0 or i==len(rows)-1 or (key==1 and label=='Travelled' and (p[2] or (i+1<len(rows) and rows[i+1][2])))]
        path, prior = [], None
        for p in selected:
            if p[key] is None:
                prior = None
                continue
            gap = label=='Travelled' and p[2]
            join = prior is not None and not gap and (label=='Travelled' or p[0]-prior[0]<=1200)
            path.append(('L' if join else 'M')+f'{x(p[0]):.2f},{y(p[key]):.2f}')
            title = escape(f'{label}: {p[key]:.2f} km at {(p[0]-start)/60:.1f} min')
            svg.append(f'<circle cx="{x(p[0]):.2f}" cy="{y(p[key]):.2f}" r="3" fill="{color}"><title>{title}</title></circle>')
            prior = p
        dash = ' stroke-dasharray="6 4"' if dashed else ''
        svg.append(f'<path d="{" ".join(path)}" fill="none" stroke="{color}" stroke-width="2"{dash} pointer-events="none"/>')
    svg.append('</svg>')
    return ('<section class="flight-panel"><h3>Flight distance</h3>'
            '<p>Horizontal distances from the first archived GPS fix, which may be after launch. Hover over chart points for values.</p>'
            '<div class="legend"><span style="color:#7851c8">Travelled</span><span style="color:#2465d0">Estimated remaining (dashed)</span><span style="color:#008678">Departure to predicted landing (straight line)</span><span style="color:#d97706">Estimated total (travelled + remaining)</span></div>'
            + ''.join(svg) + f'<p>GPS gaps over five minutes excluded: {data["gaps"]}. Travelled distance is a lower bound. Estimated total combines the last recorded travelled distance available at each forecast issue time with its remaining path. Missing forecast paths have no remaining or total estimate. Reload to include new data.</p></section>')
