// ===================== Header Secondary Actions (Log Panel) =====================
const LogPanel = (() => {
    let autoScrollEnabled = false;
    let isLogHidden = true;
    let restoreSettingsPanelAfterEnvClose = false;
    let lastLogsSnapshot = "";
    const MAX_LOG_BUFFER_CHARS = 400000;
    let uiBound = false;

    const rebootBilly = async () => {
        if (!confirm("Are you sure you want to reboot Billy? This will reboot the whole system.")) return;
        try {
            const res = await fetch('/reboot', {method: 'POST'});
            const data = await res.json();
            if (data.status === "ok") {
                showNotification("Billy is rebooting!", "success");
                setTimeout(() => { location.reload(); }, 15000);
            } else {
                showNotification(data.error || "Reboot failed", "error");
            }
        } catch (err) {
            console.error("Failed to reboot Billy:", err);
            showNotification("Failed to reboot Billy", "error");
        }
    };

    const shutdownBilly = async () => {
        if (!confirm("Are you sure you want to shutdown Billy?\n\nThis will power off the Raspberry Pi but one or more of the motors may remain engaged.\nTo fully power down, make sure to also switch off or unplug the power supply after shutdown.")) return;
        try {
            const res = await fetch('/shutdown', {method: 'POST'});
            const data = await res.json();
            if (data.status === "ok") {
                showNotification("Billy is shutting down!", "success");
                setTimeout(() => { location.reload(); }, 3000);
            } else {
                showNotification(data.error || "Shutdown failed", "error");
            }
        } catch (err) {
            console.error("Failed to shutdown Billy:", err);
            showNotification("Failed to shutdown Billy", "error");
        }
    };

    const changePassword = async (newPassword, confirmPassword) => {
        try {
            const res = await fetch('/change-password', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    new_password: newPassword,
                    confirm_password: confirmPassword
                })
            });
            
            const data = await res.json();
            if (data.status === "ok") {
                showNotification("Password changed successfully! Reloading page...", "success");
                // Reload page to pick up the new CHANGED_DEFAULT_PASS config
                setTimeout(() => location.reload(), 2000);
                return true;
            } else {
                showNotification(data.error || "Password change failed", "error");
                return false;
            }
        } catch (err) {
            console.error("Failed to change password:", err);
            showNotification("Failed to change password", "error");
            return false;
        }
    };

    const showPasswordModal = () => {
        const modal = document.getElementById("password-modal");
        const form = document.getElementById("password-form");
        const closeBtn = document.getElementById("close-password-modal");
        
        // Clear form
        form.reset();
        
        // Show modal
        modal.classList.remove("hidden");
        
        // Close modal handlers
        const closeModal = () => {
            modal.classList.add("hidden");
            form.reset();
        };
        
        closeBtn.addEventListener("click", closeModal);
        
        // Close on backdrop click
        modal.addEventListener("click", (e) => {
            if (e.target === modal) {
                closeModal();
            }
        });
        
        // Handle form submission
        form.addEventListener("submit", async (e) => {
            e.preventDefault();
            
            const formData = new FormData(form);
            const newPassword = formData.get("new_password");
            const confirmPassword = formData.get("confirm_password");
            
            // Validate passwords match
            if (newPassword !== confirmPassword) {
                showNotification("New passwords do not match", "error");
                return;
            }
            
            // Validate password length
            if (newPassword.length < 8) {
                showNotification("New password must be at least 8 characters long", "error");
                return;
            }
            
            // Change password
            const success = await changePassword(newPassword, confirmPassword);
            if (success) {
                closeModal();
            }
        });
    };

    const checkAndShowPasswordModal = async (cfg) => {
        const forcePassChange = (
            cfg.FORCE_PASS_CHANGE === 'True'
            || cfg.FORCE_PASS_CHANGE === 'true'
            || cfg.FORCE_PASS_CHANGE === true
        );
        if (!forcePassChange) return;

        try {
            const response = await fetch("/wifi/status");
            const wifiStatus = await response.json();
            if (!response.ok) {
                console.warn(
                    "Skipping forced password modal until Wi-Fi is fully connected:",
                    wifiStatus.error || "Failed to load Wi-Fi status"
                );
                return;
            }

            const hasInternetReadyConnection = Boolean(wifiStatus.connected) && !Boolean(wifiStatus.hotspot_active);
            const isCaptiveOnboarding = Boolean(wifiStatus.onboarding_active) || Boolean(wifiStatus.hotspot_active);
            if (!hasInternetReadyConnection || isCaptiveOnboarding) {
                return;
            }
        } catch (error) {
            console.warn("Skipping forced password modal until Wi-Fi is fully connected:", error);
            return;
        }

        setTimeout(() => {
            showPasswordModal();
        }, 1000); // Small delay to let page load
    };



    const applyLogLevel = async () => {
        const logLevelSelect = document.getElementById("log-level-select");
        const selectedLevel = logLevelSelect.value;

        try {
            const res = await fetch("/save", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({LOG_LEVEL: selectedLevel})
            });
            const data = await res.json();
            if (data.status === "ok") {
                showNotification(`Log level changed to ${selectedLevel}. Restarting Billy`, "success");
                
                // Restart both Billy service and webconfig service
                setTimeout(async () => {
                    try {
                        // Restart both services
                        await fetch("/restart", {method: "POST"});
                        
                    } catch (restartErr) {
                        console.error("Failed to restart services:", restartErr);
                        showNotification("Log level saved but restart failed. Please restart manually.", "warning");
                    }
                }, 1000);
                
            } else {
                showNotification(data.error || "Failed to change log level", "error");
            }
        } catch (err) {
            console.error("Failed to change log level:", err);
            showNotification("Failed to change log level", "error");
        }
    };

    const clearServiceLogs = async () => {
        try {
            const res = await fetch("/logs/clear", {method: "POST"});
            const data = await res.json();
            if (!res.ok || data.status !== "ok") {
                const errorMsg = Array.isArray(data.errors)
                    ? data.errors.join(" | ")
                    : (data.error || "Failed to clear service logs");
                showNotification(errorMsg, "error");
                return;
            }
            setLogsUI("");
            showNotification("Service logs cleared", "success");
            await fetchLogs();
        } catch (err) {
            console.error("Failed to clear service logs:", err);
            showNotification("Failed to clear service logs", "error");
        }
    };

    const fetchLogs = async () => {
        try {
            const res = await fetch("/logs");
            if (!res.ok) {
                console.error(`Failed to fetch logs: HTTP ${res.status}`);
                return {logs: ""};
            }
            const data = await res.json();
            setLogsUI(data.logs || "No logs found.");
            return data;
        } catch (err) {
            const serviceStatusApi =
                window.ServiceStatus ||
                (typeof ServiceStatus !== "undefined" ? ServiceStatus : null);
            const restartInProgress =
                serviceStatusApi &&
                typeof serviceStatusApi.isRestartInProgress === "function" &&
                serviceStatusApi.isRestartInProgress();

            if (restartInProgress) {
                setLogsUI("Restart in progress... waiting for logs to reconnect.");
                return {logs: ""};
            }

            console.error("Failed to fetch logs:", err);
            return {logs: ""};
        }
    };

    const trimLogBuffer = (text) => {
        if (text.length <= MAX_LOG_BUFFER_CHARS) return text;
        return text.slice(text.length - MAX_LOG_BUFFER_CHARS);
    };

    const isAutoScrollActive = () => {
        if (elements.scrollBtn) {
            return elements.scrollBtn.classList.contains("text-emerald-400");
        }
        return autoScrollEnabled;
    };

    const setLogsUI = (logs) => {
        if (!elements.logOutput || !elements.logContainer) return;
        const normalized = String(logs || "");
        lastLogsSnapshot = normalized;
        elements.logOutput.textContent = trimLogBuffer(normalized);
        if (isAutoScrollActive()) {
            requestAnimationFrame(() => {
                elements.logContainer.scrollTop = elements.logContainer.scrollHeight;
            });
        }
    };

    const findOverlap = (existing, incoming) => {
        const max = Math.min(existing.length, incoming.length);
        for (let k = max; k > 0; k -= 1) {
            if (existing.endsWith(incoming.slice(0, k))) {
                return k;
            }
        }
        return 0;
    };

    window.updateLogs = (incomingLogs) => {
        if (!elements.logOutput || !elements.logContainer) return;
        const incoming = String(incomingLogs || "");
        const existing = String(elements.logOutput.textContent || "");
        if (!incoming) return;
        if (!existing) {
            setLogsUI(incoming);
            return;
        }
        if (incoming === lastLogsSnapshot) return;
        lastLogsSnapshot = incoming;

        // Snapshot reset/reconnect case.
        if (incoming.includes(existing)) {
            setLogsUI(incoming);
            return;
        }
        // No new data case.
        if (existing.includes(incoming)) {
            return;
        }

        const overlap = findOverlap(existing, incoming);
        const merged = overlap > 0 ? `${existing}${incoming.slice(overlap)}` : `${existing}\n${incoming}`;
        setLogsUI(merged);
    };

    const toggleLogPanel = () => {
        isLogHidden = !isLogHidden;
        elements.logPanel.classList.toggle("hidden", isLogHidden);
        elements.toggleBtn.classList.toggle("secondary-action--active-cyan", !isLogHidden);
    };

    const loadEnvContent = async () => {
        try {
            const res = await fetch('/get-env');
            const text = await res.text();
            if (elements.envTextarea) {
                elements.envTextarea.value = text.trim();
            }
        } catch {
            showNotification("An error occurred while loading .env", "error");
        }
    };

    const openEnvEditorModal = async () => {
        if (!elements.envEditorModal) return;
        const settingsPanel = document.getElementById("settings-panel");
        if (settingsPanel && !settingsPanel.classList.contains("hidden")) {
            settingsPanel.classList.add("hidden");
            restoreSettingsPanelAfterEnvClose = true;
        } else {
            restoreSettingsPanelAfterEnvClose = false;
        }
        elements.envEditorModal.classList.remove("hidden");
        await loadEnvContent();
    };

    const closeEnvEditorModal = () => {
        if (!elements.envEditorModal) return;
        elements.envEditorModal.classList.add("hidden");
        if (restoreSettingsPanelAfterEnvClose) {
            const settingsPanel = document.getElementById("settings-panel");
            if (settingsPanel) {
                settingsPanel.classList.remove("hidden");
            }
            restoreSettingsPanelAfterEnvClose = false;
        }
    };


    // The checkbox (id="toggle-motion-btn", an iOS-style toggle now rather
    // than a button) is the source of truth - "checked" means animations are
    // ON, the opposite sense of the "reduce-motion" class/localStorage flag
    // it drives.
    const toggleMotion = () => {
        const checkbox = elements.toggleMotionBtn;
        const animationsEnabled = checkbox.checked;
        document.documentElement.classList.toggle("reduce-motion", !animationsEnabled);
        localStorage.setItem("reduceMotion", animationsEnabled ? "0" : "1");
    };

    const toggleFullscreenLog = () => {
        const icon = document.getElementById("fullscreen-icon");
        const isFullscreen = elements.logContainer.classList.toggle("log-fullscreen");
        icon.textContent = isFullscreen ? "fullscreen_exit" : "fullscreen";
    };

    const toggleAutoScroll = () => {
        autoScrollEnabled = !autoScrollEnabled;
        elements.scrollBtn.classList.toggle("text-emerald-400", autoScrollEnabled);
        elements.scrollBtn.classList.toggle("text-white", !autoScrollEnabled);
        elements.scrollBtn.title = autoScrollEnabled ? "Auto-scroll ON" : "Auto-scroll OFF";
        if (autoScrollEnabled) {
            elements.logContainer.scrollTop = elements.logContainer.scrollHeight;
        }
    };

    const saveEnv = async () => {
        if (!confirm("Are you sure you want to overwrite the .env file? This may affect how Billy runs.")) return;
        try {
            const res = await fetch('/save-env', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({content: elements.envTextarea.value})
            });
            const data = await res.json();
            if (data.status === "ok") {
                fetch('/restart', {method: 'POST'})
                    .then(res => res.json())
                    .then(data => {
                        if (data.status === "ok") {
                            showNotification(".env saved. Restarting", "success");
                            setTimeout(() => location.reload(), 3000);
                        } else {
                            showNotification(data.error || "Restart failed", "error");
                        }
                    })
                    .catch(err => showNotification(err.message, "error"));
            } else {
                showNotification(data.error || "Unknown error", "error");
            }
        } catch (err) {
            showNotification(err.message, "error");
        }
    };

    const hideSupportPanelIfDisabled = (cfg) => {
        // Hide support section if SHOW_SUPPORT is false
        const show = String(cfg.SHOW_SUPPORT || "").toLowerCase() === "true";
        const supportSection = document.getElementById("support-section");
        
        if (supportSection) {
            if (show) {
                supportSection.style.display = "block";
            } else {
                supportSection.style.display = "none";
            }
        }
    };

    let elements = {};
    const bindUI = (cfg = {}) => {
        if (uiBound) {
            const logLevelSelect = document.getElementById("log-level-select");
            if (logLevelSelect && cfg.LOG_LEVEL) {
                logLevelSelect.value = cfg.LOG_LEVEL;
            }
            checkAndShowPasswordModal(cfg);
            hideSupportPanelIfDisabled(cfg);
            return;
        }

        elements = {
            logOutput: document.getElementById("log-output"),
            logContainer: document.getElementById("log-container"),
            toggleFullscreenBtn: document.getElementById("toggle-fullscreen-btn"),
            scrollBtn: document.getElementById("scroll-bottom-btn"),
            toggleBtn: document.getElementById("toggle-log-btn"),
            logPanel: document.getElementById("log-panel"),
            openEnvEditorBtn: document.getElementById("open-env-editor-modal-btn"),
            envEditorModal: document.getElementById("env-editor-modal"),
            closeEnvEditorModalBtn: document.getElementById("close-env-editor-modal"),
            cancelEnvEditorModalBtn: document.getElementById("cancel-env-editor-modal"),
            envEditorForm: document.getElementById("env-editor-form"),
            envTextarea: document.getElementById("env-textarea"),
            saveEnvBtn: document.getElementById("save-env-btn"),
            toggleMotionBtn: document.getElementById("toggle-motion-btn"),
            powerBtn: document.getElementById("power-btn"),
            powerDropdown: document.getElementById("power-dropdown"),
            stopBillyBtn: document.getElementById("stop-billy-btn"),
            rebootBillyBtn: document.getElementById("reboot-billy-btn"),
            shutdownBillyBtn: document.getElementById("shutdown-billy-btn"),
        };

        if (!elements.toggleBtn || !elements.logPanel) {
            return;
        }

        uiBound = true;

        if (elements.powerBtn) {
            elements.powerBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                if (elements.powerDropdown) elements.powerDropdown.classList.toggle("hidden");
            });
        }

        document.addEventListener("click", (e) => {
            const menu = document.getElementById("power-menu");
            if (!menu || !menu.contains(e.target)) {
                if (elements.powerDropdown) elements.powerDropdown.classList.add("hidden");
            }
        });

        elements.toggleBtn.addEventListener("click", toggleLogPanel);
        if (elements.toggleFullscreenBtn) {
            elements.toggleFullscreenBtn.addEventListener("click", toggleFullscreenLog);
        }
        if (elements.scrollBtn) {
            elements.scrollBtn.addEventListener("click", toggleAutoScroll);
            autoScrollEnabled = isAutoScrollActive();
            elements.scrollBtn.classList.toggle("text-emerald-400", autoScrollEnabled);
            elements.scrollBtn.classList.toggle("text-white", !autoScrollEnabled);
            elements.scrollBtn.title = autoScrollEnabled ? "Auto-scroll ON" : "Auto-scroll OFF";
        }
        if (elements.toggleMotionBtn) {
            elements.toggleMotionBtn.addEventListener("change", toggleMotion);
        }
        if (elements.openEnvEditorBtn) {
            elements.openEnvEditorBtn.dataset.envEditorOpenBound = "true";
            elements.openEnvEditorBtn.addEventListener("click", openEnvEditorModal);
        }
        if (elements.closeEnvEditorModalBtn) {
            elements.closeEnvEditorModalBtn.addEventListener("click", closeEnvEditorModal);
        }
        if (elements.cancelEnvEditorModalBtn) {
            elements.cancelEnvEditorModalBtn.addEventListener("click", closeEnvEditorModal);
        }
        if (elements.envEditorModal) {
            elements.envEditorModal.addEventListener("click", (e) => {
                if (e.target === elements.envEditorModal) {
                    closeEnvEditorModal();
                }
            });
        }
        if (elements.envEditorForm) {
            elements.envEditorForm.addEventListener("submit", (e) => {
                e.preventDefault();
                saveEnv();
            });
        } else if (elements.saveEnvBtn) {
            elements.saveEnvBtn.addEventListener("click", saveEnv);
        }
        
        if (elements.stopBillyBtn) {
            elements.stopBillyBtn.addEventListener("click", async () => {
                if (window.ServiceStatus?.handleServiceAction) {
                    await window.ServiceStatus.handleServiceAction("stop");
                }
                if (elements.powerDropdown) {
                    elements.powerDropdown.classList.add("hidden");
                }
            });
        }
        if (elements.rebootBillyBtn) {
            elements.rebootBillyBtn.addEventListener("click", rebootBilly);
        }
        if (elements.shutdownBillyBtn) {
            elements.shutdownBillyBtn.addEventListener("click", shutdownBilly);
        }
        
        // Log level control
        const applyLogLevelBtn = document.getElementById("apply-log-level-btn");
        if (applyLogLevelBtn) applyLogLevelBtn.addEventListener("click", applyLogLevel);
        const clearLogsBtn = document.getElementById("clear-logs-btn");
        if (clearLogsBtn) clearLogsBtn.addEventListener("click", clearServiceLogs);
        
        // Set current log level in dropdown
        const logLevelSelect = document.getElementById("log-level-select");
        if (logLevelSelect && cfg.LOG_LEVEL) {
            logLevelSelect.value = cfg.LOG_LEVEL;
        }
        if (elements.toggleMotionBtn) {
            const isReduced = localStorage.getItem("reduceMotion") === "1";
            document.documentElement.classList.toggle("reduce-motion", isReduced);
            elements.toggleMotionBtn.checked = !isReduced;
        }

        // Load RC versions / startup flap settings into their toggles
        fetch('/config')
            .then(res => res.json())
            .then(data => {
                const rcVersionsCheckbox = document.getElementById("SHOW_RC_VERSIONS");
                if (rcVersionsCheckbox) {
                    rcVersionsCheckbox.checked = data.SHOW_RC_VERSIONS === 'True' || data.SHOW_RC_VERSIONS === true;
                }
                const flapOnBootCheckbox = document.getElementById("FLAP_ON_BOOT");
                if (flapOnBootCheckbox) {
                    flapOnBootCheckbox.checked = data.FLAP_ON_BOOT === 'True' || data.FLAP_ON_BOOT === true;
                }
                const showTooltipsCheckbox = document.getElementById("SHOW_TOOLTIPS");
                if (showTooltipsCheckbox) {
                    // Absent key defaults to shown, same as core/config.py's fallback.
                    showTooltipsCheckbox.checked = !(data.SHOW_TOOLTIPS === 'False' || data.SHOW_TOOLTIPS === false);
                }
            })
            .catch(err => console.error('Failed to load RC versions setting:', err));

        // Handle password change modal and button visibility
        checkAndShowPasswordModal(cfg);
        
        // Handle support panel visibility
        hideSupportPanelIfDisabled(cfg);
    };

    return {
        fetchLogs,
        bindUI,
        changePassword,
        showPasswordModal,
        checkAndShowPasswordModal,
        hideSupportPanelIfDisabled,
        openEnvEditorModal,
    };
})();

// Make LogPanel available globally
window.LogPanel = LogPanel;
