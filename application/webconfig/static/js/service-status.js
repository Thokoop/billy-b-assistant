// ===================== SERVICE STATUS =====================
const ServiceStatus = (() => {
    let statusCache = null;
    let lastFetch = 0; // Freshness of the complete HTTP/profile snapshot only.
    let liveVersion = 0;
    const liveFields = new Map();
    const CACHE_DURATION = 2000; // 2 seconds cache
    const RESTART_GRACE_MS = 12000;
    let restartInProgressUntil = 0;
    let restartButtonRef = null;
    let renderedControlStatus = null;
    let activityState = null;
    const activityLabels = {
        idle: 'Ready', listening: 'Listening', speaking: 'Speaking',
        playing_song: 'Playing music', bt_media: 'Playing Bluetooth audio',
        in_call: 'Bluetooth call', ringing: 'Incoming call', interrupted: 'Listening',
        starting: 'Starting…', stopping: 'Stopping…', off: 'Stopped',
        wifi_setup: 'Wi-Fi setup', wifi_setup_starting: 'Starting Wi-Fi setup…',
        error: 'Needs attention',
    };
    const serviceLabels = {
        active: 'Running', inactive: 'Stopped', failed: 'Needs attention',
        activating: 'Starting…', starting: 'Starting…', restarting: 'Restarting…',
        deactivating: 'Stopping…', stopping: 'Stopping…', unknown: 'Reconnecting…',
    };
    const updateBluetoothIndicator = (snapshot) => {
        const indicator = document.getElementById('header-bluetooth');
        if (!indicator) return;
        const devices = (snapshot?.devices || []).filter(device => device.Connected);
        indicator.hidden = devices.length === 0;
        const label = 'Bluetooth connected: ' + devices.map(device => device.Alias || 'Device').join(', ');
        indicator.title = label;
        indicator.setAttribute('aria-label', label);
    };

    const isRestartInProgress = () => Date.now() < restartInProgressUntil;

    const markRestartInProgress = (durationMs = RESTART_GRACE_MS) => {
        restartInProgressUntil = Math.max(
            restartInProgressUntil,
            Date.now() + durationMs
        );
    };

    const setRestartButtonLoading = (loading) => {
        if (!restartButtonRef) return;
        const icon = restartButtonRef.querySelector(".material-icons");
        if (!icon) return;
        icon.classList.toggle("animate-spin", !!loading);
        restartButtonRef.disabled = !!loading;
        restartButtonRef.classList.toggle("opacity-50", !!loading);
        restartButtonRef.classList.toggle("cursor-not-allowed", !!loading);
    };

    const fetchStatus = async (forceRefresh = false) => {
        const now = Date.now();
        const requestLiveVersion = liveVersion;

        // Return cached data if still fresh
        if (!forceRefresh && statusCache && (now - lastFetch) < CACHE_DURATION) {
            updateServiceStatusUI(statusCache.status, statusCache);
            return statusCache;
        }

        try {
            const bootstrapped = !forceRefresh
                && await window.BootstrapData?.take("service");
            const res = bootstrapped
                ? {ok: true, json: async () => bootstrapped}
                : await fetch("/service/status");
            if (!res.ok) {
                if (isRestartInProgress()) {
                    updateServiceStatusUI("restarting");
                    return statusCache || {status: "restarting"};
                }
                const fallback = statusCache || {status: "unknown"};
                updateServiceStatusUI("unknown");
                console.error(`Failed to fetch service status: HTTP ${res.status}`);
                return fallback;
            }

            const data = await res.json();
            if (lastFetch > now && statusCache) return statusCache;
            const merged = {...statusCache, ...data};
            // A live packet can arrive while HTTP is fetching mood/profiles.
            // Keep only fields that actually changed since this request began;
            // an older cached mood must not mask newly fetched profile data.
            for (const [key, update] of liveFields) {
                if (update.version > requestLiveVersion) merged[key] = update.value;
            }
            window.updateRecoveryNotice?.(merged.recovery);
            statusCache = merged;
            lastFetch = now;
            updateServiceStatusUI(merged.status, merged);
            return merged;
        } catch (err) {
            if (isRestartInProgress()) {
                updateServiceStatusUI("restarting");
                return statusCache || {status: "restarting"};
            }

            console.error("Failed to fetch service status:", err);
            const fallback = statusCache || {status: "unknown"};
            updateServiceStatusUI("unknown");
            return fallback;
        }
    };

    const updateServiceStatusUI = (status, details = null) => {
        if (details && typeof details === "object") activityState = details.billy_state || null;
        if (status !== "active") activityState = null;
        const statusEl = document.getElementById("service-status");
        const controlsEl = document.getElementById("service-controls");
        const logoEl = document.getElementById("status-logo");
        const stopDropdownBtn = document.getElementById("stop-billy-btn");

        const label = (status === 'active' && activityLabels[activityState]) || serviceLabels[status] || 'Status unavailable';
        if (statusEl.textContent !== label) statusEl.textContent = label;
        statusEl.title = label;
        const dot = document.getElementById('service-status-dot');
        const tone = status === 'failed' || activityState === 'error' ? '#fb7185'
            : status === 'active' ? (['bt_media', 'in_call', 'ringing'].includes(activityState) ? '#60a5fa' : '#34d399')
            : ['starting', 'activating', 'restarting', 'stopping', 'deactivating'].includes(status) ? '#fbbf24' : '#94a3b8';
        if (dot) dot.style.backgroundColor = tone;
        updateBluetoothIndicator(window.billyWebSocket?.getBluetoothStatus?.());
        statusEl.classList.remove("text-emerald-500", "text-amber-500", "text-rose-500");

        let logoSrc = "/static/images/status-inactive.png";
        if (status === "active") {
            logoSrc = "/static/images/status-active.png";
        } else if (status === "inactive") {
            logoSrc = "/static/images/status-inactive.png";
        } else if (status === "failed") {
            logoSrc = "/static/images/status-inactive.png"; // fallback
        } else if (status === "restarting" || status === "starting") {
            // Avoid fetching transient assets during service restart window.
            logoSrc = "/static/images/status-active.png";
        } else if (status === "stopping") {
            // Keep using stable icon to avoid request failures while stopping.
            logoSrc = "/static/images/status-inactive.png";
        }

        const transientStatus =
            status === "restarting" || status === "starting" || status === "stopping";
        // During restart window, do not trigger extra image requests for transient states.
        if (logoEl && !(isRestartInProgress() && transientStatus)) {
            if (!logoEl.src?.endsWith(logoSrc)) logoEl.src = logoSrc;
        }

        if (stopDropdownBtn) {
            const showStop = status === "active";
            stopDropdownBtn.classList.toggle("hidden", !showStop);
            stopDropdownBtn.classList.toggle("flex", showStop);
        }

        // Activity snapshots must not replace focused buttons or their handlers.
        if (renderedControlStatus === status) return;
        renderedControlStatus = status;
        controlsEl.innerHTML = "";
        const createButton = (label, action, colorClass, iconName) => {
            const btn = document.createElement("button");
            btn.className = `secondary-action ${colorClass} header-service-button ${action === 'restart' ? 'button-reveal' : ''}`;
            btn.type = 'button';
            btn.title = label;
            btn.setAttribute('aria-label', label);

            const icon = document.createElement("i");
            icon.className = "material-icons";
            icon.textContent = iconName;
            btn.appendChild(icon);

            const labelSpan = document.createElement("span");
            labelSpan.className = action === 'restart' ? 'button-reveal-label' : 'header-service-label';
            labelSpan.textContent = label;
            btn.appendChild(labelSpan);

            btn.onclick = () => handleServiceAction(action, btn);
            return btn;
        };

        if (status === "inactive" || status === "failed") {
            controlsEl.appendChild(createButton("Start", "start", "secondary-action--emerald secondary-action--hover--emerald", "play_arrow"));
            restartButtonRef = null;
        } else if (status === "active") {
            const restartBtn = createButton("Restart", "restart", "secondary-action--hover--amber", "restart_alt");
            controlsEl.appendChild(restartBtn);
            restartButtonRef = restartBtn;
        } else {
            const pending = createButton(status === 'stopping' || status === 'deactivating' ? 'Stopping' : 'Restart', 'restart', 'secondary-action--hover--amber', 'restart_alt');
            pending.disabled = true;
            controlsEl.appendChild(pending);
            restartButtonRef = pending;
        }
    };

    // Expose for WebSocket
    window.updateServiceStatus = (status, details = null) => {
        const patch = {...(details && typeof details === 'object' ? details : {}), status,
            billy_state: details?.billy_state || null};
        liveVersion++;
        for (const [key, value] of Object.entries(patch)) {
            liveFields.set(key, {version: liveVersion, value});
        }
        statusCache = {...statusCache, ...patch};
        // Partial live status contains no mood/profile snapshot. Renewing the
        // HTTP cache here would prevent those fields from ever being loaded.
        updateServiceStatusUI(status, statusCache);
    };

    const handleServiceAction = async (action, buttonEl = null) => {
        let previousWebconfigInstance = statusCache?.webconfig_instance || null;
        let restartRequestedAt = null;

        if (action === "restart") {
            markRestartInProgress(60000);
            sessionStorage.setItem("billy:reload_on_ws_reconnect", "1");
            if (buttonEl) {
                restartButtonRef = buttonEl;
            }
            setRestartButtonLoading(true);
            if (window.LoadingOverlay && window.LoadingOverlay.show) {
                window.LoadingOverlay.show("Restarting Billy and web interface... waiting for reconnect.");
            }
        }

        const pendingStatus = {restart: 'restarting', stop: 'stopping', start: 'starting'}[action];
        if (pendingStatus) updateServiceStatusUI(pendingStatus);
        if (action === 'restart') setRestartButtonLoading(true);

        try {
            if (action === "restart") {
                // Do not wait for the browser's TCP timeout if the service
                // disappears before sending its full response. Never retry the
                // POST: the restart may already have been accepted.
                const data = await fetchJsonWithTimeout("/restart", 2500, {method: "POST"});
                previousWebconfigInstance = data.webconfig_instance || previousWebconfigInstance;
                restartRequestedAt = Number(data.restart_requested_at) || null;
            } else {
                await fetch(`/service/${action}`);
            }
        } catch (err) {
            // Expected when restarting webconfig itself: endpoint can drop before responding.
            if (!(action === "restart" && isRestartInProgress())) {
                console.error(`Failed to ${action} service:`, err);
            }
        }

        if (action === "restart") {
            if (window.LoadingOverlay?.waitForReload) {
                window.LoadingOverlay.waitForReload(
                    previousWebconfigInstance, undefined, undefined, undefined,
                    restartRequestedAt,
                );
            }
            return;
        }

        fetchStatus();
        LogPanel.fetchLogs();
    };

    const getCachedStatus = () => statusCache;

    const api = {
        fetchStatus,
        updateServiceStatusUI,
        getCachedStatus,
        isRestartInProgress,
        handleServiceAction,
    };

    // Explicitly expose so other files can reliably check restart state.
    window.ServiceStatus = api;

    window.addEventListener("billy:websocket:connected", () => {
        if (!window.LoadingOverlay?.shouldReloadOnReconnect?.()) {
            setRestartButtonLoading(false);
        }
    });

    window.addEventListener("billy:restart-timeout", () => {
        restartInProgressUntil = 0;
        setRestartButtonLoading(false);
        showNotification("Restart is taking longer than expected. Reload the page to reconnect.", "warning", 8000);
    });

    window.addEventListener('billy:bluetooth:status', event => updateBluetoothIndicator(event.detail));
    window.addEventListener('billy:websocket:disconnected', () => {
        lastFetch = 0;
        updateServiceStatusUI(isRestartInProgress() ? 'restarting' : 'unknown');
        updateBluetoothIndicator(null);
    });

    return api;
})();
