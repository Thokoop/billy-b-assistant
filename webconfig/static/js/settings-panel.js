/**
 * User Profile Panel Management (cleaned & fixed)
 */
class UserProfilePanel {
    constructor() {
        this.panel = null;
        this.currentUser = null;
        this.profiles = [];
        this.personas = [];
        this.lastStatus = null;
        this.statusInterval = null;
        this.pendingDisplayName = null; // Store pending display name changes
        this.debugLevel = 'INFO'; // Default debug level
        this.lastLoadTime = 0;
        this.loadDebounceMs = 500; // Debounce loadAllData calls
        this.loadTimeout = null; // Track pending load timeout
        this.lastMemoryCount = 0; // Track memory count for change detection
        this.lastConfigHash = null; // Track config hash for change detection
        this.defaultUser = 'guest';
        this.isUserInitiatedSwitch = false; // Prevent polling from triggering redundant loads during user actions
        this.globalUiBound = false;
    }

    bindUI() {
        this.panel = document.getElementById('settings-panel');

        if (!this.globalUiBound) {
            // User profile button click handler
            const userProfileBtn = document.getElementById('user-profile-btn');
            if (userProfileBtn && this.panel) {
                userProfileBtn.addEventListener('click', () => this.openSettingsModal());
            }

            // Close panel button
            const closeBtn = document.getElementById('close-user-panel');
            if (closeBtn && this.panel) {
                closeBtn.addEventListener('click', () => this.closeSettingsModal());
            }

            // Click outside to close (only if both mousedown and mouseup on backdrop)
            let mouseDownOnBackdrop = false;

            if (this.panel) {
                this.panel.addEventListener('mousedown', (e) => {
                    mouseDownOnBackdrop = e.target === this.panel;
                });

                this.panel.addEventListener('mouseup', (e) => {
                    if (mouseDownOnBackdrop && e.target === this.panel) {
                        this.closeSettingsModal();
                    }
                    mouseDownOnBackdrop = false;
                });
            }

            // Set guest button
            const setGuestBtn = document.getElementById('set-guest-btn');
            if (setGuestBtn) {
                setGuestBtn.addEventListener('click', () => this.setAsGuest());
            }

            // Default user select
            const defaultUserSelect = document.getElementById('default-user-select');
            if (defaultUserSelect) {
                defaultUserSelect.addEventListener('change', (e) => this.updateDefaultUser(e.target.value));
            }

            this.globalUiBound = true;
        }

        // Persona select & save buttons
        const personaSelect = document.getElementById('persona-select');
        const updatePersonaBtn = document.getElementById('update-persona-btn');
        const updatePersonaBtnMain = document.getElementById('update-persona-btn-main');
        const profileForm = document.getElementById('profile-form');

        if (personaSelect && updatePersonaBtn && updatePersonaBtn.dataset.bound !== 'true') {
            updatePersonaBtn.addEventListener('click', () => this.updatePersona());
            personaSelect.addEventListener('change', () => {
                updatePersonaBtn.disabled = false;
            });
            updatePersonaBtn.dataset.bound = 'true';
        }
        if (updatePersonaBtnMain && updatePersonaBtnMain.dataset.bound !== 'true') {
            updatePersonaBtnMain.addEventListener('click', () => {
                this.debugLog('VERBOSE', 'Save User Profile button clicked');
                this.updatePersona();
            });
            updatePersonaBtnMain.dataset.bound = 'true';
        }

        // Handle profile form submission to prevent page refresh
        if (profileForm && profileForm.dataset.bound !== 'true') {
            profileForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.updatePersona();
            });
            profileForm.dataset.bound = 'true';

            // Bind profile settings toggle
            this.bindProfileSettingsToggle();

            // Bind profile action buttons
            this.bindProfileActionButtons();

            // Bind display name management
            this.bindDisplayNameManagement();

            // Bind Profile ID rename button
            this.bindProfileIdRename();
        }

        // Load initial data
        if (profileForm) {
            this.loadAllData(true);
        }

        // Update debug level from UI
        this.updateDebugLevel();
    }

    async loadAllData(force = false) {
        // Clear any pending load timeout
        if (this.loadTimeout) {
            clearTimeout(this.loadTimeout);
            this.loadTimeout = null;
        }

        if (!force) {
            return new Promise((resolve) => {
                this.loadTimeout = setTimeout(async () => {
                    await this._performLoad();
                    resolve();
                }, this.loadDebounceMs);
            });
        }

        await this._performLoad();
    }

    async _performLoad() {
        const now = Date.now();
        if ((now - this.lastLoadTime) < 100) {
            this.debugLog('VERBOSE', 'Skipping _performLoad - too soon since last load');
            return;
        }
        this.lastLoadTime = now;

        try {
            const data = await ConfigService.fetchConfig();
            if (!data) {
                this.debugLog('WARNING', 'No config data received');
                return;
            }

            // CURRENT_USER can be an object or a string
            this.debugLog('INFO', 'loadAllData - CURRENT_USER:', data.CURRENT_USER, 'type:', typeof data.CURRENT_USER);
            if (data.CURRENT_USER && typeof data.CURRENT_USER === 'object') {
                this.currentUser = data.CURRENT_USER.name;
                this.debugLog('INFO', 'Set currentUser from object:', this.currentUser);
            } else if (data.CURRENT_USER && typeof data.CURRENT_USER === 'string') {
                this.currentUser = data.CURRENT_USER;
                this.debugLog('INFO', 'Set currentUser from string:', this.currentUser);
            } else {
                this.currentUser = null;
                this.debugLog('INFO', 'No current user set');
            }

            // Profiles
            this.profiles = data.AVAILABLE_PROFILES || [];

            // Auto-set Guest as default if it's the only profile
            await this.autoSetGuestAsDefaultIfOnlyProfile();

            // Personas
            this.personas = (data.AVAILABLE_PERSONAS || []).map(p => ({
                id: p.name,
                name: p.name,
                description: p.description || p.name
            }));
            await this.updatePersonaSelect();

            // Default user
            this.defaultUser = data.DEFAULT_USER || 'guest';

            // UI updates (optimized to reduce redundant calls)
            this.updateProfileList(); // Updates profile list format
            this.updateDisplayNameVisibility();
            this.updateSaveButtonVisibility();
            this.updateProfileSettingsVisibility();
            this.updateProfileIdField();

            // Update persona selector to show current persona
            await this.updatePersonaSelectorForCurrentUser();
            this.updateCurrentUserDisplay();
            
            // Load data (these are async and can run in parallel)
            await Promise.all([
                this.loadDisplayName(),
                this.loadUserPersona(), // This sets persona selectors
                this.loadStats(),
                this.loadMood(),
                this.loadMemories()
            ]);
        } catch (error) {
            this.debugLog('ERROR', 'Failed to load all data:', error);
        }
    }

    // Helper function to get display name for a profile
    getDisplayName(profileName, profileData = null) {
        if (profileName.toLowerCase() === 'guest') {
            return 'Guest';
        }
        // If we have profile data, check for display_name
        if (profileData && profileData.data && profileData.data.USER_INFO && profileData.data.USER_INFO.display_name) {
            return profileData.data.USER_INFO.display_name;
        }
        // Fallback to profile name
        return profileName;
    }

    updateCurrentUserDisplay() {
        const currentUserDisplay = document.getElementById('current-user-display');
        if (currentUserDisplay) {
            if (this.currentUser && this.currentUser !== 'guest') {
                this.getCurrentUserDisplayName().then(displayName => {
                    currentUserDisplay.textContent = displayName;
                });
            } else {
                currentUserDisplay.textContent = 'Guest';
            }
        }
    }

    async getCurrentUserDisplayName() {
        try {
            const data = await ConfigService.fetchConfig();
            if (
                data && data.CURRENT_USER && data.CURRENT_USER.data &&
                data.CURRENT_USER.data.USER_INFO && data.CURRENT_USER.data.USER_INFO.display_name
            ) {
                return data.CURRENT_USER.data.USER_INFO.display_name;
            }
        } catch (error) {
            console.error('Failed to get current user display name:', error);
        }
        return this.currentUser || 'Guest';
    }

    openSettingsModal() {
        if (this.panel) {
            this.panel.classList.remove('hidden');
            const userProfileBtn = document.getElementById('user-profile-btn');
            if (userProfileBtn) {
                userProfileBtn.classList.add('bg-white/12');
                userProfileBtn.classList.remove('!text-white');
                userProfileBtn.classList.add('!text-emerald-400');
            }
            this.loadAllData();
        }
    }

    closeSettingsModal() {
        if (this.panel) {
            this.panel.classList.add('hidden');
            const userProfileBtn = document.getElementById('user-profile-btn');
            if (userProfileBtn) {
                userProfileBtn.classList.remove('bg-white/12');
                userProfileBtn.classList.remove('!text-emerald-400');
                userProfileBtn.classList.add('!text-white');
            }
        }
    }

    async loadCurrentUser() {
        try {
            const data = await ConfigService.fetchConfig();
            this.currentUser = (data && data.CURRENT_USER && data.CURRENT_USER.name) || (data && typeof data.CURRENT_USER === 'string' && data.CURRENT_USER) || null;
            this.updateCurrentUserDisplay();
        } catch (error) {
            console.error('Failed to load current user:', error);
            this.currentUser = null;
        }
    }

    async loadProfiles() {
        try {
            const data = await ConfigService.fetchConfig();
            if (data) {
                this.profiles = data.AVAILABLE_PROFILES || [];
            }
        } catch (error) {
            console.error('Failed to load profiles:', error);
        }
    }

    async updateProfileListFormat() {
        const profileList = document.getElementById('profile-list-main');
        if (!profileList) return;

        const currentUser = this.currentUser || 'guest';
        const currentDefault = this.defaultUser || 'guest';

        this.debugLog('VERBOSE', 'updateProfileListFormat - currentUser:', currentUser, 'currentDefault:', currentDefault);

        profileList.innerHTML = '';

        // Get current persona (works for both guest and logged-in users)
        let currentPersona = 'default';
        try {
            const configData = await ConfigService.fetchConfig();
            if (configData) {
                // Get persona from persona manager's current state
                if (configData.CURRENT_PERSONA) {
                    currentPersona = configData.CURRENT_PERSONA;
                } else if (configData.CURRENT_USER && configData.CURRENT_USER.data && configData.CURRENT_USER.data.USER_INFO) {
                    currentPersona = configData.CURRENT_USER.data.USER_INFO.preferred_persona || 'default';
                }
            }
        } catch (error) {
            this.debugLog('WARNING', 'Failed to load current persona:', error);
        }

        // Check if there's already a guest profile in the actual profiles list
        const hasGuestProfile = this.profiles.some(profile => {
            const profileName = typeof profile === 'string' ? profile : profile.name;
            return profileName.toLowerCase() === 'guest';
        });

        // Only create hardcoded guest row if there's no actual guest profile
        if (!hasGuestProfile) {
            // Guest row
            const guestRow = document.createElement('div');
            const isGuestActive = currentUser.toLowerCase() === 'guest' || currentUser === null;
            const isGuestDefault = currentDefault.toLowerCase() === 'guest';
            const guestPersona = isGuestActive ? currentPersona : 'default';

            guestRow.className = 'flex items-center justify-between p-3 bg-zinc-800 rounded-lg hover:bg-zinc-700 transition-colors cursor-pointer border border-zinc-700';
            guestRow.setAttribute('data-profile', 'guest');

            if (isGuestActive) {
                guestRow.classList.remove('border-zinc-700');
                guestRow.classList.add('border-emerald-500', 'bg-emerald-900/20');
            }

            guestRow.innerHTML = `
                <div class="flex items-center">
                    <button class="${isGuestDefault ? 'text-amber-400' : 'text-zinc-500'} hover:text-amber-300 p-1 rounded transition-colors" 
                            onclick="event.stopPropagation(); window.UserProfilePanel.setAsDefault('guest')" 
                            title="Set as default profile (loads automatically on boot)">
                        <span class="material-icons text-base">${isGuestDefault ? 'star' : 'star_border'}</span>
                    </button>
                    <span class="material-icons mr-3 ${isGuestActive ? 'text-emerald-400' : 'text-zinc-400'}">person_outline</span>
                    <div>
                        <div class="text-white">Guest</div>
                        <div class="text-xs text-zinc-400">${isGuestDefault ? 'Default (auto-loads on boot)' : 'Anonymous'} • ${guestPersona} </div>
                    </div>
                </div>
            `;

            guestRow.addEventListener('click', () => {
                window.MobileSplitView?.showDetail('profile-split-view');
                this.setAsGuest();
            });
            profileList.appendChild(guestRow);
        }

        // Sort profiles to put Guest first, then others alphabetically
        const sortedProfiles = [...this.profiles].sort((a, b) => {
            const nameA = typeof a === 'string' ? a : a.name;
            const nameB = typeof b === 'string' ? b : b.name;
            
            // Guest always comes first
            if (nameA.toLowerCase() === 'guest') return -1;
            if (nameB.toLowerCase() === 'guest') return 1;
            
            // Others sorted alphabetically
            return nameA.localeCompare(nameB);
        });

        // Render profiles in sorted order
        sortedProfiles.forEach(profile => {
            const profileName = typeof profile === 'string' ? profile : profile.name;
            const isCurrent = profileName.toLowerCase() === currentUser.toLowerCase();
            const isDefault = profileName.toLowerCase() === currentDefault.toLowerCase();
            const displayName = this.getDisplayName(profileName, profile);
            
            // Get preferred persona for this profile
            let preferredPersona = 'default';
            if (typeof profile === 'object' && profile.data && profile.data.USER_INFO) {
                preferredPersona = profile.data.USER_INFO.preferred_persona || 'default';
            }

            const row = document.createElement('div');
            row.className = 'flex items-center justify-between p-3 bg-zinc-800 rounded-lg hover:bg-zinc-700 transition-colors cursor-pointer border border-zinc-700';
            row.setAttribute('data-profile', profileName);

            if (isCurrent) {
                row.classList.remove('border-zinc-700');
                row.classList.add('border-emerald-500', 'bg-emerald-900/20');
            }

            // Special handling for guest profile
            const isGuestProfile = profileName.toLowerCase() === 'guest';
            const profileDescription = isGuestProfile ? 
                `${isDefault ? 'Default (auto-loads on boot)' : 'Anonymous'} • ${preferredPersona}` :
                `${isDefault ? 'Default (auto-loads on boot)' : 'User'} • ${preferredPersona}`;

            row.innerHTML = `
                <div class="flex items-center">
                    <button class="${isCurrent ? (isDefault ? 'text-amber-400' : 'text-zinc-500') : 'invisible'} hover:text-amber-300 p-1 rounded transition-colors" 
                            onclick="event.stopPropagation(); window.UserProfilePanel.setAsDefault('${profileName}')" 
                            title="Set as default profile (loads automatically on boot)"
                            ${!isCurrent ? 'disabled' : ''}>
                        <span class="material-icons text-base">${isDefault ? 'star' : 'star_border'}</span>
                    </button>
                    <span class="mr-3 material-icons ${isCurrent ? 'text-emerald-400' : 'text-zinc-400'}">${isGuestProfile ? 'person_outline' : 'person'}</span>
                    <div>
                        <div class="text-white">${displayName}</div>
                        <div class="text-xs text-zinc-400">${profileDescription}</div>
                    </div>
                </div>
                <div class="flex items-center space-x-2">
                    ${!isGuestProfile ? `
                    <button type="button" class="${profileName === currentUser ? 'secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0 opacity-50 cursor-not-allowed' : 'secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0'}"
                            onclick="event.stopPropagation(); ${profileName === currentUser ? 'window.UserProfilePanel.showCurrentUserDeleteMessage()' : `window.UserProfilePanel.deleteProfile('${profileName}')`}"
                            title="${profileName === currentUser ? 'Cannot delete current user' : 'Delete profile'}">
                        <span class="material-icons">delete</span>
                    </button>
                    ` : ''}
                </div>
            `;

            row.addEventListener('click', () => {
                window.MobileSplitView?.showDetail('profile-split-view');
                this.setAsCurrentUser(profileName);
            });
            profileList.appendChild(row);
        });
    }

    updateProfileList() {
        // Try both the old modal ID and new main column ID
        const profileTiles = document.getElementById('profile-tiles') || document.getElementById('profile-list-main');
        if (!profileTiles) return;

        // New list format
        if (profileTiles.id === 'profile-list-main') {
            this.updateProfileListFormat();
            return;
        }

        // Old tile format (keep working)
        const currentUser = this.currentUser || 'guest';
        const currentDefault = this.defaultUser || 'guest';

        this.debugLog('VERBOSE', 'updateProfileList - currentUser:', currentUser, 'currentDefault:', currentDefault);

        const guestTile = profileTiles.querySelector('[data-profile="guest"]');
        if (guestTile) {
            const isGuestDefault = currentDefault === 'guest';
            const isGuestActive = currentUser === 'guest' || currentUser === null;

            guestTile.className = isGuestActive
                ? 'relative bg-zinc-600 rounded-lg p-4 cursor-pointer transition-all duration-200 hover:bg-zinc-600 border-2 border-emerald-500 min-h-[120px] flex flex-col justify-center group'
                : 'relative bg-zinc-700 rounded-lg p-4 cursor-pointer transition-all duration-200 hover:bg-zinc-600 border-2 border-transparent min-h-[120px] flex flex-col justify-center group';

            const guestStatusText = guestTile.querySelector('.text-zinc-400.text-xs');
            if (guestStatusText) {
                guestStatusText.textContent = isGuestDefault ? 'Default' : 'Anonymous';
            }

            const starButton = guestTile.querySelector('button[title="Set as default"] span');
            if (starButton) {
                starButton.textContent = isGuestDefault ? 'star' : 'star_border';
            }
        }

        // Remove old tiles (except guest)
        const existingProfileTiles = profileTiles.querySelectorAll('[data-profile]:not([data-profile="guest"])');
        existingProfileTiles.forEach(tile => tile.remove());

        // Add tiles for each profile
        this.profiles.forEach(profile => {
            const profileName = typeof profile === 'string' ? profile : profile.name;
            const isCurrent = profileName === currentUser;
            const isDefault = profileName === currentDefault;
            const displayName = this.getDisplayName(profileName, profile);

            const tile = document.createElement('div');
            const baseClasses = 'relative rounded-lg p-4 cursor-pointer transition-all duration-200 hover:bg-zinc-600 border-2 min-h-[120px] flex flex-col justify-center group';
            const activeClasses = 'bg-zinc-600 border-emerald-500';
            const inactiveClasses = 'bg-zinc-700 border-transparent';

            tile.className = `${baseClasses} ${isCurrent ? activeClasses : inactiveClasses}`;
            tile.setAttribute('data-profile', profileName);
            tile.onclick = () => this.setAsCurrentUser(profileName);

            tile.innerHTML = `
                <div class="text-center">
                    <div class="mb-3 flex justify-center">
                        <span class="material-icons text-6xl text-emerald-400">person</span>
                    </div>
                    <div class="text-white text-sm mb-1">${displayName}</div>
                    <div class="text-zinc-400 text-xs">${isDefault ? 'Default (auto-loads on startup)' : 'User'}</div>
                </div>
                <div class="absolute top-2 right-2">
                    <button class="text-amber-400 hover:text-amber-300 transition-colors" onclick="event.stopPropagation(); window.UserProfilePanel.setAsDefault('${profileName}')" title="Set as default profile (loads automatically on startup)">
                        <span class="material-icons text-sm">${isDefault ? 'star' : 'star_border'}</span>
                    </button>
                </div>
            `;

            profileTiles.appendChild(tile);
        });
    }

    async loadPersonas() {
        try {
            const data = await ConfigService.fetchConfig();
            if (data) {
                this.personas = (data.AVAILABLE_PERSONAS || []).map(p => ({
                    id: p.name,
                    name: p.name,
                    description: p.description || p.name
                }));
                await this.updatePersonaSelect();
            }
        } catch (error) {
            console.error('Failed to load personas:', error);
        }
    }

    async updatePersonaSelect() {
        const personaSelect = document.getElementById('persona-select');
        const personaSelectMain = document.getElementById('persona-select-main');

        // Fetch full persona data to get display names and descriptions
        const personasWithDescriptions = await Promise.all(this.personas.map(async (persona) => {
            try {
                const response = await fetch(`/persona/${persona.id}`);
                if (response.ok) {
                    const data = await response.json();
                    return {
                        ...persona,
                        displayName: data.META?.name || persona.name,
                        description: data.META?.description || persona.description
                    };
                }
            } catch (error) {
                console.warn(`Failed to fetch persona ${persona.id}:`, error);
            }
            return persona;
        }));

        [personaSelect, personaSelectMain].forEach(select => {
            if (select) {
                select.innerHTML = '';

                // Add all personas (including default)
                personasWithDescriptions.forEach(persona => {
                    const option = document.createElement('option');
                    option.value = persona.id;
                    option.textContent = `${persona.displayName || persona.name} - ${persona.description}`;
                    select.appendChild(option);
                });
            }
        });
    }

    async loadUserPersona() {
        try {
            // Determine which persona to load based on current user
            let preferredPersona = 'default';
            
            if (this.currentUser && this.currentUser !== 'guest') {
                // Logged-in user: fetch from profile
                const response = await fetch(`/profiles/${this.currentUser}`);
                if (response.ok) {
                    const profileData = await response.json();
                    preferredPersona = profileData.data?.USER_INFO?.preferred_persona || 'default';
                }
            } else {
                // Guest mode: get preferred persona from guest profile in AVAILABLE_PROFILES
                try {
                    const configData = await ConfigService.fetchConfig();
                    if (configData) {
                        // Look for guest profile in AVAILABLE_PROFILES
                        const guestProfile = configData.AVAILABLE_PROFILES?.find(
                            profile => profile.name?.toLowerCase() === 'guest'
                        );
                        
                        if (guestProfile?.data?.USER_INFO?.preferred_persona) {
                            preferredPersona = guestProfile.data.USER_INFO.preferred_persona;
                        } else {
                            // Fall back to CURRENT_PERSONA (what's currently loaded)
                            preferredPersona = configData.CURRENT_PERSONA || 'default';
                        }
                    }
                } catch (error) {
                    console.warn('Failed to load guest persona from config:', error);
                }
            }

            // Update both selectors
            const selectIds = ['persona-select', 'persona-select-main'];
            selectIds.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.value = preferredPersona;
            });

            // Tell PersonaForm to load this persona (if available)
            if (window.PersonaForm?.loadPersona) {
                await window.PersonaForm.loadPersona(preferredPersona);
            }
        } catch (error) {
            console.error('Failed to load user persona:', error);
        }
    }

    // Wrapper added because original code referenced loadUserDisplayName()
    async loadUserDisplayName() {
        return this.loadDisplayName();
    }

    updateDisplayNameVisibility() {
        const displayNameSection = document.getElementById('profile-name-row-main');
        const personaSection = document.querySelector('#persona-select-main') && document.querySelector('#persona-select-main').closest('div').parentElement;
        const statsSection = document.querySelector('#section-stats-main');
        const moodSection = document.querySelector('#section-mood-main');
        const memoriesSection = document.querySelector('#section-memories-main');
        const profileSettingsSection = document.querySelector('#section-profile-settings-main');

        if (this.currentUser && this.currentUser !== 'guest') {
            // Logged in user: show everything
            if (displayNameSection) displayNameSection.style.display = 'grid';
            if (personaSection) personaSection.style.display = 'block';
            if (statsSection) statsSection.style.display = 'block';
            if (moodSection) moodSection.style.display = 'block';
            if (memoriesSection) memoriesSection.style.display = 'block';
            if (profileSettingsSection) profileSettingsSection.style.display = 'block';
        } else {
            // Guest mode: hide display name, stats, and memories, but keep persona and general mood visible
            if (displayNameSection) displayNameSection.style.display = 'none';
            if (personaSection) personaSection.style.display = 'block'; // Keep visible for guest
            if (statsSection) statsSection.style.display = 'none';
            if (moodSection) moodSection.style.display = 'block';
            if (memoriesSection) memoriesSection.style.display = 'none';
            if (profileSettingsSection) profileSettingsSection.style.display = 'block'; // Keep parent section visible
        }
    }

    async loadStats() {
        try {
            const data = await ConfigService.fetchConfig();
            const statsContent = document.getElementById('stats-content') || document.getElementById('stats-content-main');
            if (!statsContent) return;

            // Don't load stats for guest mode
            if (this.currentUser === 'guest' || this.currentUser === null) {
                statsContent.innerHTML = '<p class="text-sm text-zinc-400 italic">No stats in guest mode</p>';
                return;
            }

            // Get user info for interaction count and dates
            const userInfo = data && data.CURRENT_USER && data.CURRENT_USER.data && data.CURRENT_USER.data.USER_INFO || {};
            const interactionCount = parseInt(userInfo.interaction_count || '0');
            const createdDate = userInfo.created_date;
            const lastSeen = userInfo.last_seen;

            // Format dates with time in 24-hour format
            const formatDateTime = (dateString) => {
                if (!dateString) return 'Unknown';
                try {
                    const date = new Date(dateString);
                    return date.toLocaleString('en-US', { 
                        year: 'numeric', 
                        month: 'short', 
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit',
                        hour12: false
                    });
                } catch (e) {
                    return 'Unknown';
                }
            };

            // Create stats content
            const statsHTML = `
                <div class="p-3 bg-zinc-800/50 rounded-lg border border-zinc-600">
                    <div class="space-y-3 text-sm">
                        <div class="flex items-center gap-2">
                            <span class="material-icons text-cyan-400 text-sm">chat</span>
                            <span class="text-zinc-300">Interactions:</span>
                            <span class="text-white">${interactionCount}</span>
                        </div>
                        <div class="flex items-center gap-2">
                            <span class="material-icons text-emerald-400 text-sm">person_add</span>
                            <span class="text-zinc-300">First met:</span>
                            <span class="text-white">${formatDateTime(createdDate)}</span>
                        </div>
                        <div class="flex items-center gap-2">
                            <span class="material-icons text-amber-400 text-sm">schedule</span>
                            <span class="text-zinc-300">Last seen:</span>
                            <span class="text-white">${formatDateTime(lastSeen)}</span>
                        </div>
                    </div>
                </div>
            `;

            statsContent.innerHTML = statsHTML;
        } catch (error) {
            console.error('Failed to load stats:', error);
            const statsContent = document.getElementById('stats-content') || document.getElementById('stats-content-main');
            if (statsContent) {
                statsContent.innerHTML = '<p class="text-sm text-zinc-400 italic">Error loading stats</p>';
            }
        }
    }

    async loadMood(status = null) {
        await window.MoodPanel?.load(status);
    }

    async loadMemories() {
        try {
            const data = await ConfigService.fetchConfig();
            const memoriesList = document.getElementById('memories-list') || document.getElementById('memories-list-main');
            if (!memoriesList) return;

            if (this.currentUser === 'guest' || this.currentUser === null) {
                memoriesList.innerHTML = '<p class="text-sm text-zinc-400 italic">No memories in guest mode</p>';
                return;
            }

            const memories = data?.CURRENT_USER?.data?.core_memories || [];
            if (memories.length > 0) {
                const allMemories = [...memories].reverse();
                memoriesList.innerHTML = allMemories.map(memory => `
                    <div class="flex items-center justify-between py-2 px-3 bg-zinc-800 rounded-lg">
                        <div class="flex-1">
                            <p class="text-sm text-zinc-200">${memory.memory}</p>
                            <div class="flex items-center gap-2 mt-1">
                                <span class="text-xs text-zinc-400">${memory.category}</span>
                                <span class="text-xs text-zinc-500">•</span>
                                <span class="text-xs text-zinc-400">${memory.importance}</span>
                                <span class="text-xs text-zinc-500">•</span>
                                <span class="text-xs text-zinc-400">${new Date(memory.date).toLocaleDateString()}</span>
                            </div>
                        </div>
                        <div class="flex items-center gap-1">
                            <button type="button" class="icon-action-btn icon-action-btn--edit" data-memory-id="${memory.id || 'temp_' + memory.date}" onclick="event.stopPropagation(); window.UserProfilePanel.editMemory(this.dataset.memoryId)" title="Edit memory">
                                <span class="material-icons">edit</span>
                            </button>
                            <button type="button" class="secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0" data-memory-id="${memory.id || 'temp_' + memory.date}" onclick="event.stopPropagation(); window.UserProfilePanel.deleteMemory(this.dataset.memoryId)" title="Delete memory">
                                <span class="material-icons">delete</span>
                            </button>
                        </div>
                    </div>
                `).join('');
            } else {
                memoriesList.innerHTML = '<p class="text-sm text-zinc-400 italic">No memories yet</p>';
            }
        } catch (error) {
            console.error('Failed to load memories:', error);
            const memoriesList = document.getElementById('memories-list') || document.getElementById('memories-list-main');
            if (memoriesList) {
                memoriesList.innerHTML = '<p class="text-sm text-zinc-400 italic">Error loading memories</p>';
            }
        }
    }

    bindProfileSettingsToggle() {
        const toggle = document.getElementById('profile-settings-toggle');
        const content = document.getElementById('profile-settings-content');
        const chevron = document.getElementById('profile-settings-chevron');

        if (toggle && content && chevron) {
            toggle.addEventListener('click', () => {
                const isHidden = content.classList.contains('hidden');

                if (isHidden) {
                    content.classList.remove('hidden');
                    chevron.style.transform = 'rotate(180deg)';
                    const textSpan = toggle.querySelector('span:first-child');
                    if (textSpan) textSpan.innerHTML = '<span class="material-icons mr-2 text-emerald-400">settings</span>Hide profile settings';
                    toggle.classList.remove('rounded-t-lg');
                    toggle.classList.add('rounded-none');
                } else {
                    content.classList.add('hidden');
                    chevron.style.transform = 'rotate(0deg)';
                    const textSpan = toggle.querySelector('span:first-child');
                    if (textSpan) textSpan.innerHTML = '<span class="material-icons mr-2 text-emerald-400">settings</span>Show profile settings';
                    toggle.classList.remove('rounded-none');
                    toggle.classList.add('rounded-t-lg');
                }
            });
        }
    }

    updateProfileSettingsVisibility() {
        const toggle = document.getElementById('profile-settings-toggle');
        const content = document.getElementById('profile-settings-content');

        if (toggle && content) {
            if (this.currentUser === 'guest' || this.currentUser === null) {
                toggle.style.display = 'none';
                content.classList.add('hidden');
            } else {
                toggle.style.display = 'flex';
                content.classList.add('hidden');
            }
        }
    }

    // Bind profile action buttons
    bindProfileActionButtons() {
        const editProfileBtn = document.getElementById('edit-profile-btn');
        const deleteProfileBtn = document.getElementById('delete-profile-btn');

        if (editProfileBtn) {
            editProfileBtn.addEventListener('click', () => {
                if (this.currentUser && this.currentUser !== 'guest') {
                    this.editProfile(this.currentUser);
                } else {
                    this.showNotification('No profile selected to edit', 'warning');
                }
            });
        }

        if (deleteProfileBtn) {
            deleteProfileBtn.addEventListener('click', () => {
                if (this.currentUser && this.currentUser !== 'guest') {
                    this.deleteProfile(this.currentUser);
                } else {
                    this.showNotification('No profile selected to delete', 'warning');
                }
            });
        }
    }

    // Bind display name management
    bindDisplayNameManagement() {
        const displayNameInput = document.getElementById('display-name-input');
        const displayNameInputMain = document.getElementById('display-name-input-main');

        if (displayNameInput) {
            displayNameInput.addEventListener('input', () => this.onDisplayNameChange());
        }
        if (displayNameInputMain) {
            displayNameInputMain.addEventListener('input', () => this.onDisplayNameChange());
        }
    }

    // Bind the "Profile ID" field's rename button (Profile Settings panel)
    bindProfileIdRename() {
        const btn = document.getElementById('profile-id-rename-btn-main');
        if (btn) {
            btn.addEventListener('click', () => {
                if (this.currentUser && this.currentUser !== 'guest') {
                    this.editProfile(this.currentUser);
                }
            });
        }
    }

    // Show the current profile's internal ID (the "<name>.ini" filename,
    // sans extension) next to its editable Display Name.
    updateProfileIdField() {
        const display = document.getElementById('profile-id-display-main');
        const renameBtn = document.getElementById('profile-id-rename-btn-main');
        if (!display) return;

        const isGuest = this.currentUser === 'guest' || this.currentUser === null;
        display.textContent = isGuest ? '' : this.currentUser;
        if (renameBtn) renameBtn.classList.toggle('hidden', isGuest);
    }

    // Load display name for current user
    async loadDisplayName() {
        try {
            const displayNameInput = document.getElementById('display-name-input');
            const displayNameInputMain = document.getElementById('display-name-input-main');

            if (this.currentUser === 'guest' || this.currentUser === null) {
                if (displayNameInput) {
                    displayNameInput.value = '';
                    displayNameInput.disabled = true;
                    displayNameInput.placeholder = 'No display name in guest mode';
                }
                if (displayNameInputMain) {
                    displayNameInputMain.value = '';
                    displayNameInputMain.disabled = true;
                    displayNameInputMain.placeholder = 'No display name in guest mode';
                }
                return;
            }

            if (displayNameInput) {
                displayNameInput.disabled = false;
                displayNameInput.placeholder = 'Enter display name';
            }
            if (displayNameInputMain) {
                displayNameInputMain.disabled = false;
                displayNameInputMain.placeholder = 'Enter display name';
            }

            let displayNameValue;
            if (this.pendingDisplayName !== null) {
                displayNameValue = this.pendingDisplayName;
            } else {
                const data = await ConfigService.fetchConfig();
                if (data?.CURRENT_USER?.data?.USER_INFO?.display_name) {
                    displayNameValue = data.CURRENT_USER.data.USER_INFO.display_name;
                } else {
                    displayNameValue = this.currentUser;
                }
            }

            if (displayNameInput) displayNameInput.value = displayNameValue;
            if (displayNameInputMain) displayNameInputMain.value = displayNameValue;
        } catch (error) {
            console.error('Failed to load display name:', error);
            const displayNameInput = document.getElementById('display-name-input');
            const displayNameInputMain = document.getElementById('display-name-input-main');
            if (displayNameInput) {
                displayNameInput.value = '';
                displayNameInput.placeholder = 'Error loading display name';
            }
            if (displayNameInputMain) {
                displayNameInputMain.value = '';
                displayNameInputMain.placeholder = 'Error loading display name';
            }
        }
    }

    // Handle display name change
    onDisplayNameChange() {
        if (this.currentUser === 'guest' || this.currentUser === null) return;

        const displayNameInput = document.getElementById('display-name-input');
        const displayNameInputMain = document.getElementById('display-name-input-main');

        const newDisplayName = ((displayNameInputMain && displayNameInputMain.value) || (displayNameInput && displayNameInput.value) || '').trim();

        if (displayNameInput && displayNameInput.value !== newDisplayName) {
            displayNameInput.value = newDisplayName;
        }
        if (displayNameInputMain && displayNameInputMain.value !== newDisplayName) {
            displayNameInputMain.value = newDisplayName;
        }

        this.pendingDisplayName = newDisplayName;
        this.enableSaveButton();
    }

    async loadDefaultUser() {
        try {
            const data = await ConfigService.fetchConfig();
            if (data) {
                this.defaultUser = data.DEFAULT_USER || 'guest';
            }
        } catch (error) {
            console.error('Failed to load default user:', error);
            this.defaultUser = 'guest';
        }
    }

    async setAsCurrentUser(profileName) {
        try {
            this.showProfileLoading(profileName);
            this.isUserInitiatedSwitch = true; // Prevent polling from triggering redundant loads

            // Set current user in backend
            const response = await fetch('/current-user', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: profileName })
            });

            if (response.ok) {
                // Persist to .env
                await fetch('/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ CURRENT_USER: profileName })
                });

                await new Promise(r => setTimeout(r, 100));
                
                // Update lastStatus to prevent polling from detecting this as a change
                const newStatus = await ServiceStatus.fetchStatus();
                if (newStatus) {
                    this.lastStatus = newStatus;
                    this.lastMemoryCount = newStatus.memory_count || 0;
                    this.lastConfigHash = newStatus.config_hash;
                }
                
                await this.loadAllData(true); // This handles all UI updates and persona loading
                
                // Reset flag after a delay to allow polling to resume
                setTimeout(() => { this.isUserInitiatedSwitch = false; }, 3000);

                // Notification with persona description
                let personaToShow = 'Default';
                try {
                    const data = await ConfigService.fetchConfig();
                    const personaName = data?.CURRENT_USER?.data?.USER_INFO?.preferred_persona || 'default';
                    const personaResponse = await fetch(`/persona/${personaName}`);
                    if (personaResponse.ok) {
                        const personaData = await personaResponse.json();
                        personaToShow = personaData?.META?.description || personaName;
                    } else {
                        personaToShow = personaName;
                    }
                } catch (e) {
                    console.warn('Failed to get persona info for notification:', e);
                }

                const displayName = this.getDisplayName(
                    profileName,
                    this.profiles.find(p => (typeof p === 'string' ? p : p.name) === profileName)
                );
                this.showNotification(`Switched to ${displayName} • Loaded persona: ${personaToShow}`, 'info');

                this.hideProfileLoading(profileName);
            } else {
                this.hideProfileLoading(profileName);
                this.showNotification('Failed to set current user', 'error');
            }
        } catch (error) {
            console.error('Failed to set current user:', error);
            this.hideProfileLoading(profileName);
            this.showNotification('Failed to set current user', 'error');
        }
    }

    async setAsGuest() {
        try {
            this.showProfileLoading('guest');
            this.isUserInitiatedSwitch = true; // Prevent polling from triggering redundant loads

            const response = await fetch('/current-user', { method: 'DELETE' });

            if (response.ok) {
                await fetch('/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ CURRENT_USER: 'guest' })
                });

                await new Promise(r => setTimeout(r, 100));
                
                // Update lastStatus to prevent polling from detecting this as a change
                const newStatus = await ServiceStatus.fetchStatus();
                if (newStatus) {
                    this.lastStatus = newStatus;
                    this.lastMemoryCount = newStatus.memory_count || 0;
                    this.lastConfigHash = newStatus.config_hash;
                }
                
                await this.loadAllData(true); // This handles all UI updates and persona loading
                
                // Reset flag after a delay to allow polling to resume
                setTimeout(() => { this.isUserInitiatedSwitch = false; }, 3000);

                this.showNotification('Switched to Guest • Loaded persona: Default', 'info');
                this.hideProfileLoading('guest');
            } else {
                this.hideProfileLoading('guest');
                this.showNotification('Failed to set as guest', 'error');
            }
        } catch (error) {
            console.error('Failed to set as guest:', error);
            this.hideProfileLoading('guest');
            this.showNotification('Failed to set as guest', 'error');
        }
    }

    async deleteProfile(profileName) {
        if (profileName.toLowerCase() === 'guest') {
            this.showGuestDeleteMessage();
            return;
        }

        if (profileName === this.currentUser) {
            this.showNotification('Cannot delete the currently active user profile. Switch to a different user first, then delete this profile.', 'error', 8000);
            return;
        }

        if (!confirm(`Are you sure you want to delete the profile for ${profileName}?`)) {
            return;
        }

        try {
            const response = await fetch(`/profiles/${profileName}`, { method: 'DELETE' });

            if (response.ok) {
                this.showNotification(`Deleted profile for ${profileName}`, 'success');
                await this.loadAllData();
            } else {
                this.showNotification('Failed to delete profile', 'error');
            }
        } catch (error) {
            console.error('Failed to delete profile:', error);
            this.showNotification('Failed to delete profile', 'error');
        }
    }

    getApiUsername() {
        if (!this.currentUser || this.currentUser === 'guest') return this.currentUser;
        return this.currentUser.toLowerCase();
    }

    async deleteMemory(memoryId) {
        if (!this.currentUser || this.currentUser === 'guest') {
            this.showNotification('Cannot delete memories in guest mode', 'error');
            return;
        }

        if (!confirm('Are you sure you want to delete this memory? This action cannot be undone.')) {
            return;
        }

        try {
            const requestData = { user: this.getApiUsername(), memoryId };
            const response = await fetch('/profiles/delete-memory', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(requestData)
            });

            if (response.ok) {
                this.showNotification('Memory deleted successfully', 'success');
                await this.loadMemories();
            } else {
                const error = await response.json();
                this.showNotification(error.error || 'Failed to delete memory', 'error');
            }
        } catch (error) {
            console.error('Failed to delete memory:', error);
            this.showNotification('Failed to delete memory', 'error');
        }
    }

    async editMemory(memoryId) {
        // (unchanged UI build from your original; left as-is)
        // ... for brevity, this block remains identical to your version ...
        // NOTE: kept your full edit modal implementation unchanged
        // ---- BEGIN original edit UI (unchanged) ----
        const memoriesList = document.getElementById('memories-list') || document.getElementById('memories-list-main');
        if (!memoriesList) return;

        const memoryElements = memoriesList.querySelectorAll('.flex.items-center.justify-between');
        let memoryData = null;

        for (const element of memoryElements) {
            const editButton = element.querySelector('button[data-memory-id]');
            if (editButton && editButton.dataset.memoryId === memoryId) {
                const memoryText = element.querySelector('p.text-sm.text-zinc-200').textContent;
                const categorySpan = element.querySelector('span.text-xs.text-zinc-400');
                const importanceSpan = categorySpan.nextElementSibling.nextElementSibling;

                memoryData = {
                    memory: memoryText,
                    category: categorySpan.textContent,
                    importance: importanceSpan.textContent
                };
                break;
            }
        }

        if (!memoryData) {
            this.showNotification('Could not find memory data', 'error');
            return;
        }

        const editForm = document.createElement('div');
        editForm.className = 'fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50';
        editForm.innerHTML = `
            <div class="bg-zinc-900/50 backdrop-blur-xs border border-zinc-700 rounded-lg w-96 max-w-full mx-4 max-h-[90vh] flex flex-col shadow-2xl">
                <div class="flex justify-between items-center p-6 border-b border-zinc-700 flex-shrink-0">
                    <h3 class="text-lg text-white flex items-center">
                        <span class="material-icons mr-2 text-emerald-400">edit</span>
                        Edit Memory
                    </h3>
                    <button id="close-edit-memory" class="text-zinc-400 hover:text-white">
                        <span class="material-icons">close</span>
                    </button>
                </div>

                <div class="flex-1 overflow-y-auto">
                    <form id="edit-memory-form" class="px-6 py-4">
                        <div class="mb-4">
                            <label class="block text-sm text-zinc-300 mb-2">Memory</label>
                            <textarea id="edit-memory-text" class="w-full bg-zinc-700 text-white rounded px-3 py-2 border border-zinc-600 focus:outline-none focus:ring-2 focus:ring-cyan-500" rows="3" required>${memoryData.memory}</textarea>
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm text-zinc-300 mb-2">Category</label>
                            <select id="edit-memory-category" class="w-full bg-zinc-700 text-white rounded px-3 py-2 border border-zinc-600 focus:outline-none focus:ring-2 focus:ring-cyan-500">
                                <option value="preference" ${memoryData.category === 'preference' ? 'selected' : ''}>Preference</option>
                                <option value="fact" ${memoryData.category === 'fact' ? 'selected' : ''}>Fact</option>
                                <option value="event" ${memoryData.category === 'event' ? 'selected' : ''}>Event</option>
                                <option value="relationship" ${memoryData.category === 'relationship' ? 'selected' : ''}>Relationship</option>
                                <option value="interest" ${memoryData.category === 'interest' ? 'selected' : ''}>Interest</option>
                            </select>
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm text-zinc-300 mb-2">Importance</label>
                            <select id="edit-memory-importance" class="w-full bg-zinc-700 text-white rounded px-3 py-2 border border-zinc-600 focus:outline-none focus:ring-2 focus:ring-cyan-500">
                                <option value="high" ${memoryData.importance === 'high' ? 'selected' : ''}>High</option>
                                <option value="medium" ${memoryData.importance === 'medium' ? 'selected' : ''}>Medium</option>
                                <option value="low" ${memoryData.importance === 'low' ? 'selected' : ''}>Low</option>
                            </select>
                        </div>
                    </form>
                </div>

                <div class="border-t border-zinc-700 p-4 flex-shrink-0">
                    <div class="flex justify-end gap-3">
                        <button id="cancel-edit-memory" class="px-4 py-2 bg-zinc-600 hover:bg-zinc-500 text-white rounded transition-colors">
                            Cancel
                        </button>
                        <button id="save-edit-memory" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500  text-white rounded transition-colors">
                            Save Changes
                        </button>
                    </div>
                </div>
            </div>
        `;

        document.body.appendChild(editForm);

        const form = editForm.querySelector('#edit-memory-form');
        const closeButton = editForm.querySelector('#close-edit-memory');
        const cancelButton = editForm.querySelector('#cancel-edit-memory');
        const saveButton = editForm.querySelector('#save-edit-memory');

        const handleSave = async () => {
            const newMemory = document.getElementById('edit-memory-text').value.trim();
            const newCategory = document.getElementById('edit-memory-category').value;
            const newImportance = document.getElementById('edit-memory-importance').value;

            if (!newMemory) {
                this.showNotification('Memory text cannot be empty', 'error');
                return;
            }

            try {
                const response = await fetch('/profiles/update-memory', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        user: this.getApiUsername(),
                        memoryId: memoryId,
                        memory: newMemory,
                        category: newCategory,
                        importance: newImportance
                    })
                });

                if (response.ok) {
                    this.showNotification('Memory updated successfully', 'success');
                    await this.loadMemories();
                    document.body.removeChild(editForm);
                } else {
                    const error = await response.json();
                    this.showNotification(error.error || 'Failed to update memory', 'error');
                }
            } catch (error) {
                console.error('Failed to update memory:', error);
                this.showNotification('Failed to update memory', 'error');
            }
        };

        const closeModal = () => {
            document.body.removeChild(editForm);
        };

        saveButton.addEventListener('click', handleSave);
        closeButton.addEventListener('click', closeModal);
        cancelButton.addEventListener('click', closeModal);

        editForm.addEventListener('click', (e) => {
            if (e.target === editForm) {
                document.body.removeChild(editForm);
            }
        });
        // ---- END original edit UI (unchanged) ----
    }

    async editProfile(profileName) {
        // Prevent editing Guest profile
        if (profileName.toLowerCase() === 'guest') {
            this.showNotification('Cannot edit Guest profile name', 'warning');
            return;
        }

        const newName = prompt(`Enter new name for profile "${profileName}":`, profileName);
        if (!newName || newName.trim() === '' || newName === profileName) return;

        try {
            const response = await fetch('/profiles/rename', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ oldName: profileName, newName: newName.trim() })
            });

            if (response.ok) {
                const oldDisplayName = this.getDisplayName(profileName);
                this.showNotification(`Profile renamed from "${oldDisplayName}" to "${newName}"`, 'success');
                await this.loadAllData();
            } else {
                const error = await response.json();
                this.showNotification(error.error || 'Failed to rename profile', 'error');
            }
        } catch (error) {
            console.error('Failed to rename profile:', error);
            this.showNotification('Failed to rename profile', 'error');
        }
    }

    async setAsDefault(userName) {
        try {
            const response = await fetch('/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ DEFAULT_USER: userName })
            });

            if (response.ok) {
                this.defaultUser = userName;
                this.updateProfileList();
                this.showNotification(`Set ${userName} as default profile (will auto-load on startup)`, 'success');
            } else {
                this.showNotification('Failed to update default user', 'error');
            }
        } catch (error) {
            console.error('Failed to update default user:', error);
            this.showNotification('Failed to update default user', 'error');
        }
    }

    async updatePersona() {
        this.debugLog('VERBOSE', 'updatePersona called, currentUser:', this.currentUser);
        
        const personaSelect = document.getElementById('persona-select') || document.getElementById('persona-select-main');
        const selectedPersona = personaSelect ? personaSelect.value : null;

        if (!selectedPersona) {
            this.showNotification('No persona selected', 'error');
            return;
        }

        // Disable both buttons at the start to prevent double-clicks
        const updateBtn = document.getElementById('update-persona-btn');
        const updateBtnMain = document.getElementById('update-persona-btn-main');
        if (updateBtn) updateBtn.disabled = true;
        if (updateBtnMain) updateBtnMain.disabled = true;

        try {
            // Guest mode: just switch persona without saving to profile
            if (!this.currentUser || this.currentUser === 'guest') {
                // Check if service is running before making changes
                const statusData = await ServiceStatus.fetchStatus();
                const wasActive = statusData.status === 'active';

                const response = await fetch('/persona/switch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        persona_name: selectedPersona
                    })
                });

                if (!response.ok) {
                    const errorData = await response.json();
                    this.debugLog('ERROR', 'Persona switch API error:', errorData);
                    this.showNotification('Failed to switch persona: ' + (errorData.error || 'Unknown error'), 'error');
                    // Re-enable buttons on error
                    if (updateBtn) updateBtn.disabled = false;
                    if (updateBtnMain) updateBtnMain.disabled = false;
                    return;
                }

                this.showNotification(`Switched to ${selectedPersona} persona`, 'success');

                // Update persona UI
                await this.updatePersonaSelectorForCurrentUser();

                if (window.PersonaForm?.loadPersona) {
                    await window.PersonaForm.loadPersona(selectedPersona);
                }
                if (window.PersonaForm?.updatePersonaListSelection) {
                    window.PersonaForm.updatePersonaListSelection(selectedPersona);
                }

                // Restart Billy service if it was running to pick up the new persona
                if (wasActive) {
                    try {
                        this.showNotification('Restarting Billy to load new persona...', 'warning');
                        // Wait a moment to ensure profile is written to disk
                        await new Promise(resolve => setTimeout(resolve, 500));
                        await fetch('/restart-billy', { method: 'POST' });
                        // Wait a moment for the service to restart
                        await new Promise(resolve => setTimeout(resolve, 2000));
                        this.showNotification('Billy restarted with new persona', 'success');
                        ServiceStatus.fetchStatus();
                    } catch (err) {
                        console.error('Failed to restart Billy service:', err);
                        this.showNotification('Persona switched, but service restart failed. Please restart manually.', 'warning', 5000);
                    }
                }

                // Re-enable buttons after successful save
                if (updateBtn) updateBtn.disabled = false;
                if (updateBtnMain) updateBtnMain.disabled = false;
                return;
            }

            // Logged-in user: update profile with persona and display name
            const displayNameInput = document.getElementById('display-name-input') || document.getElementById('display-name-input-main');
            const displayName = displayNameInput ? displayNameInput.value.trim() : '';

            // Check if service is running before making changes
            const statusData = await ServiceStatus.fetchStatus();
            const wasActive = statusData.status === 'active';

            const response = await fetch('/current-user', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    action: 'update_profile',
                    preferred_persona: selectedPersona,
                    display_name: displayName
                })
            });

            if (!response.ok) {
                const errorText = await response.text();
                this.debugLog('ERROR', 'Profile API error:', errorText);
                this.showNotification('Failed to update profile: ' + errorText, 'error');
                // Re-enable buttons on error
                if (updateBtn) updateBtn.disabled = false;
                if (updateBtnMain) updateBtnMain.disabled = false;
                return;
            }

            // Clear pending display name and UI affordance
            this.pendingDisplayName = null;
            this.showNotification(`Saved ${this.currentUser}'s profile`, 'success');

            // Update persona UI without reloading everything
            await this.updatePersonaSelectorForCurrentUser();

            if (window.PersonaForm?.loadPersona) {
                await window.PersonaForm.loadPersona(selectedPersona);
            }
            if (window.PersonaForm?.updatePersonaListSelection) {
                window.PersonaForm.updatePersonaListSelection(selectedPersona);
            }

            // Restart Billy service if it was running to pick up the new persona
            if (wasActive) {
                try {
                    this.showNotification('Restarting Billy to load new persona...', 'warning');
                    // Wait a moment to ensure profile is written to disk
                    await new Promise(resolve => setTimeout(resolve, 500));
                    await fetch('/restart-billy', { method: 'POST' });
                    // Wait a moment for the service to restart
                    await new Promise(resolve => setTimeout(resolve, 2000));
                    this.showNotification('Billy restarted with new persona', 'success');
                    ServiceStatus.fetchStatus();
                } catch (err) {
                    console.error('Failed to restart Billy service:', err);
                    this.showNotification('Profile saved, but service restart failed. Please restart manually.', 'warning', 5000);
                }
            }

            // Re-enable buttons after successful save
            if (updateBtn) updateBtn.disabled = false;
            if (updateBtnMain) updateBtnMain.disabled = false;

        } catch (error) {
            console.error('Failed to update profile:', error);
            this.showNotification('Failed to update profile', 'error');
            // Re-enable buttons on error
            if (updateBtn) updateBtn.disabled = false;
            if (updateBtnMain) updateBtnMain.disabled = false;
        }
    }

    showNotification(message, type = 'info') {
        if (window.showNotification) {
            window.showNotification(message, type);
        } else {
            this.debugLog('INFO', `[${type.toUpperCase()}] ${message}`);
        }
    }

    enableSaveButton() {
        const updateBtn = document.getElementById('update-persona-btn');
        const updateBtnMain = document.getElementById('update-persona-btn-main');
        if (updateBtn) updateBtn.disabled = false;
        if (updateBtnMain) {
            updateBtnMain.disabled = false;
            updateBtnMain.classList.remove('hidden');
        }
    }

    disableSaveButton() {
        const updateBtn = document.getElementById('update-persona-btn');
        const updateBtnMain = document.getElementById('update-persona-btn-main');
        if (updateBtn) updateBtn.disabled = true;
        if (updateBtnMain) {
            updateBtnMain.disabled = true;
            updateBtnMain.classList.add('hidden');
        }
    }

    updateSaveButtonVisibility() {
        const mainBtn = document.querySelector('#update-persona-btn-main');
        const buttonGroup = mainBtn ? mainBtn.closest('.flex.rounded.shadow.overflow-hidden') : null;
        if (!buttonGroup) return;
        // Keep save button visible for guest mode too (for persona switching)
        buttonGroup.style.display = 'flex';
        
        // However, hide the dropdown menu options for guests (upload/download profile)
        const dropdownBtn = document.querySelector('#dropdown-btn-main');
        if (dropdownBtn) {
            const isGuest = this.currentUser === 'guest' || this.currentUser === null;
            dropdownBtn.style.display = isGuest ? 'none' : 'flex';
        }
    }

    onPersonaSelectChange() {
        // Save button is always enabled when persona changes
        this.enableSaveButton();
    }

    showCurrentUserDeleteMessage() {
        this.showNotification('Cannot delete the currently active user profile. Switch to a different user first, then delete this profile.', 'error', 8000);
    }

    showGuestDeleteMessage() {
        this.showNotification('The Guest profile cannot be deleted.', 'error');
    }

    debugLog(level, message, ...args) {
        const levels = { 'ERROR': 0, 'WARNING': 1, 'INFO': 2, 'VERBOSE': 3 };
        const currentLevel = levels[this.debugLevel] || 2;
        const messageLevel = levels[level] || 2;

        if (messageLevel <= currentLevel) {
            switch (level) {
                case 'ERROR':
                    console.error(`[${level}] ${message}`, ...args);
                    break;
                case 'WARNING':
                    console.warn(`[${level}] ${message}`, ...args);
                    break;
                case 'INFO':
                    console.info(`[${level}] ${message}`, ...args);
                    break;
                default:
                    console.log(`[${level}] ${message}`, ...args);
            }
        }
    }

    updateDebugLevel() {
        const logLevelSelect = document.getElementById('log-level-select');
        if (logLevelSelect) {
            this.debugLevel = logLevelSelect.value;
        }
    }

    startStatusMonitoring() {
        this.statusInterval = setInterval(async () => {
            await this.checkStatus();
        }, 3000);
    }

    stopStatusMonitoring() {
        if (this.statusInterval) {
            clearInterval(this.statusInterval);
            this.statusInterval = null;
        }
    }

    async checkStatus(status = null) {
        try {
            if (!status) status = await ServiceStatus.fetchStatus();
            if (!status) return;

            if (status.mood) {
                window.MoodPanel?.render(status.mood);
            }

            if (this.lastStatus) {
                let hasChanges = false;

                if (this.lastStatus.current_user !== status.current_user) {
                    hasChanges = true;
                }

                if (this.lastStatus.env_file && status.env_file &&
                    this.lastStatus.env_file.modified !== status.env_file.modified) {
                    hasChanges = true;
                }

                if (
                    JSON.stringify(this.lastStatus.available_profiles) !== JSON.stringify(status.available_profiles) ||
                    JSON.stringify(this.lastStatus.available_personas) !== JSON.stringify(status.available_personas)
                ) {
                    hasChanges = true;
                }

                if (status.memory_count !== undefined && status.memory_count !== this.lastMemoryCount) {
                    if (status.memory_count > this.lastMemoryCount) {
                        hasChanges = true;
                    }
                    this.lastMemoryCount = status.memory_count;
                }

                if (status.config_hash && status.config_hash !== this.lastConfigHash) {
                    this.lastConfigHash = status.config_hash;
                    try {
                        const configResponse = await fetch('/config');
                        if (configResponse.ok) {
                            const configData = await configResponse.json();
                            if (window.SettingsForm?.refreshFromConfig) {
                                window.SettingsForm.refreshFromConfig(configData);
                            }
                        }
                    } catch (error) {
                        console.error('Failed to fetch config after change:', error);
                    }
                }

                if (hasChanges && !this.isUserInitiatedSwitch) {
                    if (this.lastStatus.current_user !== status.current_user) {
                        await new Promise(r => setTimeout(r, 200));
                        await this.loadAllData(true);
                    } else {
                        await this.loadAllData();
                    }

                    if (window.SettingsForm?.refreshFromConfig) {
                        const configData = await ConfigService.fetchConfig();
                        if (configData) window.SettingsForm.refreshFromConfig(configData);
                    }

                    if (window.PersonaForm?.updatePersonaListSelection) {
                        await this.updatePersonaSelectorForCurrentUser();
                    }
                }
            }

            this.lastStatus = status;
        } catch (error) {
            console.error('Failed to check status:', error);
        }
    }

    // Update persona selector to show user's preferred persona
    async updatePersonaSelectorForCurrentUser() {
        try {
            // Wait for PersonaForm to be available (retry)
            let retries = 0;
            const maxRetries = 10;

            while (!window.PersonaForm || !window.PersonaForm.loadPersona) {
                if (retries >= maxRetries) {
                    console.warn('PersonaForm not available after retries, skipping persona loading');
                    break;
                }
                await new Promise(resolve => setTimeout(resolve, 100));
                retries++;
            }

            // Update both persona selectors
            const personaSelects = [
                document.getElementById('persona-select'),
                document.getElementById('persona-select-main')
            ].filter(Boolean); // Remove null elements
            
            if (personaSelects.length > 0) {
                let targetPersona = 'default';
                
                if (this.currentUser && this.currentUser !== 'guest') {
                    // Logged-in user: get preferred persona from user data
                    const currentUserData = await this.getCurrentUserData();
                    if (currentUserData?.data?.USER_INFO) {
                        targetPersona = currentUserData.data.USER_INFO.preferred_persona || 'default';
                    }
                } else {
                    // Guest mode: get preferred persona from guest profile in AVAILABLE_PROFILES
                    try {
                        const configData = await ConfigService.fetchConfig();
                        this.debugLog('VERBOSE', 'Config data for guest mode:', configData);
                        if (configData) {
                            // Look for guest profile in AVAILABLE_PROFILES
                            const guestProfile = configData.AVAILABLE_PROFILES?.find(
                                profile => profile.name?.toLowerCase() === 'guest'
                            );
                            
                            if (guestProfile?.data?.USER_INFO?.preferred_persona) {
                                targetPersona = guestProfile.data.USER_INFO.preferred_persona;
                                this.debugLog('VERBOSE', `Using guest profile's preferred_persona: ${targetPersona}`);
                            } else {
                                // Fall back to CURRENT_PERSONA (what's currently loaded)
                                targetPersona = configData.CURRENT_PERSONA || 'default';
                                this.debugLog('VERBOSE', `Using CURRENT_PERSONA fallback: ${targetPersona}`);
                            }
                        }
                    } catch (error) {
                        console.warn('Failed to load guest persona from config:', error);
                        targetPersona = 'default';
                    }
                }
                
                // Update all persona selectors
                this.debugLog('VERBOSE', `Setting persona selectors to: ${targetPersona}`);
                personaSelects.forEach((personaSelect, index) => {
                    this.debugLog('VERBOSE', `Persona selector ${index}: current=${personaSelect.value}, target=${targetPersona}`);
                    if (personaSelect.value !== targetPersona) {
                        personaSelect.value = targetPersona;
                        this.debugLog('VERBOSE', `Updated persona selector ${index} to: ${targetPersona}`);
                    }
                });
                
                // Update persona list selection highlight
                if (window.PersonaForm?.updatePersonaListSelection) {
                    window.PersonaForm.updatePersonaListSelection(targetPersona);
                    this.debugLog('VERBOSE', `Updated persona list selection to: ${targetPersona}`);
                }
            }
        } catch (error) {
            console.error('Failed to update persona selector for current user:', error);
        }
    }

    async getCurrentUserData() {
        try {
            const data = await ConfigService.fetchConfig();
            if (data && data.CURRENT_USER && typeof data.CURRENT_USER === 'object') {
                return data.CURRENT_USER;
            }
            return null;
        } catch (error) {
            console.error('Failed to get current user data:', error);
            return null;
        }
    }

    showProfileLoading(profileName) {
        const profileTile = document.querySelector(`[data-profile="${profileName}"]`);
        if (profileTile) {
            profileTile.classList.add('opacity-50', 'pointer-events-none');

            const loadingSpinner = document.createElement('div');
            loadingSpinner.className = 'absolute inset-0 flex items-center justify-center bg-zinc-800/80 rounded-lg';
            loadingSpinner.innerHTML = `
                <div class="flex flex-col items-center">
                    <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-emerald-400"></div>
                    <span class="text-xs text-emerald-400 mt-1">Loading...</span>
                </div>
            `;
            loadingSpinner.id = `loading-${profileName}`;
            profileTile.appendChild(loadingSpinner);
        }
    }

    hideProfileLoading(profileName) {
        const profileTile = document.querySelector(`[data-profile="${profileName}"]`);
        if (profileTile) {
            profileTile.classList.remove('opacity-50', 'pointer-events-none');
            const loadingSpinner = document.getElementById(`loading-${profileName}`);
            if (loadingSpinner) loadingSpinner.remove();
        }
    }

    // Load the user's preferred persona in the persona form
    async loadUserPreferredPersona(userName) {
        try {
            if (!window.PersonaForm?.loadPersona) {
                console.warn('PersonaForm not available, skipping persona loading');
                return;
            }

            let personaToLoad = 'default';
            if (userName && userName !== 'guest') {
                const data = await ConfigService.fetchConfig();
                personaToLoad = data?.CURRENT_USER?.data?.USER_INFO?.preferred_persona || 'default';
            }

            await window.PersonaForm.loadPersona(personaToLoad);
            if (window.PersonaForm.updatePersonaListSelection) {
                window.PersonaForm.updatePersonaListSelection(personaToLoad);
            }
            if (window.PersonaForm.handlePersonaChangeNotification) {
                window.PersonaForm.handlePersonaChangeNotification(personaToLoad);
            }
        } catch (error) {
            console.error('Failed to load user preferred persona:', error);
        }
    }

    // Global refresh method that can be called from other modules
    async refreshUserProfile() {
        await this.loadAllData();
    }

    // (optional) handle /save DEFAULT_USER <select> in header if present
    async updateDefaultUser(user) {
        await this.setAsDefault(user);
    }

    showNewProfileInfo() {
        this.showNotification('Start a conversation with Billy and introduce yourself and a new profile will be created', 'info');
    }

    async autoSetGuestAsDefaultIfOnlyProfile() {
        try {
            // Check if Guest is the only profile
            const hasGuestProfile = this.profiles.some(profile => {
                const profileName = typeof profile === 'string' ? profile : profile.name;
                return profileName.toLowerCase() === 'guest';
            });

            const nonGuestProfiles = this.profiles.filter(profile => {
                const profileName = typeof profile === 'string' ? profile : profile.name;
                return profileName.toLowerCase() !== 'guest';
            });

            // If Guest exists and there are no other profiles, set it as default
            if (hasGuestProfile && nonGuestProfiles.length === 0) {
                const currentDefault = this.defaultUser || 'guest';
                if (currentDefault.toLowerCase() !== 'guest') {
                    this.debugLog('INFO', 'Auto-setting Guest as default profile (only profile available)');
                    await this.setAsDefault('guest'); // Use lowercase to match folder name
                }
            }
        } catch (error) {
            this.debugLog('WARNING', 'Failed to auto-set Guest as default:', error);
        }
    }
}

// Download user profile function
window.downloadUserProfile = async function () {
    try {
        const currentUser = window.UserProfilePanel && window.UserProfilePanel.currentUser;
        if (!currentUser || currentUser === 'guest') {
            showNotification('No user profile to download', 'warning');
            return;
        }

        const response = await fetch(`/profiles/export/${currentUser}`);
        if (response.ok) {
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${currentUser}.ini`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
            showNotification(`Downloaded ${currentUser} profile`, 'success');
        } else {
            showNotification('Failed to download profile', 'error');
        }
    } catch (error) {
        console.error('Download failed:', error);
        showNotification('Download failed: ' + error.message, 'error');
    }
};

// Upload user profile function
window.uploadUserProfile = async function (input) {
    const file = input.files[0];
    if (!file) return;

    try {
        const currentUser = window.UserProfilePanel && window.UserProfilePanel.currentUser;
        if (!currentUser || currentUser === 'guest') {
            showNotification('No user profile to upload to', 'warning');
            return;
        }

        const formData = new FormData();
        formData.append('file', file);

        const response = await fetch(`/profiles/import/${currentUser}`, {
            method: 'POST',
            body: formData
        });

        if (response.ok) {
            showNotification(`Uploaded profile to ${currentUser}`, 'success');
            if (window.UserProfilePanel?.loadAllData) {
                await window.UserProfilePanel.loadAllData();
            }
        } else {
            const error = await response.json();
            showNotification('Upload failed: ' + error.error, 'error');
        }
    } catch (error) {
        console.error('Upload failed:', error);
        showNotification('Upload failed: ' + error.message, 'error');
    }

    input.value = '';
};

// Make it globally available
window.UserProfilePanel = new UserProfilePanel();

// Global helpers
window.refreshUserProfile = () => window.UserProfilePanel?.refreshUserProfile();
window.syncPersonaWithCurrentUser = () => window.UserProfilePanel?.updatePersonaSelectorForCurrentUser();

// Functions used by inline onclick attributes
window.setAsDefault = (userName) => window.UserProfilePanel.setAsDefault(userName);
window.setAsCurrentUser = (profileName) => window.UserProfilePanel.setAsCurrentUser(profileName);
window.setAsGuest = () => window.UserProfilePanel.setAsGuest();
window.deleteProfile = (profileName) => window.UserProfilePanel.deleteProfile(profileName);
window.showCurrentUserDeleteMessage = () => window.UserProfilePanel.showCurrentUserDeleteMessage();
window.deleteMemory = (memoryId) => window.UserProfilePanel.deleteMemory(memoryId);
window.editMemory = (memoryId) => window.UserProfilePanel.editMemory(memoryId);
window.editProfile = (profileName) => window.UserProfilePanel.editProfile(profileName);

// Settings toggle functionality + cross-sync between modal & main controls
document.addEventListener('DOMContentLoaded', () => {
    const settingsToggle = document.getElementById('settings-toggle');
    const settingsContent = document.getElementById('settings-content');
    const settingsChevron = document.getElementById('settings-chevron');

    if (settingsToggle && settingsContent && settingsChevron) {
        settingsToggle.addEventListener('click', () => {
            const isHidden = settingsContent.classList.contains('hidden');
            if (isHidden) {
                settingsContent.classList.remove('hidden');
                settingsChevron.style.transform = 'rotate(180deg)';
            } else {
                settingsContent.classList.add('hidden');
                settingsChevron.style.transform = 'rotate(0deg)';
            }
        });
    }

    // Sync modal <> main inputs
    const displayNameMain = document.getElementById('display-name-input-main');
    const displayNameModal = document.getElementById('display-name-input');

    if (displayNameMain && displayNameModal) {
        displayNameMain.addEventListener('input', (e) => {
            displayNameModal.value = e.target.value;
            window.UserProfilePanel.onDisplayNameChange();
        });
        displayNameModal.addEventListener('input', (e) => {
            displayNameMain.value = e.target.value;
            window.UserProfilePanel.onDisplayNameChange();
        });
    }

    const personaSelectMain = document.getElementById('persona-select-main');
    const personaSelectModal = document.getElementById('persona-select');

    if (personaSelectMain && personaSelectModal) {
        const sync = (from, to) => (e) => {
            to.value = e.target.value;
            window.UserProfilePanel.onPersonaSelectChange();
        };
        personaSelectMain.addEventListener('change', sync(personaSelectMain, personaSelectModal));
        personaSelectModal.addEventListener('change', sync(personaSelectModal, personaSelectMain));
    }
});
