const MainPageRouter = (() => {
    const activeClasses = [
        "bg-white/12",
        "!text-emerald-400",
    ];

    const getMainContent = () => document.getElementById("main-content");

    const getPageNameFromPath = (pathName) => {
        switch (pathName) {
            case "/":
            case "/personas":
                return "personas";
            case "/profiles":
                return "profiles";
            case "/songs-page":
                return "songs";
            case "/library":
                return "library";
            case "/settings":
                return "settings";
            default:
                return null;
        }
    };

    const updateActiveNav = (pageName) => {
        document.querySelectorAll("[data-page-link]").forEach((link) => {
            const isActive = link.dataset.page === pageName;
            activeClasses.forEach((className) => {
                link.classList.toggle(className, isActive);
            });
        });
    };

    const initializePage = async (pageName) => {
        window.MobileSplitView?.bindAll();
        Sections.collapsible();

        if (pageName === "personas") {
            await PersonaForm.populateVoiceOptions();
            await PersonaForm.loadPersona();
            window.bindWakeupClips?.();
            PersonaForm.handlePersonaSave();
            PersonaForm.bindPersonaSelector();
            await PersonaForm.populatePersonaSelector();
            PersonaForm.initPersonaMouthArticulationSlider();
            window.addBackstoryField = PersonaForm.addBackstoryField;
            if (window.PersonaForm && window.PersonaForm.initCreatePersonaModal) {
                window.PersonaForm.initCreatePersonaModal();
            }
            setTimeout(() => {
                if (window.syncPersonaWithCurrentUser) {
                    window.syncPersonaWithCurrentUser();
                }
            }, 100);
            return;
        }

        if (pageName === "profiles") {
            if (window.UserProfilePanel?.bindUI) {
                window.UserProfilePanel.bindUI();
            }
            return;
        }

        if (pageName === "songs") {
            SongsManager.init();
            return;
        }

        if (pageName === "library") {
            SettingsForm.bindNewsSources();
            if (window.KnowledgeManager?.bindUI) {
                window.KnowledgeManager.bindUI();
            }
            window.MobileSplitView?.bindAll();
            return;
        }

        if (pageName === "settings") {
            const cfg = await AppConfig.load();
            SettingsForm.handleSettingsSave();
            SettingsForm.saveDropdownSelections();
            SettingsForm.populateDropdowns(cfg);
            SettingsForm.initBooleanToggles();
            SettingsForm.initShowTooltipsToggle();
            await SettingsForm.initHostFields();
            SettingsForm.initMouthArticulationSlider();
            SettingsForm.initStatusLedBrightnessSlider();
            SettingsForm.initMouthBoostSlider();
            SettingsForm.bindFactoryReset();
            SettingsForm.bindEnvEditorCard();
            SettingsForm.bindNewsSources();
            SettingsForm.bindWakeWordKeywordUpload();
            SettingsForm.bindCameraPreview();
            SettingsForm.bindWiFiSection();
            SettingsForm.bindApiProviderFields();
            if (window.AudioPanel) {
                window.AudioPanel.bindUI?.();
                window.AudioPanel.loadMicGain?.();
                await window.AudioPanel.loadAudioDeviceSelectors?.();
                window.AudioPanel.updateDeviceLabels?.();
                setTimeout(() => window.AudioPanel?.updateDeviceLabels?.(), 3000);
            }
            window.ServiceStatus?.fetchStatus?.();
            window.LogPanel?.bindUI?.(cfg);
            window.BillyVersionUI?.refresh();
            window.ReleaseNotes?.refresh();
            window.MobileSplitView?.bindAll();
            return;
        }

        window.MobileSplitView?.bindAll();
    };

    const swapMainContent = (nextMain) => {
        const currentMain = getMainContent();
        if (!currentMain || !nextMain) return null;
        currentMain.replaceWith(nextMain);
        return nextMain;
    };

    const loadPage = async (url, { pushState = true } = {}) => {
        const response = await fetch(url, {
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });

        if (!response.ok) {
            throw new Error(`Failed to load page: ${response.status}`);
        }

        const html = await response.text();
        const parser = new DOMParser();
        const nextDocument = parser.parseFromString(html, "text/html");
        const nextMain = nextDocument.getElementById("main-content");

        if (!nextMain) {
            window.location.href = url;
            return;
        }

        const mountedMain = swapMainContent(nextMain);
        const nextTitle = nextDocument.querySelector("title")?.textContent;
        const pageName = mountedMain?.dataset.page || getPageNameFromPath(new URL(url, window.location.origin).pathname);

        if (nextTitle) {
            document.title = nextTitle;
        }
        if (pushState) {
            window.history.pushState({ url }, "", url);
        }

        updateActiveNav(pageName);
        await initializePage(pageName);
        window.scrollTo({ top: 0, behavior: "auto" });
    };

    const shouldHandleLink = (event, link) => {
        if (!link) return false;
        if (event.defaultPrevented) return false;
        if (event.button !== 0) return false;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;

        const url = new URL(link.href, window.location.origin);
        if (
            url.pathname === window.location.pathname &&
            url.search === window.location.search
        ) {
            return false;
        }
        return url.origin === window.location.origin && Boolean(getPageNameFromPath(url.pathname));
    };

    const bindUI = () => {
        if (document.body.dataset.pageRouterBound === "true") return;
        document.body.dataset.pageRouterBound = "true";

        document.addEventListener("click", async (event) => {
            const link = event.target.closest("[data-page-link]");
            if (!shouldHandleLink(event, link)) return;

            event.preventDefault();

            try {
                await loadPage(link.href);
            } catch (error) {
                console.error("Failed to navigate page:", error);
                window.location.href = link.href;
            }
        });

        window.addEventListener("popstate", async () => {
            const pageName = getPageNameFromPath(window.location.pathname);
            if (!pageName) return;

            try {
                await loadPage(window.location.href, { pushState: false });
            } catch (error) {
                console.error("Failed to restore page:", error);
                window.location.reload();
            }
        });
    };

    // A tab left open across a Pi reboot/service restart won't automatically
    // recover its data once the backend comes back - the websocket reconnects
    // (see websocket.js), but nothing re-fetches whatever the current page
    // loaded on its own initial render. Re-run that page's init logic (the
    // same one every SPA navigation already uses) once the connection is
    // confirmed back after a real drop, instead of requiring a manual refresh.
    let sawDisconnect = false;

    window.addEventListener("billy:websocket:disconnected", () => {
        sawDisconnect = true;
    });

    window.addEventListener("billy:websocket:connected", () => {
        if (!sawDisconnect) return;
        sawDisconnect = false;
        // A UI-initiated restart already has its own full-reload flow (see
        // LoadingOverlay.waitForReload) - don't race it with a soft refresh.
        if (window.LoadingOverlay?.shouldReloadOnReconnect?.()) return;
        const pageName = getPageNameFromPath(window.location.pathname);
        if (pageName) {
            initializePage(pageName);
        }
    });

    return {
        bindUI,
        loadPage,
        initializePage,
        updateActiveNav,
    };
})();

window.MainPageRouter = MainPageRouter;
