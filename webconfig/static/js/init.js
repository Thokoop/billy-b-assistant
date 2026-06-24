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
    const cfg = await AppConfig.load();
    LogPanel.bindUI(cfg);
    // Initial fetch, then WebSocket takes over
    LogPanel.fetchLogs();
    ServiceStatus.fetchStatus();

    if (typeof AudioPanel !== 'undefined') {
        AudioPanel.updateDeviceLabels();
        AudioPanel.loadMicGain();
        AudioPanel.loadAudioDeviceSelectors();
        // Late refreshes for first-boot device-enumeration races.
        setTimeout(() => AudioPanel.updateDeviceLabels(), 3000);
        setTimeout(() => AudioPanel.updateDeviceLabels(), 12000);
    }
    PersonaForm.loadPersona();
    SettingsForm.handleSettingsSave();
    SettingsForm.saveDropdownSelections();
    SettingsForm.populateDropdowns(cfg);
    SettingsForm.initMouthArticulationSlider();
    SettingsForm.bindFactoryReset();
    SettingsForm.bindEnvEditorCard();
    SettingsForm.bindNewsSources();
    SettingsForm.bindWakeWordKeywordUpload();
    SettingsForm.bindCameraPreview();
    SettingsForm.bindWiFiSection();
    SettingsForm.bindApiProviderFields();
    PersonaForm.handlePersonaSave();
    PersonaForm.bindPersonaSelector();
    PersonaForm.populatePersonaSelector();
    PersonaForm.populateVoiceOptions();
    PersonaForm.initPersonaMouthArticulationSlider();
    window.addBackstoryField = PersonaForm.addBackstoryField;
    window.savePersonaAs = PersonaForm.savePersonaAs;
    window.PersonaForm = PersonaForm;
    
    // Sync persona with current user after PersonaForm is ready
    setTimeout(() => {
        if (window.syncPersonaWithCurrentUser) {
            window.syncPersonaWithCurrentUser();
        }
    }, 100);
    MotorPanel.bindUI();
    PinProfile.bindUI(cfg);
        if (window.UserProfilePanel && window.UserProfilePanel.bindUI) {
            window.UserProfilePanel.bindUI();
        }
    Sections.collapsible();
    ReleaseNotes.init();
    SongsManager.init();
    
    // Initialize Create Persona Modal
    if (window.PersonaForm && window.PersonaForm.initCreatePersonaModal) {
        window.PersonaForm.initCreatePersonaModal();
    }
});
