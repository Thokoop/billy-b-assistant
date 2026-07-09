// ===================== AUDIO =====================
const AudioPanel = (() => {
    const MIC_FULL_SCALE = 32768;
    let micCheckSource = null;
    let serviceWasRunning = false; // Track if service was running before mic test
    let deviceInfoRetryTimer = null;
    let deviceInfoRequestInFlight = false;
    let micRecordingPollTimer = null;
    let micRecordingServiceWasRunning = false;
    let currentMicMeterScale = MIC_FULL_SCALE;
    let currentMicMaxRms = 0;
    let currentMicRmsWindow = [];
    let serviceStoppedForMicTest = false;

    function bindUI() {
        const bindButton = (id, handler) => {
            const button = document.getElementById(id);
            if (!button || button.dataset.audioBound === "true") return;
            button.dataset.audioBound = "true";
            button.addEventListener("click", handler);
        };
        bindButton("mic-check-btn", toggleMicCheck);
        bindButton("mic-record-btn", toggleMicRecording);
        bindButton("mic-record-play-btn", playMicRecording);
        bindButton("speaker-check-btn", handleSpeakerCheck);
        bindMicGainSlider();
        bindMicThresholdControls();
    }

    async function handleSpeakerCheck() {
        try {
            const speakerSelect = document.getElementById("SPEAKER_PREFERENCE");
            const selectedSpeakerPreference = speakerSelect ? String(speakerSelect.value || "") : "";
            const data = await ServiceStatus.fetchStatus();
            if (data.status === "active") {
                showNotification("Stopping Billy service for speaker test...", "warning");
                
                // Stop the Billy service
                const stopResponse = await fetch("/stop-billy", {method: "POST"});
                if (!stopResponse.ok) {
                    const reason = `HTTP ${stopResponse.status}`;
                    console.error("Failed to stop Billy service:", reason);
                    showNotification(`Failed to stop Billy service: ${reason}`, "error");
                    return;
                }
                
                // Wait a moment for the service to stop
                await new Promise(resolve => setTimeout(resolve, 2000));
                
                showNotification("Billy service stopped. Running speaker test...", "success");
            }
            
            const res = await fetch("/speaker-test", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({speaker_preference: selectedSpeakerPreference}),
            });
            const result = await res.json();
            if (!res.ok) {
                throw new Error(result.error || `HTTP ${res.status}`);
            }
            showNotification("Speaker test triggered");
        } catch (err) {
            console.error("Failed to trigger speaker test:", err);
            showNotification("Failed to trigger speaker test: " + err.message, "error");
        }
    }

    async function saveAudioPreference(key, value) {
        try {
            const saveResponse = await fetch("/save", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({[key]: value}),
            });
            if (!saveResponse.ok) {
                console.error(`Failed saving ${key}: HTTP ${saveResponse.status}`);
                return false;
            }
            const saveData = await saveResponse.json();
            await fetch("/config/auto-refresh", {method: "POST"});
            if (saveData && saveData.audio_restart_required) {
                await fetch("/restart-billy", {method: "POST"});
                showNotification(`${key === "MIC_PREFERENCE" ? "Microphone" : "Speaker"} updated – Billy restarted`, "success");
            }
            return true;
        } catch (error) {
            console.error(`Failed to save ${key}:`, error);
            return false;
        }
    }

    async function loadAudioDeviceSelectors() {
        const micSelect = document.getElementById("MIC_PREFERENCE");
        const speakerSelect = document.getElementById("SPEAKER_PREFERENCE");
        if (!micSelect || !speakerSelect) return;

        const populateSelect = (selectEl, devices, selectedValue, storageKey) => {
            const previous = localStorage.getItem(storageKey) || "";
            const target = selectedValue || previous || "";

            selectEl.innerHTML = "";
            const autoOption = document.createElement("option");
            autoOption.value = "";
            autoOption.textContent = "Auto detect";
            selectEl.appendChild(autoOption);

            const values = new Set([""]);
            (devices || []).forEach((entry) => {
                const opt = document.createElement("option");
                opt.value = String(entry.value || "");
                opt.textContent = String(entry.label || entry.value || "");
                selectEl.appendChild(opt);
                values.add(opt.value);
            });

            if (target && !values.has(target)) {
                const stale = document.createElement("option");
                stale.value = target;
                if (String(target).startsWith("usbpath:")) {
                    const path = String(target).split(":", 2)[1] || "?";
                    stale.textContent = `USB device on bus path ${path} (saved; currently unavailable)`;
                } else {
                    stale.textContent = `${target} (saved; currently unavailable)`;
                }
                selectEl.appendChild(stale);
            }

            selectEl.value = values.has(target) || target ? target : "";
            localStorage.setItem(storageKey, selectEl.value);
        };

        try {
            const res = await fetch("/audio/devices");
            const data = await res.json();
            if (!res.ok) {
                console.error("Failed to load audio devices:", data.error || `HTTP ${res.status}`);
                setTimeout(() => updateDeviceLabels(), 2000);
                return;
            }

            populateSelect(
                micSelect,
                data.input_devices,
                data.selected_mic,
                "dropdown_MIC_PREFERENCE",
            );
            populateSelect(
                speakerSelect,
                data.output_devices,
                data.selected_speaker,
                "dropdown_SPEAKER_PREFERENCE",
            );

            if (micSelect.dataset.audioPreferenceBound !== "true") {
                micSelect.dataset.audioPreferenceBound = "true";
                micSelect.addEventListener("change", () => {
                    localStorage.setItem("dropdown_MIC_PREFERENCE", micSelect.value);
                    saveAudioPreference("MIC_PREFERENCE", micSelect.value);
                });
            }
            if (speakerSelect.dataset.audioPreferenceBound !== "true") {
                speakerSelect.dataset.audioPreferenceBound = "true";
                speakerSelect.addEventListener("change", () => {
                    localStorage.setItem(
                        "dropdown_SPEAKER_PREFERENCE",
                        speakerSelect.value,
                    );
                    saveAudioPreference("SPEAKER_PREFERENCE", speakerSelect.value);
                });
            }

            // Re-resolve labels after device selectors are populated.
            updateDeviceLabels();
        } catch (error) {
            console.error("Failed to load audio device selectors:", error);
            // Retry label refresh even if selectors fail once during boot races.
            setTimeout(() => updateDeviceLabels(), 2000);
        }
    }

    async function toggleMicCheck() {
        const btn = document.getElementById("mic-check-btn");
        if (!btn) return;
        const isActive = btn.classList.contains("bg-emerald-600");
        if (isActive) {
            await finishMicCheck();
            showNotification("Mic check stopped");
        } else {
            try {
                await stopMicCheck();
                const data = await ServiceStatus.fetchStatus();
                serviceWasRunning = (data.status === "active");
                
                if (serviceWasRunning) {
                    showNotification("Stopping Billy service for mic test...", "warning");
                    await fetch("/stop-billy", {method: "POST"});
                    serviceStoppedForMicTest = true;
                    // Wait a moment for the service to stop
                    await new Promise(resolve => setTimeout(resolve, 2000));
                    showNotification("Billy service stopped. Starting mic test...", "success");
                }
                
                startMicCheck();
                btn.classList.remove("bg-zinc-800");
                btn.classList.add("bg-emerald-600");
                showNotification("Mic check started");
            } catch (err) {
                console.error("Failed to toggle mic check:", err);
                showNotification("Mic check failed: " + err.message, "error");
            }
        }
    }

    async function stopMicCheck() {
        const source = micCheckSource;
        micCheckSource = null;
        if (source) source.close();
        await fetch("/mic-check/stop").catch(err => console.error("Failed to stop mic check:", err));
        updateMicBar(0);
        updateMicMarker("mic-peak-line", 0, false);
        updateMicMarker("mic-average-line", 0, false);
        await refreshMicRecordingStatus();
    }

    async function finishMicCheck({message = null, isError = false} = {}) {
        const btn = document.getElementById("mic-check-btn");
        if (btn) {
            btn.classList.remove("bg-emerald-600");
            btn.classList.add("bg-zinc-800");
        }
        await stopMicCheck();
        if (message) {
            setMicRecordStatus(message, false);
        }
        if (isError) {
            updateMicBar(0);
        }
    }

    function startMicCheck() {
        const currentThreshold = getThresholdInputValue();
        currentMicMaxRms = 0;
        currentMicRmsWindow = [];
        currentMicMeterScale = getMicMeterScale();
        updateThresholdLine(currentThreshold);
        updateMicMarker("mic-peak-line", 0, false);
        updateMicMarker("mic-average-line", 0, false);
        setMicRmsStatus(0, 0, 0);
        const params = new URLSearchParams({
            threshold: String(currentThreshold),
            gate: "1",
        });
        micCheckSource = new EventSource(`/mic-check?${params.toString()}`);
        micCheckSource.onmessage = (e) => {
            let data;
            try { data = JSON.parse(e.data); }
            catch (err) { console.error("Invalid JSON from /mic-check:", e.data); return; }
            if (data.error) {
                console.error("Mic check error:", data.error);
                finishMicCheck({
                    message: `Recording error: ${data.error}`,
                    isError: true,
                }).catch(err => console.error("Failed to finish mic check after error:", err));
                return;
            }
            const rms = Number(data.rms || 0);
            const threshold = Number(data.threshold || 0);
            currentMicMaxRms = Math.max(currentMicMaxRms, rms);
            const averageRms = updateRollingRmsAverage(rms);
            currentMicMeterScale = getMicMeterScale();
            const percent = Math.min((rms / currentMicMeterScale) * 100, 100);
            const thresholdPercent = Math.min((threshold / currentMicMeterScale) * 100, 100);
            updateMicBar(percent, thresholdPercent);
            updateThresholdLine(threshold);
            updateMicMarker("mic-peak-line", currentMicMaxRms, currentMicMaxRms > 0);
            updateMicMarker("mic-average-line", averageRms, currentMicRmsWindow.length > 0);
            if (data.recording) {
                setMicRmsStatus(rms, averageRms, currentMicMaxRms);
            }
        };
        micCheckSource.onerror = async () => {
            if (!micCheckSource) return;
            console.error("Mic check connection closed.");
            await finishMicCheck();
            showNotification("Mic check finished");
        };
    }

    function setMicRecordStatus(text, hasRecording = false) {
        const status = document.getElementById("mic-record-status");
        const playBtn = document.getElementById("mic-record-play-btn");
        if (status) status.textContent = text;
        if (playBtn) playBtn.disabled = !hasRecording;
    }

    function setMicRmsStatus(currentRms, averageRms, peakRms) {
        const status = document.getElementById("mic-record-status");
        const playBtn = document.getElementById("mic-record-play-btn");
        if (!status) return;
        status.innerHTML = [
            `<span style="color: #34d399;">Current: ${formatRms(currentRms)}</span>`,
            `<span style="color: #67e8f9;">Avg: ${formatRms(averageRms)}</span>`,
            `<span style="color: #ffffff;">Peak: ${formatRms(peakRms)}</span>`,
        ].join(" <span class=\"text-slate-500\">|</span> ");
        if (playBtn) playBtn.disabled = true;
    }

    function setMicRecordButton(recording) {
        const btn = document.getElementById("mic-record-btn");
        if (!btn) return;
        const icon = btn.querySelector(".material-icons");
        btn.classList.toggle("bg-red-700", recording);
        btn.classList.toggle("hover:bg-red-600", recording);
        btn.classList.toggle("bg-zinc-800", !recording);
        btn.classList.toggle("hover:bg-zinc-700", !recording);
        if (icon) icon.textContent = recording ? "stop" : "fiber_manual_record";
        const label = btn.querySelector("[data-mic-record-label]");
        if (label) label.textContent = recording ? "Stop recording" : "Record mic";
    }

    async function refreshMicRecordingStatus() {
        try {
            const res = await fetch("/mic-record/status");
            const data = await res.json();
            if (data.error) {
                setMicRecordStatus(`Recording error: ${data.error}`, Boolean(data.exists));
                setMicRecordButton(false);
                return data;
            }
            if (data.recording) {
                setMicRecordButton(true);
                setMicRecordStatus(`Recording test sample... ${data.elapsed}s / ${data.max_seconds}s`, false);
            } else {
                setMicRecordButton(false);
                setMicRecordStatus(
                    data.exists ? "Last test recording ready." : "No test recording yet.",
                    Boolean(data.exists),
                );
            }
            return data;
        } catch (error) {
            console.error("Failed to fetch mic recording status:", error);
            setMicRecordStatus("Could not read mic recording status.", false);
            return null;
        }
    }

    function startMicRecordingPoll() {
        clearInterval(micRecordingPollTimer);
        micRecordingPollTimer = setInterval(async () => {
            const data = await refreshMicRecordingStatus();
            if (!data || !data.recording) {
                clearInterval(micRecordingPollTimer);
                micRecordingPollTimer = null;
                if (micRecordingServiceWasRunning) {
                    micRecordingServiceWasRunning = false;
                    try {
                        showNotification("Mic recording finished. Restarting Billy service...", "warning");
                        await fetch("/restart-billy", {method: "POST"});
                        showNotification("Billy service restarted", "success");
                        ServiceStatus.fetchStatus();
                    } catch (error) {
                        console.error("Failed to restart Billy service:", error);
                        showNotification("Mic recording finished, but Billy service restart failed", "error");
                    }
                }
            }
        }, 1000);
    }

    async function toggleMicRecording() {
        const current = await refreshMicRecordingStatus();
        if (current && current.recording) {
            try {
                await fetch("/mic-record/stop", {method: "POST"});
                const stopped = await refreshMicRecordingStatus();
                if (stopped && stopped.exists) {
                    showNotification("Mic recording saved", "success");
                }
            } catch (error) {
                console.error("Failed to stop mic recording:", error);
                showNotification("Failed to stop mic recording", "error");
            }
            return;
        }

        if (micCheckSource) {
            showNotification("Stop the mic level test before recording.", "warning");
            return;
        }

        try {
            const data = await ServiceStatus.fetchStatus();
            micRecordingServiceWasRunning = data.status === "active";
            if (micRecordingServiceWasRunning) {
                showNotification("Stopping Billy service for mic recording...", "warning");
                await fetch("/stop-billy", {method: "POST"});
                await new Promise(resolve => setTimeout(resolve, 2000));
            }

            const res = await fetch("/mic-record/start", {method: "POST"});
            const result = await res.json();
            if (!res.ok) {
                throw new Error(result.error || result.status || `HTTP ${res.status}`);
            }
            setMicRecordButton(true);
            setMicRecordStatus(`Recording... 0s / ${result.max_seconds}s`, false);
            showNotification("Mic recording started. It will stop automatically after 30 seconds.", "success");
            startMicRecordingPoll();
        } catch (error) {
            console.error("Failed to start mic recording:", error);
            showNotification(`Mic recording failed: ${error.message}`, "error");
            setMicRecordButton(false);
            if (micRecordingServiceWasRunning) {
                micRecordingServiceWasRunning = false;
                await fetch("/restart-billy", {method: "POST"}).catch(err => console.error("Failed to restart Billy:", err));
            }
        }
    }

    async function playMicRecording() {
        let shouldRestart = false;
        try {
            const speakerSelect = document.getElementById("SPEAKER_PREFERENCE");
            const selectedSpeakerPreference = speakerSelect ? String(speakerSelect.value || "") : "";
            const serviceStatus = await ServiceStatus.fetchStatus();
            const serviceIsActive = serviceStatus.status === "active";
            shouldRestart = serviceIsActive || serviceStoppedForMicTest || serviceWasRunning;
            if (serviceIsActive) {
                showNotification("Stopping Billy service for mic recording playback...", "warning");
                await fetch("/stop-billy", {method: "POST"});
                await new Promise(resolve => setTimeout(resolve, 2000));
            }
            const res = await fetch("/mic-record/play", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({speaker_preference: selectedSpeakerPreference}),
            });
            const result = await res.json();
            if (!res.ok) {
                throw new Error(result.error || `HTTP ${res.status}`);
            }
            showNotification("Playing mic test recording", "success");
            if (shouldRestart) {
                const durationMs = Math.max(1000, Number(result.duration || 30) * 1000);
                setTimeout(async () => {
                    try {
                        await fetch("/restart-billy", {method: "POST"});
                        ServiceStatus.fetchStatus();
                        serviceStoppedForMicTest = false;
                        serviceWasRunning = false;
                    } catch (error) {
                        console.error("Failed to restart Billy after mic playback:", error);
                    }
                }, durationMs + 1500);
            }
        } catch (error) {
            console.error("Failed to play mic recording:", error);
            showNotification(`Playback failed: ${error.message}`, "error");
            if (shouldRestart) {
                await fetch("/restart-billy", {method: "POST"}).catch(err => console.error("Failed to restart Billy:", err));
                serviceStoppedForMicTest = false;
                serviceWasRunning = false;
            }
        }
    }

    function updateMicBar(percentage, thresholdPercent = 0) {
        const bar = document.getElementById("mic-level-bar");
        bar.style.width = `${percentage}%`;
        bar.classList.toggle("bg-zinc-500", percentage < thresholdPercent);
        bar.classList.toggle("bg-emerald-500", percentage >= thresholdPercent && percentage < 70);
        bar.classList.toggle("bg-amber-500", percentage >= 70 && percentage < 90);
        bar.classList.toggle("bg-red-500", percentage >= 90);
    }

    function updateRollingRmsAverage(rms) {
        const now = Date.now();
        currentMicRmsWindow.push({time: now, rms});
        currentMicRmsWindow = currentMicRmsWindow.filter((entry) => now - entry.time <= 10000);
        const total = currentMicRmsWindow.reduce((sum, entry) => sum + entry.rms, 0);
        return total / Math.max(1, currentMicRmsWindow.length);
    }

    function updateMicMarker(id, rms, visible = true) {
        const marker = document.getElementById(id);
        if (!marker) return;
        marker.classList.toggle("hidden", !visible);
        const percent = Math.min((Number(rms || 0) / currentMicMeterScale) * 100, 100);
        marker.style.left = `${percent}%`;
    }

    function getMicMeterScale() {
        return MIC_FULL_SCALE;
    }

    function formatRms(value) {
        const number = Number(value || 0);
        if (!Number.isFinite(number)) return "0";
        return String(Math.round(number));
    }

    function getThresholdInputValue() {
        const input = getThresholdInput();
        const raw = Number(input?.value || 0);
        if (!Number.isFinite(raw)) return 0;
        return Math.max(0, Math.min(32768, raw));
    }

    function getThresholdInput() {
        return document.getElementById("SILENCE_THRESHOLD");
    }

    function updateMicMeterScale(threshold = getThresholdInputValue()) {
        currentMicMeterScale = getMicMeterScale();
        return currentMicMeterScale;
    }

    function updateThresholdLine(threshold, meterScale = null) {
        const thresholdLine = document.getElementById("threshold-line");
        if (!thresholdLine) return;
        const scale = meterScale || currentMicMeterScale || getMicMeterScale();
        const percent = Math.min((Number(threshold || 0) / scale) * 100, 100);
        thresholdLine.style.left = `${percent}%`;
    }

    function syncThresholdFromInput({sendToActiveTest = true} = {}) {
        const threshold = getThresholdInputValue();
        updateMicMeterScale(threshold);
        updateThresholdLine(threshold);
        if (sendToActiveTest) {
            updateMicCheckConfig();
        }
    }

    async function updateMicCheckConfig() {
        if (!micCheckSource) return;
        try {
            await fetch("/mic-check/config", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    threshold: getThresholdInputValue(),
                    gate_recording: true,
                }),
            });
        } catch (error) {
            console.error("Failed to update mic check config:", error);
        }
    }

    function setThresholdInputValue(value) {
        const input = getThresholdInput();
        if (!input) return;
        const threshold = Math.max(0, Math.min(32768, Math.round(Number(value || 0))));
        input.value = String(threshold);
        syncThresholdFromInput();
    }

    async function loadMicGain() {
        const label = document.getElementById("mic-gain-value");
        const slider = document.getElementById("mic-gain");
        const fill = document.getElementById("mic-gain-fill");
        if (!label || !slider || !fill) return;
        try {
            const res = await fetch("/mic-gain");
            const data = await res.json();
            if (data.gain !== undefined) {
                updateMicGainUi(data.gain, data);
            } else {
                label.textContent = "Unavailable";
            }
        } catch (err) {
            label.textContent = "Error";
        }
    }

    function updateMicGainUi(value, options = {}) {
        const label = document.getElementById("mic-gain-value");
        const slider = document.getElementById("mic-gain");
        const fill = document.getElementById("mic-gain-fill");
        const status = document.getElementById("mic-gain-status");
        if (!label || !slider || !fill) return;

        const min = Number.isFinite(Number(options.min)) ? Number(options.min) : Number(slider.min || 0);
        const max = Number.isFinite(Number(options.max)) ? Number(options.max) : Number(slider.max || 16);
        const gain = Math.max(min, Math.min(max, Number(value || 0)));
        slider.min = String(min);
        slider.max = String(max);
        slider.value = String(gain);
        label.textContent = String(gain);
        const range = Math.max(1, max - min);
        const percent = ((gain - min) / range) * 100;
        fill.style.width = `${percent}%`;
        fill.dataset.value = String(gain);
        if (status) {
            if (gain <= min) {
                status.textContent = `Mic gain is at minimum. Try ${max} if the recording is very quiet.`;
                status.classList.remove("hidden");
            } else {
                status.textContent = "";
                status.classList.add("hidden");
            }
        }
    }

    function bindMicGainSlider() {
        const micGainElement = document.getElementById("mic-gain");
        if (!micGainElement || micGainElement.dataset.audioBound === "true") return;
        micGainElement.dataset.audioBound = "true";
        micGainElement.addEventListener("input", async () => {
            const value = parseInt(micGainElement.value, 10);
            updateMicGainUi(value);
            const response = await fetch("/mic-gain", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({value})
            });
            if (response.ok) {
                const data = await response.json().catch(() => null);
                if (data && data.gain !== undefined) {
                    updateMicGainUi(data.gain, data);
                }
            }
        });
    }

    function bindMicThresholdControls() {
        const micBar = document.getElementById("mic-bar-container");
        const thresholdLine = document.getElementById("threshold-line");
        const silenceThresholdInput = getThresholdInput();
        if (!thresholdLine || !micBar || !silenceThresholdInput || thresholdLine.dataset.audioBound === "true") return;
        thresholdLine.dataset.audioBound = "true";
        let dragging = false;
        thresholdLine.addEventListener("mousedown", (e) => { dragging = true; e.preventDefault(); });
        document.addEventListener("mousemove", (e) => {
            if (!dragging) return;
            const rect = micBar.getBoundingClientRect();
            if (rect.width === 0) return;
            let offsetX = e.clientX - rect.left;
            offsetX = Math.max(0, Math.min(offsetX, rect.width));
            const percent = offsetX / rect.width;
            const scaledThreshold = Math.round(percent * currentMicMeterScale);
            setThresholdInputValue(scaledThreshold);
        });
        document.addEventListener("mouseup", () => { dragging = false; });
        silenceThresholdInput.addEventListener("input", () => {
            syncThresholdFromInput();
        });
        window.addEventListener("load", () => {
            syncThresholdFromInput({sendToActiveTest: false});
        });
        syncThresholdFromInput({sendToActiveTest: false});
    }

    refreshMicRecordingStatus();

    const speakerSlider = document.getElementById("speaker-volume");
    if (speakerSlider) {
        fetch("/volume")
            .then(res => res.json())
            .then(data => {
                if (data.volume !== undefined) {
                    speakerSlider.value = data.volume;
                    const fill = document.getElementById("speaker-volume-fill");
                    if (fill) {
                        const percent = (data.volume / 100) * 100;
                        fill.style.width = `${percent}%`;
                        fill.dataset.value = data.volume;
                    }
                }
            });
        let volumeDebounceTimeout;
        speakerSlider.addEventListener("input", () => {
        clearTimeout(volumeDebounceTimeout);
        volumeDebounceTimeout = setTimeout(() => {
            fetch("/volume", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({volume: parseInt(speakerSlider.value)})
            }).catch(err => console.error("Failed to set speaker volume:", err));
        }, 500);
        });
    }

    async function updateDeviceLabels(retries = 0) {
        const micLabel = document.getElementById("mic-label");
        const speakerLabel = document.getElementById("speaker-label");
        if (!micLabel && !speakerLabel) return;
        if (deviceInfoRequestInFlight) return;

        try {
            deviceInfoRequestInFlight = true;
            const res = await fetch("/device-info");
            const data = await res.json();
            const updateParentClass = (id, value) => {
                const el = document.getElementById(id);
                if (el && el.parentElement) {
                    el.textContent = value;
                    el.parentElement.classList.add("text-emerald-500");
                }
            };
            updateParentClass("mic-label", data.mic);
            updateParentClass("speaker-label", data.speaker);

            const hasUnknown = String(data.mic || "").toLowerCase() === "unknown"
                || String(data.speaker || "").toLowerCase() === "unknown";
            if (hasUnknown && retries < 6) {
                clearTimeout(deviceInfoRetryTimer);
                deviceInfoRetryTimer = setTimeout(() => updateDeviceLabels(retries + 1), 1500);
            }
        } catch (error) {
            console.error("Failed to fetch device info:", error);
            if (retries < 6) {
                clearTimeout(deviceInfoRetryTimer);
                deviceInfoRetryTimer = setTimeout(() => updateDeviceLabels(retries + 1), 1500);
            }
        } finally {
            deviceInfoRequestInFlight = false;
        }
    }

    bindUI();

    return {bindUI, loadMicGain, updateDeviceLabels, loadAudioDeviceSelectors};
})();

// Make AudioPanel globally available
window.AudioPanel = AudioPanel;
