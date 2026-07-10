/**
 * Profile Management Panel
 * Handles user profile operations and persona switching
 */

const ProfilePanel = (() => {
    let currentUser = null;
    let availablePersonas = [];

    // Helper function to get display name for a profile
    const getDisplayName = (profileName, profileData = null) => {
        if (profileName === 'guest') {
            return 'Guest';
        }
        
        // If we have profile data, check for display_name
        if (profileData && profileData.data && profileData.data.USER_INFO && profileData.data.USER_INFO.display_name) {
            return profileData.data.USER_INFO.display_name;
        }
        
        // Fallback to profile name
        return profileName;
    };

    const elements = {
        // Main panel elements
        currentUserInfo: document.getElementById("current-user-info"),
        newUserName: document.getElementById("new-user-name"),
        switchUserBtn: document.getElementById("switch-user-btn"),
        profilesList: document.getElementById("profiles-list"),
        personaSelect: document.getElementById("persona-select"),
        userMemoriesSection: document.getElementById("user-memories-section"),
        userMemories: document.getElementById("user-memories"),
        refreshProfiles: document.getElementById("refresh-profiles"),
        
        // Header elements
        userProfileBtn: document.getElementById("user-profile-btn"),
        userProfileDropdown: document.getElementById("user-profile-dropdown"),
        headerCurrentUserName: document.getElementById("current-user-name"),
        headerProfilesList: document.getElementById("header-profiles-list"),
        headerPersonaSelect: document.getElementById("header-persona-select")
    };

    const bindUI = () => {
        // Main panel event listeners
        if (elements.switchUserBtn) {
            elements.switchUserBtn.addEventListener("click", switchUser);
        }
        if (elements.newUserName) {
            elements.newUserName.addEventListener("keypress", (e) => {
                if (e.key === "Enter") switchUser();
            });
        }
        if (elements.personaSelect) {
            elements.personaSelect.addEventListener("change", changePersona);
        }
        if (elements.refreshProfiles) {
            elements.refreshProfiles.addEventListener("click", loadProfiles);
        }

        // Header event listeners
        if (elements.userProfileBtn) {
            elements.userProfileBtn.addEventListener("click", toggleHeaderDropdown);
        }
        if (elements.headerPersonaSelect) {
            elements.headerPersonaSelect.addEventListener("change", changePersonaFromHeader);
        }

        // Close dropdown when clicking outside
        document.addEventListener("click", (e) => {
            if (elements.userProfileDropdown && !elements.userProfileBtn.contains(e.target) && !elements.userProfileDropdown.contains(e.target)) {
                elements.userProfileDropdown.classList.add("hidden");
            }
        });

        // Load initial data
        loadCurrentUser();
        loadProfiles();
        loadPersonas();
    };

    const loadCurrentUser = async () => {
        try {
            const response = await fetch("/current-user");
            const data = await response.json();
            
            if (data.user) {
                currentUser = data.user;
                updateCurrentUserDisplay();
                loadUserMemories();
            } else {
                currentUser = null;
                updateCurrentUserDisplay();
            }
        } catch (error) {
            console.error("Failed to load current user:", error);
            // Don't show error notification - guest mode is expected behavior
            currentUser = null;
            updateCurrentUserDisplay();
        }
    };

    const updateCurrentUserDisplay = () => {
        if (!currentUser) {
            // Update main panel
            if (elements.currentUserInfo) {
                elements.currentUserInfo.innerHTML = '<div class="text-zinc-400 text-sm">Guest mode</div>';
            }
            // Update header
            if (elements.headerCurrentUserName) {
                elements.headerCurrentUserName.textContent = "Guest";
            }
            return;
        }

        const interactionCount = currentUser.data.USER_INFO && currentUser.data.USER_INFO.interaction_count || "0";
        const preferredPersona = currentUser.data.USER_INFO && currentUser.data.USER_INFO.preferred_persona || "default";
        const bondLevel = currentUser.data.RELATIONSHIP && currentUser.data.RELATIONSHIP.bond_level || "new";

        // Update main panel
        if (elements.currentUserInfo) {
            elements.currentUserInfo.innerHTML = `
                <div class="text-white">${currentUser.name}</div>
                <div class="text-zinc-400 text-sm mt-1">
                    Interactions: ${interactionCount} | 
                    Persona: ${preferredPersona} | 
                    Bond: ${bondLevel}
                </div>
            `;
        }

        // Update header
        if (elements.headerCurrentUserName) {
            elements.headerCurrentUserName.textContent = currentUser.name;
        }

        // Update persona selects
        if (elements.personaSelect) {
            elements.personaSelect.value = preferredPersona;
        }
        if (elements.headerPersonaSelect) {
            elements.headerPersonaSelect.value = preferredPersona;
        }
    };

    const loadUserMemories = () => {
        if (!currentUser || !currentUser.memories) {
            elements.userMemoriesSection.classList.add("hidden");
            return;
        }

        if (currentUser.memories.length === 0) {
            elements.userMemories.innerHTML = '<div class="text-zinc-400 text-sm">No memories yet</div>';
        } else {
            const memoriesHtml = currentUser.memories.map(memory => `
                <div class="text-sm mb-2 p-2 bg-zinc-600 rounded">
                    <div class="text-white">${memory.memory}</div>
                    <div class="text-zinc-400 text-xs mt-1">
                        ${memory.category} • ${memory.importance} • ${new Date(memory.timestamp * 1000).toLocaleDateString()}
                    </div>
                </div>
            `).join("");
            elements.userMemories.innerHTML = memoriesHtml;
        }

        elements.userMemoriesSection.classList.remove("hidden");
    };

    const switchUser = async () => {
        const userName = elements.newUserName.value.trim();
        if (!userName) {
            showNotification("Please enter a user name", "error");
            return;
        }

        try {
            const response = await fetch("/current-user", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({ name: userName })
            });

            const data = await response.json();
            
            if (response.ok) {
                showNotification(`Switched to user: ${userName}`, "success");
                elements.newUserName.value = "";
                loadCurrentUser();
                loadProfiles();
            } else {
                showNotification(data.error || "Failed to switch user", "error");
            }
        } catch (error) {
            console.error("Failed to switch user:", error);
            showNotification("Failed to switch user", "error");
        }
    };

    const loadProfiles = async () => {
        try {
            const response = await fetch("/profiles");
            const data = await response.json();
            
            if (data.profiles && data.profiles.length > 0) {
                // Update main panel
                if (elements.profilesList) {
                    const mainProfilesHtml = data.profiles.map(profile => `
                        <div class="flex justify-between items-center p-2 bg-zinc-700 rounded">
                            <div>
                                <div class="text-white">${profile.display_name || profile.name}</div>
                                <div class="text-zinc-400 text-xs">
                                    ${new Date(profile.modified * 1000).toLocaleDateString()}
                                </div>
                            </div>
                            <div class="flex gap-2">
                                <button onclick="switchToProfile('${profile.name}')" 
                                        class="px-3 py-1 bg-emerald-600 text-white text-sm rounded hover:bg-emerald-500 transition-colors">
                                    Switch
                                </button>
                                <button onclick="deleteProfile('${profile.name}')" 
                                        class="px-3 py-1 bg-red-600 text-white text-sm rounded hover:bg-red-500 transition-colors"
                                        title="Delete profile">
                                    <span class="material-icons text-sm">delete</span>
                                </button>
                            </div>
                        </div>
                    `).join("");
                    elements.profilesList.innerHTML = mainProfilesHtml;
                }
                
                // Update header profiles list
                if (elements.headerProfilesList) {
                    const headerProfilesHtml = data.profiles.map(profile => {
                        const isCurrentUser = currentUser && currentUser.name === profile.name;
                        return `
                            <div class="flex items-center justify-between p-1 hover:bg-zinc-700 rounded transition-colors">
                                <div class="flex-1 flex items-center gap-2">
                                    <button onclick="switchToProfile('${profile.name}')" 
                                            class="flex-1 text-left px-2 py-1 text-xs text-white hover:text-emerald-400 transition-colors">
                                        ${profile.display_name || profile.name}
                                    </button>
                                    ${isCurrentUser ? '<span class="text-xs text-emerald-400">●</span>' : ''}
                                </div>
                                <div class="flex gap-1">
                                    <button onclick="setAsCurrentUser('${profile.name}')" 
                                            class="px-2 py-1 text-xs text-blue-400 hover:text-blue-300 hover:bg-blue-900/20 rounded transition-colors"
                                            title="Set as current user"
                                            ${isCurrentUser ? 'disabled class="opacity-50 cursor-not-allowed"' : ''}>
                                        <span class="material-icons text-xs">person_pin</span>
                                    </button>
                                    <button onclick="deleteProfile('${profile.name}')" 
                                            class="px-2 py-1 text-xs text-red-400 hover:text-red-300 hover:bg-red-900/20 rounded transition-colors"
                                            title="Delete profile">
                                        <span class="material-icons text-xs">delete</span>
                                    </button>
                                </div>
                            </div>
                        `;
                    }).join("");
                    elements.headerProfilesList.innerHTML = headerProfilesHtml;
                }
            } else {
                const noProfilesHtml = '<div class="text-zinc-400 text-sm">No profiles found</div>';
                if (elements.profilesList) {
                    elements.profilesList.innerHTML = noProfilesHtml;
                }
                if (elements.headerProfilesList) {
                    elements.headerProfilesList.innerHTML = '<div class="text-zinc-400 text-xs">No profiles found</div>';
                }
            }
        } catch (error) {
            console.error("Failed to load profiles:", error);
            elements.profilesList.innerHTML = '<div class="text-red-400 text-sm">Failed to load profiles</div>';
        }
    };

    const switchToProfile = async (profileName) => {
        try {
            const response = await fetch("/current-user", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({ name: profileName })
            });

            const data = await response.json();
            
            if (response.ok) {
                // Get display name for notification
                const displayName = getDisplayName(profileName);
                showNotification(`Switched to profile: ${displayName}`, "success");
                loadCurrentUser();
            } else {
                showNotification(data.error || "Failed to switch profile", "error");
            }
        } catch (error) {
            console.error("Failed to switch profile:", error);
            showNotification("Failed to switch profile", "error");
        }
    };

    const loadPersonas = async () => {
        try {
            const response = await fetch("/personas");
            const data = await response.json();
            
            if (data.personas && data.personas.length > 0) {
                availablePersonas = data.personas;
                const personasHtml = data.personas.map(persona => `
                    <option value="${persona.name}">${persona.description}</option>
                `).join("");
                
                // Update main panel
                if (elements.personaSelect) {
                    elements.personaSelect.innerHTML = personasHtml;
                }
                
                // Update header persona select
                if (elements.headerPersonaSelect) {
                    elements.headerPersonaSelect.innerHTML = personasHtml;
                }
            } else {
                const noPersonasHtml = '<option value="">No personas available</option>';
                if (elements.personaSelect) {
                    elements.personaSelect.innerHTML = noPersonasHtml;
                }
                if (elements.headerPersonaSelect) {
                    elements.headerPersonaSelect.innerHTML = noPersonasHtml;
                }
            }
        } catch (error) {
            console.error("Failed to load personas:", error);
            elements.personaSelect.innerHTML = '<option value="">Failed to load personas</option>';
        }
    };

    const changePersona = async () => {
        const selectedPersona = elements.personaSelect.value;
        if (!selectedPersona || !currentUser) return;

        try {
            // This would need to be implemented as a tool call or API endpoint
            // For now, we'll just show a notification
            showNotification(`Persona changed to: ${selectedPersona}`, "info");
            
            // Update the current user's preferred persona
            currentUser.data.USER_INFO.preferred_persona = selectedPersona;
            updateCurrentUserDisplay();
        } catch (error) {
            console.error("Failed to change persona:", error);
            showNotification("Failed to change persona", "error");
        }
    };

    // Header-specific functions
    const toggleHeaderDropdown = () => {
        // Open the new user profile panel instead of the dropdown
        if (window.UserProfilePanel) {
            window.UserProfilePanel.showPanel();
        }
    };

    const changePersonaFromHeader = async () => {
        const selectedPersona = elements.headerPersonaSelect && elements.headerPersonaSelect.value;
        if (!selectedPersona || !currentUser) return;

        try {
            // This would need to be implemented as a tool call or API endpoint
            showNotification(`Persona changed to: ${selectedPersona}`, "info");
            
            // Update the current user's preferred persona
            currentUser.data.USER_INFO.preferred_persona = selectedPersona;
            updateCurrentUserDisplay();
        } catch (error) {
            console.error("Failed to change persona:", error);
            showNotification("Failed to change persona", "error");
        }
    };

    const setAsCurrentUser = async (profileName) => {
        try {
            const response = await fetch("/current-user", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({ name: profileName })
            });

            if (response.ok) {
                // Get display name for notification
                const displayName = getDisplayName(profileName);
                showNotification(`Set "${displayName}" as current user`, "success");
                loadCurrentUser();
                loadProfiles();
            } else {
                const data = await response.json();
                showNotification(data.error || "Failed to set current user", "error");
            }
        } catch (error) {
            console.error("Failed to set current user:", error);
            showNotification("Failed to set current user", "error");
        }
    };

    const setAsGuest = async () => {
        try {
            const response = await fetch("/current-user", {
                method: "DELETE"
            });

            if (response.ok) {
                showNotification("Switched to guest mode", "success");
                loadCurrentUser();
                loadProfiles();
            } else {
                const data = await response.json();
                showNotification(data.error || "Failed to switch to guest mode", "error");
            }
        } catch (error) {
            console.error("Failed to switch to guest mode:", error);
            showNotification("Failed to switch to guest mode", "error");
        }
    };

    const deleteProfile = async (profileName) => {
        if (!confirm(`Are you sure you want to delete the profile "${profileName}"? This action cannot be undone.`)) {
            return;
        }

        try {
            const response = await fetch(`/profiles/${encodeURIComponent(profileName)}`, {
                method: "DELETE"
            });

            if (response.ok) {
                // Get display name for notification
                const displayName = getDisplayName(profileName);
                showNotification(`Profile "${displayName}" deleted successfully`, "success");
                
                // If the deleted profile was the current user, clear current user
                if (currentUser && currentUser.name === profileName) {
                    // Clear current user on backend
                    try {
                        await fetch("/current-user", {
                            method: "DELETE"
                        });
                    } catch (e) {
                        console.warn("Failed to clear current user on backend:", e);
                    }
                    
                    currentUser = null;
                    updateCurrentUserDisplay();
                }
                
                // Refresh the profiles list
                loadProfiles();
            } else {
                const data = await response.json();
                showNotification(data.error || "Failed to delete profile", "error");
            }
        } catch (error) {
            console.error("Failed to delete profile:", error);
            showNotification("Failed to delete profile", "error");
        }
    };

    // Make functions available globally
    window.switchToProfile = switchToProfile;
    window.setAsCurrentUser = setAsCurrentUser;
    window.setAsGuest = setAsGuest;
    window.deleteProfile = deleteProfile;

    return {
        bindUI,
        loadCurrentUser,
        loadProfiles,
        loadPersonas
    };
})();

// Export the ProfilePanel
window.ProfilePanel = ProfilePanel;
