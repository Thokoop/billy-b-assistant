// ===================== UI HELPERS =====================
function showNotification(message, type = "info", duration = 2500) {
    const bar = document.getElementById("notification");
    bar.textContent = message;
    bar.classList.remove("hidden", "opacity-0", "bg-cyan-500/80", "bg-emerald-500/80", "bg-amber-500/80", "bg-rose-500/80");
    const typeClass = {
        info: "bg-cyan-500/80",
        success: "bg-emerald-500/80",
        warning: "bg-amber-500/80",
        error: "bg-rose-500/80",
    }[type] || "bg-cyan-500/80";
    bar.classList.add(typeClass, "opacity-100");
    setTimeout(() => {
        bar.classList.remove("opacity-100");
        bar.classList.add("opacity-0");
        setTimeout(() => bar.classList.add("hidden"), 300);
    }, duration);
}

function toggleInputVisibility(inputId) {
    const input = document.getElementById(inputId);
    const icon = document.getElementById(`${inputId}_icon`);
    const isHidden = input.type === "password";
    input.type = isHidden ? "text" : "password";
    icon.textContent = isHidden ? "visibility_off" : "visibility";
}

function toggleDropdown(btn) {
    document.querySelectorAll('.dropdown-menu').forEach(menu => {
        if (!menu.classList.contains('hidden') && !menu.parentElement.contains(btn)) {
            menu.classList.add('hidden');
            const arrow = menu.parentElement.querySelector('.dropdown-toggle .material-icons');
            if (arrow) arrow.classList.remove('rotate-180');
        }
    });
    let dropdown = btn.closest('.relative').querySelector('.dropdown-menu');
    if (!dropdown) return;
    dropdown.classList.toggle('hidden');
    const arrow = btn.querySelector('.material-icons');
    if (arrow) arrow.classList.toggle('rotate-180');
}

function findTooltipTrigger(tooltip) {
    if (!tooltip) return null;
    if (tooltip.id) {
        const explicit = document.querySelector(
            `[data-tooltip-target="${tooltip.id}"]`
        );
        if (explicit) return explicit;
    }
    return tooltip
        .closest(".relative")
        ?.querySelector('.material-icons[onclick*="toggleTooltip"]');
}

function updateTooltipContainerLayers() {
    const sections = document.querySelectorAll(".collapsible-section");
    sections.forEach(section => {
        const hasVisibleTooltip = !!section.querySelector(
            '[data-tooltip][data-visible="true"]'
        );
        section.style.position = "relative";
        section.style.zIndex = hasVisibleTooltip ? "60" : "0";
    });
}

// Driven by the SHOW_TOOLTIPS setting (Advanced Settings). A single class
// on <html> plus a CSS rule targeting every [onclick*="toggleTooltip"] icon
// (the same selector findTooltipTrigger() below already uses to locate
// them) covers every page, including ones the SPA router injects later -
// no per-page JS needed to re-hide icons after this runs once.
function applyShowTooltipsPreference(show) {
    document.documentElement.classList.toggle('tooltips-hidden', !show);
}

// Driven by the "UI Animations & Effects" toggle. Applied globally on every
// page load (not just when the Settings page's own checkbox happens to be in
// the DOM) so the .reduce-motion class - and anything CSS keys off it, like
// the body gradient or the LED rainbow shift - stays correct regardless of
// which page the user lands on.
function applyReduceMotionPreference(reduced) {
    document.documentElement.classList.toggle('reduce-motion', reduced);
}

function toggleTooltip(el, evt) {
    if (!el) return;
    const clickEvent = evt || window.event;
    if (clickEvent) {
        clickEvent.preventDefault();
        clickEvent.stopPropagation();
    }
    el.classList.toggle("text-cyan-400");

    const explicitTargetId = el.getAttribute("data-tooltip-target");
    if (explicitTargetId) {
        const explicitTooltip = document.getElementById(explicitTargetId);
        if (explicitTooltip) {
            const visible = explicitTooltip.getAttribute("data-visible") === "true";
            explicitTooltip.setAttribute("data-visible", visible ? "false" : "true");
            updateTooltipContainerLayers();
            return;
        }
    }

    const label = el.closest("label");
    let tooltip = null;
    if (label && label.parentElement) {
        tooltip = label.parentElement.querySelector("[data-tooltip]");
    }
    if (!tooltip) {
        const container =
            el.closest(".relative") ||
            el.parentElement ||
            el.closest("div");
        if (container) {
            tooltip =
                container.querySelector("[data-tooltip]") ||
                container.parentElement?.querySelector("[data-tooltip]");
        }
    }
    if (!tooltip) return;

    const visible = tooltip.getAttribute("data-visible") === "true";
    tooltip.setAttribute("data-visible", visible ? "false" : "true");
    updateTooltipContainerLayers();
}

document.addEventListener('click', (e) => {
    // Close dropdowns when clicking outside
    document.querySelectorAll('.dropdown-menu').forEach(menu => {
        if (!menu.classList.contains('hidden') && !menu.closest('.relative').contains(e.target)) {
            menu.classList.add('hidden');
            const arrow = menu.parentElement.querySelector('.dropdown-toggle .material-icons');
            if (arrow) arrow.classList.remove('rotate-180');
        }
    });
    
    // Close tooltips when clicking outside
    document.querySelectorAll('[data-tooltip]').forEach(tooltip => {
        if (tooltip.getAttribute('data-visible') !== 'true') {
            return;
        }

        if (tooltip.contains(e.target)) {
            return;
        }

        const helpIcon = findTooltipTrigger(tooltip);
        if (helpIcon && helpIcon.contains(e.target)) {
            return;
        }

        tooltip.setAttribute('data-visible', 'false');
        if (helpIcon) {
            helpIcon.classList.remove('text-cyan-400');
        }
    });
    updateTooltipContainerLayers();
});

// A fetch that always settles. A request to a service that is going down can
// be accepted and then never answered, and a bare fetch() waits on that for as
// long as the browser allows, stalling whatever awaited it.
function fetchWithTimeout(url, timeoutMs = 5000, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, {cache: "no-store", ...options, signal: controller.signal})
        .finally(() => clearTimeout(timer));
}

window.fetchWithTimeout = fetchWithTimeout;

// A fetch whose failure path is its own concern. Call sites used to write
//     if (!res.ok) throw new Error(...)
// inside the very try/catch that handles it, which reads as control flow
// rather than as an error: here the throw belongs to this function's
// contract, and the caller's catch is a genuine boundary.
//
// accept(data, res) decides success where res.ok is not the whole story - a
// 200 carrying {status: "error"} is still a failure. error supplies the
// message to use when the body does not name one, as a string or as a
// (data, res) => string for endpoints that report it under another key.
async function requestJson(url, options = {}) {
    const {accept, error: fallback, ...init} = options;
    const res = await fetch(url, init);
    // A body that is missing or is not JSON is not itself the failure: a
    // route can answer 204, and a failing one often returns an error page.
    // Whether that counts is for accept (or res.ok) to say, below.
    const data = await res.json().catch(() => null);
    if (accept ? accept(data, res) : res.ok) return data;
    const detail = typeof fallback === "function" ? fallback(data, res) : fallback;
    throw new Error((data && data.error) || detail || `HTTP ${res.status}`);
}

window.requestJson = requestJson;

// ===================== LOADING OVERLAY =====================
const LoadingOverlay = (() => {
    const overlayId = "loading-overlay";
    const textId = "loading-overlay-text";
    const reloadFlagKey = "billy:reload_on_ws_reconnect";
    let reloadPollTimeout = null;
    let restartSawUnavailable = false;

    const shouldReloadOnReconnect = () => (
        sessionStorage.getItem(reloadFlagKey) === "1"
    );

    const clearReloadFlag = () => {
        sessionStorage.removeItem(reloadFlagKey);
    };

    const reloadSoon = () => {
        clearReloadFlag();
        if (reloadPollTimeout) {
            clearTimeout(reloadPollTimeout);
            reloadPollTimeout = null;
        }
        restartSawUnavailable = false;
        setTimeout(() => {
            window.location.reload();
        }, 200);
    };

    const show = (message = "Restarting Billy... reconnecting interface.") => {
        const overlay = document.getElementById(overlayId);
        const text = document.getElementById(textId);
        if (!overlay) return;
        if (text) text.textContent = message;
        overlay.classList.remove("hidden");
    };

    const hide = () => {
        const overlay = document.getElementById(overlayId);
        if (!overlay) return;
        overlay.classList.add("hidden");
    };

    const isVisible = () => {
        const overlay = document.getElementById(overlayId);
        return !!overlay && !overlay.classList.contains("hidden");
    };

    const waitForReload = (
        previousWebconfigInstance = null,
        initialDelayMs = 250,
        timeoutMs = 60000,
        // If neither an outage nor a new instance is ever observed (the restart
        // of the web interface failed, or it came back between two polls),
        // reload anyway instead of leaving the overlay up: a reload always
        // shows the truth.
        fallbackReloadMs = 20000,
        // When the restart was asked for. A readiness marker older than this
        // belongs to the run being replaced, not the one coming back.
        restartRequestedAt = null,
    ) => {
        const startedAt = Date.now();
        let announcedWaitingForBilly = false;
        // Billy only reports readiness on builds that write the marker. Until
        // one is seen, fall back to the service state as before.
        let sawReadyMarker = false;

        // Returns true when the overlay is finished with (a reload is on its
        // way, or the wait timed out); anything else means poll again.
        const pollOnce = async () => {
            let reachable = false;
            let instanceChanged = false;
            let billyState = null;
            let billyReadyAt = null;

            try {
                // The web interface is killed mid-request while it restarts,
                // which leaves the socket open and this fetch hanging. Without
                // a deadline the poll below never runs again and the overlay
                // sits there for good.
                const res = await fetchWithTimeout("/health", 2500);
                reachable = res.ok;
                const data = res.ok ? await res.json().catch(() => ({})) : {};
                billyState = data.billy_service || null;
                billyReadyAt = Number(data.billy_ready_at) || null;
                if (billyReadyAt) sawReadyMarker = true;
                instanceChanged = Boolean(
                    previousWebconfigInstance
                    && data.webconfig_instance
                    && data.webconfig_instance !== previousWebconfigInstance
                );
                if (!res.ok) {
                    restartSawUnavailable = true;
                }
            } catch (err) {
                restartSawUnavailable = true;
                // Expected while billy-webconfig.service is restarting.
            }

            const elapsed = Date.now() - startedAt;
            // The web interface is back when it answers again after going away,
            // or when it answers as a different process. If neither is ever seen
            // (it came back between two polls, or it never went down), fall back
            // to trusting that it is up after a while.
            const interfaceBack = reachable && (
                restartSawUnavailable
                || instanceChanged
                || elapsed >= fallbackReloadMs
            );

            if (interfaceBack) {
                // Billy takes longer to come back than the interface does,
                // and his unit reports "active" the moment the process is
                // exec'd - seconds before he can actually do anything. Wait
                // for him to say so himself where he can, and only fall back
                // to the service state on builds that never report it.
                // Both are the device's own clock in seconds: the marker's
                // modification time and the moment /restart was called.
                const readyAfterRestart = Boolean(
                    billyReadyAt
                    && (!restartRequestedAt || billyReadyAt > restartRequestedAt)
                );
                const billyReady = readyAfterRestart
                    || (!sawReadyMarker && (
                        !billyState
                        || billyState === "active"
                        || billyState === "failed"
                    ))
                    || billyState === "failed";
                if (billyReady || elapsed >= timeoutMs) {
                    reloadSoon();
                    return true;
                }
                if (!announcedWaitingForBilly) {
                    announcedWaitingForBilly = true;
                    show("Web interface is back. Waiting for Billy to start...");
                }
            }

            if (elapsed >= timeoutMs) {
                clearReloadFlag();
                hide();
                window.dispatchEvent(new CustomEvent("billy:restart-timeout"));
                return true;
            }

            return false;
        };

        // Every exit from pollOnce() schedules the next tick, including the
        // ones it takes by throwing. A single failed poll can slow recovery
        // down; it must never be able to stop it.
        const poll = async () => {
            let done = false;
            try {
                done = await pollOnce();
            } catch (err) {
                console.error("Restart poll failed:", err);
            }
            if (!done) {
                reloadPollTimeout = setTimeout(poll, 350);
            }
        };

        if (reloadPollTimeout) {
            clearTimeout(reloadPollTimeout);
        }
        reloadPollTimeout = setTimeout(poll, initialDelayMs);
    };

    window.addEventListener("billy:restart-unavailable", () => {
        restartSawUnavailable = true;
    });

    window.addEventListener("billy:websocket:connected", () => {
        if (isVisible()) {
            hide();
        }
        if (shouldReloadOnReconnect()) {
            reloadSoon();
        }
    });

    return { show, hide, isVisible, waitForReload, shouldReloadOnReconnect };
})();

window.LoadingOverlay = LoadingOverlay;

// ===================== MOBILE SPLIT VIEW =====================
const MobileSplitView = (() => {
    const mobileQuery = "(max-width: 47.98rem)";
    const transitionMs = 180;
    const slideDistance = 24;
    let resizeBound = false;

    const isMobileViewport = () => window.matchMedia(mobileQuery).matches;

    const setVisible = (pane, visible) => {
        if (!pane) return;
        pane.classList.toggle("hidden", !visible);
    };

    const clearPaneStyles = (pane) => {
        if (!pane) return;
        pane.style.position = "";
        pane.style.inset = "";
        pane.style.width = "";
        pane.style.zIndex = "";
        pane.style.willChange = "";
    };

    const applyStaticState = (root) => {
        if (!root) return;

        const masterPane = root.querySelector("[data-mobile-split-master]");
        const detailPane = root.querySelector("[data-mobile-split-detail]");
        const backBtn = root.querySelector("[data-mobile-split-back]");
        const isDetailActive = root.dataset.mobileSplitState === "detail";
        const isMobile = isMobileViewport();
        const hideMaster = isMobile && isDetailActive;
        const hideDetail = isMobile && !isDetailActive;

        clearPaneStyles(masterPane);
        clearPaneStyles(detailPane);
        root.style.position = "";
        root.style.overflow = "";
        root.style.minHeight = "";

        setVisible(masterPane, !hideMaster);
        setVisible(detailPane, !hideDetail);

        if (backBtn) {
            backBtn.classList.toggle("hidden", !hideMaster);
            backBtn.classList.toggle("flex", hideMaster);
        }

    };

    const animateStateChange = (root, nextState) => {
        if (!root || !isMobileViewport() || !window.Element?.prototype?.animate) {
            root.dataset.mobileSplitState = nextState;
            applyStaticState(root);
            return;
        }

        if (root.dataset.mobileSplitAnimating === "true") {
            root.dataset.mobileSplitState = nextState;
            return;
        }

        const masterPane = root.querySelector("[data-mobile-split-master]");
        const detailPane = root.querySelector("[data-mobile-split-detail]");
        const backBtn = root.querySelector("[data-mobile-split-back]");
        const currentState = root.dataset.mobileSplitState || "list";

        if (!masterPane || !detailPane || currentState === nextState) {
            root.dataset.mobileSplitState = nextState;
            applyStaticState(root);
            return;
        }

        const showingDetail = nextState === "detail";
        const outgoing = showingDetail ? masterPane : detailPane;
        const incoming = showingDetail ? detailPane : masterPane;
        const direction = showingDetail ? 1 : -1;

        setVisible(masterPane, true);
        setVisible(detailPane, true);
        root.dataset.mobileSplitAnimating = "true";

        const outgoingHeight = outgoing.offsetHeight;
        const incomingHeight = incoming.offsetHeight;
        root.style.minHeight = `${Math.max(outgoingHeight, incomingHeight)}px`;
        root.style.position = "relative";
        root.style.overflow = "hidden";

        [outgoing, incoming].forEach((pane, index) => {
            pane.style.position = "absolute";
            pane.style.inset = "0";
            pane.style.width = "100%";
            pane.style.zIndex = index === 0 ? "1" : "2";
            pane.style.willChange = "transform, opacity";
        });

        backBtn?.classList.remove("hidden");
        backBtn?.classList.add("flex");

        const outgoingAnimation = outgoing.animate(
            [
                { opacity: 1, transform: "translateX(0)" },
                { opacity: 0, transform: `translateX(${-direction * slideDistance}px)` },
            ],
            {
                duration: transitionMs,
                easing: "cubic-bezier(0.22, 1, 0.36, 1)",
                fill: "forwards",
            }
        );

        const incomingAnimation = incoming.animate(
            [
                { opacity: 0, transform: `translateX(${direction * slideDistance}px)` },
                { opacity: 1, transform: "translateX(0)" },
            ],
            {
                duration: transitionMs,
                easing: "cubic-bezier(0.22, 1, 0.36, 1)",
                fill: "forwards",
            }
        );

        Promise.allSettled([outgoingAnimation.finished, incomingAnimation.finished]).finally(() => {
            root.dataset.mobileSplitAnimating = "false";
            root.dataset.mobileSplitState = nextState;
            applyStaticState(root);
        });
    };

    const bindSplit = (root) => {
        if (!root) return;

        if (!root.dataset.mobileSplitState) {
            root.dataset.mobileSplitState = "list";
        }

        if (root.dataset.mobileSplitBound !== "true") {
            const backBtn = root.querySelector("[data-mobile-split-back]");
            backBtn?.addEventListener("click", () => {
                showList(root.id);
            });
            root.dataset.mobileSplitBound = "true";
        }

        applyStaticState(root);
    };

    const bindAll = () => {
        document.querySelectorAll("[data-mobile-split]").forEach((root) => {
            bindSplit(root);
        });

        if (!resizeBound) {
            window.addEventListener("resize", () => {
                document.querySelectorAll("[data-mobile-split]").forEach((root) => {
                    applyStaticState(root);
                });
            });
            resizeBound = true;
        }
    };

    const showDetail = (splitId) => {
        const root = document.getElementById(splitId);
        if (!root) return;
        animateStateChange(root, "detail");
    };

    const showList = (splitId) => {
        const root = document.getElementById(splitId);
        if (!root) return;
        animateStateChange(root, "list");
    };

    return { bindAll, showDetail, showList };
})();

window.MobileSplitView = MobileSplitView;
