// ===================== PERSONA FORM =====================
const PersonaForm = (() => {
    // Debug logging utility that respects log level
    const debugLog = (level, message, ...args) => {
        const levels = { 'ERROR': 0, 'WARNING': 1, 'INFO': 2, 'VERBOSE': 3 };
        
        // Get current debug level from settings
        let currentDebugLevel = 'INFO'; // default
        if (window.UserProfilePanel && window.UserProfilePanel.debugLevel) {
            currentDebugLevel = window.UserProfilePanel.debugLevel;
        }
        
        const currentLevel = levels[currentDebugLevel] || 2;
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
    };

    const addBackstoryField = (key = "", value = "") => {
        const wrapper = document.createElement("div");
        wrapper.className = "flex items-center space-x-2";

        const keyInput = Object.assign(document.createElement("input"), {
            type: "text",
            value: key,
            placeholder: "Key",
            className: "w-1/3 p-2 bg-zinc-800 text-white rounded"
        });

        const valInput = Object.assign(document.createElement("input"), {
            type: "text",
            value: value,
            placeholder: "Value",
            className: "flex-1 p-2 bg-zinc-800 text-white rounded"
        });

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0";
        const icon = document.createElement("span");
        icon.className = "material-icons align-middle";
        icon.textContent = "delete";
        removeBtn.appendChild(icon);
        removeBtn.onclick = () => wrapper.remove();

        wrapper.append(keyInput, valInput, removeBtn);
        document.getElementById("backstory-fields").appendChild(wrapper);
    };

    const renderPersonalitySliders = (personality) => {
        const container = document.getElementById("personality-sliders");
        if (!container) {
            console.error('personality-sliders container not found!');
            return;
        }
        container.innerHTML = "";

        // Define the core personality traits with descriptions
        const coreTraits = {
            'humor': 'Humor',
            'confidence': 'Confidence', 
            'warmth': 'Warmth',
            'curiosity': 'Curiosity',
            'verbosity': 'Talkative',
            'formality': 'Formality',
            'sarcasm': 'Sarcasm',
            'honesty': 'Honesty'
        };

        // Default values for missing traits
        const defaultTraitValues = {
            'humor': 70,
            'confidence': 40,
            'warmth': 60,
            'curiosity': 50,
            'verbosity': 20,
            'formality': 50,
            'sarcasm': 60,
            'honesty': 100,
        };

        // Use core traits if personality is empty, otherwise filter to only include valid traits
        let traitsToRender = coreTraits;
        if (Object.keys(personality).length > 0) {
            // Filter personality to only include traits that are in our core set
            traitsToRender = {};
            for (const [key, value] of Object.entries(personality)) {
                if (coreTraits.hasOwnProperty(key)) {
                    traitsToRender[key] = value;
                }
            }
            // Add any missing core traits with default values
            for (const key of Object.keys(coreTraits)) {
                if (!traitsToRender.hasOwnProperty(key)) {
                    traitsToRender[key] = defaultTraitValues[key] || 50; // Use specific default or fallback
                }
            }
        }

        for (const [key, value] of Object.entries(traitsToRender)) {
            const wrapper = document.createElement("div");
            wrapper.className = "flex gap-2 space-y-1";

            const label = document.createElement("div");
            label.className = "flex w-36 justify-between items-center text-sm text-slate-300";
            const displayName = coreTraits[key] || key;
            label.innerHTML = `<span>${displayName}</span>`;

            // Create the main slider container
            const sliderContainer = document.createElement("div");
            sliderContainer.className = "flex flex-col w-full";

            // Create 5-block discrete slider
            const blockSlider = document.createElement("div");
            blockSlider.className = "flex w-full gap-1";
            blockSlider.style.userSelect = "none";

            // Define the 5 levels
            const levels = [
                { name: 'min', range: [0, 14], color: 'bg-red-500' },
                { name: 'low', range: [15, 34], color: 'bg-orange-500' },
                { name: 'med', range: [35, 64], color: 'bg-yellow-500' },
                { name: 'high', range: [65, 84], color: 'bg-emerald-500' },
                { name: 'max', range: [85, 100], color: 'bg-violet-500' }
            ];

            // Remove numeric display - no longer showing 0-100 values

            // Function to update the block slider
            const updateBlockSlider = (slider, newValue) => {
                // Update all blocks
                const blocks = slider.querySelectorAll('[data-level]');
                blocks.forEach(block => {
                    const minVal = parseInt(block.dataset.minValue);
                    const maxVal = parseInt(block.dataset.maxValue);
                    const isActive = newValue >= minVal && newValue <= maxVal;
                    
                    if (isActive) {
                        // Active block: show color, text, and border
                        const color = block.dataset.color;
                        const label = block.dataset.label;
                        block.className = `flex-1 h-6 rounded cursor-pointer transition-all duration-200 hover:opacity-80 ${color} flex items-center justify-center text-xs text-slate-200`;
                        block.textContent = label;
                    } else {
                        // Inactive block: dark grey background, no text
                        block.className = `flex-1 h-6 rounded cursor-pointer transition-all duration-200 hover:opacity-80 bg-zinc-700 flex items-center justify-center text-xs text-slate-200`;
                        block.textContent = '';
                    }
                });
            };

            // Create blocks
            levels.forEach((level) => {
                const block = document.createElement("div");
                // Default to dark grey background, no text
                block.className = `flex-1 h-6 rounded cursor-pointer transition-all duration-200 hover:opacity-80 bg-zinc-700 flex items-center justify-center text-xs text-slate-200`;
                block.dataset.level = level.name;
                block.dataset.minValue = level.range[0];
                block.dataset.maxValue = level.range[1];
                block.dataset.color = level.color;
                block.dataset.label = level.name;
                
                // Check if current value falls in this level
                const isActive = value >= level.range[0] && value <= level.range[1];
                if (isActive) {
                    // Active block: show color, text, and border
                    block.className = `flex-1 h-6 rounded cursor-pointer transition-all duration-200 hover:opacity-80 ${level.color} flex items-center justify-center text-xs text-slate-200`;
                    block.textContent = level.name;
                }
                
                // Add click handler
                block.addEventListener('click', () => {
                    // Set value to middle of the range
                    const newValue = Math.round((level.range[0] + level.range[1]) / 2);
                    updateBlockSlider(blockSlider, newValue);
                });
                
                blockSlider.appendChild(block);
            });

            // Assemble the slider container
            sliderContainer.appendChild(blockSlider);

            wrapper.appendChild(label);
            wrapper.appendChild(sliderContainer);
            container.appendChild(wrapper);
        }
    };

    function setupSlider(barId, fillId, inputId, min, max) {
        const bar = document.getElementById(barId);
        const fill = document.getElementById(fillId);
        const input = document.getElementById(inputId);

        let isDragging = false;
        const updateUI = (val) => {
            const percent = ((val - min) / (max - min)) * 100;
            fill.style.width = `${percent}%`;
            fill.dataset.value = val;
        };
        const updateFromMouse = (e) => {
            const rect = bar.getBoundingClientRect();
            const percent = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1);
            const val = Math.round(min + percent * (max - min));
            input.value = val;
            input.dispatchEvent(new Event("input", {bubbles: true}));
            updateUI(val);
        };
        bar.addEventListener("mousedown", (e) => { isDragging = true; updateFromMouse(e); });
        document.addEventListener("mousemove", (e) => { if (isDragging) updateFromMouse(e); });
        document.addEventListener("mouseup", () => { isDragging = false; });
        input.addEventListener("input", () => updateUI(Number(input.value)));
        updateUI(Number(input.value));
    }

    // Only setup sliders if the elements exist (they're now in the settings modal)
    const micGainBar = document.getElementById("mic-gain-bar");
    
    if (micGainBar) {
        setupSlider("mic-gain-bar", "mic-gain-fill", "mic-gain", 0, 16);
    }
    // Export persona function
    window.exportPersona = async function() {
        try {
            // Get the currently selected persona from the UI
            const selectedRow = document.querySelector('#persona-list [data-persona].border-emerald-500');
            const currentPersona = selectedRow && selectedRow.getAttribute('data-persona') || 'default';
            
            debugLog('VERBOSE', 'Exporting persona:', currentPersona);
            debugLog('VERBOSE', 'Selected row:', selectedRow);
            debugLog('VERBOSE', 'Export URL:', `/persona/export/${currentPersona}`);
            
            const response = await fetch(`/persona/export/${currentPersona}`);
            if (response.ok) {
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${currentPersona}.ini`;
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
                document.body.removeChild(a);
                showNotification(`Exported ${currentPersona} persona`, 'success');
            } else {
                console.error('Export failed with status:', response.status);
                showNotification('Failed to export persona', 'error');
            }
        } catch (error) {
            console.error('Export failed:', error);
            showNotification('Export failed: ' + error.message, 'error');
        }
    };

    // Import persona function
    window.importPersona = async function(input) {
        const file = input.files[0];
        if (!file) return;

        try {
            // Get the currently selected persona from the UI
            const selectedRow = document.querySelector('#persona-list [data-persona].border-emerald-500');
            const currentPersona = selectedRow && selectedRow.getAttribute('data-persona') || 'default';
            
            debugLog('VERBOSE', 'Importing persona to:', currentPersona);
            
            const formData = new FormData();
            formData.append('file', file);
            
            const response = await fetch(`/persona/import/${currentPersona}`, {
                method: 'POST',
                body: formData
            });

            if (response.ok) {
                showNotification(`Imported persona to ${currentPersona}`, 'success');
                // Reload the current persona
                if (window.PersonaForm && window.PersonaForm.loadPersona) {
                    await window.PersonaForm.loadPersona(currentPersona);
                }
                // Refresh the persona list to show any changes
                if (window.PersonaForm && window.PersonaForm.populatePersonaSelector) {
                    await window.PersonaForm.populatePersonaSelector();
                }
                // Refresh user profile panel if it exists
                if (window.UserProfilePanel && window.UserProfilePanel.loadAllData) {
                    await window.UserProfilePanel.loadAllData();
                }
            } else {
                const error = await response.json();
                showNotification('Import failed: ' + error.error, 'error');
            }
        } catch (error) {
            console.error('Import failed:', error);
            showNotification('Import failed: ' + error.message, 'error');
        }
        
        // Reset the input
        input.value = '';
    };


    const renderBackstoryFields = (backstory) => {
        const container = document.getElementById("backstory-fields");
        if (!container) return;
        container.innerHTML = "";
        Object.entries(backstory).forEach(([k, v]) => addBackstoryField(k, v));
    };

    let activePersonaName = null;

    const loadPersona = async (personaName = null) => {
        const metaText = document.getElementById("meta-text");
        if (!metaText) return;

        // If no persona specified, try to get current user's preferred persona
        if (!personaName) {
            try {
                const configData = await ConfigService.fetchConfig();
                if (configData && configData.CURRENT_USER && configData.CURRENT_USER.data && configData.CURRENT_USER.data.USER_INFO) {
                    personaName = configData.CURRENT_USER.data.USER_INFO.preferred_persona || 'default';
                } else {
                    personaName = 'default';
                }
            } catch (error) {
                console.error('Failed to get current user persona:', error);
                personaName = 'default';
            }
        }

        // Load the persona data
        const res = await fetch(`/persona/${personaName}`);
        const data = await res.json();
        
        if (data.error) {
            showNotification(`Failed to load persona: ${data.error}`, 'error');
            return;
        }

        renderPersonalitySliders(data.PERSONALITY);
        renderBackstoryFields(data.BACKSTORY);
        metaText.value = data.META && data.META.instructions || "";
        
        // Load display name and description
        const displayNameInput = document.getElementById("persona-display-name");
        if (displayNameInput) {
            displayNameInput.value = data.META && data.META.name || "";
        }
        const descriptionInput = document.getElementById("persona-description");
        if (descriptionInput) {
            descriptionInput.value = data.META && data.META.description || "";
        }
        
        // Load voice setting
        const voiceSelect = document.getElementById("VOICE");
        if (voiceSelect) {
            // Set voice from persona data, or default to 'ballad' if not specified
            voiceSelect.value = data.META && data.META.voice || 'ballad';
        }
        
        // Load mouth articulation setting
        const mouthArticulationInput = document.getElementById("MOUTH_ARTICULATION");
        if (mouthArticulationInput) {
            // Set mouth articulation from persona data, or default to 5 if not specified
            const mouthArticulation = data.META && data.META.mouth_articulation || 5;
            mouthArticulationInput.value = mouthArticulation;
            updatePersonaMouthArticulationUI(mouthArticulation);
        }

        // Update the current persona tracking for export/import
        window.PersonaForm = window.PersonaForm || {};
        activePersonaName = personaName;
        window.PersonaForm.currentPersona = personaName;
        
        // Update the UI to show the active persona
        updatePersonaListSelection(personaName);

        // Load wakeup clips for the persona being edited.
        loadWakeupClips(personaName);
    };

    const handlePersonaSave = () => {
        const personaForm = document.getElementById("persona-form");
        if (!personaForm || personaForm.dataset.bound === "true") return;

        personaForm.addEventListener("submit", async (e) => {
            e.preventDefault();

            // Check if the save button is disabled
            const saveButton = document.getElementById("save-persona-btn");
            if (saveButton && saveButton.disabled) {
                debugLog('VERBOSE', "Save button is disabled, ignoring form submission");
                return;
            }

            // Check if the form submission was triggered by a disabled delete button
            const activeElement = document.activeElement;
            if (activeElement && activeElement.classList.contains('cursor-not-allowed') && 
                activeElement.classList.contains('opacity-50')) {
                debugLog('VERBOSE', "Form submission triggered by disabled delete button, ignoring");
                return;
            }

            // Also check the event target for disabled delete buttons
            if (e.target && e.target.classList.contains('cursor-not-allowed') && 
                e.target.classList.contains('opacity-50')) {
                debugLog('VERBOSE', "Form submission triggered by disabled delete button (event target), ignoring");
                return;
            }

            const statusData = await ServiceStatus.fetchStatus();
            const wasActive = statusData.status;

            const personality = {};
            // Get trait values from the block sliders
            document.querySelectorAll("#personality-sliders .flex.w-full.gap-1").forEach((slider) => {
                // Find the active block (has text content)
                const activeBlock = Array.from(slider.querySelectorAll('[data-level]')).find(block => block.textContent !== '');
                if (activeBlock) {
                    // Get the trait name from the label in the same wrapper
                    const wrapper = slider.closest('.flex.gap-2');
                    const label = wrapper && wrapper.querySelector('.text-slate-300');
                    if (label) {
                        // Extract trait name from the label text
                        const traitDisplay = label.textContent.trim();
                        // Map display names back to trait keys
                        const traitMap = {
                            'Humor': 'humor',
                            'Confidence': 'confidence',
                            'Warmth': 'warmth',
                            'Curiosity': 'curiosity',
                            'Talkative': 'verbosity',
                            'Formality': 'formality',
                            'Sarcasm': 'sarcasm',
                            'Honesty': 'honesty'
                        };
                        const traitKey = traitMap[traitDisplay];
                        if (traitKey) {
                            // Calculate value from the block's range
                            const minVal = parseInt(activeBlock.dataset.minValue);
                            const maxVal = parseInt(activeBlock.dataset.maxValue);
                            personality[traitKey] = Math.round((minVal + maxVal) / 2);
                        }
                    }
                }
            });

            const backstory = {};
            document.querySelectorAll("#backstory-fields > div").forEach((row) => {
                const [keyInput, valInput] = row.querySelectorAll("input");
                if (keyInput.value.trim() !== "") {
                    backstory[keyInput.value.trim()] = valInput.value.trim();
                }
            });

            const instructions = document.getElementById("meta-text").value.trim();
            const displayName = document.getElementById("persona-display-name")?.value.trim() || "";
            const description = document.getElementById("persona-description")?.value.trim() || "";
            const voice = document.getElementById("VOICE").value;
            const mouthArticulationInput = document.getElementById("MOUTH_ARTICULATION");
            const mouthArticulation = mouthArticulationInput ? mouthArticulationInput.value : "5";
            
            debugLog('VERBOSE', 'Mouth articulation input found:', !!mouthArticulationInput);
            debugLog('VERBOSE', 'Mouth articulation value:', mouthArticulation);
            
            // Build META object
            const meta = {
                name: displayName,
                description: description,
                instructions: instructions,
                voice: voice,
                mouth_articulation: mouthArticulation
            };

            const wakeup = window.collectWakeupRows ? window.collectWakeupRows() : {};

            // Get the currently selected persona from the list
            const selectedRow = document.querySelector('#persona-list [data-persona].border-emerald-500');
            const personaName = selectedRow && selectedRow.getAttribute('data-persona') || 'default';
            
            debugLog('INFO', 'Saving persona:', personaName);
            debugLog('VERBOSE', 'Wake-up data:', wakeup);
            
            await fetch("/persona", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    persona_name: personaName,
                    PERSONALITY: personality, 
                    BACKSTORY: backstory, 
                    META: meta,
                    WAKEUP: wakeup 
                })
            });

            showNotification(`Persona "${personaName}" saved`, "success");
            
            // Refresh the persona list to show updated voice summary
            await populatePersonaSelector();
            
            // Always refresh the persona form to show updated values
            await loadPersona(personaName);
            
            if (wasActive === "active") {
                // Auto-refresh configuration instead of restarting services
                try {
                    const refreshResponse = await fetch("/config/auto-refresh", {method: "POST"});
                    const refreshData = await refreshResponse.json();
                    
                    if (refreshData.status === "ok") {
                        showNotification("Persona saved and applied", "success");
                        ServiceStatus.fetchStatus();
                    } else {
                        console.error("Auto-refresh failed, falling back to restart:", refreshData.error || "Auto-refresh failed");
                        // Fallback to restart Billy service if auto-refresh fails
                        await fetch("/restart-billy", {method: "POST"});
                        showNotification("Persona saved – service restarted", "success");
                        ServiceStatus.fetchStatus();
                    }
                } catch (error) {
                    console.error("Auto-refresh failed, falling back to restart:", error);
                    // Fallback to restart Billy service if auto-refresh fails
                    await fetch("/restart-billy", {method: "POST"});
                    showNotification("Persona saved – service restarted", "success");
                    ServiceStatus.fetchStatus();
                }
            }
        });
        personaForm.dataset.bound = "true";
    };

    let isPopulatingPersonas = false;

    const populatePersonaSelector = async () => {
        const personaList = document.getElementById("persona-list");
        if (!personaList) return;

        // Prevent multiple simultaneous calls
        if (isPopulatingPersonas) {
            debugLog('VERBOSE', 'populatePersonaSelector already in progress, skipping...');
            return;
        }

        isPopulatingPersonas = true;
        debugLog('VERBOSE', 'Starting populatePersonaSelector...');

        try {
            const configData = await ConfigService.fetchConfig();
            if (configData && configData.AVAILABLE_PERSONAS) {
                personaList.innerHTML = '';
                
                // No guest mode restrictions - anyone with web UI access can manage personas
                
                // Get currently selected persona - check both user preference and form state
                let currentPersona = null;
                
                // First check if there's a currently selected persona in the form
                const selectedRow = document.querySelector('#persona-list [data-persona].border-emerald-500');
                if (selectedRow) {
                    currentPersona = selectedRow.getAttribute('data-persona');
                }

                if (!currentPersona) {
                    currentPersona = activePersonaName || window.PersonaForm?.currentPersona || null;
                }
                
                // Fallback to user's preferred persona or current persona for guest mode
                if (!currentPersona) {
                    if (configData && configData.CURRENT_USER && typeof configData.CURRENT_USER === 'object' && configData.CURRENT_USER.data && configData.CURRENT_USER.data.USER_INFO) {
                        currentPersona = configData.CURRENT_USER.data.USER_INFO.preferred_persona || 'default';
                    } else if (configData && configData.CURRENT_PERSONA) {
                        currentPersona = configData.CURRENT_PERSONA;
                    } else {
                        // Final fallback
                        currentPersona = 'default';
                    }
                }
                
                debugLog('VERBOSE', 'Current persona for delete button logic:', currentPersona);
                
                // Sort personas: default first, then custom ones
                const sortedPersonas = configData.AVAILABLE_PERSONAS.sort((a, b) => {
                    if (a.name === 'default') return -1;
                    if (b.name === 'default') return 1;
                    return a.name.localeCompare(b.name);
                });
                
                // Load full persona data including name and description
                const personasWithFullData = await Promise.all(sortedPersonas.map(async (persona) => {
                    try {
                        const response = await fetch(`/persona/${persona.name}`);
                        const data = await response.json();
                        return {
                            ...persona,
                            displayName: data.META && data.META.name || persona.name,
                            description: data.META && data.META.description || '',
                            voice: data.META && data.META.voice || 'alloy'
                        };
                    } catch (error) {
                        console.error(`Failed to load data for ${persona.name}:`, error);
                        return {
                            ...persona,
                            displayName: persona.name,
                            description: '',
                            voice: 'alloy'
                        };
                    }
                }));
                
                personasWithFullData.forEach(persona => {
                    const row = document.createElement('div');
                    row.className = 'flex items-center justify-between p-3 bg-zinc-800 rounded-lg hover:bg-zinc-700 transition-colors cursor-pointer border border-zinc-700';
                    row.setAttribute('data-persona', persona.name);
                    
                    const isDefault = persona.name === 'default';
                    const isCurrentPersona = persona.name === currentPersona;
                    
                    // Check if this persona is preferred by any user across all profiles
                    let isPreferredPersona = false;
                    if (configData && configData.AVAILABLE_PROFILES) {
                        isPreferredPersona = configData.AVAILABLE_PROFILES.some(profile => 
                            profile.data && profile.data.USER_INFO && 
                            profile.data.USER_INFO.preferred_persona === persona.name
                        );
                    }
                    
                    debugLog('VERBOSE', `Persona ${persona.name}: isDefault=${isDefault}, isCurrentPersona=${isCurrentPersona}, isPreferredPersona=${isPreferredPersona}, currentPersona=${currentPersona}`);
                    
                    // Build display text: "Name - Description" or just "Name" if no description
                    const displayText = persona.description 
                        ? `${persona.displayName} - ${persona.description}`
                        : persona.displayName;
                    
                    row.innerHTML = `
                        <div class="flex items-center space-x-3">
                            <span class="material-icons ${isCurrentPersona ? 'text-emerald-400' : 'text-zinc-400'}">set_meal</span>
                            <div>
                                <div class="text-white">${displayText}</div>
                                <div class="text-xs text-zinc-400">${persona.voice}</div>
                            </div>
                        </div>
                        <div class="flex items-center space-x-2">
        ${!isDefault ? `
          <button type="button" 
                  class="${isCurrentPersona || isPreferredPersona ? 'secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0 opacity-50 cursor-not-allowed' : 'secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0'}" 
                  onclick="event.stopPropagation(); ${isCurrentPersona || isPreferredPersona ? `window.PersonaForm.showPreferredPersonaDeleteMessage('${persona.name}')` : `window.PersonaForm.deletePersona('${persona.name}')`}" 
                  title="${isCurrentPersona || isPreferredPersona ? 'Cannot delete preferred persona' : 'Delete persona'}">
              <span class="material-icons">delete</span>
          </button>
      ` : ''}
                        </div>
                    `;
                    
                    // Add click handler to load persona
                    row.addEventListener('click', async () => {
                        window.MobileSplitView?.showDetail('persona-split-view');
                        await loadPersona(persona.name);
                    });
                    
                    personaList.appendChild(row);
                });
                
                // Update the selection to show the current persona as active
                updatePersonaListSelection(currentPersona);
            }
        } catch (error) {
            console.error('Failed to load available personas:', error);
        } finally {
            isPopulatingPersonas = false;
            debugLog('VERBOSE', 'Finished populatePersonaSelector');
        }
    };

    const deletePersona = async (personaName) => {
        if (personaName === 'default') {
            showNotification('Cannot delete the default persona', 'error');
            return;
        }

        // Check if this persona is currently active
        const selectedRow = document.querySelector('#persona-list [data-persona].border-emerald-500');
        const currentPersona = selectedRow && selectedRow.getAttribute('data-persona');
        if (personaName === currentPersona) {
            showNotification('Cannot delete the currently active persona. Switch to a different persona first, then delete this one.', 'error', 8000);
            return;
        }

        // Check if this persona is preferred by any user
        try {
            const response = await fetch('/config');
            const configData = await response.json();
            
            if (configData && configData.AVAILABLE_PROFILES) {
                const connectedUser = configData.AVAILABLE_PROFILES.find(profile => 
                    profile.data && profile.data.USER_INFO && 
                    profile.data.USER_INFO.preferred_persona === personaName
                );
                
                if (connectedUser) {
                    const userName = connectedUser.name;
                    showNotification(`Cannot delete "${personaName}" persona - it is currently set as ${userName}'s preferred persona. Change the user's preferred persona first, then delete this one.`, 'error', 8000);
                    return;
                }
            }
        } catch (error) {
            console.error('Failed to check persona connections:', error);
        }

        if (!confirm(`Are you sure you want to delete the "${personaName}" persona? This action cannot be undone.`)) {
            return;
        }

        try {
            const response = await fetch(`/persona/${personaName}`, {
                method: 'DELETE'
            });

            if (response.ok) {
                showNotification(`Persona deleted: ${personaName}`, 'success');
                // Clear persona cache to ensure fresh data
                if (window.PersonaForm && window.PersonaForm.clearPersonaCache) {
                    window.PersonaForm.clearPersonaCache();
                }
                // Reload the persona list
                await populatePersonaSelector();
                // Load default persona if the deleted one was currently loaded
                await loadPersona('default');
            } else {
                const error = await response.json();
                showNotification(error.error || 'Failed to delete persona', 'error');
            }
        } catch (error) {
            console.error('Failed to delete persona:', error);
            showNotification('Failed to delete persona', 'error');
        }
    };

    const updatePersonaListSelection = (selectedPersonaName) => {
        const personaList = document.getElementById("persona-list");
        if (!personaList) return;

        debugLog('VERBOSE', 'Updating persona selection to:', selectedPersonaName);

        // Remove active class from all rows
        const rows = personaList.querySelectorAll('[data-persona]');
        rows.forEach(row => {
            row.classList.remove('border-emerald-500', 'bg-emerald-900/20');
            row.classList.add('border-zinc-700');
            
            // Reset set_meal icon color
            const setMealIcon = row.querySelector('.set_meal');
            if (setMealIcon) {
                setMealIcon.classList.remove('text-emerald-500');
                setMealIcon.classList.add('text-zinc-400');
                debugLog('VERBOSE', 'Reset icon color for:', row.dataset.persona);
            }
        });

        // Add active class to selected row
        const selectedRow = personaList.querySelector(`[data-persona="${selectedPersonaName}"]`);
        if (selectedRow) {
            selectedRow.classList.remove('border-zinc-700');
            selectedRow.classList.add('border-emerald-500', 'bg-emerald-900/20');
            
            // Set set_meal icon to green for active persona
            const setMealIcon = selectedRow.querySelector('.material-icons');
            if (setMealIcon && setMealIcon.textContent === 'set_meal') {
                setMealIcon.classList.remove('text-zinc-400');
                setMealIcon.classList.add('text-emerald-500');
                debugLog('VERBOSE', 'Set icon to green for:', selectedPersonaName);
            } else {
                debugLog('VERBOSE', 'No set_meal icon found for:', selectedPersonaName);
            }
        } else {
            debugLog('VERBOSE', 'No selected row found for:', selectedPersonaName);
        }

        // Also update persona selector dropdowns
        const personaSelect = document.getElementById('persona-select');
        if (personaSelect) {
            personaSelect.value = selectedPersonaName;
        }
        
        const headerPersonaSelect = document.getElementById('header-persona-select');
        if (headerPersonaSelect) {
            headerPersonaSelect.value = selectedPersonaName;
        }
    };

    // Function to ensure icon colors are always in sync with border colors
    const syncIconColors = () => {
        const personaList = document.getElementById("persona-list");
        if (!personaList) return;

        const rows = personaList.querySelectorAll('[data-persona]');
        rows.forEach(row => {
            const setMealIcon = row.querySelector('.material-icons');
            if (setMealIcon && setMealIcon.textContent === 'set_meal') {
                if (row.classList.contains('border-emerald-500')) {
                    setMealIcon.classList.remove('text-zinc-400');
                    setMealIcon.classList.add('text-emerald-500');
                } else {
                    setMealIcon.classList.remove('text-emerald-500');
                    setMealIcon.classList.add('text-zinc-400');
                }
            }
        });
    };

    // Update persona mouth articulation UI
    const updatePersonaMouthArticulationUI = (value) => {
        const bar = document.getElementById('persona-mouth-articulation-bar');
        const fill = document.getElementById('persona-mouth-articulation-fill');
        const valueDisplay = document.getElementById('persona-mouth-articulation-value');
        
        if (bar && fill && valueDisplay) {
            const percent = ((value - 1) / 9) * 100; // 1-10 range to 0-100%
            fill.style.width = `${percent}%`;
            fill.dataset.value = value;
            valueDisplay.textContent = value;
        }
    };

    // Initialize persona mouth articulation slider
    const initPersonaMouthArticulationSlider = () => {
        const bar = document.getElementById('persona-mouth-articulation-bar');
        const fill = document.getElementById('persona-mouth-articulation-fill');
        const input = document.getElementById('MOUTH_ARTICULATION');
        const valueDisplay = document.getElementById('persona-mouth-articulation-value');

        if (!bar || !fill || !input || !valueDisplay) return;

        let isDragging = false;
        
        const updateUI = (val) => {
            const percent = ((val - 1) / 9) * 100; // 1-10 range to 0-100%
            fill.style.width = `${percent}%`;
            fill.dataset.value = val;
            input.value = val;
            input.setAttribute('value', val);
            valueDisplay.textContent = val;
        };
        
        const updateFromMouse = (e) => {
            const rect = bar.getBoundingClientRect();
            const percent = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1);
            const val = Math.round(1 + percent * 9); // 1-10 range
            input.value = val;
            input.setAttribute('value', val);
            input.dispatchEvent(new Event("input", {bubbles: true}));
            updateUI(val);
        };
        
        bar.addEventListener("mousedown", (e) => { isDragging = true; updateFromMouse(e); });
        document.addEventListener("mousemove", (e) => { if (isDragging) updateFromMouse(e); });
        document.addEventListener("mouseup", () => { isDragging = false; });
        input.addEventListener("input", () => updateUI(Number(input.value)));
        
        // Initialize with current value
        updateUI(Number(input.value));
    };

    const bindPersonaSelector = () => {
        // No longer needed since personas are loaded on click
        // But keeping the function for consistency
    };

    const savePersonaAs = async () => {
        const personaName = prompt('Enter a name for the new persona:\n(This will create a copy of the current persona)');
        if (!personaName || personaName.trim() === '') {
            return;
        }

        // Clean the name: lowercase and remove special characters except hyphens and underscores
        const cleanName = personaName.trim().toLowerCase().replace(/[^a-z0-9\-_]/g, '');
        
        if (cleanName === '') {
            showNotification('Invalid persona name. Please use only letters, numbers, hyphens, and underscores.', 'error');
            return;
        }

        // Check if persona already exists
        try {
            const response = await fetch(`/persona/${cleanName}`);
            if (response.ok) {
                if (!confirm(`A persona named "${cleanName}" already exists. Do you want to overwrite it?`)) {
                    return;
                }
            }
        } catch (error) {
            // Persona doesn't exist, which is fine
        }

        try {
            // Get current persona data using the same logic as handlePersonaSave
            const personality = {};
            // Get trait values from the block sliders
            document.querySelectorAll("#personality-sliders .flex.w-full.gap-1").forEach((slider) => {
                // Find the active block (has text content)
                const activeBlock = Array.from(slider.querySelectorAll('[data-level]')).find(block => block.textContent !== '');
                if (activeBlock) {
                    // Get the trait name from the label in the same wrapper
                    const wrapper = slider.closest('.flex.gap-2');
                    const label = wrapper && wrapper.querySelector('.text-slate-300');
                    if (label) {
                        // Extract trait name from the label text
                        const traitDisplay = label.textContent.trim();
                        // Map display names back to trait keys
                        const traitMap = {
                            'Humor': 'humor',
                            'Confidence': 'confidence',
                            'Warmth': 'warmth',
                            'Curiosity': 'curiosity',
                            'Talkative': 'verbosity',
                            'Formality': 'formality',
                            'Sarcasm': 'sarcasm',
                            'Honesty': 'honesty'
                        };
                        const traitKey = traitMap[traitDisplay];
                        if (traitKey) {
                            // Calculate value from the block's range
                            const minVal = parseInt(activeBlock.dataset.minValue);
                            const maxVal = parseInt(activeBlock.dataset.maxValue);
                            personality[traitKey] = Math.round((minVal + maxVal) / 2);
                        }
                    }
                }
            });

            const backstory = {};
            document.querySelectorAll("#backstory-fields > div").forEach((row) => {
                const [keyInput, valInput] = row.querySelectorAll("input");
                if (keyInput.value.trim() !== "") {
                    backstory[keyInput.value.trim()] = valInput.value.trim();
                }
            });

            const instructions = document.getElementById("meta-text").value.trim();
            const displayName = document.getElementById("persona-display-name")?.value.trim() || personaName.trim();
            const description = document.getElementById("persona-description")?.value.trim() || "";
            const voice = document.getElementById("VOICE").value;
            const mouthArticulationInput = document.getElementById("MOUTH_ARTICULATION");
            const mouthArticulation = mouthArticulationInput ? mouthArticulationInput.value : "5";
            
            debugLog('VERBOSE', 'Mouth articulation input found:', !!mouthArticulationInput);
            debugLog('VERBOSE', 'Mouth articulation value:', mouthArticulation);

            const wakeup = window.collectWakeupRows ? window.collectWakeupRows() : {};

            const personaData = {
                PERSONALITY: personality,
                BACKSTORY: backstory,
                META: {
                    name: displayName,
                    description: description,
                    instructions: instructions,
                    voice: voice,
                    mouth_articulation: mouthArticulation
                },
                WAKEUP: wakeup
            };

            // Ensure VOICE is at the top level for backend compatibility

            // Save the persona
            const saveResponse = await fetch('/persona', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    ...personaData,
                    persona_name: cleanName,
                    VOICE: voice
                })
            });

            if (saveResponse.ok) {
                showNotification(`Persona "${personaName}" saved successfully!`, 'success');
                
                // Refresh the persona selector to show the new persona
                await populatePersonaSelector();
                
                // Load the new persona (this will switch to it and show it as active)
                await loadPersona(cleanName);
                
                // Refresh user profile panel if it exists
                if (window.UserProfilePanel && window.UserProfilePanel.loadAllData) {
                    await window.UserProfilePanel.loadAllData();
                }
                
                // Show additional notification that we're now editing the new persona
                setTimeout(() => {
                    showNotification(`Now editing "${personaName}" - you can make further adjustments`, 'info');
                }, 500);
            } else {
                const error = await saveResponse.json();
                showNotification(error.error || 'Failed to save persona', 'error');
            }
        } catch (error) {
            console.error('Failed to save persona as:', error);
            showNotification('Failed to save persona', 'error');
        }
    };

    // Show message for why active persona can't be deleted
    const showActivePersonaDeleteMessage = () => {
        showNotification('Cannot delete active persona - it is currently linked to a user profile. Switch to a different persona first, then delete this one.', 'warning', 8000);
    };

    // Show message for why preferred persona can't be deleted
    const showPreferredPersonaDeleteMessage = async (personaName) => {
        try {
            // Get the user profile that has this persona as preferred
            const response = await fetch('/config');
            const configData = await response.json();
            
            let connectedUser = null;
            if (configData && configData.AVAILABLE_PROFILES) {
                connectedUser = configData.AVAILABLE_PROFILES.find(profile => 
                    profile.data && profile.data.USER_INFO && 
                    profile.data.USER_INFO.preferred_persona === personaName
                );
            }
            
            const userName = connectedUser ? connectedUser.name : 'a user';
            showNotification(`Cannot delete "${personaName}" persona - it is currently set as ${userName}'s preferred persona. Change the user's preferred persona first, then delete this one.`, 'error', 8000);
        } catch (error) {
            console.error('Failed to get user info for persona deletion message:', error);
            showNotification('Cannot delete preferred persona - it is currently set as a user\'s preferred persona. Change the user\'s preferred persona first, then delete this one.', 'error', 8000);
        }
    };

    // Clear persona cache
    const clearPersonaCache = () => {
        // Clear any cached persona data
        debugLog('VERBOSE', 'Clearing persona cache');
    };

    const handlePersonaChangeNotification = (personaName) => {
        // Update the UI to reflect the persona change
        debugLog('INFO', 'Persona changed to:', personaName);
        
        // Use a small delay to ensure the DOM is ready
        setTimeout(() => {
            updatePersonaListSelection(personaName);
            // Also sync icon colors to ensure they match border colors
            syncIconColors();
        }, 100);
        
        // No notification needed - the visual UI update is sufficient
    };

    const handlePersonalityChange = (personalityTraits) => {
        // Update the UI to reflect personality trait changes
        debugLog('INFO', 'Personality traits changed:', personalityTraits);
        
        // Re-render the personality sliders with the new values
        // This updates the custom block sliders to reflect changes made via voice commands
        renderPersonalitySliders(personalityTraits);
    };

    // Create Persona Modal Functions
    let selectedPresetId = null;

    const openCreatePersonaModal = async () => {
        const modal = document.getElementById('create-persona-modal');
        if (!modal) return;
        
        // Clear previous input
        const nameInput = document.getElementById('new-persona-name');
        if (nameInput) nameInput.value = '';
        
        // Load presets
        await loadPresets();
        
        // Show modal
        modal.classList.remove('hidden');
    };

    const closeCreatePersonaModal = () => {
        const modal = document.getElementById('create-persona-modal');
        if (modal) {
            modal.classList.add('hidden');
            selectedPresetId = null;
        }
    };

    const loadPresets = async () => {
        try {
            const response = await fetch('/persona/presets');
            const presets = await response.json();
            
            const presetList = document.getElementById('preset-list');
            if (!presetList) return;
            
            // Sort presets to put "blank" first, then alphabetically by name
            const sortedPresets = presets.sort((a, b) => {
                if (a.id === 'blank') return -1;
                if (b.id === 'blank') return 1;
                return a.name.localeCompare(b.name);
            });
            
            presetList.innerHTML = sortedPresets.map(preset => `
                <div class="preset-option p-3 border border-zinc-700 rounded cursor-pointer hover:border-emerald-500 transition-colors ${preset.id === selectedPresetId ? 'border-emerald-500 bg-emerald-900/20' : ''}"
                     data-preset-id="${preset.id}"
                     onclick="window.PersonaForm.selectPreset('${preset.id}')">
                    <div class="text-white">${preset.name}</div>
                    ${preset.description ? `<div class="text-sm text-zinc-400 mt-1">${preset.description}</div>` : ''}
                </div>
            `).join('');
            
            // Select first preset by default (which will be "blank" if it exists)
            if (sortedPresets.length > 0 && !selectedPresetId) {
                selectPreset(sortedPresets[0].id);
            }
        } catch (error) {
            console.error('Failed to load presets:', error);
            showNotification('Failed to load persona templates', 'error');
        }
    };

    const selectPreset = (presetId) => {
        selectedPresetId = presetId;
        
        // Update UI to show selection
        const presetOptions = document.querySelectorAll('.preset-option');
        presetOptions.forEach(option => {
            if (option.dataset.presetId === presetId) {
                option.classList.add('border-emerald-500', 'bg-emerald-900/20');
            } else {
                option.classList.remove('border-emerald-500', 'bg-emerald-900/20');
            }
        });
    };

    const createPersonaFromModal = async () => {
        const nameInput = document.getElementById('new-persona-name');
        const personaName = nameInput?.value.trim();
        
        if (!personaName) {
            showNotification('Please enter a persona name', 'error');
            return;
        }
        
        if (!selectedPresetId) {
            showNotification('Please select a template', 'error');
            return;
        }
        
        try {
            const response = await fetch('/persona/create-from-preset', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    preset_id: selectedPresetId,
                    persona_name: personaName
                })
            });
            
            const result = await response.json();
            
            if (response.ok) {
                showNotification(`Persona "${result.persona_name}" created successfully`, 'success');
                closeCreatePersonaModal();
                
                // Refresh persona list
                await populatePersonaSelector();
                
                // Load the new persona
                await loadPersona(result.persona_name);
            } else {
                showNotification(`Failed to create persona: ${result.error}`, 'error');
            }
        } catch (error) {
            console.error('Failed to create persona:', error);
            showNotification('Failed to create persona', 'error');
        }
    };

    // Initialize modal event listeners
    const initCreatePersonaModal = () => {
        const modal = document.getElementById('create-persona-modal');
        if (!modal || modal.dataset.bound === 'true') return;

        // Close button
        const closeBtn = document.getElementById('close-create-persona-modal');
        if (closeBtn) {
            closeBtn.addEventListener('click', closeCreatePersonaModal);
        }

        // Click outside to close (only if both mousedown and mouseup on backdrop)
        let mouseDownOnBackdrop = false;
        
        modal.addEventListener('mousedown', (e) => {
            mouseDownOnBackdrop = e.target === modal;
        });
        
        modal.addEventListener('mouseup', (e) => {
            if (mouseDownOnBackdrop && e.target === modal) {
                closeCreatePersonaModal();
            }
            mouseDownOnBackdrop = false;
        });
        modal.dataset.bound = 'true';
    };

    // Populate voice options dynamically from config
    const populateVoiceOptions = async () => {
        const voiceSelect = document.getElementById('VOICE');
        if (!voiceSelect) return;

        try {
            // Provider settings may have just changed on the settings page, so do
            // not reuse its short-lived config cache when rebuilding this list.
            const configData = await ConfigService.fetchConfig(true);
            if (configData && configData.VOICE_OPTIONS) {
                const currentValue = voiceSelect.value;
                // Clear existing options
                voiceSelect.innerHTML = '';

                // Add new options
                configData.VOICE_OPTIONS.forEach(voice => {
                    const option = document.createElement('option');
                    option.value = voice;
                    option.textContent = voice.charAt(0).toUpperCase() + voice.slice(1); // Capitalize first letter
                    voiceSelect.appendChild(option);
                });

                // Restore selection or default to first option
                if (configData.VOICE_OPTIONS.includes(currentValue)) {
                    voiceSelect.value = currentValue;
                } else if (configData.VOICE_OPTIONS.length > 0) {
                    voiceSelect.value = configData.VOICE_OPTIONS[0];
                }
            }
        } catch (error) {
            console.error('Failed to populate voice options:', error);
        }
    };

    return {addBackstoryField, loadPersona, handlePersonaSave, bindPersonaSelector, populatePersonaSelector, deletePersona, savePersonaAs, showActivePersonaDeleteMessage, showPreferredPersonaDeleteMessage, clearPersonaCache, updatePersonaListSelection, handlePersonaChangeNotification, handlePersonalityChange, syncIconColors, initPersonaMouthArticulationSlider, openCreatePersonaModal, closeCreatePersonaModal, selectPreset, createPersonaFromModal, initCreatePersonaModal, populateVoiceOptions};
})();
