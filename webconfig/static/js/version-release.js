// ===================== VERSION & UPDATE =====================
(() => {
    let websocketDisconnectedDuringUpdate = false;
    let simulateWatchdogTimer = null;
    let simulateForceReloadTimer = null;

    window.BillyVersionInfo = window.BillyVersionInfo || {
        current: "unknown",
        latest: "unknown",
        update_available: false,
    };

    const getUpdateBtn = () => document.getElementById("update-btn");
    const getSimulateUpdateBtn = () => document.getElementById("simulate-update-btn");
    const getLatestVersionLabel = () => document.getElementById("latest-version");
    const getCurrentVersionField = () => document.getElementById("software-current-version");
    const getLatestVersionField = () => document.getElementById("software-latest-version");
    const getReleaseBadges = () => Array.from(document.querySelectorAll("[data-release-badge]"));

    const normalizeVersionLabel = (version) => String(version || "")
        .trim()
        .replace(/^v/i, "")
        .toLowerCase();

    const hasEffectiveUpdate = (info) => {
        const current = normalizeVersionLabel(info.current);
        const latest = normalizeVersionLabel(info.latest || info.current);
        return !!info.update_available && current && latest && current !== latest;
    };

    window.addEventListener("billy:websocket:disconnected", () => {
        websocketDisconnectedDuringUpdate = true;
    });

    const setButtonLoading = (button, loading) => {
        if (!button) return;
        const icon = button.querySelector(".material-icons");
        if (icon) {
            icon.classList.toggle("animate-spin", !!loading);
        }
        button.disabled = !!loading;
        button.classList.toggle("opacity-50", !!loading);
        button.classList.toggle("cursor-not-allowed", !!loading);
    };

    const refreshUi = () => {
        const info = window.BillyVersionInfo || {};
        const updateAvailable = hasEffectiveUpdate(info);
        const updateBtn = getUpdateBtn();
        const latestVersionLabel = getLatestVersionLabel();
        const currentVersionField = getCurrentVersionField();
        const latestVersionField = getLatestVersionField();

        if (latestVersionLabel) {
            latestVersionLabel.textContent = `Update to ${info.latest || "latest"}`;
        }
        if (currentVersionField) {
            currentVersionField.textContent = info.current || "unknown";
        }
        if (latestVersionField) {
            latestVersionField.textContent = info.latest || info.current || "unknown";
        }
        if (updateBtn) {
            updateBtn.classList.toggle("hidden", !updateAvailable);
            updateBtn.classList.toggle("inline-flex", updateAvailable);
        }
        getReleaseBadges().forEach((badge) => {
            badge.classList.toggle("hidden", !updateAvailable);
            badge.classList.toggle("inline-flex", updateAvailable);
        });
    };

    const applyVersionInfo = (data) => {
        window.BillyVersionInfo = {
            current: data.current || "unknown",
            latest: data.latest || data.current || "unknown",
            update_available: !!data.update_available,
        };

        refreshUi();
        document.dispatchEvent(new CustomEvent("billy:version-info-updated", {
            detail: window.BillyVersionInfo,
        }));
    };

    const bindUpdateButton = () => {
        const updateBtn = getUpdateBtn();
        if (!updateBtn || updateBtn.dataset.bound === "true") return;
        updateBtn.dataset.bound = "true";

        updateBtn.addEventListener("click", () => {
            if (!confirm("Are you sure you want to update Billy to the latest version?\n\nThis can take a few minutes. Don't power off the device.")) return;
            setButtonLoading(updateBtn, true);
            sessionStorage.setItem("billy:reload_on_ws_reconnect", "1");
            if (window.LoadingOverlay && window.LoadingOverlay.show) {
                window.LoadingOverlay.show("Updating software... this can take a few minutes.");
            }
            showNotification("Update started");
            fetch("/update", {method: "POST"})
                .then(res => res.json())
                .then(data => {
                    if (data.status === "up-to-date") {
                        showNotification("Already up to date.", "info");
                        setButtonLoading(updateBtn, false);
                        if (window.LoadingOverlay && window.LoadingOverlay.hide) {
                            window.LoadingOverlay.hide();
                        }
                        return;
                    }
                    if (data.message) {
                        showNotification(data.message);
                    }
                    let attempts = 0;
                    const maxAttempts = 24;
                    const checkForUpdate = async () => {
                        try {
                            const res = await fetch("/version");
                            const versionData = await res.json();
                            applyVersionInfo(versionData);
                            if (versionData.update_available === false) {
                                showNotification("Update complete. Reloading...", "info");
                                setButtonLoading(updateBtn, false);
                                setTimeout(() => location.reload(), 1500);
                                return;
                            }
                        } catch (err) {
                            console.error("Version check failed:", err);
                        }
                        attempts += 1;
                        if (attempts < maxAttempts) {
                            setTimeout(checkForUpdate, 5000);
                        } else {
                            showNotification("Update timed out after 2 minutes. Reloading");
                            setButtonLoading(updateBtn, false);
                            setTimeout(() => location.reload(), 1500);
                        }
                    };
                    setTimeout(checkForUpdate, 5000);
                })
                .catch(err => {
                    console.error("Failed to update:", err);
                    showNotification("Failed to update", "error");
                    setButtonLoading(updateBtn, false);
                    if (window.LoadingOverlay && window.LoadingOverlay.hide) {
                        window.LoadingOverlay.hide();
                    }
                });
        });
    };

    const bindSimulateUpdateButton = () => {
        const simulateUpdateBtn = getSimulateUpdateBtn();
        if (!simulateUpdateBtn || simulateUpdateBtn.dataset.bound === "true") return;
        simulateUpdateBtn.dataset.bound = "true";

        simulateUpdateBtn.addEventListener("click", async () => {
            setButtonLoading(simulateUpdateBtn, true);
            sessionStorage.setItem("billy:reload_on_ws_reconnect", "1");
            if (window.LoadingOverlay && window.LoadingOverlay.show) {
                window.LoadingOverlay.show("Reinstalling current version...");
            }
            showNotification("Reinstall current version started", "info");
            let waitForReconnect = false;
            websocketDisconnectedDuringUpdate = false;
            if (simulateWatchdogTimer) clearTimeout(simulateWatchdogTimer);
            if (simulateForceReloadTimer) {
                clearTimeout(simulateForceReloadTimer);
                simulateForceReloadTimer = null;
            }
            simulateWatchdogTimer = setTimeout(() => {
                setButtonLoading(simulateUpdateBtn, false);
                if (window.LoadingOverlay && window.LoadingOverlay.hide) {
                    window.LoadingOverlay.hide();
                }
                showNotification("Reinstall timeout reached. Reloading page...", "warning", 3000);
                setTimeout(() => location.reload(), 1200);
            }, 30000);
            try {
                const controller = new AbortController();
                const requestTimeout = setTimeout(() => controller.abort(), 15000);
                const res = await fetch("/update-simulate", {
                    method: "POST",
                    signal: controller.signal,
                });
                clearTimeout(requestTimeout);
                let data = null;
                try {
                    data = await res.json();
                } catch (_) {
                    data = null;
                }
                if (!res.ok || !data || data.status === "error") {
                    throw new Error((data && data.error) || "Reinstall failed");
                }
                if (data.status === "restarting") {
                    showNotification(data.message || "Restarting services...", "success");
                    waitForReconnect = true;
                    simulateForceReloadTimer = setTimeout(() => {
                        showNotification("Reinstall complete. Reloading page...", "info", 2500);
                        location.reload();
                    }, 10000);
                    setTimeout(() => {
                        if (!websocketDisconnectedDuringUpdate) {
                            if (simulateWatchdogTimer) {
                                clearTimeout(simulateWatchdogTimer);
                                simulateWatchdogTimer = null;
                            }
                            setButtonLoading(simulateUpdateBtn, false);
                            if (window.LoadingOverlay && window.LoadingOverlay.hide) {
                                window.LoadingOverlay.hide();
                            }
                            if (simulateForceReloadTimer) {
                                clearTimeout(simulateForceReloadTimer);
                                simulateForceReloadTimer = null;
                            }
                            showNotification("No restart detected. Reloading page...", "warning", 3000);
                            setTimeout(() => location.reload(), 1200);
                        }
                    }, 8000);
                    return;
                }
                showNotification(data.message || "Reinstall complete", "success");
            } catch (err) {
                console.error("Failed to reinstall current version:", err);
                showNotification("Failed to reinstall current version", "error");
            } finally {
                if (!waitForReconnect) {
                    if (simulateWatchdogTimer) {
                        clearTimeout(simulateWatchdogTimer);
                        simulateWatchdogTimer = null;
                    }
                    if (simulateForceReloadTimer) {
                        clearTimeout(simulateForceReloadTimer);
                        simulateForceReloadTimer = null;
                    }
                    setButtonLoading(simulateUpdateBtn, false);
                    if (window.LoadingOverlay && window.LoadingOverlay.hide) {
                        window.LoadingOverlay.hide();
                    }
                }
            }
        });
    };

    fetch("/version")
        .then(res => res.json())
        .then(applyVersionInfo)
        .catch(err => {
            console.error("Failed to load version info", err);
            refreshUi();
        });

    window.addEventListener("billy:websocket:connected", () => {
        setButtonLoading(getUpdateBtn(), false);
        setButtonLoading(getSimulateUpdateBtn(), false);
        websocketDisconnectedDuringUpdate = false;
        if (simulateWatchdogTimer) {
            clearTimeout(simulateWatchdogTimer);
            simulateWatchdogTimer = null;
        }
        if (simulateForceReloadTimer) {
            clearTimeout(simulateForceReloadTimer);
            simulateForceReloadTimer = null;
        }
    });

    window.BillyVersionUI = {
        refresh: () => {
            refreshUi();
            bindUpdateButton();
            bindSimulateUpdateButton();
        },
    };
})();

// ===================== RELEASE NOTES =====================
const ReleaseNotes = (() => {
    let currentNote = null;

    const getTitle = () => document.getElementById("release-title");
    const getBody = () => document.getElementById("release-body");
    const getLink = () => document.getElementById("release-link");

    async function fetchNote() {
        const res = await fetch("/release-note");
        if (!res.ok) throw new Error("Failed to fetch /release-note");
        return res.json();
    }

    function render(note) {
        currentNote = note;
        const title = getTitle();
        const body = getBody();
        const link = getLink();
        const versionInfo = window.BillyVersionInfo || {};
        const noteTag = note.tag || versionInfo.latest || versionInfo.current || "Latest";

        if (title) {
            title.innerHTML = `
                <span class="material-icons text-emerald-400 select-none" aria-hidden="true">campaign</span>
                <span>Release Notes – ${noteTag}</span>
            `;
        }
        if (body) {
            body.innerHTML = marked.parse(note.body || "No content.");
        }
        if (link) {
            if (note.url) {
                link.href = note.url;
                link.classList.remove("hidden");
            } else {
                link.classList.add("hidden");
            }
        }
    }

    async function init() {
        document.addEventListener("billy:version-info-updated", () => {
            if (currentNote) {
                render(currentNote);
            }
        });
        try {
            const note = await fetchNote();
            render(note);
        } catch (e) {
            console.warn("Release notes unavailable:", e);
        }
    }

    return {
        init,
        refresh: () => {
            if (currentNote) {
                render(currentNote);
            }
            window.BillyVersionUI?.refresh();
        },
    };
})();

window.ReleaseNotes = ReleaseNotes;
