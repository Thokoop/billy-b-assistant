// ===================== COLLAPSIBLE SECTIONS =====================
const Sections = (() => {
    function collapsible() {
        document.querySelectorAll('.collapsible-section').forEach(section => {
            const header = section.querySelector('h3');
            if (!header) return;

            // Skip if already initialized to prevent duplicate event listeners
            if (header.hasAttribute('data-collapsible-initialized')) {
                return;
            }

            // Look for existing expand_more icon first
            let icon = header.querySelector('.material-icons:last-child');
            if (!icon || icon.textContent !== 'expand_more') {
                // Create expand_more icon if it doesn't exist
                icon = document.createElement('span');
                icon.className = 'material-icons transition-transform duration-200 ml-2 rotate-0';
                icon.textContent = 'expand_more';
                header.appendChild(icon);
            } else {
                icon.classList.add('transition-transform', 'duration-200', 'ml-2');
                icon.classList.add('rotate-0');
            }

            const id = section.id;
            const storedState = localStorage.getItem('collapse_' + id);
            const collapsed = storedState
                ? storedState === 'closed'
                : section.dataset.defaultCollapsed === 'true';
            section.classList.toggle('collapsed', collapsed);
            icon.classList.toggle('rotate-180', !collapsed);
            icon.classList.toggle('rotate-0', collapsed);
            header.classList.toggle('mb-4', !collapsed);
            
            // Update icon colors based on active state
            const sectionIcon = header.querySelector('.material-icons:first-child');
            if (sectionIcon) {
                if (collapsed) {
                    sectionIcon.classList.remove('text-emerald-400');
                    sectionIcon.classList.add('text-white');
                } else {
                    sectionIcon.classList.remove('text-white');
                    sectionIcon.classList.add('text-emerald-400');
                }
            }

            [...section.children].forEach(child => {
                if (child !== header && !child.classList.contains('force-hidden')) {
                    child.classList.toggle('hidden', collapsed);
                }
            });

            header.addEventListener('click', () => {
                const collapsed = section.classList.toggle('collapsed');
                [...section.children].forEach(child => { if (child !== header) child.classList.toggle('hidden', collapsed); });
                icon.classList.toggle('rotate-180', !collapsed);
                icon.classList.toggle('rotate-0', collapsed);
                header.classList.toggle('mb-4', !collapsed);
                localStorage.setItem('collapse_' + id, collapsed ? 'closed' : 'open');
                
                // Update icon colors based on active state
                const sectionIcon = header.querySelector('.material-icons:first-child');
                if (sectionIcon) {
                    if (collapsed) {
                        sectionIcon.classList.remove('text-emerald-400');
                        sectionIcon.classList.add('text-white');
                    } else {
                        sectionIcon.classList.remove('text-white');
                        sectionIcon.classList.add('text-emerald-400');
                    }
                }
            });
            
            // Mark as initialized AFTER everything is set up
            header.setAttribute('data-collapsible-initialized', 'true');
        });
    }

    return {collapsible};
})();

