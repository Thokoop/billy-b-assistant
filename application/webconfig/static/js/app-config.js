// ===================== APP CONFIG (uses centralized ConfigService) =====================
const AppConfig = (() => {
    let cfg = null;

    const load = async () => {
        cfg = await ConfigService.fetchConfig();
        return cfg || {};
    };

    const refresh = async () => {
        // Force reload configuration from server
        ConfigService.clearCache();
        cfg = await ConfigService.fetchConfig();
        return cfg || {};
    };

    const get = () => cfg || {};
    return { load, refresh, get };
})();


