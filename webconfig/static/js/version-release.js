// ===================== VERSION & UPDATE =====================
(() => {
    const updateBtn = document.getElementById("update-btn");
    const simulateUpdateBtn = document.getElementById("simulate-update-btn");
    let websocketDisconnectedDuringUpdate = false;
    let simulateWatchdogTimer = null;
    let simulateForceReloadTimer = null;

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

    fetch("/version")
        .then(res => res.json())
        .then(data => {
            const currentBtn = document.getElementById("current-version");
            if (currentBtn) {
                const label = currentBtn.querySelector(".label");
                if (label) label.textContent = `${data.current}`;
            }
            if (data.update_available) {
                const latestSpan = document.getElementById("latest-version");
                if (latestSpan) {
                    latestSpan.textContent = `${data.latest}`;
                    latestSpan.classList.remove("hidden");
                }
                if (updateBtn) {
                    updateBtn.classList.add('flex');
                    updateBtn.classList.remove("hidden");
                }
            }
        })
        .catch(err => { console.error("Failed to load version info", err); });

    if (updateBtn) {
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
                if (data.message) { showNotification(data.message); }
                let attempts = 0, maxAttempts = 24;
                const checkForUpdate = async () => {
                    try {
                        const res = await fetch("/version");
                        const data = await res.json();
                        if (data.update_available === false) {
                            showNotification("Update complete. Reloading...", "info");
                            setButtonLoading(updateBtn, false);
                            setTimeout(() => location.reload(), 1500);
                            return;
                        }
                    } catch (err) {
                        console.error("Version check failed:", err);
                    }
                    attempts++;
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
    }

    if (simulateUpdateBtn) {
        simulateUpdateBtn.addEventListener("click", async () => {
            setButtonLoading(simulateUpdateBtn, true);
            sessionStorage.setItem("billy:reload_on_ws_reconnect", "1");
            if (window.LoadingOverlay && window.LoadingOverlay.show) {
                window.LoadingOverlay.show("Reinstalling current version...");
            }
            showNotification("Reinstall current version started", "info");
            let waitForReconnect = false;
            websocketDisconnectedDuringUpdate = false;
            if (simulateWatchdogTimer) {
                clearTimeout(simulateWatchdogTimer);
            }
            if (simulateForceReloadTimer) {
                clearTimeout(simulateForceReloadTimer);
                simulateForceReloadTimer = null;
            }
            simulateWatchdogTimer = setTimeout(() => {
                setButtonLoading(simulateUpdateBtn, false);
                if (window.LoadingOverlay && window.LoadingOverlay.hide) {
                    window.LoadingOverlay.hide();
                }
                showNotification(
                    "Reinstall timeout reached. Reloading page...",
                    "warning",
                    3000
                );
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
                    // Always force a reload shortly after restart begins so the UI
                    // doesn't remain stuck on the loading overlay if websocket
                    // reconnect events are missed.
                    simulateForceReloadTimer = setTimeout(() => {
                        showNotification("Reinstall complete. Reloading page...", "info", 2500);
                        location.reload();
                    }, 10000);
                    // If no websocket disconnect happens shortly, assume restart
                    // did not actually occur and clear loading UI to avoid hanging.
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
                            showNotification(
                                "No restart detected. Reloading page...",
                                "warning",
                                3000
                            );
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
    }

    window.addEventListener("billy:websocket:connected", () => {
        setButtonLoading(updateBtn, false);
        setButtonLoading(simulateUpdateBtn, false);
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
})();

// ===================== RELEASE NOTES =====================
const ReleaseNotes = (() => {
    const keyFor = (tag) => `release_notice_read_${tag}`;
    const els = {
        panel:       () => document.getElementById("release-panel"),
        title:       () => document.getElementById("release-title"),
        body:        () => document.getElementById("release-body"),
        link:        () => document.getElementById("release-link"),
        markReadBtn: () => document.getElementById("release-mark-read"),
        closeBtn:    () => document.getElementById("release-close"),
        badge:       () => document.getElementById("release-badge"),
        toggleBtn:   () => document.getElementById("current-version"),
    };

    async function fetchNote() {
        const res = await fetch("/release-note");
        if (!res.ok) throw new Error("Failed to fetch /release-note");
        return res.json();
    }
    function isRead(tag) { return localStorage.getItem(keyFor(tag)) === "1"; }
    function markRead(tag) {
        localStorage.setItem(keyFor(tag), "1");
        const badge = els.badge(); if (badge) badge.classList.add("!hidden");
        const mark = els.markReadBtn(); if (mark) mark.classList.add("!hidden");
        showNotification("Marked release notes as read", "success");
    }
    function render(note) {
        const t = els.title(); const b = els.body(); const l = els.link();
        const mark = els.markReadBtn(); const badge = els.badge();
        if (t) t.textContent = `Release Notes – ${note.tag}`;
        if (b) b.innerHTML = marked.parse(note.body || "No content.");
        if (l) {
            if (note.url) { l.href = note.url; l.classList.remove("hidden"); }
            else { l.classList.add("hidden"); }
        }
        const read = isRead(note.tag);
        if (badge) badge.classList.toggle("!hidden", read);
        if (mark) mark.classList.toggle("!hidden", read);
        if (mark && !read) { mark.onclick = () => markRead(note.tag); }
        const close = els.closeBtn();
        if (close) {
            close.onclick = () => {
                const panel = els.panel(); const btn = els.toggleBtn();
                if (panel) panel.classList.add("hidden");
                if (btn) { btn.classList.remove("bg-emerald-500", "hover:bg-emerald-400", "text-black"); btn.classList.add("bg-zinc-700", "hover:bg-zinc-600"); }
            };
        }
    }
    async function init() {
        try { const note = await fetchNote(); render(note); }
        catch (e) { console.warn("Release notes unavailable:", e); const badge = els.badge(); if (badge) badge.classList.add("hidden"); }
    }
    return { init };
})();
