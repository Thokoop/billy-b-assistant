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
        timeoutMs = 45000,
    ) => {
        const startedAt = Date.now();

        const poll = async () => {
            try {
                const res = await fetch("/health", {cache: "no-store"});
                const data = res.ok ? await res.json().catch(() => ({})) : {};
                const instanceChanged = Boolean(
                    previousWebconfigInstance
                    && data.webconfig_instance
                    && data.webconfig_instance !== previousWebconfigInstance
                );
                if (res.ok && (restartSawUnavailable || instanceChanged)) {
                    reloadSoon();
                    return;
                }
                if (!res.ok) {
                    restartSawUnavailable = true;
                }
            } catch (err) {
                restartSawUnavailable = true;
                // Expected while billy-webconfig.service is restarting.
            }

            if (Date.now() - startedAt >= timeoutMs) {
                clearReloadFlag();
                hide();
                window.dispatchEvent(new CustomEvent("billy:restart-timeout"));
                return;
            }

            reloadPollTimeout = setTimeout(poll, 350);
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
