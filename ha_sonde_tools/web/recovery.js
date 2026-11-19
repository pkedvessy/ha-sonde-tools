(() => {
    const button = document.getElementById("location-button");
    const result = document.getElementById("location-result");
    const target = JSON.parse(document.getElementById("recovery-target").textContent);
    let watch = null;
    function stop() {
        if (watch === null) return;
        navigator.geolocation.clearWatch(watch);
        watch = null;
        button.textContent = "Show distance and bearing";
    }
    button.addEventListener("click", () => {
        if (watch !== null) {
            stop();
            result.textContent = "Location sharing stopped.";
            return;
        }
        if (!navigator.geolocation || !window.isSecureContext) {
            result.textContent = "Location requires HTTPS and browser permission. Navigation links remain available.";
            return;
        }
        button.textContent = "Stop using location";
        watch = navigator.geolocation.watchPosition(({coords}) => {
            const rad = Math.PI / 180;
            const a = coords.latitude * rad;
            const b = target[0] * rad;
            const lon = (target[1] - coords.longitude) * rad;
            const h = Math.sin((b - a) / 2) ** 2 + Math.cos(a) * Math.cos(b) * Math.sin(lon / 2) ** 2;
            const km = 12742 * Math.asin(Math.min(1, Math.sqrt(h)));
            const bearing = Math.atan2(Math.sin(lon) * Math.cos(b),
                Math.cos(a) * Math.sin(b) - Math.sin(a) * Math.cos(b) * Math.cos(lon));
            result.textContent = (km < 1 ? Math.round(km * 1000) + " m" : km.toFixed(2) + " km") +
                " · bearing " + ((Math.round(bearing / rad) + 360) % 360) + "° · GPS ±" +
                Math.round(coords.accuracy) + " m";
        }, () => {
            stop();
            result.textContent = "Location unavailable. Check browser permissions or use the navigation link.";
        }, {enableHighAccuracy: true, maximumAge: 10000, timeout: 15000});
    });
    window.addEventListener("pagehide", stop);
})();
