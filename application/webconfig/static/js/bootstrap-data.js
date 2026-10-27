// ===================== BOOTSTRAP DATA =====================
// One request for everything the page needs to draw itself, instead of a
// separate round trip per field. Each section is handed out exactly once: a
// later refresh goes to that section's own endpoint, so nothing here can serve
// a stale value after the user has changed something.
const BootstrapData = (() => {
    // Everything the settings page needs to draw itself. The endpoint runs
    // these concurrently, so including the ones that probe hardware or shell
    // out costs about as much as the slowest of them, not the sum.
    const SECTIONS = [
        "config", "hostname", "timezone", "version", "release_note",
        "mic_gain", "volume", "device_info", "audio_devices", "camera",
        "wifi", "ha_agents", "wakeword_keywords", "service",
    ].join(",");
    let pending = null;

    const load = () => {
        if (!pending) {
            pending = fetchWithTimeout(`/ui/bootstrap?include=${SECTIONS}`, 15000)
                .then((res) => (res.ok ? res.json() : {}))
                .catch((error) => {
                    // Not fatal: every caller falls back to its own endpoint.
                    console.error("Failed to load bootstrap data:", error);
                    return {};
                });
        }
        return pending;
    };

    // Resolves to the section's payload the first time it is asked for, and to
    // null every time after that, and also when the section failed server-side.
    const take = async (name) => {
        const all = await load();
        if (!all || !Object.prototype.hasOwnProperty.call(all, name)) return null;
        const value = all[name];
        delete all[name];
        if (!value || value.error) return null;
        return value;
    };

    const reset = () => {
        pending = null;
    };

    return {load, take, reset};
})();

window.BootstrapData = BootstrapData;
