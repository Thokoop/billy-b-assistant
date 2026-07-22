// ===================== SERVICE STATUS =====================
const ServiceStatus = (() => {
    let statusCache = null;
    let lastFetch = 0;
    const CACHE_DURATION = 2000; // 2 seconds cache
    const RESTART_GRACE_MS = 12000;
    let restartInProgressUntil = 0;
    let restartButtonRef = null;

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
        
        // Return cached data if still fresh
        if (!forceRefresh && statusCache && (now - lastFetch) < CACHE_DURATION) {
            updateServiceStatusUI(statusCache.status);
            return statusCache;
        }

        try {
            const res = await fetch("/service/status");
            if (!res.ok) {
                if (isRestartInProgress()) {
                    updateServiceStatusUI("restarting");
                    return statusCache || {status: "restarting"};
                }
                const fallback = statusCache || {status: "unknown"};
                updateServiceStatusUI(fallback.status || "unknown");
                console.error(`Failed to fetch service status: HTTP ${res.status}`);
                return fallback;
            }

            const data = await res.json();
            statusCache = data;
            lastFetch = now;
            updateServiceStatusUI(data.status);
            return data;
        } catch (err) {
            if (isRestartInProgress()) {
                updateServiceStatusUI("restarting");
                return statusCache || {status: "restarting"};
            }

            console.error("Failed to fetch service status:", err);
            const fallback = statusCache || {status: "unknown"};
            updateServiceStatusUI(fallback.status || "unknown");
            return fallback;
        }
    };

    const updateServiceStatusUI = (status) => {
        const statusEl = document.getElementById("service-status");
        const controlsEl = document.getElementById("service-controls");
        const logoEl = document.getElementById("status-logo");
        const stopDropdownBtn = document.getElementById("stop-billy-btn");

        statusEl.textContent = `(${status})`;
        statusEl.classList.remove("text-emerald-500", "text-amber-500", "text-rose-500");

        let logoSrc = "/static/images/status-inactive.png";
        if (status === "active") {
            statusEl.classList.add("text-emerald-500");
            logoSrc = "/static/images/status-active.png";
        } else if (status === "inactive") {
            statusEl.classList.add("text-amber-500");
            logoSrc = "/static/images/status-inactive.png";
        } else if (status === "failed") {
            statusEl.classList.add("text-rose-500");
            logoSrc = "/static/images/status-inactive.png"; // fallback
        } else if (status === "restarting" || status === "starting") {
            statusEl.classList.add("text-amber-500");
            // Avoid fetching transient assets during service restart window.
            logoSrc = "/static/images/status-active.png";
        } else if (status === "stopping") {
            statusEl.classList.add("text-rose-500");
            // Keep using stable icon to avoid request failures while stopping.
            logoSrc = "/static/images/status-inactive.png";
        }

        const transientStatus =
            status === "restarting" || status === "starting" || status === "stopping";
        // During restart window, do not trigger extra image requests for transient states.
        if (logoEl && !(isRestartInProgress() && transientStatus)) {
            logoEl.src = logoSrc;
        }

        if (stopDropdownBtn) {
            const showStop = status === "active";
            stopDropdownBtn.classList.toggle("hidden", !showStop);
            stopDropdownBtn.classList.toggle("flex", showStop);
        }

        controlsEl.innerHTML = "";
        const createButton = (label, action, colorClass, iconName) => {
            const btn = document.createElement("button");
            btn.className = `secondary-action ${colorClass} text-sm p-2`;

            const icon = document.createElement("i");
            icon.className = "material-icons";
            icon.textContent = iconName;
            btn.appendChild(icon);

            const labelSpan = document.createElement("span");
            labelSpan.className = "hidden text-sm md:inline";
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
            controlsEl.textContent = "Unknown status.";
            restartButtonRef = null;
        }
    };

    // Expose for WebSocket
    window.updateServiceStatus = updateServiceStatusUI;

    const handleServiceAction = async (action, buttonEl = null) => {
        const statusEl = document.getElementById("service-status");
        const logoEl = document.getElementById("status-logo");

        if (action === "restart") {
            markRestartInProgress();
            sessionStorage.setItem("billy:reload_on_ws_reconnect", "1");
            if (buttonEl) {
                restartButtonRef = buttonEl;
            }
            setRestartButtonLoading(true);
            if (window.LoadingOverlay && window.LoadingOverlay.show) {
                window.LoadingOverlay.show("Restarting Billy and web interface... waiting for reconnect.");
            }
        }

        const statusMap = {
            restart: {text: "restarting", color: "text-amber-500", logo: "/static/images/status-active.png"},
            stop:    {text: "stopping",   color: "text-rose-500",   logo: "/static/images/status-inactive.png"},
            start:   {text: "starting",   color: "text-emerald-500",logo: "/static/images/status-inactive.png"}
        };

        if (statusMap[action]) {
            const {text, color, logo} = statusMap[action];
            statusEl.textContent = text;
            statusEl.classList.remove("text-emerald-500", "text-amber-500", "text-rose-500");
            statusEl.classList.add(color);
            // Avoid image fetches during restart while webconfig is going down.
            if (logoEl && !(action === "restart" && isRestartInProgress())) {
                logoEl.src = logo;
            }
        }

        try {
            if (action === "restart") {
                await fetch("/restart", {method: "POST"});
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
                window.LoadingOverlay.waitForReload();
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
        setRestartButtonLoading(false);
    });

    return api;
})();
