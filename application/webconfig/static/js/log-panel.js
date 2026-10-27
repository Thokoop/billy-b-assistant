// ===================== Header Secondary Actions (Log Panel) =====================
const LogPanel = (() => {
    let envRevision = null;
    let envBaseline = null;
    let envSource = '.env';
    let envLoadId = 0;
    let envLoading = false;
    let autoScrollEnabled = false;
    let isLogHidden = true;
    let restoreSettingsPanelAfterEnvClose = false;
    let recoveryModalAfterEnvClose = null;
    let lastLogsSnapshot = "";
    const MAX_LOG_BUFFER_CHARS = 400000;
    let uiBound = false;
    let logSource = 'billy';
    let logTimer = null;
    let logRequest = null;
    let logGeneration = 0;
    let logSnapshotRevision = 0;
    const logsVisible = () => !isLogHidden && !document.hidden;

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

    // Clearing the view must not vacuum the Pi's shared system journal.
    const clearLogView = () => {
        setLogsUI('');
    };

    const fetchLogs = async () => {
        if (!logsVisible() || logRequest) return;
        const generation = logGeneration;
        const revision = logSnapshotRevision;
        const source = logSource;
        const controller = new AbortController();
        logRequest = controller;
        const timeout = setTimeout(() => controller.abort(), 5000);
        try {
            const res = await fetch(`/logs?source=${encodeURIComponent(source)}`, {
                cache: 'no-store', signal: controller.signal,
            });
            if (!res.ok) {
                if (generation === logGeneration && revision === logSnapshotRevision && logsVisible()) {
                    showLogError('Waiting for service logs to reconnect…');
                }
                return;
            }
            const data = await res.json();
            if (generation === logGeneration && revision === logSnapshotRevision && logsVisible()) {
                if ((data.source || 'billy') !== source) {
                    showLogError('This software version does not provide the selected logs. Reload after updating.');
                } else {
                    window.updateLogs(data.logs || '', source, data.error || '');
                }
            }
            return data;
        } catch (err) {
            if (generation === logGeneration && revision === logSnapshotRevision && logsVisible()) {
                showLogError('Waiting for service logs to reconnect…');
            }
        } finally {
            clearTimeout(timeout);
            if (logRequest === controller) logRequest = null;
        }
    };

    const showLogError = (message) => {
        if (!elements.logError) return;
        elements.logError.hidden = !message;
        elements.logError.textContent = message;
    };

    const syncLogSubscription = () => {
        if (logTimer) clearInterval(logTimer);
        logTimer = null;
        logGeneration += 1;
        logRequest?.abort();
        logRequest = null;
        window.billyWebSocket?.subscribeLogs?.(logsVisible() ? logSource : null);
        if (!logsVisible()) return;
        fetchLogs();
        // Compatibility for installations without WebSockets, plus reconnects.
        logTimer = setInterval(() => {
            if (!window.billyWebSocket?.hasLogStream?.(logSource)) fetchLogs();
        }, 2000);
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

    window.updateLogs = (incomingLogs, source = 'billy', error = '') => {
        if (!logsVisible() || source !== logSource) return;
        if (!elements.logOutput || !elements.logContainer) return;
        logSnapshotRevision += 1;
        showLogError(error);
        const incoming = String(incomingLogs || '');
        if (incoming === lastLogsSnapshot) return;
        // Compare source snapshots, not the displayed buffer. This lets Clear
        // view retain its position without deleting system diagnostics.
        let delta = incoming;
        if (lastLogsSnapshot) {
            if (lastLogsSnapshot.includes(incoming)) return;
            delta = incoming.slice(findOverlap(lastLogsSnapshot, incoming));
        }
        lastLogsSnapshot = incoming;
        const existing = elements.logOutput.textContent || '';
        setLogsUI(existing && delta ? `${existing}${existing.endsWith('\n') ? '' : '\n'}${delta}` : existing || delta);
    };

    const openLogPanel = () => {
        const panel = document.getElementById('log-panel');
        if (!panel) return;
        isLogHidden = false;
        panel.classList.remove('hidden');
        document.getElementById('toggle-log-btn')?.classList.add('secondary-action--active-amber');
        panel.scrollIntoView?.({block: 'nearest'});
        syncLogSubscription();
    };

    const toggleLogPanel = () => {
        isLogHidden = !isLogHidden;
        elements.logPanel.classList.toggle("hidden", isLogHidden);
        elements.toggleBtn.classList.toggle("secondary-action--active-amber", !isLogHidden);
        syncLogSubscription();
    };

    const loadEnvContent = async (source = '.env') => {
        const loadId = ++envLoadId;
        envLoading = true;
        if (elements.saveEnvBtn) elements.saveEnvBtn.disabled = true;
        if (elements.envSource) elements.envSource.disabled = true;
        if (elements.envTextarea) elements.envTextarea.disabled = true;
        try {
            const url = source === '.env' ? '/get-env' : `/get-env?source=${encodeURIComponent(source)}`;
            const res = await fetch(url, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
            if (!res.ok) {
                if (loadId === envLoadId) {
                    showNotification('Configuration could not be loaded. Your edits have been kept.', "error");
                }
                return;
            }
            const text = await res.text();
            if (loadId !== envLoadId) return;
            const revision = res.headers.get('X-Config-Revision');
            if (!revision) {
                showNotification('Configuration revision is missing. Reopen the editor before saving.', "error");
                return;
            }
            envRevision = revision;
            envBaseline = text;
            envSource = source;
            if (elements.envTextarea) elements.envTextarea.value = text;
        } catch (error) {
            if (loadId === envLoadId) showNotification(error.message, "error");
        } finally {
            if (loadId === envLoadId) {
                envLoading = false;
                if (elements.saveEnvBtn) elements.saveEnvBtn.disabled = !envRevision;
                if (elements.envTextarea) elements.envTextarea.disabled = false;
                if (elements.envSource) {
                    elements.envSource.value = envSource;
                    elements.envSource.disabled = false;
                }
            }
        }
    };

    const loadEnvHistory = async () => {
        if (!elements.envSource) return;
        try {
            const res = await fetch('/env-history', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
            if (!res.ok) {
                showNotification('Configuration history is unavailable; the current file can still be edited.', "error");
                return;
            }
            const data = await res.json();
            const options = data.files.map((file) => {
                const option = document.createElement('option');
                option.value = file.name;
                const label = file.kind === 'backup' ? 'Working backup' : 'Preserved damaged file';
                option.textContent = file.kind === 'current' ? 'Current configuration'
                    : `${label} — ${new Date(file.saved_at * 1000).toLocaleString()}`;
                return option;
            });
            elements.envSource.replaceChildren(...options);
            elements.envSource.value = envSource;
        } catch (error) {
            showNotification(error.message, "error");
        }
    };

    const openEnvEditorModal = async () => {
        if (!elements.envEditorModal || !elements.envEditorModal.classList.contains("hidden")) return;
        const recoveryModal = document.getElementById("factory-reset-modal");
        recoveryModalAfterEnvClose = recoveryModal && !recoveryModal.classList.contains("hidden")
            ? recoveryModal : null;
        recoveryModalAfterEnvClose?.classList.add("hidden");
        const settingsPanel = document.getElementById("settings-panel");
        if (settingsPanel && !settingsPanel.classList.contains("hidden")) {
            settingsPanel.classList.add("hidden");
            restoreSettingsPanelAfterEnvClose = true;
        } else {
            restoreSettingsPanelAfterEnvClose = false;
        }
        elements.envEditorModal.classList.remove("hidden");
        envRevision = null;
        envBaseline = null;
        envSource = '.env';
        if (elements.envSource) elements.envSource.value = '.env';
        await Promise.all([loadEnvContent(), loadEnvHistory()]);
    };

    const closeEnvEditorModal = () => {
        if (!elements.envEditorModal) return;
        elements.envEditorModal.classList.add("hidden");
        if (recoveryModalAfterEnvClose?.isConnected) {
            recoveryModalAfterEnvClose.classList.remove("hidden");
            recoveryModalAfterEnvClose.querySelector('#open-env-editor-modal-btn')?.focus();
        }
        recoveryModalAfterEnvClose = null;
        envLoadId += 1;
        envLoading = false;
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
        applyReduceMotionPreference(!animationsEnabled);
        localStorage.setItem("reduceMotion", animationsEnabled ? "0" : "1");
    };

    // #toggle-motion-btn lives inside a page-router-swapped section, so it's
    // a fresh DOM node on every SPA navigation - re-query and re-bind it each
    // time bindUI() runs (guarded per-element via dataset.bound, since
    // uiBound itself only reflects whether the *first-ever* bind happened).
    // The .reduce-motion class itself is applied globally by init.js on every
    // page load via applyReduceMotionPreference() - this only syncs the
    // checkbox to match, for whichever page actually has it.
    const bindMotionToggle = () => {
        const checkbox = document.getElementById("toggle-motion-btn");
        if (!checkbox || checkbox.dataset.bound === "true") return;
        checkbox.dataset.bound = "true";
        elements.toggleMotionBtn = checkbox;
        checkbox.addEventListener("change", toggleMotion);
        checkbox.checked = localStorage.getItem("reduceMotion") !== "1";
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
        if (envLoading || !envRevision) return;
        if (!confirm("Are you sure you want to overwrite the .env file? This may affect how Billy runs.")) return;
        try {
            const res = await fetch('/save-env', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({content: elements.envTextarea.value, revision: envRevision})
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
            bindMotionToggle();
            return;
        }

        elements = {
            logOutput: document.getElementById("log-output"),
            logSource: document.getElementById("log-source-select"),
            logError: document.getElementById("log-error"),
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
            envSource: document.getElementById("env-source"),
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
        setLogsUI('');
        elements.logSource?.addEventListener('change', () => {
            logSource = elements.logSource.value;
            lastLogsSnapshot = '';
            setLogsUI('');
            showLogError('');
            syncLogSubscription();
        });
        document.addEventListener('visibilitychange', syncLogSubscription);

        if (elements.powerBtn) {
            elements.powerBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                if (elements.powerDropdown) {
                    const hidden = elements.powerDropdown.classList.toggle("hidden");
                    elements.powerBtn.setAttribute('aria-expanded', String(!hidden));
                }
            });
        }

        document.addEventListener("click", (e) => {
            const menu = document.getElementById("power-menu");
            if (!menu || !menu.contains(e.target)) {
                if (elements.powerDropdown) elements.powerDropdown.classList.add("hidden");
                elements.powerBtn?.setAttribute('aria-expanded', 'false');
            }
        });

        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && elements.powerDropdown && !elements.powerDropdown.classList.contains('hidden')) {
                elements.powerDropdown.classList.add('hidden');
                elements.powerBtn?.setAttribute('aria-expanded', 'false');
                elements.powerBtn?.focus();
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
        bindMotionToggle();
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
        if (elements.envSource) {
            elements.envSource.addEventListener('change', () => {
                if (envBaseline !== null && elements.envTextarea.value !== envBaseline
                    && !confirm('Discard your unsaved edits and open another configuration?')) {
                    elements.envSource.value = envSource;
                    return;
                }
                loadEnvContent(elements.envSource.value);
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
                    elements.powerBtn?.setAttribute('aria-expanded', 'false');
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
        if (clearLogsBtn) clearLogsBtn.addEventListener("click", clearLogView);
        
        // Set current log level in dropdown
        const logLevelSelect = document.getElementById("log-level-select");
        if (logLevelSelect && cfg.LOG_LEVEL) {
            logLevelSelect.value = cfg.LOG_LEVEL;
        }

        // Load startup flap settings into their toggles. These
        // come from the configuration this function was handed - fetching
        // /config again here asked the device for a payload already in hand.
        const data = cfg || {};
        const flapOnBootCheckbox = document.getElementById("FLAP_ON_BOOT");
        if (flapOnBootCheckbox) {
            flapOnBootCheckbox.checked = data.FLAP_ON_BOOT === 'True' || data.FLAP_ON_BOOT === true;
        }
        const showTooltipsCheckbox = document.getElementById("SHOW_TOOLTIPS");
        if (showTooltipsCheckbox) {
            // Absent key defaults to shown, same as core/config.py's fallback.
            showTooltipsCheckbox.checked = !(data.SHOW_TOOLTIPS === 'False' || data.SHOW_TOOLTIPS === false);
        }

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
        openLogPanel,
    };
})();

// Make LogPanel available globally
window.LogPanel = LogPanel;
