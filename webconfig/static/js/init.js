// ===================== CONSOLIDATED POLLING =====================
let lastKnownPersona = null;
let lastKnownPersonality = null;
let isInitialLoad = true;

// Handle status updates from WebSocket
window.handleStatusUpdate = (status) => {
    // Handle persona changes (skip on initial load to avoid conflicts)
    if (status.current_persona && status.current_persona !== lastKnownPersona) {
        if (!isInitialLoad && window.PersonaForm && window.PersonaForm.handlePersonaChangeNotification) {
            window.PersonaForm.handlePersonaChangeNotification(status.current_persona);
        }
        lastKnownPersona = status.current_persona;
        isInitialLoad = false;
    }
    
    // Handle personality changes
    if (status.current_personality && JSON.stringify(status.current_personality) !== JSON.stringify(lastKnownPersonality)) {
        if (!isInitialLoad && window.PersonaForm && window.PersonaForm.handlePersonalityChange) {
            window.PersonaForm.handlePersonalityChange(status.current_personality);
        }
        lastKnownPersonality = status.current_personality;
        isInitialLoad = false;
    }
    
    // Update service status UI
    if (window.ServiceStatus && window.ServiceStatus.updateServiceStatusUI) {
        window.ServiceStatus.updateServiceStatusUI(status.status);
    }
    
    // Let other components handle their own updates via status
    if (window.UserProfilePanel && window.UserProfilePanel.checkStatus) {
        window.UserProfilePanel.checkStatus(status);
    }
};

// ===================== INITIALIZE =====================
document.addEventListener("DOMContentLoaded", async () => {
    if (window.MainPageRouter?.bindUI) {
        window.MainPageRouter.bindUI();
    }

    const cfg = await AppConfig.load();
    // Absent key (fresh install / pre-existing .env) defaults to shown, same
    // as core/config.py's SHOW_TOOLTIPS fallback.
    applyShowTooltipsPreference(!(cfg?.SHOW_TOOLTIPS === 'False' || cfg?.SHOW_TOOLTIPS === false));
    LogPanel.bindUI(cfg);
    // Initial fetch, then WebSocket takes over
    LogPanel.fetchLogs();
    ServiceStatus.fetchStatus();

    if (typeof AudioPanel !== 'undefined') {
        AudioPanel.bindUI?.();
        AudioPanel.updateDeviceLabels();
        AudioPanel.loadMicGain();
        AudioPanel.loadAudioDeviceSelectors();
        // Late refreshes for first-boot device-enumeration races.
        setTimeout(() => AudioPanel.updateDeviceLabels(), 3000);
        setTimeout(() => AudioPanel.updateDeviceLabels(), 12000);
    }
    SettingsForm.bindNewsSources();
    window.addBackstoryField = PersonaForm.addBackstoryField;
    window.savePersonaAs = PersonaForm.savePersonaAs;
    window.PersonaForm = PersonaForm;

    MotorPanel.bindUI();
    PinProfile.bindUI(cfg);
    ReleaseNotes.init();
    window.BillyVersionUI?.refresh();

    if (window.MainPageRouter?.updateActiveNav) {
        window.MainPageRouter.updateActiveNav(document.getElementById("main-content")?.dataset.page || "personas");
    }
    if (window.MainPageRouter?.initializePage) {
        await window.MainPageRouter.initializePage(document.getElementById("main-content")?.dataset.page || "personas");
    }
});
