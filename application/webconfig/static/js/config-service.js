// ===================== CONFIG SERVICE =====================
const ConfigService = (() => {
    let configCache = null;
    let lastFetch = 0;
    const CACHE_DURATION = 2000; // 2 seconds cache

    const fetchConfig = async (forceRefresh = false) => {
        const now = Date.now();
        
        // Return cached data if still fresh
        if (!forceRefresh && configCache && (now - lastFetch) < CACHE_DURATION) {
            return configCache;
        }

        try {
            // On the first load of the page this comes from the combined
            // bootstrap request that is already in flight, so /config is not
            // fetched a second time. Every later call goes to /config itself.
            const data = (!forceRefresh && await window.BootstrapData?.take("config"))
                || await (await fetch("/config")).json();
            configCache = data;
            lastFetch = now;
            return data;
        } catch (error) {
            console.error("Failed to fetch config:", error);
            return null;
        }
    };

    const getCachedConfig = () => configCache;

    const clearCache = () => {
        configCache = null;
        lastFetch = 0;
    };

    return { fetchConfig, getCachedConfig, clearCache };
})();
