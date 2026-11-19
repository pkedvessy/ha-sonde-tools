(() => {
    const container = document.getElementById("full-map");
    if (!window.L) {
        if (!container.children.length) {
            container.textContent = "The interactive map could not load. The History tab still has map links.";
        }
        return;
    }
    const {center, radius, notifyRadius, events, tiles, compact, flight, heatmap, corridor} = JSON.parse(document.getElementById("map-data").textContent);
    container.replaceChildren();
    const start = flight?.tracks[0]?.latest.position || flight?.forecasts.at(-1)?.position || center || heatmap?.[0]?.position || corridor?.[0]?.[0] || events[0].position;
    const map = L.map(container, {preferCanvas: true, scrollWheelZoom: !compact}).setView(start, 10);
    const status = document.getElementById("basemap-status");
    const unavailable = "Basemap unavailable. Event markers and search circles are still shown.";
    if (tiles && tiles.style) {
        try {
            if (!L.maplibreGL || !window.maplibregl) throw new Error("MapLibre unavailable");
            const layer = L.maplibreGL({
                style: tiles.style,
                attributionControl: {customAttribution: '<a href="https://openfreemap.org/">OpenFreeMap</a> · ' +
                    '<a href="https://openmaptiles.org/">OpenMapTiles</a> · ' +
                    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}
            }).addTo(map);
            const vectorMap = layer.getMaplibreMap();
            vectorMap.on("error", () => { status.textContent = unavailable; });
            vectorMap.on("load", () => { status.textContent = ""; });
        } catch (error) {
            status.textContent = unavailable;
        }
    } else if (tiles) {
        const layer = L.tileLayer(tiles.url, {
            maxZoom: 19, attribution: tiles.attribution, referrerPolicy: "strict-origin-when-cross-origin"
        });
        layer.once("tileerror", () => {
            layer.remove();
            status.textContent = unavailable;
        });
        layer.addTo(map);
    } else {
        status.textContent = "No basemap configured. Select an event marker to open its street map.";
    }
    const bounds = L.latLngBounds(events.map(event => event.position));
    if (center && !flight) {
        const circle = L.circle(center, {radius, weight: 2, fillOpacity: 0.06}).addTo(map);
        bounds.extend(circle.getBounds());
        if (notifyRadius !== radius) {
            const notify = L.circle(center, {radius: notifyRadius, dashArray: "6 5", fill: false}).addTo(map);
            bounds.extend(notify.getBounds());
        }
    }
    if (bounds.isValid()) map.fitBounds(bounds, {padding: [24, 24], maxZoom: 15});
    document.addEventListener("DOMContentLoaded", () => {
        map.invalidateSize({pan: false});
        if (compact && bounds.isValid()) map.fitBounds(bounds, {padding: [24, 24], maxZoom: 15});
    }, {once: true});
    function sondeMarker(position, color, glyph, popup, destination) {
        const badge = document.createElement("div");
        badge.className = "map-pin-badge";
        badge.style.setProperty("--pin-color", color);
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("fill", "none");
        svg.setAttribute("stroke", "currentColor");
        svg.setAttribute("stroke-width", "2");
        svg.setAttribute("stroke-linecap", "round");
        svg.setAttribute("stroke-linejoin", "round");
        const path = document.createElementNS(svg.namespaceURI, "path");
        const shapes = {
            sonde: "M12 3a6 6 0 0 0-6 6c0 4 4 7 6 9 2-2 6-5 6-9a6 6 0 0 0-6-6ZM10 18h4l1 3H9l1-3Z",
            target: "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 5v6m-3-3h6",
            found: "m5 12 4 4L19 6",
            fix: "M19 10c0 4-7 10-7 10S5 14 5 10a7 7 0 1 1 14 0Zm-7-2v4",
        };
        path.setAttribute("d", shapes[glyph]);
        svg.append(path);
        badge.append(svg);
        const pin = L.divIcon({className: "map-pin", html: badge, iconSize: [28, 28], iconAnchor: [14, 14], popupAnchor: [0, -12]});
        return L.marker(position, {icon: pin, title: popup.textContent}).bindPopup(popup).addTo(destination);
    }

    function appendPositionLinks(popup, serial, position, osmUrl) {
        const links = document.createElement("div");
        const googleQuery = new URLSearchParams({api: "1", query: position.join(",")});
        const destinations = [
            ["SondeHub", "https://sondehub.org/" + encodeURIComponent(serial)],
            ["Google Maps", "https://www.google.com/maps/search/?" + googleQuery],
            ["Flight", "?" + new URLSearchParams({tab: "flight", serial})],
            ["Recovery Mode", "?" + new URLSearchParams({tab: "recovery", serial})]
        ];
        if (osmUrl) destinations.push(["OpenStreetMap", osmUrl]);
        for (const [label, url] of destinations) {
            const link = document.createElement("a");
            link.href = url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = label;
            links.append(link, document.createTextNode(" "));
        }
        popup.append(links);
    }

    function showAltitudeProfile(track) {
        const svg = document.getElementById("altitude-profile");
        const slider = document.getElementById("profile-index");
        const caption = document.getElementById("profile-caption");
        const fixes = track?.profile.filter(fix => fix.altitude != null && Number.isFinite(Number(fix.altitude))) || [];
        if (!fixes.length) {
            svg.hidden = true;
            document.getElementById("profile-control").hidden = true;
            caption.textContent = "No archived altitude data.";
            return;
        }
        const first = fixes[0];
        const last = fixes.at(-1);
        const peak = fixes.reduce((highest, fix) => Number(fix.altitude) > Number(highest.altitude) ? fix : highest);
        const low = fixes.reduce((min, fix) => Math.min(min, Number(fix.altitude)), Number(first.altitude));
        const x = fix => 24 + 592 * (fix.observed - first.observed) / Math.max(1, last.observed - first.observed);
        const y = fix => 134 - 108 * (Number(fix.altitude) - low) / Math.max(1, Number(peak.altitude) - low);
        const make = (tag, attrs) => {
            const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
            for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
            svg.append(element);
            return element;
        };
        make("path", {d: "M24 20V134H616", stroke: "currentColor", opacity: ".35", fill: "none"});
        let path = "";
        let previous = null;
        for (const fix of fixes) {
            const gap = previous && fix.observed - previous.observed > 300;
            path += (previous && !gap ? "L" : "M") + x(fix) + "," + y(fix);
            if (gap) make("line", {x1: x(fix), x2: x(fix), y1: 24, y2: 134,
                stroke: "currentColor", opacity: ".35", "stroke-dasharray": "4 4"});
            previous = fix;
        }
        make("path", {d: path, stroke: "#7851c8", "stroke-width": 3, fill: "none"});
        const peakIndex = fixes.indexOf(peak);
        const labels = [
            [peak, fixes.slice(peakIndex + 1).some(fix => Number(fix.verticalSpeed) < -1) ? "Possible burst" : "Highest fix"],
            [last, "Last fix"]
        ];
        for (const [fix, label] of labels) {
            make("circle", {cx: x(fix), cy: y(fix), r: 4, fill: "#7851c8"});
            const text = make("text", {x: x(fix), y: fix === last ? 155 : 15, fill: "currentColor",
                "text-anchor": x(fix) > 480 ? "end" : "start", "font-size": 12});
            text.textContent = label;
        }
        const cursor = make("circle", {r: 5, fill: "#c82568"});
        const marker = L.circleMarker(last.position, {color: "#c82568", radius: 6}).addTo(map);
        slider.max = fixes.length - 1;
        slider.value = fixes.length - 1;
        const selectFix = index => {
            const fix = fixes[index];
            if (!fix) {
                marker.remove();
                cursor.setAttribute("visibility", "hidden");
                caption.textContent = "No observed fix yet";
                return;
            }
            if (!map.hasLayer(marker)) marker.addTo(map);
            marker.setLatLng(fix.position);
            cursor.removeAttribute("visibility");
            cursor.setAttribute("cx", x(fix));
            cursor.setAttribute("cy", y(fix));
            caption.textContent = new Date(fix.observed * 1000).toLocaleString() + " · " + fix.altitude + " m";
        };
        slider.addEventListener("input", () => selectFix(Number(slider.value)));
        svg.addEventListener("click", event => {
            const bounds = svg.getBoundingClientRect();
            const clicked = (event.clientX - bounds.left) * 640 / bounds.width;
            const best = fixes.reduce((index, fix, i) => Math.abs(x(fix) - clicked) < Math.abs(x(fixes[index]) - clicked) ? i : index, 0);
            slider.value = best;
            selectFix(best);
        });
        selectFix(fixes.length - 1);
        return {fixes, burst: labels[0][1] === "Possible burst" ? peak.observed : null,
            select(index) { slider.value = Math.max(0, index); selectFix(index); }};
    }

    if (flight) {
        let latest = flight.latest;
        function showFlightCard() {
            const card = document.getElementById("flight-card");
            if (!latest) {
                card.textContent = "No observed GPS fix.";
                return;
            }
            const now = Date.now() / 1000;
            const age = Math.max(0, Math.round(now - latest.observed));
            const speed = latest.verticalSpeed == null ? null : Number(latest.verticalSpeed);
            const motion = speed == null || !Number.isFinite(speed) ? "Motion unknown" :
                speed > 1 ? "Ascending" : speed < -1 ? "Descending" : "Low vertical speed";
            const parts = [age <= 180 ? motion : "Last known · " + motion];
            if (latest.altitude != null) parts.push(latest.altitude + " m altitude");
            if (speed != null && Number.isFinite(speed)) parts.push(speed.toFixed(1) + " m/s vertical");
            if (center) parts.push((map.distance(center, latest.position) / 1000).toFixed(1) + " km from zone center");
            if (latest.forecastEta > now) parts.push("ETA " + new Date(latest.forecastEta * 1000).toLocaleString());
            parts.push("GPS fix " + (age < 120 ? age + " s" : Math.floor(age / 60) + " min") + " ago");
            card.textContent = parts.join(" · ");
        }
        showFlightCard();
        window.setInterval(showFlightCard, 1000);
        let refreshing = false;
        window.setInterval(async () => {
            if (document.hidden || refreshing) return;
            refreshing = true;
            try {
                const query = new URLSearchParams({latest: "1", serial: flight.serial});
                const response = await fetch("?" + query, {cache: "no-store"});
                if (response.ok) {
                    latest = await response.json();
                    showFlightCard();
                }
            } catch (error) {
                // Keep the last fix and let its age continue to update.
            } finally {
                refreshing = false;
            }
        }, 30000);

        const observed = L.latLngBounds([]);
        const track = flight.tracks[0];
        if (track) {
            for (const segment of track.segments) {
                L.polyline(segment, {color: "#7851c8", weight: 3}).addTo(map);
                observed.extend(L.latLngBounds(segment));
            }
            const popup = document.createElement("div");
            popup.textContent = "Last observed GPS fix";
            appendPositionLinks(popup, flight.serial, track.latest.position);
            L.circleMarker(track.latest.position, {color: "#7851c8"}).bindPopup(popup).addTo(map);
            if (flight.limited) {
                document.getElementById("track-status").textContent = "Showing the newest 12,000 observed route points.";
            }
        }
        const profile = showAltitudeProfile(track);
        if (flight.recovery) {
            const popup = document.createElement("div");
            popup.textContent = "Reported recovery";
            appendPositionLinks(popup, flight.serial, flight.recovery);
            sondeMarker(flight.recovery, "#008678", "found", popup, map);
            observed.extend(flight.recovery);
        }
        if (observed.isValid()) map.fitBounds(observed, {padding: [24, 24], maxZoom: 15});
        const slider = document.getElementById("forecast-snapshot");
        let selectForecast = () => {};
        if (slider) {
            const caption = document.getElementById("forecast-caption");
            const predicted = L.layerGroup().addTo(map);
            const drift = L.layerGroup().addTo(map);
            const spread = L.layerGroup().addTo(map);
            L.control.layers({}, {"Forecast spread": spread}).addTo(map);
            const scoring = flight.recovery ? flight.forecasts.filter(forecast => forecast.score) : [];
            const bestError = scoring.reduce((best, forecast) =>
                Math.min(best, map.distance(forecast.position, flight.recovery)), Infinity);
            let selected = 0;
            async function showForecast(fit) {
                const request = ++selected;
                const index = Number(slider.value);
                const forecast = flight.forecasts[index];
                caption.textContent = forecast.label + " · ETA " + forecast.eta;
                drift.clearLayers();
                spread.clearLayers();
                const earlier = [];
                for (let i = index - 1; i >= 0; i--) {
                    if (flight.forecasts[i + 1].issued - flight.forecasts[i].issued > 7200) break;
                    earlier.unshift(flight.forecasts[i]);
                }
                for (const snapshot of earlier) {
                    const dot = L.circleMarker(snapshot.position, {radius: 3, color: "#2465d0", opacity: 0.45, fillOpacity: 0.35});
                    const label = document.createElement("span");
                    label.textContent = snapshot.label;
                    dot.bindTooltip(label).addTo(drift);
                }
                const recent = earlier.find(snapshot => forecast.issued - snapshot.issued <= 1200);
                const recentPositions = earlier.filter(snapshot => forecast.issued - snapshot.issued <= 1200);
                if (recentPositions.length) {
                    const spreadRadius = recentPositions.reduce((largest, snapshot) =>
                        Math.max(largest, map.distance(snapshot.position, forecast.position)), 0);
                    if (spreadRadius > 0) {
                        L.circle(forecast.position, {radius: spreadRadius, color: "#2465d0", weight: 1,
                            fillOpacity: 0.12}).bindTooltip("Forecast spread (not a confidence area)").addTo(spread);
                        caption.textContent += " · Spread " + (spreadRadius / 1000).toFixed(2) + " km";
                    }
                }
                if (recent) {
                    const delta = map.distance(recent.position, forecast.position);
                    const radians = Math.PI / 180;
                    const a = recent.position[0] * radians;
                    const b = forecast.position[0] * radians;
                    const lon = (forecast.position[1] - recent.position[1]) * radians;
                    const bearing = Math.atan2(Math.sin(lon) * Math.cos(b),
                        Math.cos(a) * Math.sin(b) - Math.sin(a) * Math.cos(b) * Math.cos(lon));
                    const direction = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][
                        (Math.round(bearing / radians / 45) + 8) % 8];
                    const minutes = Math.max(1, Math.round((forecast.issued - recent.issued) / 60));
                    caption.textContent += " · Drift " + (delta / 1000).toFixed(1) + " km" +
                        (delta >= 50 ? " " + direction : "") + " over " + minutes + " min";
                }
                if (forecast.score) {
                    caption.textContent += " · " + (map.distance(forecast.position, flight.recovery) / 1000).toFixed(2) +
                        " km from reported recovery (best " + (bestError / 1000).toFixed(2) + " km)";
                }
                predicted.clearLayers();
                const popup = document.createElement("div");
                popup.textContent = "Predicted landing · " + forecast.eta;
                appendPositionLinks(popup, flight.serial, forecast.position);
                sondeMarker(forecast.position, "#2465d0", "target", popup, predicted);
                const visible = L.latLngBounds([forecast.position]);
                if (observed.isValid()) visible.extend(observed);
                if (forecast.hasPath) {
                    try {
                        const query = new URLSearchParams({path: "1", serial: flight.serial, issued: forecast.issued});
                        const response = await fetch("?" + query);
                        if (!response.ok) throw new Error("Forecast path unavailable");
                        const segments = await response.json();
                        if (request !== selected) return;
                        for (const segment of segments) {
                            L.polyline(segment, {color: "#2465d0", weight: 3, dashArray: "6 5"}).addTo(predicted);
                            visible.extend(L.latLngBounds(segment));
                        }
                    } catch (error) {
                        if (request !== selected) return;
                        caption.textContent += " · Route unavailable";
                    }
                } else {
                    caption.textContent += " · Landing point only";
                }
                if (fit) map.fitBounds(visible, {padding: [24, 24], maxZoom: 15});
            }
            selectForecast = index => {
                if (index < 0) {
                    selected++;
                    predicted.clearLayers();
                    drift.clearLayers();
                    spread.clearLayers();
                    caption.textContent = "No forecast issued yet";
                } else {
                    slider.value = index;
                    showForecast(false);
                }
            };
            slider.addEventListener("input", () => showForecast(false));
            showForecast(true);
        }

        const moments = [...new Set([...(profile?.fixes.map(fix => fix.observed) || []),
                                     ...flight.forecasts.map(forecast => forecast.issued)])].sort((a, b) => a - b);
        const play = document.getElementById("replay-button");
        const replayTime = document.getElementById("replay-time");
        if (moments.length > 1) {
            play.hidden = false;
            let timer = null;
            let step = 0;
            let fixIndex = -2;
            let forecastIndex = -2;
            function pause(label) {
                window.clearInterval(timer);
                timer = null;
                play.textContent = label;
            }
            function advance() {
                const at = moments[step++];
                replayTime.textContent = new Date(at * 1000).toLocaleString();
                if (profile) {
                    const index = profile.fixes.findLastIndex(fix => fix.observed <= at);
                    if (index !== fixIndex) profile.select(index);
                    fixIndex = index;
                }
                const index = flight.forecasts.findLastIndex(forecast => forecast.issued <= at);
                if (index !== forecastIndex) selectForecast(index);
                forecastIndex = index;
                if (step === moments.length) {
                    pause("Replay flight");
                } else if (at === profile?.burst) {
                    pause("Resume after possible burst");
                }
            }
            play.addEventListener("click", () => {
                if (timer !== null) {
                    pause("Resume flight");
                    return;
                }
                if (step === moments.length) step = 0;
                fixIndex = -2;
                forecastIndex = -2;
                play.textContent = "Pause flight";
                timer = window.setInterval(advance, 500);
                advance();
            });
        }
    }
    if (corridor) {
        const footprint = L.latLngBounds([]);
        for (const route of corridor) {
            L.polyline(route, {color: "#008678", weight: 3, opacity: 0.65}).addTo(map);
            L.circleMarker(route.at(-1), {color: "#008678", radius: 4}).addTo(map);
            footprint.extend(L.latLngBounds(route));
        }
        L.circleMarker(corridor[0][0], {color: "#c82568", radius: 6}).bindTooltip("Last GPS fix").addTo(map);
        map.fitBounds(footprint, {padding: [24, 24], maxZoom: 17});
    }
    if (heatmap) {
        const largest = heatmap.reduce((max, cell) => Math.max(max, cell.count), 1);
        for (const cell of heatmap) {
            const weight = Math.sqrt(cell.count / largest);
            const popup = document.createElement("span");
            popup.textContent = cell.label || cell.count + (cell.count === 1 ? " location" : " locations") + " in this grid cell";
            L.circle(cell.position, {radius: 2800, stroke: Boolean(cell.lastHeard), color: "#b32e37", weight: 2, fillColor: weight > 0.65 ? "#d43b36" : "#f39a23",
                fillOpacity: 0.16 + 0.64 * weight}).bindPopup(popup).addTo(map);
        }
        if (heatmap.length && !center) {
            map.fitBounds(L.latLngBounds(heatmap.map(cell => cell.position)), {padding: [24, 24], maxZoom: 12});
        }
    }
    const landingMarkers = L.layerGroup().addTo(map);
    let displayedEvents;
    renderLandingMarkers(events);

    if (!compact && !flight && !heatmap && !corridor) {
        const trackStatus = document.getElementById("track-status");
        const routes = L.layerGroup().addTo(map);
        const livePositions = L.layerGroup().addTo(map);
        const query = new URLSearchParams(window.location.search);
        query.set("tracks", "1");
        query.set("map_events", "1");
        let updating = false;
        async function refreshTracks() {
            if (updating || document.hidden) return;
            updating = true;
            try {
                const response = await fetch("?" + query.toString(), {cache: "no-store"});
                if (!response.ok) throw new Error("Track request failed");
                const {tracks, limited, events: freshEvents} = await response.json();
                if (freshEvents) renderLandingMarkers(freshEvents);
                const count = document.getElementById("map-event-count");
                if (count && freshEvents) count.textContent = freshEvents.length;
                routes.clearLayers();
                livePositions.clearLayers();
                let liveCount = 0;
                for (const track of tracks) {
                    const label = document.createElement("span");
                    label.textContent = track.serial;
                    for (const segment of track.segments) {
                        if (segment.length < 2) continue;
                        L.polyline(segment, {color: "#7851c8", weight: 3, opacity: 0.8})
                            .bindTooltip(label.cloneNode(true)).addTo(routes);
                    }
                    if (!track.latest.live) continue;
                    liveCount++;
                    const popup = document.createElement("div");
                    const time = new Date(track.latest.observed * 1000).toLocaleString();
                    const altitude = track.latest.altitude == null ? "" : " · " + track.latest.altitude + " m";
                    popup.textContent = track.serial + " · GPS fix " + time + altitude;
                    appendPositionLinks(popup, track.serial, track.latest.position);
                    sondeMarker(track.latest.position, "#c82568", "sonde", popup, livePositions);
                }
                trackStatus.textContent = liveCount + " live positions · " + tracks.length + " archived tracks" +
                    (limited ? " · Showing the newest 12,000 track points. Narrow the filters for older points." : "");
            } catch (error) {
                livePositions.clearLayers();
                trackStatus.textContent = "Track refresh failed. Displayed routes may be out of date.";
            } finally {
                updating = false;
            }
        }
        L.control.layers({}, {"Observed routes": routes, "Live positions": livePositions}).addTo(map);
        refreshTracks();
        window.setInterval(refreshTracks, 30000);
        document.addEventListener("visibilitychange", refreshTracks);
    }

    function renderLandingMarkers(items) {
        const signature = JSON.stringify(items);
        if (signature === displayedEvents) return;
        displayedEvents = signature;
        landingMarkers.clearLayers();
        for (const event of items) {
            const popup = document.createElement("div");
            const title = document.createElement("strong");
            title.textContent = event.serial;
            popup.append(title);
            for (const value of [event.kind, event.status, event.time].filter(Boolean)) {
                const line = document.createElement("div");
                line.textContent = value;
                popup.append(line);
            }
            appendPositionLinks(popup, event.serial, event.position, event.mapUrl);
            const glyph = event.status === "Recovered" ? "found" : event.kind === "Predicted landing" ? "target" :
                event.kind === "Latest saved GPS fix" ? "sonde" : "fix";
            sondeMarker(event.position, event.color, glyph, popup, landingMarkers);
        }
    }
})();
