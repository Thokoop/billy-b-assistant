// ===================== SONGS MANAGER =====================
const SongsManager = (() => {
    let currentSong = null;
    let isEditMode = false;
    let songNameManuallyEdited = false;

    const slugifySongName = (title) => title
        .toLowerCase()
        .replace(/[^a-z0-9_]+/g, '_')
        .replace(/^_+|_+$/g, '');

    // Debug logging utility
    const debugLog = (level, message, ...args) => {
        const levels = { 'ERROR': 0, 'WARNING': 1, 'INFO': 2, 'VERBOSE': 3 };
        
        let currentDebugLevel = 'INFO';
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

    const showNotification = (message, type = 'info') => {
        if (window.UserProfilePanel && window.UserProfilePanel.showNotification) {
            window.UserProfilePanel.showNotification(message, type);
        } else {
            debugLog('INFO', message);
        }
    };

    // fetch() only throws on network-level failures (server unreachable), not
    // on HTTP error statuses - exactly the case right after a Pi reboot where
    // the webconfig service isn't accepting connections yet. Retries a few
    // times before giving up, so a tab left open across a restart recovers
    // on its own instead of needing a manual refresh.
    const fetchWithRetry = async (url, options = {}, attempts = 5, delayMs = 1500) => {
        let lastError;
        for (let i = 0; i < attempts; i++) {
            try {
                return await fetch(url, options);
            } catch (error) {
                lastError = error;
                if (i < attempts - 1) {
                    await new Promise((resolve) => setTimeout(resolve, delayMs));
                }
            }
        }
        throw lastError;
    };

    const showListView = () => {
        document.getElementById('songs-list-view')?.classList.remove('hidden');
        document.getElementById('song-edit-view')?.classList.add('hidden');
        document.getElementById('song-edit-footer')?.classList.add('hidden');
        document.getElementById('song-edit-empty-state')?.classList.remove('hidden');
        window.MobileSplitView?.showList('songs-split-view');

        document.getElementById('back-to-songs-list-btn')?.classList.add('hidden');

        currentSong = null;
        isEditMode = false;
        loadSongs();
    };

    const showEditView = (songName = null) => {
        document.getElementById('song-edit-empty-state')?.classList.add('hidden');
        document.getElementById('song-edit-view')?.classList.remove('hidden');
        document.getElementById('song-edit-footer')?.classList.remove('hidden');
        window.MobileSplitView?.showDetail('songs-split-view');
        
        isEditMode = songName !== null;
        currentSong = songName;

        document.getElementById('back-to-songs-list-btn')?.classList.remove('hidden');

        // Show/hide song name field (only for new songs)
        const songNameField = document.getElementById('song-name-field');
        const songNameInput = document.getElementById('song-name');

        const folderDisplayField = document.getElementById('song-folder-display-field');
        const folderDisplay = document.getElementById('song-folder-display');

        if (isEditMode) {
            songNameField.classList.add('hidden');
            songNameInput.removeAttribute('required');
            folderDisplayField.classList.remove('hidden');
            folderDisplay.textContent = songName;
        } else {
            songNameField.classList.remove('hidden');
            songNameInput.setAttribute('required', 'required');
            folderDisplayField.classList.add('hidden');
        }

        loadSongs();

        if (songName) {
            loadSongData(songName);
        } else {
            resetForm();
        }
    };

    const loadSongs = async () => {
        try {
            const response = await fetch('/songs');
            if (!response.ok) throw new Error('Failed to load songs');
            
            const songs = await response.json();
            renderSongsList(songs);
        } catch (error) {
            debugLog('ERROR', 'Failed to load songs:', error);
            showNotification('Failed to load songs', 'error');
        }
    };

    const renderSongsList = (songs) => {
        const grid = document.getElementById('songs-grid');
        const emptyState = document.getElementById('songs-empty-state');

        if (songs.length === 0) {
            grid.classList.add('hidden');
            emptyState.classList.remove('hidden');
            return;
        }

        grid.classList.remove('hidden');
        emptyState.classList.add('hidden');

        // Examples are a template gallery, not part of the active rotation -
        // keep them out of the way at the bottom. Array.sort is stable, so
        // each group keeps the alphabetical order it already arrived in.
        const sortedSongs = [...songs].sort(
            (a, b) => (a.is_example ? 1 : 0) - (b.is_example ? 1 : 0)
        );

        grid.innerHTML = sortedSongs.map(song => {
            // Vocals is the only required stem - Full Mix and Drums are optional.
            const hasRequiredFiles = !!song.has_vocals;
            const isExample = song.is_example || false;
            const isSelected = currentSong === song.name;

            const statusIcon = hasRequiredFiles ? '' :
                '<span class="material-icons text-amber-400 text-sm" title="Missing required Vocals stem">warning</span>';
            
            // Example songs have a different style and action
            if (isExample) {
                return `
                    <div class="bg-zinc-800/50 border border-amber-600/50 rounded-lg p-4 hover:border-amber-500 transition-colors">
                        <div class="flex items-start justify-between">
                            <div class="flex-1">
                                <div class="flex items-center gap-2">
                                    <h4 class="text-white">${song.title}</h4>
                                    <span class="text-xs bg-amber-600/20 text-amber-400 px-2 py-0.5 rounded">Example</span>
                                </div>
                                <p class="text-xs text-zinc-500">${song.name}</p>
                                ${song.keywords ? `
                                    <p class="text-sm text-zinc-400">${song.keywords}</p>
                                ` : ''}
                            </div>
                            <button onclick="window.SongsManager.copyExample('${song.name}')"
                                    class="secondary-action secondary-action--hover--amber h-11 w-11 p-0 shrink-0"
                                    title="Copy to Custom Songs">
                                <span class="material-icons">content_copy</span>
                            </button>
                        </div>
                    </div>
                `;
            }

            const isEnabled = song.enabled !== false;

            return `
                <div id="song-card-${song.name}"
                     class="${isSelected ? 'bg-emerald-500/10 border-emerald-500 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]' : 'bg-zinc-800 border-zinc-700 hover:border-emerald-500'} ${isEnabled ? '' : 'opacity-50'} border rounded-lg p-4 transition-colors cursor-pointer"
                     onclick="window.SongsManager.editSong('${song.name}')">
                    <div class="flex items-start justify-between">
                        <div class="flex-1">
                            <h4 class="text-white">${song.title}</h4>
                            ${song.keywords ? `
                                <p class="text-sm text-zinc-400">${song.keywords}</p>
                            ` : ''}
                        </div>
                        <div class="flex items-center gap-2 shrink-0">
                            ${statusIcon}
                            <label class="wf-toggle" title="${isEnabled ? 'Enabled - Billy can play this' : 'Disabled - Billy will skip this'}"
                                   onclick="event.stopPropagation()">
                                <input type="checkbox" ${isEnabled ? 'checked' : ''}
                                       onchange="window.SongsManager.toggleSongEnabled('${song.name}', this.checked)">
                                <span class="wf-toggle-track"><span class="wf-toggle-thumb"></span></span>
                            </label>
                            <button type="button"
                                    class="secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0"
                                    onclick="event.stopPropagation(); window.SongsManager.deleteSong('${song.name}')"
                                    title="Delete song">
                                <span class="material-icons">delete</span>
                            </button>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    };

    const loadSongData = async (songName) => {
        try {
            const response = await fetch(`/songs/${songName}`);
            if (!response.ok) throw new Error('Failed to load song data');
            
            const song = await response.json();
            
            // Populate form
            document.getElementById('song-title').value = song.title || '';
            document.getElementById('song-keywords').value = song.keywords || '';
            document.getElementById('song-bpm').value = song.bpm || 120;
            gainSlider?.setValue(song.gain || 1.0);
            document.getElementById('song-tail-threshold').value = song.tail_threshold || 1500;
            document.getElementById('song-compensate-tail').value = song.compensate_tail || 0;
            document.getElementById('song-head-moves').value = song.head_moves || '';
            document.getElementById('song-tail-moves').value = song.tail_moves || '';
            document.getElementById('song-mouth-mutes').value = song.mouth_mutes || '';
            await setSongMouthArticulation(song.mouth_articulation);
            setSongLedColor(song.led_color);
            document.getElementById('song-half-tempo').checked = song.half_tempo_tail_flap || false;

            // Update file status indicators
            updateFileStatus('full', song.has_full);
            updateFileStatus('vocals', song.has_vocals);
            updateFileStatus('drums', song.has_drums);

            // Point each audio element at its file - these drive the
            // waveform editor's playback, not a per-file play button.
            if (song.has_full) {
                const audioElement = document.getElementById('full-audio');
                if (audioElement) audioElement.src = `/songs/${songName}/full.wav`;
            }
            if (song.has_vocals) {
                const audioElement = document.getElementById('vocals-audio');
                if (audioElement) audioElement.src = `/songs/${songName}/vocals.wav`;
            }
            if (song.has_drums) {
                const audioElement = document.getElementById('drums-audio');
                if (audioElement) audioElement.src = `/songs/${songName}/drums.wav`;
            }

            await wfLoadWaveformEditorForSong(songName, song);
        } catch (error) {
            debugLog('ERROR', 'Failed to load song data:', error);
            showNotification('Failed to load song data', 'error');
        }
    };

    const updateFileStatus = (fileType, exists) => {
        const statusEl = document.getElementById(`${fileType}-status`);
        if (statusEl) {
            statusEl.innerHTML = exists
                ? '<span class="material-icons text-emerald-400 text-base align-middle">check_circle</span>'
                : '';
        }
        const card = document.getElementById(`${fileType}-card`);
        if (card) {
            card.classList.toggle('border-emerald-500', exists);
            card.classList.toggle('border-zinc-700', !exists);
        }
    };

    // Reusable draggable-bar slider (same UX as the persona form's Mouth
    // Articulation control). Returns { setValue } or null if the DOM isn't there.
    const createRangeSlider = ({ barId, fillId, valueId, inputId, min, max, decimals = 0 }) => {
        const bar = document.getElementById(barId);
        const fill = document.getElementById(fillId);
        const input = document.getElementById(inputId);
        const valueDisplay = document.getElementById(valueId);
        if (!bar || !fill || !input || !valueDisplay) return null;

        const round = (v) => {
            const factor = 10 ** decimals;
            return Math.round(v * factor) / factor;
        };
        const format = (v) => decimals > 0 ? v.toFixed(decimals) : String(v);

        const setValue = (rawVal) => {
            const val = round(Math.min(max, Math.max(min, Number(rawVal))));
            const percent = ((val - min) / (max - min)) * 100;
            fill.style.width = `${percent}%`;
            fill.dataset.value = val;
            input.value = val;
            valueDisplay.textContent = format(val);
            return val;
        };

        let isDragging = false;
        const updateFromMouse = (e) => {
            const rect = bar.getBoundingClientRect();
            const percent = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1);
            setValue(min + percent * (max - min));
        };

        bar.addEventListener("mousedown", (e) => { isDragging = true; updateFromMouse(e); });
        document.addEventListener("mousemove", (e) => { if (isDragging) updateFromMouse(e); });
        document.addEventListener("mouseup", () => { isDragging = false; });
        input.addEventListener("input", () => setValue(input.value));

        return { setValue };
    };

    let gainSlider = null;
    let mouthArticulationSlider = null;

    const initSongSliders = () => {
        gainSlider = createRangeSlider({
            barId: 'song-gain-bar', fillId: 'song-gain-fill',
            valueId: 'song-gain-value', inputId: 'song-gain',
            min: 0.1, max: 3.0, decimals: 1,
        });
        mouthArticulationSlider = createRangeSlider({
            barId: 'song-mouth-articulation-bar', fillId: 'song-mouth-articulation-fill',
            valueId: 'song-mouth-articulation-value', inputId: 'song-mouth-articulation',
            min: 1, max: 10, decimals: 0,
        });
    };

    // Three states, saved into the same metadata string: '' -> rainbow (the
    // default), the literal string 'off' -> stays dark all song, anything
    // else -> a '#rrggbb' solid pulse color. Mode switching is explicit via
    // the Rainbow/Solid/Off tabs rather than inferred from the swatch, so a
    // picked color is remembered separately and survives round-trips through
    // Rainbow/Off without being lost.
    let songLedColorValue = '';
    let songLedRememberedColor = '#00c8ff';

    const songLedMode = () => {
        if (songLedColorValue === 'off') return 'off';
        if (!songLedColorValue) return 'rainbow';
        return 'solid';
    };

    const updateSongLedColorLabel = () => {
        const rainbow = document.getElementById('song-led-color-rainbow');
        const offBar = document.getElementById('song-led-color-off-bar');
        const input = document.getElementById('song-led-color');
        const barWrap = document.getElementById('song-led-color-bar-wrap');
        const dot = document.getElementById('song-led-color-dot');
        const labelWrap = document.getElementById('song-led-color-label-wrap');
        const modeToggle = document.getElementById('song-led-mode-toggle');
        const mode = songLedMode();

        // Only one of the three bars is ever shown; the native swatch is the
        // only one that's actually interactive (clicking it opens the OS
        // color picker), so it's the only one that stays clickable. It's
        // fully removed from layout (not just hidden) outside Solid mode -
        // Off's bar is deliberately shorter than the other two (there's
        // nothing to preview or pick), and the swatch staying in the layout
        // at its normal height would force the wrapper tall regardless.
        if (rainbow) rainbow.style.display = mode === 'rainbow' ? 'flex' : 'none';
        if (offBar) offBar.style.display = mode === 'off' ? 'block' : 'none';
        if (input) {
            input.style.display = mode === 'solid' ? '' : 'none';
            input.style.pointerEvents = mode === 'solid' ? '' : 'none';
        }
        if (barWrap) barWrap.style.height = mode === 'off' ? '10px' : '30px';

        // The dot mirrors the actual pulse color, same as the Head/Tail/Mute
        // dots mirror their own lane - falls back to neutral/muted grays for
        // Rainbow/Off since neither has a real color to preview. The label
        // text normally follows the dot too, except in Rainbow specifically:
        // with no gradient/text inside the bar anymore (the tab itself says
        // "Rainbow"), a plain white label reads better than the same muted
        // gray the dot uses.
        const dotColor = mode === 'solid' ? songLedColorValue : mode === 'off' ? '#52525b' : '#71717a';
        if (dot) dot.style.background = dotColor;
        if (labelWrap) labelWrap.style.color = mode === 'rainbow' ? '#fff' : dotColor;

        if (modeToggle) {
            modeToggle.querySelectorAll('button').forEach((b) => {
                b.classList.toggle('active', b.dataset.mode === mode);
            });
        }
    };

    const setSongLedColor = (storedValue) => {
        songLedColorValue = storedValue || '';
        if (songLedColorValue && songLedColorValue !== 'off') {
            songLedRememberedColor = songLedColorValue;
        }
        const input = document.getElementById('song-led-color');
        if (input) input.value = songLedMode() === 'solid' ? songLedColorValue : songLedRememberedColor;
        updateSongLedColorLabel();
    };

    const initSongLedColor = () => {
        const input = document.getElementById('song-led-color');
        const modeToggle = document.getElementById('song-led-mode-toggle');
        if (!input || !modeToggle) return;

        input.addEventListener('input', () => {
            songLedColorValue = input.value;
            songLedRememberedColor = input.value;
            updateSongLedColorLabel();
        });

        modeToggle.addEventListener('click', (e) => {
            const btn = e.target.closest('button');
            if (!btn) return;
            const mode = btn.dataset.mode;
            if (mode === 'rainbow') {
                songLedColorValue = '';
            } else if (mode === 'off') {
                songLedColorValue = 'off';
            } else {
                songLedColorValue = songLedRememberedColor;
                input.value = songLedRememberedColor;
            }
            updateSongLedColorLabel();
        });
    };

    const applySongLedColorVisibility = async () => {
        const field = document.getElementById('song-led-color-field');
        if (!field) return;
        const cfg = await ConfigService.fetchConfig();
        const enabled = String(cfg?.STATUS_LED_ENABLED ?? '').toLowerCase() === 'true';
        field.classList.toggle('hidden', !enabled);
    };

    // Mouth Articulation always has a value in the form (like Gain) - no
    // "override" toggle. If the song has never set one, seed the slider from
    // whichever persona is currently loaded, so the shown value matches what
    // playback would actually use until you drag the slider yourself.
    const setSongMouthArticulation = async (storedValue) => {
        if (storedValue !== '' && storedValue !== null && storedValue !== undefined) {
            mouthArticulationSlider?.setValue(storedValue);
            return;
        }
        try {
            const response = await fetch('/persona/current/mouth-articulation');
            const data = await response.json();
            mouthArticulationSlider?.setValue(data.mouth_articulation ?? 5);
        } catch (error) {
            debugLog('WARNING', 'Failed to fetch persona mouth articulation, using default:', error);
            mouthArticulationSlider?.setValue(5);
        }
    };

    const resetForm = () => {
        document.getElementById('song-edit-form').reset();
        songNameManuallyEdited = false;
        document.getElementById('song-name').value = '';
        document.getElementById('song-title').value = '';
        document.getElementById('song-keywords').value = '';
        document.getElementById('song-bpm').value = 120;
        gainSlider?.setValue(1.0);
        document.getElementById('song-tail-threshold').value = 1500;
        document.getElementById('song-compensate-tail').value = 0;
        document.getElementById('song-head-moves').value = '';
        document.getElementById('song-tail-moves').value = '';
        document.getElementById('song-mouth-mutes').value = '';
        setSongMouthArticulation('');
        setSongLedColor('');
        document.getElementById('song-half-tempo').checked = false;

        // Reset file status and clear any stale audio from a previous song
        ['full', 'vocals', 'drums'].forEach(type => {
            updateFileStatus(type, false);
            const audioElement = document.getElementById(`${type}-audio`);
            if (audioElement) { audioElement.pause(); audioElement.src = ''; }
        });
        wfResetWaveformEditorForNewSong();
    };

    const saveSong = async () => {
        const saveBtn = document.getElementById('save-song-btn');
        const saveBtnText = document.getElementById('save-song-btn-text');
        const originalText = saveBtnText.textContent;

        try {
            saveBtnText.textContent = 'Saving...';
            saveBtn.disabled = true;

            let songName = currentSong;

            // Get form data
            const formData = {
                title: document.getElementById('song-title').value,
                keywords: document.getElementById('song-keywords').value,
                bpm: parseFloat(document.getElementById('song-bpm').value),
                gain: parseFloat(document.getElementById('song-gain').value),
                tail_threshold: parseFloat(document.getElementById('song-tail-threshold').value),
                compensate_tail: parseFloat(document.getElementById('song-compensate-tail').value),
                head_moves: document.getElementById('song-head-moves').value,
                // Auto mode's tail behavior is driven live by real-time drum
                // detection server-side, never by a stored schedule - keep
                // this empty so a stale manual schedule can't linger unused.
                tail_moves: wfTailMode === 'auto' ? '' : document.getElementById('song-tail-moves').value,
                mouth_mutes: document.getElementById('song-mouth-mutes').value,
                mouth_articulation: document.getElementById('song-mouth-articulation').value,
                led_color: songLedColorValue,
                half_tempo_tail_flap: document.getElementById('song-half-tempo').checked,
            };

            if (!isEditMode) {
                // Creating new song
                songName = document.getElementById('song-name').value;
                if (!songName) {
                    showNotification('Song name is required', 'error');
                    return;
                }
                formData.name = songName;

                const response = await fetch('/songs', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(formData)
                });

                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.error || 'Failed to create song');
                }

                const result = await response.json();
                songName = result.name; // Use the sanitized name from backend
                currentSong = songName;
                isEditMode = true;
            } else {
                // Updating existing song
                const response = await fetch(`/songs/${songName}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(formData)
                });

                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.error || 'Failed to update song');
                }
            }

            // Upload audio files if selected
            await uploadAudioFiles(songName);

            showNotification(`Song '${songName}' saved successfully`, 'success');
            
            // Reload the song list
            await loadSongs();
            
            // Stay in edit mode to allow further edits
            await loadSongData(songName);

        } catch (error) {
            debugLog('ERROR', 'Failed to save song:', error);
            showNotification(error.message || 'Failed to save song', 'error');
        } finally {
            saveBtnText.textContent = originalText;
            saveBtn.disabled = false;
        }
    };

    const uploadAudioFiles = async (songName) => {
        const fileTypes = ['full', 'vocals', 'drums'];
        
        for (const fileType of fileTypes) {
            const fileInput = document.getElementById(`${fileType}-file`);
            if (fileInput.files.length > 0) {
                const file = fileInput.files[0];
                const formData = new FormData();
                formData.append('file', file);

                try {
                    const response = await fetch(`/songs/${songName}/upload/${fileType}`, {
                        method: 'POST',
                        body: formData
                    });

                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.error || `Failed to upload ${fileType}.wav`);
                    }

                    debugLog('INFO', `Uploaded ${fileType}.wav successfully`);
                    updateFileStatus(fileType, true);
                } catch (error) {
                    debugLog('ERROR', `Failed to upload ${fileType}.wav:`, error);
                    showNotification(`Failed to upload ${fileType}.wav: ${error.message}`, 'error');
                }
            }
        }
    };

    // Optimistically flips the switch and dims/undims the card immediately,
    // then reverts both if the save fails - toggling should feel instant
    // (it's a one-tap list action) but must not silently drift from the
    // server's actual state on a failed request.
    const toggleSongEnabled = async (songName, enabled) => {
        const card = document.getElementById(`song-card-${songName}`);
        card?.classList.toggle('opacity-50', !enabled);

        try {
            const response = await fetch(`/songs/${songName}/enabled`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled }),
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to update song');
            }
        } catch (error) {
            debugLog('ERROR', 'Failed to toggle song enabled state:', error);
            showNotification(error.message || 'Failed to update song', 'error');
            card?.classList.toggle('opacity-50', enabled);
            const checkbox = card?.querySelector('.wf-toggle input');
            if (checkbox) checkbox.checked = !enabled;
        }
    };

    const deleteSong = async (songName = currentSong) => {
        if (!songName) return;

        if (!confirm(`Are you sure you want to delete "${songName}"? This cannot be undone.`)) {
            return;
        }

        try {
            const response = await fetch(`/songs/${songName}`, {
                method: 'DELETE'
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to delete song');
            }

            showNotification(`Song '${songName}' deleted successfully`, 'success');
            if (songName === currentSong) {
                showListView();
            } else {
                await loadSongs();
            }
        } catch (error) {
            debugLog('ERROR', 'Failed to delete song:', error);
            showNotification(error.message || 'Failed to delete song', 'error');
        }
    };

    // Same blob + throwaway-<a> pattern as persona/profile export - fetch
    // the zip, hand the browser a temporary object URL to save it, then
    // release that URL immediately (the download itself doesn't need it to
    // stay alive).
    const downloadSong = async (songName) => {
        if (!songName) return;
        try {
            const response = await fetch(`/songs/export/${songName}`);
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to download song');
            }
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${songName}.zip`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            showNotification(`Downloaded '${songName}' song`, 'success');
        } catch (error) {
            debugLog('ERROR', 'Failed to download song:', error);
            showNotification(error.message || 'Failed to download song', 'error');
        }
    };

    // Bound to the Save Song dropdown's "Download Song" entry - mirrors
    // Profile's downloadUserProfile()/Persona's exportPersona() taking no
    // argument, since those act on the one entity that's open too.
    const downloadCurrentSong = () => downloadSong(currentSong);

    // The target song name isn't chosen up front like persona/profile
    // import - a song bundle is several files at once, created fresh from
    // the upload rather than merged into whatever's currently open, so the
    // backend derives the name from the zip's own filename (same as a new
    // song's name is derived on creation).
    const uploadSong = async (input) => {
        const file = input.files[0];
        if (!file) return;

        const formData = new FormData();
        formData.append('file', file);

        try {
            const response = await fetch('/songs/import', {
                method: 'POST',
                body: formData
            });

            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to upload song');
            }

            showNotification(`Uploaded '${result.name}' song`, 'success');
            await loadSongs();
        } catch (error) {
            debugLog('ERROR', 'Failed to upload song:', error);
            showNotification(error.message || 'Failed to upload song', 'error');
        } finally {
            input.value = '';
        }
    };

    const copyExample = async (exampleName) => {
        try {
            const response = await fetch(`/songs/copy-example/${exampleName}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to copy example');
            }

            const result = await response.json();
            showNotification(`Copied '${exampleName}' to custom songs!`, 'success');
            await loadSongs();
        } catch (error) {
            debugLog('ERROR', 'Failed to copy example:', error);
            showNotification(error.message || 'Failed to copy example', 'error');
        }
    };

    const setupAudioPreview = (fileType) => {
        const fileInput = document.getElementById(`${fileType}-file`);
        const audioElement = document.getElementById(`${fileType}-audio`);

        if (!fileInput || !audioElement) return;

        // Handle file selection - actual playback now lives entirely in the
        // waveform editor below (one shared player for all three stems).
        fileInput.addEventListener('change', (e) => {
            const file = e.target.files[0];

            if (file) {
                const url = URL.createObjectURL(file);
                audioElement.src = url;
                updateFileStatus(fileType, true);

                const statusEl = document.getElementById(`${fileType}-status`);
                if (statusEl) statusEl.title = file.name;

                wfHandleFreshUpload(fileType, file);
            } else {
                updateFileStatus(fileType, false);
            }
        });
    };

    // ===================== WAVEFORM POINTER EDITOR =====================
    // Real Web Audio API waveform rendering + Pointer Events marker editing
    // for head_moves/tail_moves/mouth_mutes, built from an approved design
    // sketch. decodeAudioData is used only to extract visual data (waveform
    // peaks, drums RMS envelope) - actual playback stays on the existing
    // native <audio> elements (already wired with .src, already reliable),
    // driven via .currentTime/.play()/.pause()/timeupdate-equivalent events.

    const WF_STEM_META = {
        vocals: { color: '#22d3ee' },
        full: { color: '#e4e4e7' },
        drums: { color: '#fbbf24' },
    };
    const WF_LANE_INPUT_IDS = {
        head: 'song-head-moves',
        tail: 'song-tail-moves',
        mute: 'song-mouth-mutes',
    };

    let wfAudioCtx = null;
    let wfStemBuffers = {};
    let wfStemAvailability = { vocals: false, full: false, drums: false };
    let wfMainStemType = null;
    let wfMainDuration = 0;
    let wfCurrentStem = null;
    let wfZoom = 1;
    let wfBaseWidth = 320;
    let wfCurrentPxWidth = 0;
    let wfMarkers = { head: [], tail: [], mute: [] };
    let wfTailMode = 'manual';
    let wfDrumsRms = null;
    let wfLoadGeneration = 0;
    let wfPlayheadRaf = null;
    let wfResizeObserver = null;

    const wfGetAudioContext = () => {
        if (!wfAudioCtx) {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            wfAudioCtx = new Ctx();
        }
        return wfAudioCtx;
    };

    const wfHexToRgba = (hex, a) => {
        const v = hex.replace('#', '');
        const r = parseInt(v.substring(0, 2), 16);
        const g = parseInt(v.substring(2, 4), 16);
        const b = parseInt(v.substring(4, 6), 16);
        return `rgba(${r},${g},${b},${a})`;
    };

    const wfDecodeStemFromUrl = async (songName, stemType) => {
        const response = await fetch(`/songs/${songName}/${stemType}.wav`);
        if (!response.ok) throw new Error(`Failed to fetch ${stemType}.wav`);
        const arrayBuffer = await response.arrayBuffer();
        const buffer = await wfGetAudioContext().decodeAudioData(arrayBuffer);
        wfStemBuffers[stemType] = buffer;
        return buffer;
    };

    const wfDecodeStemFromFile = async (stemType, file) => {
        const arrayBuffer = await file.arrayBuffer();
        const buffer = await wfGetAudioContext().decodeAudioData(arrayBuffer);
        wfStemBuffers[stemType] = buffer;
        return buffer;
    };

    const wfComputeMainStemType = () => {
        if (wfStemBuffers.full) return 'full';
        if (wfStemBuffers.vocals) return 'vocals';
        return null;
    };

    // ---- Waveform + ruler drawing ----

    // Buckets by real elapsed time against `wfMainDuration`, not by the
    // buffer's own sample length - a stem (e.g. an extracted drums track)
    // can be shorter or longer than the main timeline, and bucketing by its
    // own length would stretch/compress it to visually fill the full width,
    // silently misaligning it with the ruler and with the time-based Tail
    // RMS reference below (which already maps against wfMainDuration).
    const wfGetPeaks = (buffer, count, mainDuration) => {
        const channelData = buffer.getChannelData(0);
        const sampleRate = buffer.sampleRate;
        const duration = mainDuration || (channelData.length / sampleRate);
        const peaks = new Float32Array(count);
        for (let i = 0; i < count; i++) {
            const start = Math.floor((i / count) * duration * sampleRate);
            const end = Math.min(channelData.length, Math.floor(((i + 1) / count) * duration * sampleRate));
            let max = 0;
            for (let j = start; j < end; j++) {
                const abs = Math.abs(channelData[j]);
                if (abs > max) max = abs;
            }
            peaks[i] = max;
        }
        return peaks;
    };

    const wfDrawWaveform = () => {
        const canvas = document.getElementById('wf-wave');
        if (!canvas || !wfCurrentPxWidth) return;
        const ctx = canvas.getContext('2d');
        const cssHeight = 72;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.round(wfCurrentPxWidth * dpr));
        canvas.height = Math.max(1, Math.round(cssHeight * dpr));
        canvas.style.width = wfCurrentPxWidth + 'px';
        canvas.style.height = cssHeight + 'px';
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, wfCurrentPxWidth, cssHeight);

        const buffer = wfCurrentStem ? wfStemBuffers[wfCurrentStem] : null;
        if (!buffer) return;

        const barCount = Math.max(20, Math.round(160 * (wfCurrentPxWidth / wfBaseWidth)));
        const peaks = wfGetPeaks(buffer, barCount, wfMainDuration);
        const color = WF_STEM_META[wfCurrentStem]?.color || '#22d3ee';
        const barWidth = Math.max(1, wfCurrentPxWidth / barCount - 1);
        ctx.fillStyle = wfHexToRgba(color, 0.55);
        for (let i = 0; i < barCount; i++) {
            const amp = Math.max(0.03, peaks[i]);
            const h = amp * cssHeight;
            const x = (i / barCount) * wfCurrentPxWidth;
            ctx.fillRect(x, (cssHeight - h) / 2, barWidth, h);
        }
    };

    // "Nice" tick intervals to choose from so labels never crowd regardless
    // of song length or zoom - a 12s clip and a 3-minute song both get a
    // readable ruler instead of the same fixed 2s spacing overlapping badly
    // on the longer one.
    const WF_RULER_INTERVALS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    const WF_RULER_MIN_PX_PER_TICK = 56;

    const wfBuildRuler = () => {
        const ruler = document.getElementById('wf-ruler');
        if (!ruler || !wfMainDuration) return;
        ruler.innerHTML = '';
        const duration = wfMainDuration;
        const pxWidth = wfCurrentPxWidth || wfBaseWidth;
        let interval = WF_RULER_INTERVALS[WF_RULER_INTERVALS.length - 1];
        for (const candidate of WF_RULER_INTERVALS) {
            if ((candidate / duration) * pxWidth >= WF_RULER_MIN_PX_PER_TICK) {
                interval = candidate;
                break;
            }
        }
        const ticks = [];
        for (let t = 0; t <= duration; t += interval) ticks.push(t);
        // The real end time is always its own tick; drop the nearest regular
        // one if it would land too close to it, so labels don't crowd.
        if (ticks.length && duration - ticks[ticks.length - 1] < interval * 0.4) ticks.pop();
        ticks.push(duration);
        ticks.forEach((t) => {
            const el = document.createElement('span');
            el.style.left = (t / duration * 100) + '%';
            el.textContent = (Math.round(t * 10) / 10) + 's';
            if (t === 0) el.style.transform = 'translateX(0)';
            else if (t === duration) el.style.transform = 'translateX(-100%)';
            ruler.appendChild(el);
        });
    };

    // ---- Drums RMS envelope (Tail Auto reference) ----

    const wfComputeDrumsRms = () => {
        const buffer = wfStemBuffers.drums;
        if (!buffer) { wfDrumsRms = null; return; }
        const channelData = buffer.getChannelData(0);
        const windowSec = 0.05;
        const windowSize = Math.max(1, Math.floor(windowSec * buffer.sampleRate));
        const numWindows = Math.ceil(channelData.length / windowSize);
        const values = new Float32Array(numWindows);
        for (let w = 0; w < numWindows; w++) {
            const start = w * windowSize;
            const end = Math.min(channelData.length, start + windowSize);
            let sumSq = 0;
            for (let i = start; i < end; i++) sumSq += channelData[i] * channelData[i];
            values[w] = Math.sqrt(sumSq / Math.max(1, end - start));
        }
        // Normalize to this drum stem's own peak so the envelope always uses
        // the full visual range regardless of this song's mix level.
        let max = 0;
        for (let w = 0; w < numWindows; w++) if (values[w] > max) max = values[w];
        if (max > 0) for (let w = 0; w < numWindows; w++) values[w] /= max;
        // core/audio.py compares drum RMS against tail_threshold on a raw
        // int16-equivalent scale (see calculate_input_rms there), not this
        // envelope's own 0-1 normalized scale - keep the conversion factor
        // so the threshold line can be placed on the same normalized axis.
        wfDrumsRms = { windowSec, values, maxRmsInt16: max * 32768 };
    };

    const wfRenderRmsInto = (track) => {
        track.querySelectorAll('.wf-rms-bar, .wf-threshold-line').forEach(n => n.remove());
        if (!wfDrumsRms || !wfMainDuration) return;
        const w = wfCurrentPxWidth;
        const trackHeight = track.clientHeight || 30;
        const maxBar = Math.max(4, trackHeight - 4);
        const { windowSec, values, maxRmsInt16 } = wfDrumsRms;
        // One bar per pixel column - at 3px steps this undersampled the
        // 20-samples/sec envelope at low zoom (each bar silently skipped
        // 2 of every 3 windows), which is why it looked sparser than the
        // real data until you zoomed in far enough for 3px to cover less
        // than one window.
        for (let x = 0; x < w - 1; x += 1) {
            const t = (x / w) * wfMainDuration;
            const idx = Math.min(values.length - 1, Math.floor(t / windowSec));
            const amp = values[idx] || 0;
            const bar = document.createElement('div');
            bar.className = 'wf-rms-bar';
            bar.style.left = x + 'px';
            bar.style.height = Math.max(2, amp * maxBar) + 'px';
            track.appendChild(bar);
        }

        const thresholdInput = document.getElementById('song-tail-threshold');
        const threshold = thresholdInput ? parseFloat(thresholdInput.value.replace(',', '.')) : NaN;
        if (!isNaN(threshold) && maxRmsInt16 > 0) {
            const frac = Math.max(0, Math.min(1, threshold / maxRmsInt16));
            const line = document.createElement('div');
            line.className = 'wf-threshold-line';
            line.style.bottom = (2 + frac * maxBar) + 'px';
            line.title = `Tail Threshold: ${threshold}`;
            track.appendChild(line);
        }
    };

    // ---- Marker lanes (Head / Tail / Mute mouth) ----

    const wfParseMoves = (str) => {
        if (!str) return [];
        return str.split(',').map(s => s.trim()).filter(Boolean).map((pair) => {
            const parts = pair.split(':');
            const start = parseFloat(parts[0]);
            const dur = parseFloat(parts[1]);
            return { start: isNaN(start) ? 0 : start, dur: isNaN(dur) ? 0.1 : dur };
        }).filter(m => m.dur > 0);
    };

    const wfFormatMoves = (markers) => markers.slice()
        .sort((a, b) => a.start - b.start)
        .map(m => `${m.start.toFixed(1)}:${m.dur.toFixed(1)}`)
        .join(', ');

    const wfSyncHiddenInput = (lane) => {
        const input = document.getElementById(WF_LANE_INPUT_IDS[lane]);
        if (input) input.value = wfFormatMoves(wfMarkers[lane]);
    };

    // Two pointers can never coexist in a lane - rather than blocking a
    // drag/resize that would overlap a neighbor, let it happen and fold the
    // overlapping markers into one spanning their union once the gesture
    // settles (drag/resize release, or a new marker landing on an existing
    // one). Called with the index of the marker that just moved/resized/was
    // added; mutates wfMarkers[lane] in place.
    const wfMergeOverlaps = (lane, idx) => {
        const markers = wfMarkers[lane];
        const target = markers[idx];
        if (!target) return;
        let start = target.start;
        let end = target.start + target.dur;
        let merged = false;
        const survivors = [];
        for (let i = 0; i < markers.length; i++) {
            if (i === idx) continue;
            const m = markers[i];
            const mEnd = m.start + m.dur;
            if (m.start < end && mEnd > start) {
                start = Math.min(start, m.start);
                end = Math.max(end, mEnd);
                merged = true;
            } else {
                survivors.push(m);
            }
        }
        if (!merged) return;
        survivors.push({
            start: Math.round(start * 10) / 10,
            dur: Math.round((end - start) * 10) / 10,
        });
        survivors.sort((a, b) => a.start - b.start);
        wfMarkers[lane] = survivors;
    };

    // Full-lane version of the above, for data that may already contain
    // overlaps from before this editor existed (hand-typed into the old
    // plain-text Head/Tail/Mouth Mute fields, which never enforced this).
    const wfNormalizeOverlaps = (lane) => {
        const sorted = wfMarkers[lane].slice().sort((a, b) => a.start - b.start);
        const merged = [];
        for (const m of sorted) {
            const last = merged[merged.length - 1];
            if (last && m.start < last.start + last.dur) {
                const end = Math.max(last.start + last.dur, m.start + m.dur);
                last.dur = Math.round((end - last.start) * 10) / 10;
            } else {
                merged.push({ start: m.start, dur: m.dur });
            }
        }
        wfMarkers[lane] = merged;
    };

    const wfRenderLane = (lane) => {
        const track = document.getElementById(`wf-track-${lane}`);
        if (!track) return;
        track.querySelectorAll('.wf-marker').forEach(n => n.remove());

        const isTailAuto = lane === 'tail' && wfTailMode === 'auto';
        track.dataset.readonly = isTailAuto ? 'true' : 'false';

        if (isTailAuto) {
            wfRenderRmsInto(track);
            return;
        }
        track.querySelectorAll('.wf-rms-bar, .wf-threshold-line').forEach(n => n.remove());

        const w = wfCurrentPxWidth;
        const duration = wfMainDuration;
        if (!duration) return;

        wfMarkers[lane].forEach((m, idx) => {
            const el = document.createElement('div');
            el.className = 'wf-marker';
            el.style.left = (m.start / duration * w) + 'px';
            el.style.width = Math.max(6, m.dur / duration * w) + 'px';

            el.title = 'Double-click to remove';

            const handle = document.createElement('div');
            handle.className = 'wf-handle';
            el.appendChild(handle);
            track.appendChild(el);

            // renderLane() rebuilds this marker's DOM node every frame while
            // dragging, so listeners must live on document (not el/handle) -
            // a node removed mid-drag silently stops receiving events.
            el.addEventListener('pointerdown', (e) => {
                // Double-click/double-tap removes the marker. This is
                // detected here (pointerdown timing on m itself, which
                // survives the re-render below) rather than via a native
                // 'dblclick' listener - preventDefault() a few lines down is
                // needed to stop the browser's own drag/text-selection
                // handling, but doing so also suppresses the compatibility
                // click/dblclick events the browser would otherwise
                // synthesize from pointer events, so 'dblclick' would never
                // actually fire here.
                const now = Date.now();
                if (m._wfLastPointerDownAt && now - m._wfLastPointerDownAt < 400) {
                    m._wfLastPointerDownAt = 0;
                    wfMarkers[lane].splice(idx, 1);
                    wfRenderLane(lane);
                    wfSyncHiddenInput(lane);
                    return;
                }
                m._wfLastPointerDownAt = now;

                e.preventDefault();
                if (e.target === handle) {
                    const startX = e.clientX;
                    const startDur = m.dur;
                    const onMove = (ev) => {
                        const dx = ev.clientX - startX;
                        const newDur = Math.max(0.15, Math.min(duration - m.start, startDur + dx / w * duration));
                        m.dur = Math.round(newDur * 10) / 10;
                        wfRenderLane(lane);
                        wfSyncHiddenInput(lane);
                    };
                    const onUp = () => {
                        document.removeEventListener('pointermove', onMove);
                        document.removeEventListener('pointerup', onUp);
                        wfMergeOverlaps(lane, idx);
                        wfRenderLane(lane);
                        wfSyncHiddenInput(lane);
                    };
                    document.addEventListener('pointermove', onMove);
                    document.addEventListener('pointerup', onUp);
                    e.stopPropagation();
                } else {
                    const startX = e.clientX;
                    const startStart = m.start;
                    const onMove = (ev) => {
                        const dx = ev.clientX - startX;
                        const newStart = Math.max(0, Math.min(duration - m.dur, startStart + dx / w * duration));
                        m.start = Math.round(newStart * 10) / 10;
                        wfRenderLane(lane);
                        wfSyncHiddenInput(lane);
                    };
                    const onUp = () => {
                        document.removeEventListener('pointermove', onMove);
                        document.removeEventListener('pointerup', onUp);
                        wfMergeOverlaps(lane, idx);
                        wfRenderLane(lane);
                        wfSyncHiddenInput(lane);
                    };
                    document.addEventListener('pointermove', onMove);
                    document.addEventListener('pointerup', onUp);
                }
            });
        });
    };

    const wfRenderAllLanes = () => {
        wfRenderLane('head');
        wfRenderLane('tail');
        wfRenderLane('mute');
    };

    const wfBindLaneClickToAdd = () => {
        ['head', 'tail', 'mute'].forEach((lane) => {
            const track = document.getElementById(`wf-track-${lane}`);
            if (!track) return;
            track.addEventListener('click', (e) => {
                if (lane === 'tail' && wfTailMode === 'auto') return;
                if (e.target !== track) return;
                if (!wfMainDuration) return;
                const rect = track.getBoundingClientRect();
                const t = Math.max(0, Math.min(wfMainDuration - 0.6, (e.clientX - rect.left) / rect.width * wfMainDuration));
                wfMarkers[lane].push({ start: Math.round(t * 10) / 10, dur: 0.6 });
                wfMergeOverlaps(lane, wfMarkers[lane].length - 1);
                wfRenderLane(lane);
                wfSyncHiddenInput(lane);
            });
        });
    };

    // ---- Zoom ----

    const wfApplyZoom = () => {
        const editorScroll = document.getElementById('wf-editor-scroll');
        const editorInner = document.getElementById('wf-editor-inner');
        const ruler = document.getElementById('wf-ruler');
        if (!editorScroll || !editorInner || !ruler) return;

        wfBaseWidth = Math.max(320, editorScroll.clientWidth);
        // clientWidth can be fractional (sub-pixel layout, high-DPI, etc.);
        // Math.round() would sometimes round the zoomed width up past what
        // actually fits, leaving a 1px overflow that shows as a scrollbar
        // with almost no real travel even at zoom 1.0x. floor() guarantees
        // it never exceeds the available space.
        wfCurrentPxWidth = Math.floor(wfBaseWidth * wfZoom);

        // editor-inner must carry the same explicit width as its zoomed
        // children - otherwise it never actually grows as their containing
        // block, and the sticky Tail Auto/Manual toggle has no real box to
        // stick within (it just scrolls away instead of pinning to the edge).
        editorInner.style.width = wfCurrentPxWidth + 'px';
        ruler.style.width = wfCurrentPxWidth + 'px';

        wfDrawWaveform();
        wfBuildRuler();
        wfRenderAllLanes();

        // The playhead's pixel offset is only valid for the pxWidth it was
        // last computed against - recompute it from the actual current
        // playback position so it doesn't visually drift after a zoom change.
        const currentEl = wfCurrentStem ? wfAudioElementFor(wfCurrentStem) : null;
        wfSetPlayheadPosition(currentEl ? currentEl.currentTime : 0);

        const zoomLabel = document.getElementById('wf-zoom-label');
        if (zoomLabel) zoomLabel.textContent = wfZoom.toFixed(1) + '×';
    };

    const wfInitZoomControls = () => {
        const range = document.getElementById('wf-zoom-range');
        const outBtn = document.getElementById('wf-zoom-out');
        const inBtn = document.getElementById('wf-zoom-in');
        if (!range || !outBtn || !inBtn) return;
        range.addEventListener('input', () => { wfZoom = Number(range.value); wfApplyZoom(); });
        outBtn.addEventListener('click', () => {
            wfZoom = Math.max(1, Math.round((wfZoom - 0.5) * 10) / 10);
            range.value = wfZoom; wfApplyZoom();
        });
        inBtn.addEventListener('click', () => {
            wfZoom = Math.min(6, Math.round((wfZoom + 0.5) * 10) / 10);
            range.value = wfZoom; wfApplyZoom();
        });
    };

    // The editor can initialize while its ancestor is at clientWidth 0 -
    // inside #song-edit-view's hidden toggle, the mobile split-view's hidden
    // detail pane, or this collapsible section - any of which would
    // otherwise lock the waveform at the 320px floor forever.
    const wfSetupResizeObserver = () => {
        const editorScroll = document.getElementById('wf-editor-scroll');
        if (!editorScroll || wfResizeObserver || typeof ResizeObserver === 'undefined') return;
        wfResizeObserver = new ResizeObserver(() => {
            if (document.getElementById('wf-editor-body')?.classList.contains('hidden')) return;
            wfApplyZoom();
        });
        wfResizeObserver.observe(editorScroll);
    };

    // ---- Playback ----

    const wfAudioElementFor = (stemType) => document.getElementById(`${stemType}-audio`);

    const wfPauseAllStems = (except = null) => {
        ['vocals', 'full', 'drums'].forEach((type) => {
            const el = wfAudioElementFor(type);
            if (el && el !== except && !el.paused) el.pause();
        });
    };

    const wfUpdatePlayIcon = () => {
        const icon = document.getElementById('wf-play-icon');
        if (!icon) return;
        const el = wfCurrentStem ? wfAudioElementFor(wfCurrentStem) : null;
        icon.textContent = (el && !el.paused && !el.ended) ? 'pause' : 'play_arrow';
    };

    // One playhead line above the waveform, plus one inside each lane track
    // (so it's easy to line a marker up against the current position) - all
    // driven from the same source of truth and kept in lockstep.
    const WF_PLAYHEAD_IDS = ['wf-playhead', 'wf-playhead-head', 'wf-playhead-tail', 'wf-playhead-mute'];

    const wfSetPlayheadPosition = (t) => {
        if (!wfMainDuration) return;
        const clamped = Math.max(0, Math.min(wfMainDuration, t));
        const left = (clamped / wfMainDuration * wfCurrentPxWidth) + 'px';
        WF_PLAYHEAD_IDS.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.style.left = left;
        });
    };

    const wfStopPlayheadLoop = () => {
        if (wfPlayheadRaf) cancelAnimationFrame(wfPlayheadRaf);
        wfPlayheadRaf = null;
    };

    const wfStartPlayheadLoop = () => {
        wfStopPlayheadLoop();
        const tick = () => {
            const el = wfCurrentStem ? wfAudioElementFor(wfCurrentStem) : null;
            if (el) wfSetPlayheadPosition(el.currentTime);
            wfPlayheadRaf = requestAnimationFrame(tick);
        };
        wfPlayheadRaf = requestAnimationFrame(tick);
    };

    const wfBindPlaybackControls = () => {
        const playBtn = document.getElementById('wf-play-btn');
        const canvas = document.getElementById('wf-wave');

        playBtn?.addEventListener('click', () => {
            const el = wfCurrentStem ? wfAudioElementFor(wfCurrentStem) : null;
            if (!el) return;
            if (el.paused || el.ended) {
                wfPauseAllStems(el);
                el.play();
            } else {
                el.pause();
            }
        });

        // Clicking the waveform always repositions the playhead. It only
        // starts playback if this stem was already playing (i.e. it's a
        // scrub-while-playing) - if it was paused, it stays paused so you
        // can position it without the song immediately taking off.
        canvas?.addEventListener('click', (e) => {
            const el = wfCurrentStem ? wfAudioElementFor(wfCurrentStem) : null;
            if (!el || !wfMainDuration) return;
            const rect = canvas.getBoundingClientRect();
            if (!rect.width) return;
            const t = Math.max(0, Math.min(wfMainDuration - 0.05, (e.clientX - rect.left) / rect.width * wfMainDuration));
            const wasPlaying = !el.paused && !el.ended;
            wfPauseAllStems(el);
            // The playhead always jumps to the clicked position immediately,
            // regardless of this element's own readiness - it's driven by
            // `t`, not by reading back `el.currentTime`.
            wfSetPlayheadPosition(t);
            const seek = () => {
                // A non-main stem (e.g. an extracted vocals/drums file) can be
                // a hair shorter than wfMainDuration even when it represents
                // the same song - clamp to its own duration too, since some
                // browsers silently ignore (rather than clamp) a seek past
                // the end of a shorter track.
                const target = Number.isFinite(el.duration) ? Math.min(t, Math.max(0, el.duration - 0.05)) : t;
                el.currentTime = target;
                if (wasPlaying) el.play();
            };
            if (el.readyState >= 1) {
                // HAVE_METADATA or better - duration/seeking already reliable.
                seek();
            } else {
                // Metadata (and therefore `el.duration`) isn't loaded yet -
                // setting currentTime now can silently fail to stick in some
                // browsers instead of queuing, so wait for it explicitly.
                el.addEventListener('loadedmetadata', seek, { once: true });
            }
        });

        ['vocals', 'full', 'drums'].forEach((type) => {
            const el = wfAudioElementFor(type);
            if (!el) return;
            el.addEventListener('play', () => {
                if (wfCurrentStem === type) { wfUpdatePlayIcon(); wfStartPlayheadLoop(); }
            });
            el.addEventListener('pause', () => {
                if (wfCurrentStem === type) { wfUpdatePlayIcon(); wfStopPlayheadLoop(); }
            });
            el.addEventListener('ended', () => {
                if (wfCurrentStem === type) {
                    wfUpdatePlayIcon();
                    wfStopPlayheadLoop();
                    wfSetPlayheadPosition(0);
                }
            });
        });
    };

    // ---- Stem tabs ----

    const wfSelectStemTab = async (stemType) => {
        if (!wfStemAvailability[stemType]) return;

        // Carry position (and playing state) across the switch, so picking a
        // different stem mid-song continues seamlessly from the same point -
        // e.g. hearing a section as Full Mix, then flipping to Drums to check
        // what's driving the tail there - instead of stopping playback and
        // losing your place.
        const prevEl = wfCurrentStem ? wfAudioElementFor(wfCurrentStem) : null;
        const wasPlaying = !!(prevEl && !prevEl.paused && !prevEl.ended);
        const resumeAt = prevEl ? prevEl.currentTime : 0;

        wfPauseAllStems();
        wfCurrentStem = stemType;
        document.querySelectorAll('#wf-stem-tabs button').forEach((btn) => {
            const active = btn.dataset.stem === stemType;
            btn.classList.toggle('active', active);
            btn.style.color = active ? (WF_STEM_META[stemType]?.color || '') : '';
        });
        const playBtn = document.getElementById('wf-play-btn');
        if (playBtn) playBtn.setAttribute('aria-label', `Play ${stemType}.wav`);
        wfUpdatePlayIcon();

        if (!wfStemBuffers[stemType] && currentSong) {
            try {
                await wfDecodeStemFromUrl(currentSong, stemType);
            } catch (error) {
                debugLog('WARNING', `Failed to decode ${stemType} for preview:`, error);
            }
        }
        wfDrawWaveform();

        const newEl = wfAudioElementFor(stemType);
        if (newEl && prevEl) {
            const seekAndMaybePlay = () => {
                const target = Number.isFinite(newEl.duration)
                    ? Math.min(resumeAt, Math.max(0, newEl.duration - 0.05))
                    : resumeAt;
                newEl.currentTime = target;
                wfSetPlayheadPosition(target);
                if (wasPlaying) newEl.play();
            };
            if (newEl.readyState >= 1) {
                seekAndMaybePlay();
            } else {
                // Metadata isn't loaded yet - setting currentTime now can
                // silently fail to stick in some browsers, so wait for it
                // (same guard as the waveform-click seek handler above).
                newEl.addEventListener('loadedmetadata', seekAndMaybePlay, { once: true });
            }
        }
    };

    const wfUpdateStemTabsAvailability = () => {
        document.querySelectorAll('#wf-stem-tabs button').forEach((btn) => {
            btn.disabled = !wfStemAvailability[btn.dataset.stem];
        });
        if ((!wfCurrentStem || !wfStemAvailability[wfCurrentStem]) && wfMainStemType) {
            wfSelectStemTab(wfMainStemType);
        }
    };

    const wfInitStemTabs = () => {
        document.getElementById('wf-stem-tabs')?.addEventListener('click', (e) => {
            const btn = e.target.closest('button');
            if (!btn || btn.disabled) return;
            wfSelectStemTab(btn.dataset.stem);
        });
    };

    // ---- Tail Auto/Manual ----

    const wfUpdateTailModeVisibility = () => {
        const toggle = document.getElementById('wf-tail-mode-toggle');
        if (!toggle) return;
        const hasDrums = !!wfStemAvailability.drums;
        // .wf-lane-mode is unlayered CSS (not inside a Tailwind @layer), so
        // it beats the .hidden utility's `display: none` in the cascade -
        // toggle the style directly instead of fighting that with a class
        // (same fix already used for the LED color Clear button).
        toggle.style.display = hasDrums ? '' : 'none';
        if (!hasDrums) wfTailMode = 'manual';
        toggle.querySelectorAll('button').forEach((b) => {
            b.classList.toggle('active', b.dataset.mode === wfTailMode);
        });
        // The BPM/threshold/compensate fields only mean anything while the
        // automatic drum-driven tail flap is actually active - showing them
        // only in that mode makes the "drums stem only" caveat self-evident
        // instead of needing its own tooltip.
        document.getElementById('wf-tail-advanced')?.classList.toggle('hidden', wfTailMode !== 'auto');
    };

    const wfInitTailModeToggle = () => {
        document.getElementById('wf-tail-mode-toggle')?.addEventListener('click', (e) => {
            const btn = e.target.closest('button');
            if (!btn) return;
            wfTailMode = btn.dataset.mode;
            wfUpdateTailModeVisibility();
            wfRenderLane('tail');
        });
        // Keep the threshold line on the Tail Auto envelope in sync as you
        // tune the value, so you can see where it'll cross the drum hits
        // without saving and reopening the song.
        document.getElementById('song-tail-threshold')?.addEventListener('input', () => {
            if (wfTailMode === 'auto') wfRenderLane('tail');
        });
    };

    // ---- Lifecycle glue ----

    const wfShowEmptyState = () => {
        document.getElementById('wf-empty-state')?.classList.remove('hidden');
        document.getElementById('wf-editor-body')?.classList.add('hidden');
        // .wf-play-row sets `display: flex` unconditionally (unlayered CSS
        // beats the .hidden utility), so it needs the same inline-style
        // toggle as .wf-lane-mode above rather than a class.
        const playRow = document.getElementById('wf-play-row');
        if (playRow) playRow.style.display = 'none';
    };

    const wfShowEditorBody = () => {
        document.getElementById('wf-empty-state')?.classList.add('hidden');
        document.getElementById('wf-editor-body')?.classList.remove('hidden');
        const playRow = document.getElementById('wf-play-row');
        if (playRow) playRow.style.display = '';
    };

    const wfResetState = () => {
        wfStemBuffers = {};
        wfStemAvailability = { vocals: false, full: false, drums: false };
        wfMainStemType = null;
        wfMainDuration = 0;
        wfCurrentStem = null;
        wfMarkers = { head: [], tail: [], mute: [] };
        wfTailMode = 'manual';
        wfDrumsRms = null;
        wfStopPlayheadLoop();
        // wfMainDuration is 0 at this point, so wfSetPlayheadPosition() would
        // no-op - reset every playhead element directly instead.
        WF_PLAYHEAD_IDS.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.style.left = '0px';
        });
        wfUpdatePlayIcon();
    };

    const wfResetWaveformEditorForNewSong = () => {
        wfLoadGeneration++;
        wfResetState();
        wfUpdateStemTabsAvailability();
        wfUpdateTailModeVisibility();
        wfShowEmptyState();
    };

    const wfLoadWaveformEditorForSong = async (songName, song) => {
        const myGeneration = ++wfLoadGeneration;
        wfResetState();

        wfStemAvailability = {
            vocals: !!song.has_vocals,
            full: !!song.has_full,
            drums: !!song.has_drums,
        };
        wfMarkers = {
            head: wfParseMoves(song.head_moves),
            tail: wfParseMoves(song.tail_moves),
            mute: wfParseMoves(song.mouth_mutes),
        };
        ['head', 'tail', 'mute'].forEach(wfNormalizeOverlaps);
        // Preserve pre-existing manual tail data rather than silently
        // defaulting to Auto and orphaning it - only default to Auto when
        // there's a drums stem and no manual tail schedule yet.
        wfTailMode = (wfStemAvailability.drums && wfMarkers.tail.length === 0) ? 'auto' : 'manual';

        wfUpdateStemTabsAvailability();
        wfUpdateTailModeVisibility();

        if (!wfStemAvailability.vocals && !wfStemAvailability.full) {
            wfShowEmptyState();
            return;
        }
        wfShowEditorBody();

        try {
            const mainType = wfStemAvailability.full ? 'full' : 'vocals';
            await wfDecodeStemFromUrl(songName, mainType);
            if (myGeneration !== wfLoadGeneration) return; // stale - user moved on

            wfMainStemType = wfComputeMainStemType();
            wfMainDuration = wfStemBuffers[wfMainStemType].duration;
            await wfSelectStemTab(mainType);
            if (myGeneration !== wfLoadGeneration) return;

            wfApplyZoom();

            if (wfStemAvailability.drums) {
                wfDecodeStemFromUrl(songName, 'drums').then(() => {
                    if (myGeneration !== wfLoadGeneration) return;
                    wfComputeDrumsRms();
                    wfRenderLane('tail');
                }).catch((error) => {
                    debugLog('WARNING', 'Failed to decode drums stem for waveform:', error);
                });
            }
        } catch (error) {
            debugLog('ERROR', 'Failed to decode audio for waveform editor:', error);
            wfShowEmptyState();
        }
    };

    const wfHandleFreshUpload = async (fileType, file) => {
        try {
            await wfDecodeStemFromFile(fileType, file);
            wfStemAvailability[fileType] = true;
            wfUpdateStemTabsAvailability();
            wfUpdateTailModeVisibility();

            wfMainStemType = wfComputeMainStemType();
            if (wfMainStemType) {
                wfMainDuration = wfStemBuffers[wfMainStemType].duration;
                wfShowEditorBody();
                await wfSelectStemTab(fileType);
                wfApplyZoom();
            }
            if (fileType === 'drums') {
                wfComputeDrumsRms();
                wfRenderLane('tail');
            }
        } catch (error) {
            debugLog('WARNING', `Failed to decode uploaded ${fileType} for waveform preview:`, error);
        }
    };

    const wfInitWaveformEditor = () => {
        wfInitStemTabs();
        wfInitZoomControls();
        wfBindPlaybackControls();
        wfBindLaneClickToAdd();
        wfInitTailModeToggle();
        wfSetupResizeObserver();
    };

    const initSongNameAutoDerive = () => {
        const titleInput = document.getElementById('song-title');
        const nameInput = document.getElementById('song-name');
        titleInput?.addEventListener('input', () => {
            if (isEditMode || songNameManuallyEdited) return;
            nameInput.value = slugifySongName(titleInput.value);
        });
        nameInput?.addEventListener('input', () => {
            songNameManuallyEdited = true;
        });
    };

    const init = () => {
        const isSongsPage = !!document.getElementById('songs-grid');
        const createBtn = document.getElementById('create-song-btn');

        // #main-content gets replaced wholesale on normal SPA navigation (fresh
        // DOM => must bind), but init() can also be re-run against the *same*
        // DOM (e.g. after a reconnect) => must not double-bind. dataset.bound
        // lives on the element itself so it naturally resets on a real swap.
        if (createBtn && createBtn.dataset.bound !== 'true') {
            createBtn.dataset.bound = 'true';

            // View navigation
            createBtn.addEventListener('click', () => showEditView(null));
            document.getElementById('back-to-songs-list-btn')?.addEventListener('click', showListView);

            // Form actions
            document.getElementById('save-song-btn')?.addEventListener('click', saveSong);

            // Audio preview controls
            setupAudioPreview('full');
            setupAudioPreview('vocals');
            setupAudioPreview('drums');

            initSongSliders();
            initSongLedColor();
            applySongLedColorVisibility();
            initSongNameAutoDerive();
            wfInitWaveformEditor();
        }

        if (isSongsPage) {
            showListView();
            loadSongs();
        }
    };

    return {
        init,
        editSong: showEditView,
        loadSongs,
        copyExample,
        deleteSong,
        toggleSongEnabled,
        downloadSong,
        downloadCurrentSong,
        uploadSong
    };
})();

// Make it globally accessible
window.SongsManager = SongsManager;
