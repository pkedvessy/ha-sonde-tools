try {
    if (window.parent !== window && !location.search) {
        const serial = new URLSearchParams(window.parent.location.search).get("sonde_tools_flight");
        if (serial && /^[A-Za-z0-9_-]{1,80}$/.test(serial)) {
            location.replace("?" + new URLSearchParams({tab: "flight", serial}));
        }
    }
} catch (error) {
    // An iframe on another origin cannot inspect its parent.
}
