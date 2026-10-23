// ===================== VERSION & UPDATE =====================
(() => {

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


    const setButtonLoading = (button, loading) => {
        if (!button) return;
        const icon = button.querySelector(".material-icons");
        if (icon) {
            icon.classList.remove("animate-spin");
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

    let recoveryVersions = {current: 'Checking…', previous: null, available: false};
    const setText = (id, value) => {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    };
    const setSoftwareProgress = message => {
        setText('software-operation-status', message);
        setText('recovery-software-status', message);
        if (softwareBusy) window.LoadingOverlay?.showSoftware?.(message || 'Preparing Billy…');
        else window.LoadingOverlay?.hideSoftware?.();
    };
    const renderRecovery = () => {
        setText('recovery-current-version', recoveryVersions.current);
        setText('recovery-previous-version', recoveryVersions.available
            ? (recoveryVersions.previous || 'Version unknown') : 'No previous version saved');
        setText('recovery-previous-help', recoveryVersions.available
            ? 'Return to this saved installation.' : 'Available after a successful software installation.');
    };
    let softwareTimer = null;
    let softwareBusy = false;
    let sawSoftwareJob = false;
    let softwarePolling = false;
    let softwareStatusKnown = false;
    let submittingSoftware = false;
    let packagesChecking = false;
    let lastJobId = null;
    let awaitingNewJob = false;
    let requestDeadline = 0;
    const busyStates = new Set(['queued', 'preparing', 'installing_packages', 'activating', 'validating', 'rolling_back']);
    const actionButtons = () => [getUpdateBtn(), getSimulateUpdateBtn(), document.getElementById('rollback-software-btn'), document.getElementById('repair-packages-btn')];

    async function softwareJson(url, options = {}) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 5000);
        try {
            const response = await fetch(url, {...options, cache: 'no-store', signal: controller.signal});
            const body = await response.json();
            if (!response.ok) throw new Error(body.error || 'Software operation failed');
            return body;
        } finally { clearTimeout(timer); }
    }

    async function pollSoftware() {
        if (softwarePolling) return;
        softwarePolling = true;
        clearTimeout(softwareTimer);
        try {
            const data = await softwareJson('/software/status');
            softwareStatusKnown = true;
            recoveryVersions = {current: data.current || window.BillyVersionInfo.current || 'Version unknown',
                previous: data.previous || null, available: !!data.rollback_available};
            renderRecovery();
            const job = data.job || {};
            if (awaitingNewJob && (!job.id || job.id === lastJobId)) {
                if (Date.now() < requestDeadline) return;
                awaitingNewJob = false;
                sawSoftwareJob = false;
                showNotification('The request could not be confirmed. Check the status before trying again.', 'warning');
            } else if (awaitingNewJob) awaitingNewJob = false;
            lastJobId = job.id || null;
            softwareBusy = submittingSoftware || busyStates.has(job.state);
            setSoftwareProgress(job.state === 'complete' ? '' : (job.message || ''));
            actionButtons().forEach(button => setButtonLoading(button, softwareBusy));
            const rollback = document.getElementById('rollback-software-btn');
            if (rollback) rollback.disabled = softwareBusy || !data.rollback_available;
            const packages = data.packages || {};
            packagesChecking = packages.state === 'checking';
            const packageMessage = document.getElementById('system-packages-status');
            const packageSummary = document.getElementById('system-packages-summary');
            const message = packages.message || 'System-component checks require the compatibility update.';
            const needsAttention = packages.state === 'attention';
            const action = packages.damaged?.length
                ? {label: 'Repair components', badge: 'Repair needed', icon: 'build', summary: 'Repair the components Billy needs to run.'}
                : packages.missing?.length
                ? {label: 'Install components', badge: 'Installation needed', icon: 'download', summary: 'Install the missing components Billy needs to run.'}
                : packages.updates?.length
                ? {label: 'Update components', badge: 'Update available', icon: 'system_update_alt', summary: 'Update the components Billy needs to run.'}
                : {label: 'Set up components', badge: 'Setup needed', icon: 'build', summary: 'Finish setting up the components Billy needs to run.'};
            if (packageSummary) packageSummary.textContent = needsAttention ? action.summary : message;
            // The badge already explains the generic attention state. Keep
            // specific backend messages visible, including actionable errors.
            if (packageMessage) packageMessage.textContent = !packageSummary
                || (needsAttention && message !== 'System components need attention.') ? message : '';
            const badge = document.getElementById('system-packages-badge');
            if (badge) {
                badge.hidden = !needsAttention;
                badge.textContent = action.badge;
            }
            setText('system-packages-action-label', action.label);
            setText('system-packages-action-icon', action.icon);
            const details = [...new Set([...(packages.missing || []), ...(packages.damaged || []), ...(packages.updates || [])])].join(', ');
            const packageDetails = document.getElementById('system-packages-details');
            if (packageDetails) packageDetails.textContent = details;
            const detailPanel = document.getElementById('system-packages-detail-panel');
            if (detailPanel) {
                detailPanel.hidden = !needsAttention || !details;
                if (detailPanel.hidden) detailPanel.open = false;
            }
            const repair = document.getElementById('repair-packages-btn');
            if (repair) {
                repair.hidden = !packages.repairable;
                repair.disabled = softwareBusy || !packages.repairable;
            }
            const packageOperation = job.state === 'installing_packages'
                || (job.action === 'packages' && (softwareBusy || (job.state === 'failed' && packages.state !== 'ready')));
            setText('system-packages-operation', packageOperation ? (job.message || '') : '');
            if (softwareBusy) sawSoftwareJob = true;
            else if (sawSoftwareJob) {
                sawSoftwareJob = false;
                if (job.state === 'complete' || job.state === 'rolled_back') {
                    showNotification(job.message, job.state === 'complete' ? 'success' : 'warning');
                    setTimeout(() => location.reload(), 1200);
                } else if (job.state === 'failed') showNotification(job.message, 'error');
            }
        } catch (error) {
            // The job continues independently. Never resend a POST after timeout.
            if (sawSoftwareJob) setSoftwareProgress('Reconnecting to Billy… The software operation continues.');
        } finally {
            softwarePolling = false;
            if (softwareBusy || sawSoftwareJob || packagesChecking) softwareTimer = setTimeout(pollSoftware, 2000);
        }
    }

    function bindSoftwareButton(button, url, confirmation) {
        if (!button || button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', async () => {
            if (!softwareStatusKnown) {
                await pollSoftware();
                if (!softwareStatusKnown) {
                    showNotification('Waiting for the software status. Please try again shortly.', 'info');
                    return;
                }
            }
            if (softwareBusy || submittingSoftware || !confirm(typeof confirmation === 'function' ? confirmation() : confirmation)) return;
            softwareBusy = true;
            submittingSoftware = true;
            awaitingNewJob = true;
            requestDeadline = Date.now() + 30000;
            sawSoftwareJob = true;
            actionButtons().forEach(item => setButtonLoading(item, true));
            setSoftwareProgress('Preparing Billy…');
            try { sessionStorage.removeItem('billy:reload_on_ws_reconnect'); } catch (_) { /* Storage may be disabled. */ }
            try {
                const data = await softwareJson(url, {method: 'POST', headers: {'X-Billy-Update': '1'}});
                if (data.job?.id) awaitingNewJob = false;
                if (data.status === 'up-to-date') {
                    awaitingNewJob = false;
                    sawSoftwareJob = false;
                    softwareBusy = false;
                    setSoftwareProgress('Already up to date.');
                    actionButtons().forEach(item => setButtonLoading(item, false));
                    showNotification('Already up to date.', 'info');
                }
            } catch (error) {
                if (error.name !== 'AbortError' && !(error instanceof TypeError)) {
                    sawSoftwareJob = false;
                    awaitingNewJob = false;
                    softwareBusy = false;
                    setSoftwareProgress(error.message);
                    actionButtons().forEach(item => setButtonLoading(item, false));
                }
                showNotification(error.name === 'AbortError'
                    ? 'Checking whether Billy accepted the request…'
                    : error.message, 'warning');
            }
            submittingSoftware = false;
            await pollSoftware();
        });
    }

    const bindUpdateButton = () => bindSoftwareButton(getUpdateBtn(), '/update',
        'Update Billy? The new software is prepared first. Billy restarts briefly when it is ready. Keep the power connected.');
    const bindSimulateUpdateButton = () => bindSoftwareButton(getSimulateUpdateBtn(), '/update-simulate',
        () => `Reinstall ${recoveryVersions.current}? Your settings, profiles and memories are kept.`);
    const bindPackagesButton = () => bindSoftwareButton(document.getElementById('repair-packages-btn'), '/software/packages',
        'Install or repair Billy’s required system components? Billy may pause briefly. Your settings are kept. Keep the power connected.');
    const bindRollbackButton = () => bindSoftwareButton(document.getElementById('rollback-software-btn'), '/software/rollback',
        () => `Restore ${recoveryVersions.previous || 'the previous installation'}? Current version: ${recoveryVersions.current}. Your settings, profiles and memories are kept.`);

    Promise.resolve(window.BootstrapData?.take("version"))
        .then(bootstrapped => bootstrapped || fetch("/version").then(res => res.json()))
        .then(applyVersionInfo)
        .catch(err => {
            console.error("Failed to load version info", err);
            refreshUi();
        });

    window.addEventListener('billy:websocket:connected', pollSoftware);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) pollSoftware();
    });

    window.BillyVersionUI = {
        reloadVersion: async () => {
            try { applyVersionInfo(await softwareJson('/version')); }
            catch (error) { console.warn('Version refresh unavailable:', error); }
        },
        refresh: () => {
            refreshUi();
            bindUpdateButton();
            bindSimulateUpdateButton();
            bindRollbackButton();
            bindPackagesButton();
            pollSoftware();
        },
    };
})();

// ===================== RELEASE NOTES =====================
const ReleaseNotes = (() => {
    let currentNote = null;
    let retryTimer = null;
    let retryAttempts = 0;
    const MAX_BACKGROUND_RETRIES = 6;

    const getTitle = () => document.getElementById("release-title");
    const getBody = () => document.getElementById("release-body");
    const getLink = () => document.getElementById("release-link");

    async function fetchNote() {
        const bootstrapped = await window.BootstrapData?.take("release_note");
        if (bootstrapped) return bootstrapped;
        const res = await fetch("/release-note");
        if (!res.ok) throw new Error("Failed to fetch /release-note");
        return res.json();
    }

    async function fetchAndRender() {
        retryTimer = null;
        try {
            const note = await fetchNote();
            render(note);
            if (
                !note.body
                && retryAttempts < MAX_BACKGROUND_RETRIES
            ) {
                retryAttempts += 1;
                retryTimer = setTimeout(fetchAndRender, 750);
            }
        } catch (e) {
            console.warn("Release notes unavailable:", e);
        }
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
        await fetchAndRender();
    }

    return {
        init,
        reload: () => {
            clearTimeout(retryTimer);
            currentNote = null;
            retryAttempts = 0;
            fetchAndRender();
        },
        refresh: () => {
            if (currentNote) {
                render(currentNote);
            }
            if (!currentNote?.body && !retryTimer) {
                fetchAndRender();
            }
            window.BillyVersionUI?.refresh();
        },
    };
})();

window.ReleaseNotes = ReleaseNotes;
