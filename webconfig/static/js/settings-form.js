// ===================== SETTINGS FORM =====================
const SettingsForm = (() => {
    const MODEL_ALIASES = {
        // Legacy/compat aliases mapped to current options
        'gpt-4o-realtime': 'gpt-realtime',
        'gpt-4o-realtime-preview': 'gpt-4o-mini-realtime-preview'
    };

    const normalizeModelValue = (value) => {
        if (!value) return value;
        const v = String(value).trim();
        return MODEL_ALIASES[v] || v;
    };

    const BOOLEAN_SELECT_IDS = new Set([
        'AEC_ENABLED',
        'STATUS_LED_ENABLED',
        'WAKE_WORD_ENABLED',
    ]);

    const normalizeSelectValue = (id, value) => {
        if (value === undefined || value === null) return value;
        if (BOOLEAN_SELECT_IDS.has(id)) {
            const normalized = String(value).trim().toLowerCase();
            if (['true', '1', 'yes', 'y', 'on', 'enabled'].includes(normalized)) {
                return 'true';
            }
            if (['false', '0', 'no', 'n', 'off', 'disabled'].includes(normalized)) {
                return 'false';
            }
        }
        if (id === 'OPENAI_MODEL' || id === 'XAI_MODEL') {
            return normalizeModelValue(value);
        }
        return String(value).trim();
    };

    const setSelectValueSafely = (element, value) => {
        if (!element || !value) return false;
        const exists = Array.from(element.options).some(opt => opt.value === value);
        if (!exists) return false;
        element.value = value;
        return true;
    };

    const ensureSelectHasValue = (element, preferredValue, fallbackValue = null) => {
        if (!element) return false;
        if (setSelectValueSafely(element, preferredValue)) return true;
        if (setSelectValueSafely(element, fallbackValue)) return true;
        if (element.options.length > 0) {
            element.value = element.options[0].value;
            return true;
        }
        return false;
    };

    const getCameraSelectionFromConfig = (cfg) => {
        const hardware = String(cfg?.CAMERA_HARDWARE || "none").trim().toLowerCase();
        const index = String(cfg?.CAMERA_DEVICE_INDEX || "0").trim();
        if (hardware === "usb_webcam") return `usb_webcam:${index}`;
        if (hardware === "rpi_camera") return "rpi_camera";
        return "none";
    };

    const populateCameraHardwareDropdown = async (cfg = null, preferredValue = null) => {
        const select = document.getElementById("CAMERA_HARDWARE");
        if (!select) return;

        const fallbackOptions = [
            { value: "none", label: "None" },
            { value: "rpi_camera", label: "Raspberry Pi Camera Module" },
            { value: "usb_webcam", label: "USB Webcam" }
        ];
        const renderOptions = (entries) => {
            const usableEntries = Array.isArray(entries)
                ? entries.filter(entry => entry && entry.value)
                : [];
            const options = usableEntries.length > 0 ? usableEntries : fallbackOptions;
            select.replaceChildren(...options.map((entry) => {
                const opt = document.createElement("option");
                opt.value = String(entry.value);
                opt.textContent = String(entry.label || entry.value);
                return opt;
            }));
        };

        try {
            const response = await fetch("/camera/devices");
            if (!response.ok) {
                console.error(`Camera discovery returned HTTP ${response.status}`);
                return;
            }
            const data = await response.json();
            renderOptions(data.options);

            const savedSelection = localStorage.getItem("dropdown_CAMERA_HARDWARE");
            const configSelection = getCameraSelectionFromConfig(cfg || {});
            const hasConfiguredSelection = Boolean(
                cfg && Object.prototype.hasOwnProperty.call(cfg, "CAMERA_HARDWARE")
            );
            const target = preferredValue
                || (hasConfiguredSelection ? configSelection : savedSelection)
                || "none";
            if (!setSelectValueSafely(select, target)) {
                if (!setSelectValueSafely(select, configSelection)) {
                    setSelectValueSafely(select, "none");
                }
            }
            localStorage.setItem("dropdown_CAMERA_HARDWARE", select.value);
        } catch (error) {
            console.error("Failed to load detected camera devices:", error);
            if (select.options.length === 0) {
                renderOptions(fallbackOptions);
            }
            const configSelection = getCameraSelectionFromConfig(cfg || {});
            ensureSelectHasValue(
                select,
                preferredValue || configSelection,
                "none"
            );
        }
    };

    const bindCameraPreview = () => {
        const button = document.getElementById("test-camera-btn");
        const select = document.getElementById("CAMERA_HARDWARE");
        const rotationInput = document.getElementById("CAMERA_ROTATION");
        const rotationButton = document.getElementById("camera-rotation-btn");
        const rotationIcon = document.getElementById("camera-rotation-icon");
        const rotationLabel = document.getElementById("camera-rotation-label");
        const image = document.getElementById("camera-preview-image");
        const status = document.getElementById("camera-preview-status");
        if (!button || !select || !image || !status || !rotationInput || !rotationButton || !rotationIcon || !rotationLabel) return;

        const setStatus = (text, isError = false) => {
            status.textContent = text;
            status.classList.toggle("text-red-300", isError);
            status.classList.toggle("text-slate-400", !isError);
        };

        const clearPreview = () => {
            image.removeAttribute("src");
            image.classList.add("hidden");
        };

        const normalizeRotation = (value) => {
            const parsed = Number.parseInt(String(value || "0"), 10);
            if (Number.isNaN(parsed)) return 0;
            const normalized = ((parsed % 360) + 360) % 360;
            return [0, 90, 180, 270].includes(normalized) ? normalized : 0;
        };

        const updateRotationUi = (value) => {
            const rotation = normalizeRotation(value);
            rotationInput.value = String(rotation);
            rotationLabel.textContent = `${rotation}°`;
            rotationIcon.style.transform = `rotate(${rotation}deg)`;
        };

        updateRotationUi(rotationInput.value);

        select.addEventListener("change", () => {
            clearPreview();
            setStatus("Camera selection changed. Run test again.");
        });

        rotationButton.addEventListener("click", () => {
            const nextRotation = (normalizeRotation(rotationInput.value) + 90) % 360;
            updateRotationUi(nextRotation);
            clearPreview();
            setStatus(`Orientation set to ${nextRotation}°. Run test again.`);
        });

        button.addEventListener("click", async () => {
            const selection = String(select.value || "none");
            const rotation = normalizeRotation(rotationInput.value);
            button.disabled = true;
            button.classList.add("opacity-60", "cursor-not-allowed");
            setStatus("Capturing preview...");
            try {
                const response = await fetch("/camera/preview", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({selection, rotation}),
                });
                const data = await response.json();
                if (!response.ok || !data.ok) {
                    const error = data.error || "Preview failed";
                    clearPreview();
                    setStatus(error, true);
                    showNotification(`Camera preview failed: ${error}`, "error", 4000);
                    return;
                }

                image.src = data.image_url;
                image.classList.remove("hidden");
                setStatus(`Preview captured (${data.bytes || 0} bytes).`);
                showNotification("Camera preview updated", "success", 2000);
            } catch (error) {
                clearPreview();
                setStatus(String(error), true);
                showNotification(`Camera preview failed: ${error}`, "error", 4000);
            } finally {
                button.disabled = false;
                button.classList.remove("opacity-60", "cursor-not-allowed");
            }
        });
    };

    const populateDropdowns = (cfg) => {
        // Populate dropdown values with saved configuration
        const dropdowns = [
            { id: 'REALTIME_AI_PROVIDER', key: 'REALTIME_AI_PROVIDER' },
            { id: 'OPENAI_MODEL', key: 'OPENAI_MODEL' },
            { id: 'XAI_MODEL', key: 'XAI_MODEL' },
            { id: 'VOICE', key: 'VOICE' },
            { id: 'RUN_MODE', key: 'RUN_MODE' },
            { id: 'TURN_EAGERNESS', key: 'TURN_EAGERNESS' },
            { id: 'BILLY_MODEL', key: 'BILLY_MODEL' },
            { id: 'CAMERA_HARDWARE', key: 'CAMERA_HARDWARE' },
            { id: 'BILLY_PINS_SELECT', key: 'BILLY_PINS' },
            { id: 'HA_LANG', key: 'HA_LANG' },
            { id: 'STATUS_LED_ENABLED', key: 'STATUS_LED_ENABLED' },
            { id: 'WAKE_WORD_ENABLED', key: 'WAKE_WORD_ENABLED' },
            { id: 'AEC_ENABLED', key: 'AEC_ENABLED' },
            { id: 'AEC_BARGE_IN_SNR_DB', key: 'AEC_BARGE_IN_SNR_DB' },
            { id: 'WAKE_WORD_BACKEND', key: 'WAKE_WORD_BACKEND' },
            { id: 'WIFI_COUNTRY', key: 'WIFI_COUNTRY' }
        ];

        dropdowns.forEach(({ id, key }) => {
            const element = document.getElementById(id);
            if (element) {
                if (id === 'CAMERA_HARDWARE') {
                    populateCameraHardwareDropdown(cfg);
                    return;
                }
                // First try to get from localStorage (user's last selection)
                const savedValue = localStorage.getItem(`dropdown_${id}`);
                // Then fall back to config value
                const configValue = id === "REALTIME_AI_PROVIDER"
                    ? (cfg[key] || "openai")
                    : cfg[key];
                // For backend/model/boolean selectors, prefer .env/config over localStorage.
                const preferConfigValue = id === 'OPENAI_MODEL'
                    || id === 'XAI_MODEL'
                    || id === 'WAKE_WORD_BACKEND'
                    || id === 'REALTIME_AI_PROVIDER'
                    || BOOLEAN_SELECT_IDS.has(id);
                const preferredValue = preferConfigValue ? configValue : (savedValue || configValue);
                const fallbackValue = preferConfigValue ? savedValue : null;

                if (preferConfigValue) {
                    const normalizedPreferred = normalizeSelectValue(id, preferredValue);
                    const normalizedFallback = normalizeSelectValue(id, fallbackValue);

                    if (ensureSelectHasValue(element, normalizedPreferred, normalizedFallback)) {
                        localStorage.setItem(`dropdown_${id}`, element.value);
                    } else {
                        // Clear stale localStorage when no matching option exists.
                        localStorage.removeItem(`dropdown_${id}`);
                    }
                } else if (preferredValue) {
                    ensureSelectHasValue(
                        element,
                        normalizeSelectValue(id, preferredValue),
                        id === "WIFI_COUNTRY" ? "NL" : null
                    );
                } else if (id === "WIFI_COUNTRY") {
                    ensureSelectHasValue(element, "NL");
                }
            }
        });

        const cameraRotationInput = document.getElementById("CAMERA_ROTATION");
        const cameraRotationLabel = document.getElementById("camera-rotation-label");
        const cameraRotationIcon = document.getElementById("camera-rotation-icon");
        if (cameraRotationInput && cameraRotationLabel && cameraRotationIcon) {
            const parsed = Number.parseInt(String(cfg?.CAMERA_ROTATION || "0"), 10);
            const rotation = [0, 90, 180, 270].includes(parsed) ? parsed : 0;
            cameraRotationInput.value = String(rotation);
            cameraRotationLabel.textContent = `${rotation}°`;
            cameraRotationIcon.style.transform = `rotate(${rotation}deg)`;
        }
    };

    const saveDropdownSelections = () => {
        // Save dropdown selections to localStorage when they change
        const dropdowns = [
            'REALTIME_AI_PROVIDER', 'OPENAI_MODEL', 'XAI_MODEL', 'VOICE', 'RUN_MODE', 'TURN_EAGERNESS',
            'BILLY_MODEL', 'CAMERA_HARDWARE', 'BILLY_PINS_SELECT', 'HA_LANG', 'STATUS_LED_ENABLED',
            'WAKE_WORD_ENABLED', 'WAKE_WORD_BACKEND', 'WIFI_COUNTRY', 'AEC_ENABLED',
            'AEC_BARGE_IN_SNR_DB'
        ];

        dropdowns.forEach(id => {
            const element = document.getElementById(id);
            if (element) {
                element.addEventListener('change', () => {
                    localStorage.setItem(`dropdown_${id}`, element.value);
                });
            }
        });
    };

    const handleSettingsSave = () => {
        const form = document.getElementById("config-form");
        if (!form || form.dataset.bound === "true") {
            return;
        }
        form.dataset.bound = "true";
        form.addEventListener("submit", async function (e) {
            e.preventDefault();

            const saveButton = document.getElementById("save-btn");
            const saveDropdownButton = document.getElementById("dropdown-btn");
            const originalSaveMarkup = saveButton?.innerHTML;
            if (saveButton) {
                saveButton.disabled = true;
                saveButton.classList.add("opacity-70", "cursor-not-allowed");
                saveButton.innerHTML = '<span class="material-icons align-middle animate-spin">sync</span>Saving…';
            }
            if (saveDropdownButton) saveDropdownButton.disabled = true;

            try {

            const formData = new FormData(this);
            const payload = Object.fromEntries(formData.entries());

            const flaskPortInput = document.getElementById("FLASK_PORT");
            const oldPort = parseInt(flaskPortInput.getAttribute("data-original")) || 80;
            const newPort = parseInt(payload["FLASK_PORT"] || "80");

            const hostnameInput = document.getElementById("hostname");
            const oldHostname = (hostnameInput.getAttribute("data-original") || hostnameInput.defaultValue || "").trim();
            const newHostname = (formData.get("hostname") || "").trim();

            const pinSelect = document.getElementById("BILLY_PINS_SELECT");
            if (pinSelect) {
                payload.BILLY_PINS = pinSelect.value; // "new" | "legacy"
            }

            const cameraSelect = document.getElementById("CAMERA_HARDWARE");
            if (cameraSelect) {
                const selected = String(cameraSelect.value || "none");
                if (selected.startsWith("usb_webcam:")) {
                    const idx = selected.split(":")[1] || "0";
                    payload.CAMERA_HARDWARE = "usb_webcam";
                    payload.CAMERA_DEVICE_INDEX = idx;
                } else if (selected === "usb_webcam") {
                    payload.CAMERA_HARDWARE = "usb_webcam";
                    payload.CAMERA_DEVICE_INDEX = String(payload.CAMERA_DEVICE_INDEX || "0");
                } else if (selected === "rpi_camera") {
                    payload.CAMERA_HARDWARE = "rpi_camera";
                    payload.CAMERA_DEVICE_INDEX = "0";
                } else {
                    payload.CAMERA_HARDWARE = "none";
                }
            }

            // Manually add MOUTH_ARTICULATION value
            const mouthArticulationInput = document.getElementById("MOUTH_ARTICULATION");
            if (mouthArticulationInput) {
                payload.MOUTH_ARTICULATION = mouthArticulationInput.value;
            }

            // Manually add SHOW_RC_VERSIONS value (only set to True when checked)
            const showRCVersionsCheckbox = document.getElementById("SHOW_RC_VERSIONS");
            if (showRCVersionsCheckbox && showRCVersionsCheckbox.checked) {
                payload.SHOW_RC_VERSIONS = 'True';
            } else {
                payload.SHOW_RC_VERSIONS = 'False';
            }

            // Manually add FLAP_ON_BOOT value (only set to True when checked)
            const flapOnBootCheckbox = document.getElementById("FLAP_ON_BOOT");
            if (flapOnBootCheckbox && flapOnBootCheckbox.checked) {
                payload.FLAP_ON_BOOT = 'True';
            } else {
                payload.FLAP_ON_BOOT = 'False';
            }

            let hostnameChanged = false;

            const saveResponse = await fetch("/save", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload),
            });
            const saveResult = await saveResponse.json();
            if (!saveResponse.ok || saveResult.status !== "ok") {
                throw new Error(saveResult.error || "The settings could not be saved");
            }
            const portChanged = saveResult.port_changed || (oldPort !== newPort);

            // Re-read the saved .env through webconfig so controls stay aligned
            // with normalized server values without restarting the web UI.
            try {
                const refreshedConfig = await ConfigService.fetchConfig(true);
                if (refreshedConfig) {
                    refreshFromConfig(refreshedConfig);
                }
            } catch (refreshError) {
                // Saving succeeded; a control-refresh failure must not be
                // reported as if writing .env failed.
                console.error("Settings saved, but form refresh failed:", refreshError);
            }

            if (newHostname && newHostname !== oldHostname) {
                const hostResponse = await fetch("/hostname", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({hostname: newHostname})
                });
                const hostResult = await hostResponse.json();
                if (hostResult.hostname) {
                    hostnameChanged = true;
                    showNotification(`Hostname updated to ${hostResult.hostname}.local`, "success", 5000);
                }
            }

            if (portChanged || hostnameChanged) {
                try {
                    await fetch("/restart", {method: "POST"});
                    showNotification("Settings saved — restarting interface in background", "success");
                } catch (error) {
                    console.error("Failed to restart Billy services after save:", error);
                    showNotification("Settings saved, but the background restart failed", "warning", 5000);
                }

                const targetHost = hostnameChanged ? `${newHostname}.local` : window.location.hostname;
                const targetPort = portChanged ? newPort : (window.location.port || 80);

                showNotification(`Redirecting to http://${targetHost}:${targetPort}/...`, "warning", 5000);
                setTimeout(() => {
                    window.location.href = `http://${targetHost}:${targetPort}/`;
                }, 3000);
            } else {
                try {
                    sessionStorage.removeItem("billy:reload_on_ws_reconnect");
                    await fetch("/restart-billy", {method: "POST"});
                    showNotification("Settings saved — restarting Billy in background", "success");
                } catch (error) {
                    console.error("Failed to restart Billy after save:", error);
                    showNotification("Settings saved, but the Billy restart failed", "warning", 5000);
                }
            }
            } catch (error) {
                console.error("Failed to save settings:", error);
                showNotification(`Settings were not saved: ${error.message}`, "error", 6000);
            } finally {
                if (saveButton) {
                    saveButton.disabled = false;
                    saveButton.classList.remove("opacity-70", "cursor-not-allowed");
                    saveButton.innerHTML = originalSaveMarkup;
                }
                if (saveDropdownButton) saveDropdownButton.disabled = false;
            }
        });
    };

    const bindFactoryReset = () => {
        const resetBtn = document.getElementById("factory-reset-btn");
        const resetBtnWrapper = document.getElementById("factory-reset-btn-wrapper");
        const resetCard = document.getElementById("reset-defaults-card");
        const modal = document.getElementById("factory-reset-modal");
        const closeBtn = document.getElementById("close-factory-reset-modal");
        const cancelBtn = document.getElementById("cancel-factory-reset");
        const confirmBtn = document.getElementById("confirm-factory-reset");
        const envCheckbox = document.getElementById("factory-reset-env");
        const profilesCheckbox = document.getElementById("factory-reset-profiles");
        const personasCheckbox = document.getElementById("factory-reset-personas");
        const logsCheckbox = document.getElementById("factory-reset-logs");
        const gitCheckbox = document.getElementById("factory-reset-git");
        const wifiCheckbox = document.getElementById("factory-reset-wifi");
        const rebootCheckbox = document.getElementById("factory-reset-reboot");
        const advancedWrap = document.getElementById("factory-reset-advanced");
        const advancedToggle = document.getElementById("toggle-factory-advanced");

        if (!resetBtn || !resetBtnWrapper) return;
        if (!modal || !closeBtn || !cancelBtn || !confirmBtn) return;
        if (wifiCheckbox && rebootCheckbox) {
            wifiCheckbox.addEventListener("change", () => {
                if (wifiCheckbox.checked) {
                    rebootCheckbox.checked = true;
                    rebootCheckbox.disabled = true;
                } else {
                    rebootCheckbox.disabled = false;
                }
            });
        }
        if (advancedWrap && advancedToggle) {
            advancedToggle.addEventListener("click", () => {
                const isHidden = advancedWrap.classList.contains("hidden");
                advancedWrap.classList.toggle("hidden", !isHidden);
                advancedToggle.textContent = isHidden
                    ? "Hide advanced settings"
                    : "Show advanced settings";
            });
        }

        const openModal = () => {
            modal.classList.remove("hidden");
        };
        const closeModal = () => {
            modal.classList.add("hidden");
        };

        if (resetCard) {
            resetCard.addEventListener("click", () => {
                const isHidden = resetBtnWrapper.classList.contains("hidden");
                if (isHidden) {
                    resetBtnWrapper.classList.remove("hidden");
                } else {
                    resetBtnWrapper.classList.add("hidden");
                }
            });
        }

        resetBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            openModal();
        });

        closeBtn.addEventListener("click", closeModal);
        cancelBtn.addEventListener("click", closeModal);
        modal.addEventListener("click", (e) => {
            if (e.target === modal) closeModal();
        });

        confirmBtn.addEventListener("click", async () => {
            const options = {
                env: envCheckbox?.checked ?? false,
                profiles: profilesCheckbox?.checked ?? false,
                personas: personasCheckbox?.checked ?? false,
                logs: logsCheckbox?.checked ?? false,
                git: gitCheckbox?.checked ?? false,
                wifi: wifiCheckbox?.checked ?? false,
                reboot: rebootCheckbox?.checked ?? false,
            };

            const anySelected = Object.values(options).some(Boolean);
            if (!anySelected) {
                showNotification("Select at least one reset option.", "warning", 4000);
                return;
            }

            confirmBtn.disabled = true;
            confirmBtn.classList.add("opacity-50", "cursor-not-allowed");
            resetBtn.disabled = true;
            resetBtn.classList.add("opacity-50", "cursor-not-allowed");
            showNotification("Running reset to defaults...", "warning", 4000);

            try {
                const response = await fetch("/factory-reset", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({confirm: true, options}),
                });
                const data = await response.json();

                if (response.ok && data.status === "ok") {
                    const summary = [];
                    if (data.removed?.env) summary.push(".env");
                    if (data.removed?.versions) summary.push("versions.ini");
                    if (data.removed?.profiles?.length) summary.push(`${data.removed.profiles.length} profile(s)`);
                    if (data.requested?.personas) {
                        const personaCount = data.removed?.personas?.length || 0;
                        summary.push(`${personaCount} persona(s)`);
                    }
                    if (data.logs_cleared) summary.push("service logs");
                    if (data.git_reset) summary.push("git worktree");
                    if (data.requested?.wifi) {
                        summary.push("Wi-Fi connection");
                    }
                    const msg = summary.length
                        ? `Reset to defaults complete: ${summary.join(", ")}`
                        : "Reset to defaults complete.";
                    let postfix = "";
                    if (data.rebooting) {
                        postfix = " Rebooting now...";
                    } else if (data.restarting_services) {
                        postfix = " Restarting UI...";
                    }
                    showNotification(`${msg}${postfix}`, "success", 6000);
                    if (data.restarting_services) {
                        try {
                            await fetch("/restart", {method: "POST"});
                            setTimeout(() => location.reload(), 3000);
                        } catch (restartErr) {
                            console.error("Failed to restart UI:", restartErr);
                        }
                    }
                    closeModal();
                } else {
                    const errors = data.errors?.length ? data.errors.join("; ") : (data.error || "Reset to defaults incomplete");
                    showNotification(errors, "error", 8000);
                }
            } catch (error) {
                console.error("Reset to defaults failed:", error);
                showNotification("Reset to defaults failed", "error", 6000);
            } finally {
                confirmBtn.disabled = false;
                confirmBtn.classList.remove("opacity-50", "cursor-not-allowed");
                resetBtn.disabled = false;
                resetBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
        });
    };

    const bindEnvEditorCard = () => {
        const envEditorCard = document.getElementById("env-editor-card");
        const envEditorBtnWrapper = document.getElementById("env-editor-btn-wrapper");
        const openEnvEditorBtn = document.getElementById("open-env-editor-modal-btn");

        if (!envEditorCard || !envEditorBtnWrapper || !openEnvEditorBtn) return;

        envEditorCard.addEventListener("click", (e) => {
            if (e.target.closest("#open-env-editor-modal-btn")) return;
            const isHidden = envEditorBtnWrapper.classList.contains("hidden");
            envEditorBtnWrapper.classList.toggle("hidden", !isHidden);
        });

        openEnvEditorBtn.addEventListener("click", (e) => {
            e.stopPropagation();
        });
    };

    const initHostFields = async () => {
        const hostnameInput = document.getElementById("hostname");
        const flaskPortInput = document.getElementById("FLASK_PORT");

        if (flaskPortInput) {
            flaskPortInput.setAttribute("data-original", flaskPortInput.value);
        }

        if (!hostnameInput) return;

        try {
            const res = await fetch("/hostname");
            const data = await res.json();
            if (data.hostname) {
                hostnameInput.value = data.hostname;
                hostnameInput.setAttribute("data-original", data.hostname);
            }
        } catch (error) {
            console.error("Failed to load hostname:", error);
        }
    };

    const initMouthArticulationSlider = () => {
        // Use the same slider pattern as mic gain
        setupSlider("mouth-articulation-bar", "mouth-articulation-fill", "MOUTH_ARTICULATION", 1, 10);
    };

    const initStatusLedBrightnessSlider = () => {
        setupSlider(
            "status-led-brightness-bar",
            "status-led-brightness-fill",
            "STATUS_LED_BRIGHTNESS",
            0,
            1,
            {
                step: 0.05,
                valueDisplayId: "status-led-brightness-value",
                valueFormatter: (val) => `${Math.round(Number(val) * 100)}%`,
            }
        );
    };

    function setupSlider(barId, fillId, inputId, min, max, options = {}) {
        const bar = document.getElementById(barId);
        const fill = document.getElementById(fillId);
        const input = document.getElementById(inputId);
        const step = Number(options.step ?? 1);
        const valueDisplayId = options.valueDisplayId || "mouth-articulation-value";
        const decimals = Number.isInteger(options.decimals) ? options.decimals : 0;
        const valueFormatter = typeof options.valueFormatter === "function"
            ? options.valueFormatter
            : (val) => Number(val).toFixed(decimals);

        if (!bar || !fill || !input) return;

        let isDragging = false;
        const updateUI = (val) => {
            const percent = ((val - min) / (max - min)) * 100;
            fill.style.width = `${percent}%`;
            fill.dataset.value = val;
            // Ensure input value is set for form submission
            input.value = val;
            input.setAttribute('value', val);
            // Update the value display
            const valueDisplay = document.getElementById(valueDisplayId);
            if (valueDisplay) {
                valueDisplay.textContent = valueFormatter(val);
            }
        };
        const updateFromMouse = (e) => {
            const rect = bar.getBoundingClientRect();
            const percent = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1);
            const rawVal = min + percent * (max - min);
            const steppedVal = Math.round(rawVal / step) * step;
            const val = Math.min(max, Math.max(min, Number(steppedVal.toFixed(4))));
            input.value = val;
            // Ensure the input value is properly set for form submission
            input.setAttribute('value', val);
            input.dispatchEvent(new Event("input", {bubbles: true}));
            updateUI(val);
        };
        bar.addEventListener("mousedown", (e) => { isDragging = true; updateFromMouse(e); });
        document.addEventListener("mousemove", (e) => { if (isDragging) updateFromMouse(e); });
        document.addEventListener("mouseup", () => { isDragging = false; });
        input.addEventListener("input", () => updateUI(Number(input.value)));
        updateUI(Number(input.value));
    }

    const refreshFromConfig = (config) => {
        // Update dropdowns with new configuration values
        const dropdowns = [
            'REALTIME_AI_PROVIDER', 'OPENAI_MODEL', 'XAI_MODEL', 'VOICE', 'RUN_MODE', 'TURN_EAGERNESS',
            'BILLY_MODEL', 'BILLY_PINS_SELECT', 'HA_LANG', 'STATUS_LED_ENABLED',
            'WAKE_WORD_ENABLED', 'WAKE_WORD_BACKEND', 'AEC_ENABLED',
            'AEC_BARGE_IN_SNR_DB'
        ];
        dropdowns.forEach(id => {
            const element = document.getElementById(id);
            if (element && config[id] !== undefined && config[id] !== null) {
                const normalizedValue = normalizeSelectValue(id, config[id]);
                if (ensureSelectHasValue(element, normalizedValue)) {
                    localStorage.setItem(`dropdown_${id}`, element.value);
                } else {
                    localStorage.removeItem(`dropdown_${id}`);
                }
            }
        });
        const followUpRetryInput = document.getElementById("FOLLOW_UP_RETRY_LIMIT");
        if (
            followUpRetryInput
            && config.FOLLOW_UP_RETRY_LIMIT !== undefined
            && config.FOLLOW_UP_RETRY_LIMIT !== null
        ) {
            followUpRetryInput.value = String(config.FOLLOW_UP_RETRY_LIMIT);
        }
        populateCameraHardwareDropdown(config);
        const cameraRotationInput = document.getElementById("CAMERA_ROTATION");
        const cameraRotationLabel = document.getElementById("camera-rotation-label");
        const cameraRotationIcon = document.getElementById("camera-rotation-icon");
        const rotationValue = Number.parseInt(String(config.CAMERA_ROTATION || "0"), 10);
        const normalizedRotation = [0, 90, 180, 270].includes(rotationValue) ? rotationValue : 0;
        if (cameraRotationInput) {
            cameraRotationInput.value = String(normalizedRotation);
        }
        if (cameraRotationLabel) {
            cameraRotationLabel.textContent = `${normalizedRotation}°`;
        }
        if (cameraRotationIcon) {
            cameraRotationIcon.style.transform = `rotate(${normalizedRotation}deg)`;
        }
        const ledBrightness = document.getElementById("STATUS_LED_BRIGHTNESS");
        if (ledBrightness && config.STATUS_LED_BRIGHTNESS) {
            ledBrightness.value = config.STATUS_LED_BRIGHTNESS;
        }
    };

    const bindWakeWordKeywordUpload = () => {
        const uploadBtn = document.getElementById("wakeword-upload-keyword-btn");
        const fileInput = document.getElementById("wakeword-keyword-file");
        const status = document.getElementById("wakeword-keyword-status");
        const keywordPathInput = document.getElementById("WAKE_WORD_PORCUPINE_KEYWORD_PATH");
        const openWakeWordModelInput = document.getElementById("WAKE_WORD_OPENWAKEWORD_MODEL_PATH");
        const backendSelect = document.getElementById("WAKE_WORD_BACKEND");
        if (!keywordPathInput || !openWakeWordModelInput || !backendSelect) return;

        const normalizeWakeWordName = (value) => {
            const raw = String(value || "").trim();
            if (!raw) return "";
            const parts = raw.split(/[\\/]/);
            return parts[parts.length - 1] || raw;
        };

        const syncWakeWordProviderFields = () => {
            const provider = String(backendSelect.value || "porcupine").trim().toLowerCase();
            document.querySelectorAll("[data-wakeword-provider]").forEach((element) => {
                const match = element.dataset.wakewordProvider === provider;
                element.classList.toggle("hidden", !match);
            });
        };

        const populateSelectOptions = (select, options, preferredPath = null) => {
            if (!select) return;
            const currentValue = normalizeWakeWordName(
                preferredPath || select.value || select.dataset.current || ""
            );
            const merged = [...new Set([...options, currentValue].filter(Boolean))];
            select.innerHTML = "";
            merged.forEach((name) => {
                const opt = document.createElement("option");
                opt.value = name;
                opt.textContent = name;
                select.appendChild(opt);
            });
            if (currentValue && merged.includes(currentValue)) {
                select.value = currentValue;
            }
        };

        const loadWakeWordOptions = async (preferredKeywordPath = null, preferredModelPath = null) => {
            try {
                const response = await fetch("/wakeword/keywords");
                const data = await response.json();
                const keywordOptions = Array.isArray(data.keywords)
                    ? data.keywords.map(normalizeWakeWordName).filter(Boolean)
                    : [];
                const modelOptions = Array.isArray(data.models)
                    ? data.models.map(normalizeWakeWordName).filter(Boolean)
                    : [];
                populateSelectOptions(keywordPathInput, keywordOptions, preferredKeywordPath);
                populateSelectOptions(openWakeWordModelInput, modelOptions, preferredModelPath);
            } catch (error) {
                console.error("Failed to load wake-word options:", error);
            }
        };

        loadWakeWordOptions();
        syncWakeWordProviderFields();
        backendSelect.addEventListener("change", syncWakeWordProviderFields);

        if (!uploadBtn || !fileInput) return;
        uploadBtn.addEventListener("click", async () => {
            const file = fileInput.files && fileInput.files[0];
            if (!file) {
                showNotification("Select a .ppn or .onnx file first", "warning", 3000);
                return;
            }
            const lowerName = file.name.toLowerCase();
            const isPorcupine = lowerName.endsWith(".ppn");
            const isOpenWakeWord = lowerName.endsWith(".onnx");
            if (!isPorcupine && !isOpenWakeWord) {
                showNotification("Only .ppn and .onnx files are supported", "warning", 3000);
                return;
            }

            const formData = new FormData();
            formData.append("keyword_file", file);

            uploadBtn.disabled = true;
            uploadBtn.classList.add("opacity-50", "cursor-not-allowed");
            if (status) {
                status.textContent = `Uploading ${file.name}...`;
            }
            try {
                const response = await fetch("/wakeword/model/upload", {
                    method: "POST",
                    body: formData,
                });
                const data = await response.json();
                if (!response.ok) {
                    const errorMessage = data.error || "Upload failed";
                    console.error("Wake-word file upload failed:", errorMessage);
                    if (status) {
                        status.textContent = `Upload failed: ${errorMessage}`;
                    }
                    showNotification(`Upload failed: ${errorMessage}`, "error", 5000);
                    return;
                }
                if (data.backend === "porcupine" && data.filename) {
                    await loadWakeWordOptions(data.filename, null);
                } else if (data.backend === "openwakeword" && data.filename) {
                    await loadWakeWordOptions(null, data.filename);
                }
                if (backendSelect && data.backend) {
                    backendSelect.value = data.backend;
                    syncWakeWordProviderFields();
                }
                if (status) {
                    status.textContent = `Uploaded ${data.filename} for ${data.backend}`;
                }
                fileInput.value = "";
                showNotification(`${data.filename} uploaded`, "success", 3000);
            } catch (error) {
                console.error("Wake-word file upload failed:", error);
                if (status) {
                    status.textContent = `Upload failed: ${error.message}`;
                }
                showNotification(`Upload failed: ${error.message}`, "error", 5000);
            } finally {
                uploadBtn.disabled = false;
                uploadBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
        });
    };

    const bindApiProviderFields = () => {
        const providerSelect = document.getElementById("REALTIME_AI_PROVIDER");
        if (!providerSelect) return;

        const syncProviderFields = () => {
            const provider = String(providerSelect.value || "openai").trim().toLowerCase();
            document.querySelectorAll("[data-api-provider]").forEach((element) => {
                const match = element.dataset.apiProvider === provider;
                element.classList.toggle("hidden", !match);
                element.toggleAttribute("hidden", !match);
                element.style.display = match ? "" : "none";
            });
        };

        syncProviderFields();
        providerSelect.addEventListener("change", syncProviderFields);
    };

    const NEWS_SOURCE_TEMPLATES = {
        google_news_headlines: {
            name: "Google News (Headlines)",
            url: "https://news.google.com/rss?hl=en-GB&gl=GB&ceid=GB:en",
            topics: ["general", "headlines"],
        },
        google_news_localized: {
            name: "Google News (Localized search)",
            url: "https://news.google.com/rss/search?q={{query}}&hl=en-GB&gl=GB&ceid=GB:en",
            topics: ["general", "headlines"],
        },
        open_meteo_forecast: {
            name: "Open-Meteo Forecast (Amsterdam)",
            url: "https://api.open-meteo.com/v1/forecast?latitude=52.3676&longitude=4.9041&timezone=Europe%2FAmsterdam",
            topics: ["weather", "forecast"],
        },
        espn_premier_league: {
            name: "ESPN Scoreboard (Premier League)",
            url: "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
            topics: ["sports", "epl", "soccer", "football"],
        },
        espn_soccer_news: {
            name: "ESPN Soccer News (Serie A)",
            url: "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/news",
            topics: ["sports", "soccer", "football", "news", "serie a", "italy"],
        },
        espn_team_info_napoli: {
            name: "ESPN Team Info (Napoli)",
            url: "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/teams/114",
            topics: ["sports", "soccer", "football", "team", "napoli", "serie a"],
        },
        billy_project_releases: {
            name: "Billy Project Releases",
            url: "https://github.com/Thokoop/billy-b-assistant/releases.atom",
            topics: ["billy", "project", "release", "update", "changelog"],
        },
    };

    const isDynamicSourceUrl = (rawUrl) => {
        let candidate = String(rawUrl || "");
        if (!candidate) return false;
        candidate = candidate
            .replaceAll("&amp;", "&")
            .replaceAll("&#123;", "{")
            .replaceAll("&#125;", "}");

        if (/\{\{\s*query\s*}}/i.test(candidate)) return true;
        if (/\{\s*query\s*}/i.test(candidate)) return true;
        if (/%7B%7B\s*query\s*%7D%7D/i.test(candidate)) return true;
        if (/%7B\s*query\s*%7D/i.test(candidate)) return true;

        try {
            // Handle single and double encoded URLs.
            for (let i = 0; i < 2; i += 1) {
                const decoded = decodeURIComponent(candidate);
                if (decoded === candidate) break;
                candidate = decoded;
                if (/\{\{\s*query\s*}}/i.test(candidate)) return true;
                if (/\{\s*query\s*}/i.test(candidate)) return true;
                if (/%7B%7B\s*query\s*%7D%7D/i.test(candidate)) return true;
                if (/%7B\s*query\s*%7D/i.test(candidate)) return true;
            }
        } catch {
            // Ignore decode errors; final fallback below.
        }
        return false;
    };

    const applyNewsSourceTemplate = (templateKey) => {
        const nameInput = document.getElementById("news-source-name");
        const urlInput = document.getElementById("news-source-url");
        const topicsInput = document.getElementById("news-source-topics");
        if (!nameInput || !urlInput || !topicsInput) return;

        const template = NEWS_SOURCE_TEMPLATES[templateKey];
        if (!template) return;

        nameInput.value = template.name || "";
        urlInput.value = template.url || "";
        topicsInput.value = (template.topics || []).join(", ");
    };

    const renderNewsSources = (sources, options = {}) => {
        const list = document.getElementById("news-sources-list");
        if (!list) return;
        list.innerHTML = "";

        const selectedId = options.selectedId ?? null;
        const onSelect = typeof options.onSelect === "function" ? options.onSelect : null;
        const onDelete = typeof options.onDelete === "function" ? options.onDelete : null;

        if (!sources || sources.length === 0) {
            list.innerHTML = '<div class="text-sm text-slate-400">No sources configured.</div>';
            return;
        }

        const escapeHtml = (value) => String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");

        sources.forEach((source) => {
            const row = document.createElement("div");
            const isSelected = String(selectedId) === String(source.id);
            row.className = `relative rounded-lg p-4 flex flex-wrap items-start gap-4 transition-colors cursor-pointer ${
                isSelected
                    ? "bg-zinc-800 border border-emerald-500"
                    : "bg-zinc-900/70 border border-zinc-700 hover:border-zinc-500"
            }`;
            row.innerHTML = `
                <div class="grow min-w-[220px] pr-20">
                    <div class="text-sm text-slate-100 mb-1" data-view-name>${escapeHtml(source.name)}</div>
                    <a href="${escapeHtml(source.url)}" target="_blank" rel="noopener"
                       class="text-xs text-blue-300 hover:text-cyan-300 break-all underline" data-view-url>${escapeHtml(source.url)}</a>
                    <div class="text-xs text-cyan-300 mt-1" data-view-topics>${(source.topics || []).length ? escapeHtml((source.topics || []).join(", ")) : "general"}</div>
                    <div class="text-xs text-slate-400 mt-1" data-view-query>${isDynamicSourceUrl(source.url) ? "query: dynamic" : "query: static feed"}</div>
                </div>
                <div class="absolute top-3 right-3 flex items-center gap-2">
                    <button type="button" class="secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0" data-action="delete" title="Delete source">
                        <span class="material-icons">delete</span>
                    </button>
                </div>
            `;

            const remove = row.querySelector('[data-action="delete"]');
            const selectSource = () => {
                if (onSelect) onSelect(source);
            };

            row.addEventListener("click", (event) => {
                if (event.target.closest('[data-action="delete"], a[data-view-url]')) {
                    return;
                }
                selectSource();
            });
            if (remove) {
                remove.addEventListener("click", async (event) => {
                    event.stopPropagation();
                    if (onDelete) {
                        await onDelete(source);
                    }
                });
            }

            list.appendChild(row);
        });
    };

    const loadNewsSources = async () => {
        const res = await fetch("/news/sources");
        const data = await res.json();
        return data.sources || [];
    };

    const bindNewsSources = () => {
        const showCreateBtn = document.getElementById("news-show-create-source-btn");
        const formTitle = document.getElementById("news-source-form-title");
        const nameInput = document.getElementById("news-source-name");
        const urlInput = document.getElementById("news-source-url");
        const topicsInput = document.getElementById("news-source-topics");
        const addActions = document.getElementById("add-news-source-actions");
        const cancelAddBtn = document.getElementById("cancel-add-news-source-btn");
        const addBtn = document.getElementById("add-news-source-btn");
        const addBtnIcon = document.getElementById("add-news-source-btn-icon");
        const addBtnLabel = document.getElementById("add-news-source-btn-label");
        if (!addBtn) return;
        let editingSourceId = null;
        let newsSources = [];

        const setEditorState = (source = null) => {
            editingSourceId = source?.id ?? null;

            if (formTitle) {
                formTitle.textContent = source ? "Edit News Source" : "Add News Source";
            }
            if (nameInput) nameInput.value = source?.name || "";
            if (urlInput) urlInput.value = source?.url || "";
            if (topicsInput) {
                topicsInput.value = Array.isArray(source?.topics)
                    ? source.topics.join(", ")
                    : "";
            }
            if (addBtnIcon) {
                addBtnIcon.textContent = source ? "save" : "add";
            }
            if (addBtnLabel) {
                addBtnLabel.textContent = source ? "Save source" : "Add source";
            }
        };

        const updateAddActionsVisibility = () => {
            if (!addActions || !nameInput || !urlInput) return;
            const hasRequiredValues = Boolean(nameInput.value.trim() && urlInput.value.trim());
            if (editingSourceId) {
                addActions.classList.remove("hidden");
                return;
            }
            addActions.classList.toggle("hidden", !hasRequiredValues);
        };

        const openEditor = (source = null) => {
            setEditorState(source);
            updateAddActionsVisibility();
            nameInput?.focus();
        };

        const refreshNewsSources = async () => {
            try {
                newsSources = await loadNewsSources();
                const selectedSource = editingSourceId
                    ? newsSources.find((source) => String(source.id) === String(editingSourceId))
                    : null;
                if (editingSourceId && !selectedSource) {
                    setEditorState(null);
                } else if (selectedSource) {
                    setEditorState(selectedSource);
                }
                renderNewsSources(newsSources, {
                    selectedId: editingSourceId,
                    onSelect: openEditor,
                    onDelete: async (source) => {
                        const response = await fetch(`/news/sources/${source.id}`, {method: "DELETE"});
                        if (!response.ok) {
                            showNotification("Failed to delete source", "error", 3000);
                            return;
                        }
                        if (String(editingSourceId) === String(source.id)) {
                            setEditorState(null);
                        }
                        await refreshNewsSources();
                    },
                });
                updateAddActionsVisibility();
            } catch (error) {
                console.error("Failed to load news sources:", error);
                showNotification("Failed to load news sources", "error", 3000);
            }
        };

        if (cancelAddBtn) {
            cancelAddBtn.addEventListener("click", () => {
                setEditorState(null);
                updateAddActionsVisibility();
            });
        }

        if (showCreateBtn) {
            showCreateBtn.addEventListener("click", () => {
                openEditor(null);
            });
        }

        if (nameInput) {
            nameInput.addEventListener("input", updateAddActionsVisibility);
        }
        if (urlInput) {
            urlInput.addEventListener("input", updateAddActionsVisibility);
        }

        const toggleExamplesBtn = document.getElementById("toggle-news-examples-btn");
        const examplesList = document.getElementById("news-examples-list");
        const examplesToggleIcon = document.getElementById("news-examples-toggle-icon");
        const examplesToggleLabel = document.getElementById("news-examples-toggle-label");
        if (toggleExamplesBtn && examplesList && examplesToggleIcon) {
            const updateExamplesToggle = () => {
                const collapsed = examplesList.classList.contains("hidden");
                examplesToggleIcon.textContent = collapsed ? "expand_more" : "expand_less";
                if (examplesToggleLabel) {
                    examplesToggleLabel.textContent = collapsed ? "Show examples" : "Hide examples";
                }
            };
            updateExamplesToggle();
            toggleExamplesBtn.addEventListener("click", () => {
                examplesList.classList.toggle("hidden");
                updateExamplesToggle();
            });
        }
        document.querySelectorAll(".news-template-use-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                const templateKey = btn.getAttribute("data-template-key");
                if (!editingSourceId) {
                    setEditorState(null);
                }
                applyNewsSourceTemplate(templateKey || "");
                updateAddActionsVisibility();
            });
        });

        addBtn.addEventListener("click", async () => {
            const name = (nameInput?.value || "").trim();
            const url = (urlInput?.value || "").trim();
            const topics = (topicsInput?.value || "").trim();
            const isEditing = Boolean(editingSourceId);

            if (!name || !url) {
                showNotification("Source and URL are required", "warning", 2500);
                return;
            }

            const payload = {name, url, topics};
            const response = await fetch(
                isEditing ? `/news/sources/${editingSourceId}` : "/news/sources",
                {
                    method: isEditing ? "PATCH" : "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify(payload),
                }
            );
            const data = await response.json();
            if (!response.ok) {
                showNotification(
                    data.error || (isEditing ? "Failed to save source" : "Failed to add source"),
                    "error",
                    3000
                );
                return;
            }

            setEditorState(null);
            updateAddActionsVisibility();
            showNotification(isEditing ? "Source updated" : "News source added", "success", 2500);
            await refreshNewsSources();
        });

        setEditorState(null);
        updateAddActionsVisibility();
        refreshNewsSources();
    };

    const bindWiFiSection = () => {
        const form = document.getElementById("config-form");
        const wifiModal = document.getElementById("wifi-setup-modal");
        const wifiModalCloseBtn = document.getElementById("close-wifi-setup-modal");
        const wifiModalDescription = document.getElementById("wifi-setup-modal-description");
        const scanBtn = document.getElementById("wifi-scan-btn");
        const saveBtn = document.getElementById("wifi-save-btn");
        const saveLabel = document.getElementById("wifi-save-label");
        const saveIcon = document.getElementById("wifi-save-icon");
        const editToggleBtn = document.getElementById("wifi-edit-toggle-btn");
        const editToggleLabel = document.getElementById("wifi-edit-toggle-label");
        const networkList = document.getElementById("wifi-network-list");
        const ssidInput = document.getElementById("wifi-ssid");
        const ssidField = document.getElementById("wifi-ssid-field");
        const passwordInput = document.getElementById("wifi-password");
        const countrySelect = document.getElementById("wifi-country-modal") || document.getElementById("WIFI_COUNTRY");
        const countrySettingsSelect = document.getElementById("WIFI_COUNTRY");
        const countryModalSelect = document.getElementById("wifi-country-modal");
        const unifiedFields = document.getElementById("wifi-unified-fields");
        const internetSection = document.getElementById("section-internet");
        const statusEl = document.getElementById("wifi-connection-status");
        const testResultEl = document.getElementById("wifi-test-result");
        const errorResultEl = document.getElementById("wifi-error-result");
        const banner = document.getElementById("wifi-onboarding-banner");
        const setSettingsConnectionStatus = (message, connected = null) => {
            if (!statusEl) return;
            statusEl.textContent = message;
            statusEl.classList.remove(
                "text-zinc-300",
                "text-zinc-400",
                "text-amber-400",
                "text-emerald-400",
                "text-rose-400"
            );
            if (connected === true) {
                statusEl.classList.add("text-emerald-400");
            } else if (connected === "setup") {
                statusEl.classList.add("text-amber-400");
            } else if (connected === false) {
                statusEl.classList.add("text-rose-400");
            } else {
                statusEl.classList.add("text-zinc-300");
            }
        };
        const refreshSettingsWifiStatus = async () => {
            if (!statusEl) return;
            try {
                const response = await fetch("/wifi/status");
                const data = await response.json();
                if (!response.ok) {
                    setSettingsConnectionStatus(data.error || "Failed to load Wi-Fi status", false);
                    return;
                }
                const hotspotActiveNow = Boolean(data.hotspot_active);
                if (hotspotActiveNow) {
                    setSettingsConnectionStatus(
                        "Billy setup hotspot is active. Connect Billy to your home Wi-Fi to finish setup.",
                        "setup"
                    );
                } else {
                    setSettingsConnectionStatus(
                        data.connected
                            ? `Connected to ${data.ssid || "Wi-Fi"}`
                            : "Not connected",
                        Boolean(data.connected)
                    );
                }
                if (countrySettingsSelect) {
                    const countryValue = String(data.country || countrySettingsSelect.value || "NL").trim().toUpperCase();
                    ensureSelectHasValue(countrySettingsSelect, countryValue, "US");
                }
            } catch (error) {
                setSettingsConnectionStatus(String(error), false);
            }
        };
        const bindSettingsWifiEditButton = () => {
            if (!editToggleBtn || editToggleBtn.dataset.wifiEditBound === "true") return;
            editToggleBtn.dataset.wifiEditBound = "true";
            editToggleBtn.addEventListener("click", () => {
                wifiModal?.classList.remove("hidden");
                editToggleBtn.classList.add("hidden");
                document.documentElement.classList.add("overflow-hidden");
                document.body.classList.add("overflow-hidden");
            });
        };
        if (!wifiModal) {
            return;
        }
        if (wifiModal.dataset.bound === "true") {
            bindSettingsWifiEditButton();
            internetSection?.classList.remove("hidden");
            unifiedFields?.classList.remove("hidden");
            refreshSettingsWifiStatus();
            return;
        }
        if (!wifiModalCloseBtn || !wifiModalDescription || !scanBtn || !saveBtn || !saveLabel || !saveIcon || !networkList || !ssidInput || !ssidField || !passwordInput || !countrySelect || !testResultEl || !errorResultEl) {
            refreshSettingsWifiStatus();
            return;
        }
        wifiModal.dataset.bound = "true";

        const configSource = form || wifiModal;
        const onboardingActive = configSource.dataset.wifiOnboardingActive === "true";
        const configuredMode = String(configSource.dataset.wifiOnboardingMode || "legacy").trim().toLowerCase();
        const unifiedConfigured = configuredMode === "unified";
        const legacyHotspotOnboarding = onboardingActive && !unifiedConfigured;
        let hotspotActive = false;
        let savedFingerprint = "";
        let selectedNetworkValue = "";
        let hasLoadedNetworksOnce = false;
        const MANUAL_SSID_VALUE = "__manual__";
        const legacyOnboardingMessage = "Legacy Wi-Fi setup is active. Open billy.local:8080 to scan, test, and save Wi-Fi without disconnecting from Billy_Bassistant.";

        const currentFingerprint = () => JSON.stringify({
            ssid: ssidInput.value.trim(),
            password: passwordInput.value,
            country: countrySelect.value || "NL",
        });

        const setBusy = (button, busy) => {
            button.disabled = busy;
            button.classList.toggle("opacity-50", busy);
            button.classList.toggle("cursor-not-allowed", busy);
        };

        const syncSaveButton = () => {
            const enabled = Boolean(ssidInput.value.trim());
            saveBtn.disabled = !enabled;
            saveBtn.classList.toggle("opacity-50", !enabled);
            saveBtn.classList.toggle("cursor-not-allowed", !enabled);
            if (!enabled && savedFingerprint === currentFingerprint()) {
                saveLabel.textContent = "Saved";
                saveIcon.textContent = "check_circle";
            } else {
                saveLabel.textContent = "Save Wi-Fi";
                saveIcon.textContent = "save";
            }
        };

        const openWifiModal = () => {
            wifiModal.classList.remove("hidden");
            editToggleBtn?.classList.add("hidden");
            document.documentElement.classList.add("overflow-hidden");
            document.body.classList.add("overflow-hidden");
            if (!hasLoadedNetworksOnce) {
                loadNetworks();
            }
        };

        const closeWifiModal = () => {
            wifiModal.classList.add("hidden");
            document.documentElement.classList.remove("overflow-hidden");
            document.body.classList.remove("overflow-hidden");
            if (editToggleBtn && !shouldAutoOpenEditor()) {
                editToggleBtn.classList.remove("hidden");
            }
        };

        const syncOnboardingUiState = () => {
            const onboardingUi = shouldAutoOpenEditor();
            if (editToggleLabel) {
                editToggleLabel.textContent = onboardingUi ? "Wi-Fi setup" : "Edit connection";
            }
            wifiModalDescription.textContent = onboardingUi
                ? "Connect Billy to your home Wi-Fi to finish setup. Saving a new network will reboot Billy to switch over."
                : "Choose your Wi-Fi network and save the connection for Billy. Saving a new network will reboot Billy.";
        };

        const invalidateSuccessfulTest = () => {
            if (savedFingerprint !== currentFingerprint()) {
                savedFingerprint = "";
            }
            syncSaveButton();
        };

        const updateSelectedNetworkCard = () => {
            networkList.querySelectorAll(".wifi-network-item").forEach((item) => {
                const isSelected = item.dataset.ssid === selectedNetworkValue;
                item.classList.toggle("border-emerald-500", isSelected);
                item.classList.toggle("bg-emerald-900/20", isSelected);
                item.classList.toggle("border-zinc-700", !isSelected);
                item.classList.toggle("bg-zinc-800", !isSelected);
            });
        };

        const syncManualSsidVisibility = () => {
            const showManual = selectedNetworkValue === MANUAL_SSID_VALUE;
            ssidInput.disabled = !showManual;
            ssidInput.classList.toggle("bg-zinc-700", showManual);
            ssidInput.classList.toggle("bg-transparent", !showManual);
            ssidInput.classList.toggle("opacity-60", !showManual);
            ssidInput.classList.toggle("cursor-not-allowed", !showManual);
            if (!showManual) {
                ssidInput.value = selectedNetworkValue || "";
            } else {
                ssidInput.focus();
            }
            updateSelectedNetworkCard();
        };

        const signalIconFor = (signal) => {
            const strength = Number(signal) || 0;
            if (strength >= 75) return "network_wifi_3_bar";
            if (strength >= 50) return "network_wifi_2_bar";
            if (strength >= 25) return "network_wifi_1_bar";
            return "network_wifi";
        };

        const createNetworkListItem = (network) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "wifi-network-item w-full rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-3 text-left text-white transition-colors hover:border-zinc-500";
            button.dataset.ssid = network.ssid || "";
            button.dataset.manual = "false";

            const signalIcon = signalIconFor(network.signal);
            const isSecure = network.security && network.security !== "open";
            const securityIcon = isSecure ? "lock" : "lock_open";
            const securityColor = isSecure ? "text-zinc-300" : "text-zinc-500";
            const activeBadge = network.active
                ? '<span class="text-xs text-emerald-400">Connected</span>'
                : "";

            button.innerHTML = `
                <span class="flex items-center justify-between gap-3">
                    <span class="min-w-0">
                        <span class="block truncate">${network.ssid || "Hidden network"}</span>
                        ${activeBadge}
                    </span>
                    <span class="flex shrink-0 items-center gap-2">
                        <span class="material-icons text-zinc-300">${signalIcon}</span>
                        <span class="material-icons ${securityColor}">${securityIcon}</span>
                    </span>
                </span>
            `;
            return button;
        };

        const createManualNetworkListItem = () => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "wifi-network-item w-full rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-3 text-left text-white transition-colors hover:border-zinc-500";
            button.dataset.ssid = MANUAL_SSID_VALUE;
            button.dataset.manual = "true";
            button.innerHTML = `
                <span class="flex items-center justify-between gap-3">
                    <span>Enter SSID manually</span>
                    <span class="material-icons text-zinc-400">edit</span>
                </span>
            `;
            return button;
        };

        const renderNetworkList = (networks = []) => {
            networkList.innerHTML = "";
            if (!networks.length) {
                const emptyState = document.createElement("div");
                emptyState.className = "rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-3 text-sm text-zinc-400";
                emptyState.textContent = "No networks found yet. Scan again or enter the SSID manually.";
                networkList.appendChild(emptyState);
            } else {
                networks.forEach((network) => {
                    networkList.appendChild(createNetworkListItem(network));
                });
            }
            networkList.appendChild(createManualNetworkListItem());
            updateSelectedNetworkCard();
        };

        const syncSectionVisibility = () => {
            internetSection?.classList.remove("hidden");
            unifiedFields?.classList.remove("hidden");
        };

        const showLegacyHotspotMessage = () => {
            showError(legacyOnboardingMessage);
            if (banner) {
                banner.textContent = legacyOnboardingMessage;
                banner.classList.remove("hidden");
            }
        };

        const showError = (message) => {
            if (!message) {
                errorResultEl.classList.add("hidden");
                errorResultEl.textContent = "";
                return;
            }
            errorResultEl.textContent = message;
            errorResultEl.classList.remove("hidden");
        };

        const showTestResult = (message) => {
            if (!message) {
                testResultEl.classList.add("hidden");
                testResultEl.textContent = "";
                return;
            }
            testResultEl.textContent = message;
            testResultEl.classList.remove("hidden");
        };

        const setConnectionStatus = (message, connected = null) => {
            if (!statusEl) return;
            statusEl.textContent = message;
            statusEl.classList.remove(
                "text-zinc-300",
                "text-zinc-400",
                "text-amber-400",
                "text-emerald-400",
                "text-rose-400"
            );
            if (connected === true) {
                statusEl.classList.add("text-emerald-400");
            } else if (connected === "setup") {
                statusEl.classList.add("text-amber-400");
            } else if (connected === false) {
                statusEl.classList.add("text-rose-400");
            } else {
                statusEl.classList.add("text-zinc-300");
            }
        };

        const applyOnboardingLock = () => {
            if (!onboardingActive) return;
            if (banner) {
                banner.classList.remove("hidden");
            }
            [
                "section-software",
                "section-wakeword",
                "section-hardware",
                "section-audio",
                "section-mqtt",
                "section-ha",
                "section-advanced-settings",
            ].forEach((id) => {
                const section = document.getElementById(id);
                if (section) {
                    section.classList.add("hidden");
                }
            });
            if (legacyHotspotOnboarding) {
                showLegacyHotspotMessage();
            }
            syncOnboardingUiState();
        };

        const shouldAutoOpenEditor = () => onboardingActive || hotspotActive;

        const forceOnboardingModalOpen = () => {
            if (!shouldAutoOpenEditor()) return;
            openWifiModal();
        };

        const loadStatus = async () => {
            try {
                const response = await fetch("/wifi/status");
                const data = await response.json();
                if (!response.ok) {
                    setConnectionStatus(data.error || "Failed to load Wi-Fi status", false);
                    syncOnboardingUiState();
                    if (shouldAutoOpenEditor()) {
                        forceOnboardingModalOpen();
                    }
                    return;
                }
                hotspotActive = Boolean(data.hotspot_active);
                syncOnboardingUiState();
                if (hotspotActive) {
                    setConnectionStatus(
                        "Billy setup hotspot is active. Connect Billy to your home Wi-Fi to finish setup.",
                        "setup"
                    );
                } else {
                    setConnectionStatus(
                        data.connected
                            ? `Connected to ${data.ssid || "Wi-Fi"}`
                            : "Not connected",
                        Boolean(data.connected)
                    );
                }
                if (shouldAutoOpenEditor()) {
                    forceOnboardingModalOpen();
                } else if (editToggleBtn) {
                    if (wifiModal.classList.contains("hidden")) {
                        editToggleBtn.classList.remove("hidden");
                    } else {
                        editToggleBtn.classList.add("hidden");
                    }
                }
                const countryValue = String(data.country || countrySelect.value || "NL").trim().toUpperCase();
                ensureSelectHasValue(countrySelect, countryValue, "US");
            } catch (error) {
                setConnectionStatus(String(error), false);
                syncOnboardingUiState();
                if (shouldAutoOpenEditor()) {
                    forceOnboardingModalOpen();
                }
            }
        };

        const loadNetworks = async () => {
            if (legacyHotspotOnboarding) {
                showLegacyHotspotMessage();
                showNotification("Use billy.local:8080 for legacy Wi-Fi setup", "warning", 4000);
                return;
            }
            setBusy(scanBtn, true);
            showError("");
            try {
                const response = await fetch("/wifi/networks");
                const data = await response.json();
                if (!response.ok) {
                    showError(data.error || "Wi-Fi scan failed");
                    return;
                }
                const networks = Array.isArray(data.networks) ? data.networks : [];
                renderNetworkList(networks);
                syncManualSsidVisibility();
                hasLoadedNetworksOnce = true;
            } catch (error) {
                showError(error.message || String(error));
            } finally {
                setBusy(scanBtn, false);
            }
        };

        networkList.addEventListener("click", (event) => {
            const item = event.target.closest(".wifi-network-item");
            if (!item) return;
            selectedNetworkValue = item.dataset.ssid || "";
            syncManualSsidVisibility();
            invalidateSuccessfulTest();
        });

        if (countrySettingsSelect && countryModalSelect) {
            const syncCountryValue = (source, target) => {
                if (!source || !target) return;
                ensureSelectHasValue(target, source.value || "US", "US");
            };

            syncCountryValue(countrySettingsSelect, countryModalSelect);

            countrySettingsSelect.addEventListener("change", () => {
                syncCountryValue(countrySettingsSelect, countryModalSelect);
            });

            countryModalSelect.addEventListener("change", () => {
                syncCountryValue(countryModalSelect, countrySettingsSelect);
            });
        }

        [ssidInput, passwordInput, countrySelect].forEach((element) => {
            element.addEventListener("input", invalidateSuccessfulTest);
            element.addEventListener("change", invalidateSuccessfulTest);
        });

        if (editToggleBtn) {
            editToggleBtn.addEventListener("click", () => {
                showError("");
                showTestResult("");
                openWifiModal();
            });
        }

        wifiModalCloseBtn.addEventListener("click", closeWifiModal);
        wifiModal.addEventListener("click", (event) => {
            if (event.target === wifiModal) {
                closeWifiModal();
            }
        });

        scanBtn.addEventListener("click", loadNetworks);

        saveBtn.addEventListener("click", async () => {
            if (legacyHotspotOnboarding) {
                showLegacyHotspotMessage();
                showNotification("Use billy.local:8080 for legacy Wi-Fi setup", "warning", 4000);
                return;
            }
            const ssid = ssidInput.value.trim();
            const password = passwordInput.value;
            const country = countrySelect.value;
            if (!ssid) {
                showNotification("Enter a network name first", "warning", 2500);
                return;
            }

            setBusy(saveBtn, true);
            showError("");
            if (onboardingActive && unifiedConfigured) {
                showTestResult(`Trying to connect Billy to ${ssid}. If it fails, setup mode will come back automatically.`);
            }

            try {
                const response = await fetch("/wifi/save", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({ssid, password, country}),
                });
                const data = await response.json();
                if (!response.ok || !data.ok) {
                    const errorMessage = data.error || "Failed to save Wi-Fi";
                    showError(errorMessage);
                    showNotification(errorMessage, "error", 4000);
                    return;
                }
                showTestResult(
                    data.rebooting
                        ? `Saved Wi-Fi for ${ssid}. Billy is rebooting now to switch over to the new network.`
                        : `Saved Wi-Fi for ${ssid}.`
                );
                showNotification(
                    data.rebooting
                        ? "Wi-Fi saved. Billy is rebooting now."
                        : "Wi-Fi saved",
                    "success",
                    4500
                );
                savedFingerprint = currentFingerprint();
                syncSaveButton();
                if (!data.rebooting) {
                    await loadStatus();
                    if (!shouldAutoOpenEditor()) {
                        closeWifiModal();
                    }
                }
                if (onboardingActive) {
                    setTimeout(() => {
                        window.location.href = `http://${window.location.hostname}:${window.location.port || 80}/`;
                    }, 2500);
                }
            } catch (error) {
                showError(error.message || String(error));
                showNotification(error.message || String(error), "error", 4000);
            } finally {
                setBusy(saveBtn, false);
            }
        });

        applyOnboardingLock();
        forceOnboardingModalOpen();
        syncSectionVisibility();
        renderNetworkList();
        syncManualSsidVisibility();
        syncSaveButton();
        syncOnboardingUiState();
        if (editToggleBtn) {
            editToggleBtn.classList.toggle("hidden", shouldAutoOpenEditor());
        }
        loadStatus();
    };

    return {
        handleSettingsSave,
        populateDropdowns,
        saveDropdownSelections,
        initMouthArticulationSlider,
        initStatusLedBrightnessSlider,
        refreshFromConfig,
        populateCameraHardwareDropdown,
        bindCameraPreview,
        bindFactoryReset,
        bindEnvEditorCard,
        bindNewsSources,
        bindWakeWordKeywordUpload,
        bindWiFiSection,
        bindApiProviderFields,
        initHostFields,
    };
})();

// Make SettingsForm globally available
window.SettingsForm = SettingsForm;
