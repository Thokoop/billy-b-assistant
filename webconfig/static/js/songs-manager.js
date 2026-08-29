// ===================== SONGS MANAGER =====================
const SongsManager = (() => {
    let currentSong = null;
    let isEditMode = false;

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

    const showListView = () => {
        document.getElementById('songs-list-view')?.classList.remove('hidden');
        document.getElementById('song-edit-view')?.classList.add('hidden');
        document.getElementById('song-edit-footer')?.classList.add('hidden');
        document.getElementById('song-edit-empty-state')?.classList.remove('hidden');
        window.MobileSplitView?.showList('songs-split-view');
        
        // Update header
        document.getElementById('back-to-songs-list-btn')?.classList.add('hidden');
        const title = document.getElementById('songs-modal-title');
        if (title) title.textContent = 'Song Details';
        
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

        // Update header
        document.getElementById('back-to-songs-list-btn')?.classList.remove('hidden');
        const title = document.getElementById('songs-modal-title');
        if (title) title.textContent = isEditMode ? 'Edit Song' : 'New Song';

        // Show/hide song name field (only for new songs)
        const songNameField = document.getElementById('song-name-field');
        const songNameInput = document.getElementById('song-name');
        const deleteBtn = document.getElementById('delete-song-btn');
        
        if (isEditMode) {
            songNameField.classList.add('hidden');
            songNameInput.removeAttribute('required');
            deleteBtn.classList.remove('hidden');
        } else {
            songNameField.classList.remove('hidden');
            songNameInput.setAttribute('required', 'required');
            deleteBtn.classList.add('hidden');
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

        grid.innerHTML = songs.map(song => {
            // Vocals is the only required stem - Full Mix and Drums are optional.
            const hasRequiredFiles = !!song.has_vocals;
            const isExample = song.is_example || false;
            const isSelected = currentSong === song.name;

            const statusIcon = hasRequiredFiles ?
                '<span class="material-icons text-emerald-400 text-sm" title="Ready to play">check_circle</span>' :
                '<span class="material-icons text-amber-400 text-sm" title="Missing required Vocals stem">warning</span>';
            
            // Example songs have a different style and action
            if (isExample) {
                return `
                    <div class="bg-zinc-800/50 border border-amber-600/50 rounded-lg p-4 hover:border-amber-500 transition-colors">
                        <div class="flex items-start justify-between mb-2">
                            <div class="flex-1">
                                <div class="flex items-center gap-2">
                                    <h4 class="text-white">${song.title}</h4>
                                    <span class="text-xs bg-amber-600/20 text-amber-400 px-2 py-0.5 rounded">Example</span>
                                </div>
                                <p class="text-xs text-zinc-500">${song.name}</p>
                            </div>
                            ${statusIcon}
                        </div>
                        ${song.keywords ? `
                            <p class="text-sm text-zinc-400 mb-2">${song.keywords}</p>
                        ` : ''}
                        <div class="flex gap-2 text-xs text-zinc-500 mb-3">
                            <span>${song.bpm} BPM</span>
                            <span>•</span>
                            <span>Gain: ${song.gain}</span>
                        </div>
                        <button onclick="window.SongsManager.copyExample('${song.name}')" 
                                class="w-full bg-amber-600 text-white text-sm py-2 px-3 rounded flex items-center justify-center gap-2 transition-colors">
                            <span class="material-icons text-sm">content_copy</span>
                            Copy to Custom Songs
                        </button>
                    </div>
                `;
            }
            
            return `
                <div class="${isSelected ? 'bg-emerald-500/10 border-emerald-500 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]' : 'bg-zinc-800 border-zinc-700 hover:border-emerald-500'} border rounded-lg p-4 transition-colors cursor-pointer"
                     onclick="window.SongsManager.editSong('${song.name}')">
                    <div class="flex items-start justify-between mb-2">
                        <div class="flex-1">
                            <h4 class="text-white">${song.title}</h4>
                        </div>
                        ${statusIcon}
                    </div>
                    ${song.keywords ? `
                        <p class="text-sm text-zinc-400 mb-2">${song.keywords}</p>
                    ` : ''}
                    <div class="flex gap-2 text-xs text-zinc-500">
                        ${song.has_drums ? `<span>${song.bpm} BPM</span><span>•</span>` : ''}
                        <span>Gain: ${song.gain}</span>
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
            document.getElementById('song-half-tempo').checked = song.half_tempo_tail_flap || false;

            // Update file status indicators and show play buttons for existing files
            updateFileStatus('full', song.has_full);
            updateFileStatus('vocals', song.has_vocals);
            updateFileStatus('drums', song.has_drums);
            
            // Show play buttons for existing files and load them into audio elements
            if (song.has_full) {
                const playButton = document.getElementById('play-full');
                const audioElement = document.getElementById('full-audio');
                if (playButton) playButton.classList.remove('hidden');
                if (audioElement) {
                    audioElement.src = `/songs/${songName}/full.wav`;
                }
            }
            if (song.has_vocals) {
                const playButton = document.getElementById('play-vocals');
                const audioElement = document.getElementById('vocals-audio');
                if (playButton) playButton.classList.remove('hidden');
                if (audioElement) {
                    audioElement.src = `/songs/${songName}/vocals.wav`;
                }
            }
            if (song.has_drums) {
                const playButton = document.getElementById('play-drums');
                const audioElement = document.getElementById('drums-audio');
                if (playButton) playButton.classList.remove('hidden');
                if (audioElement) {
                    audioElement.src = `/songs/${songName}/drums.wav`;
                }
            }
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
        document.getElementById('song-half-tempo').checked = false;
        
        // Hide play buttons and reset file status
        ['full', 'vocals', 'drums'].forEach(type => {
            const playButton = document.getElementById(`play-${type}`);
            if (playButton) playButton.classList.add('hidden');
            updateFileStatus(type, false);
        });
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
                tail_moves: document.getElementById('song-tail-moves').value,
                mouth_mutes: document.getElementById('song-mouth-mutes').value,
                mouth_articulation: document.getElementById('song-mouth-articulation').value,
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

    const deleteSong = async () => {
        if (!currentSong) return;

        if (!confirm(`Are you sure you want to delete "${currentSong}"? This cannot be undone.`)) {
            return;
        }

        try {
            const response = await fetch(`/songs/${currentSong}`, {
                method: 'DELETE'
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to delete song');
            }

            showNotification(`Song '${currentSong}' deleted successfully`, 'success');
            showListView();
            await loadSongs();
        } catch (error) {
            debugLog('ERROR', 'Failed to delete song:', error);
            showNotification(error.message || 'Failed to delete song', 'error');
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
        const playButton = document.getElementById(`play-${fileType}`);
        const playIcon = playButton?.querySelector('.material-icons');

        if (!fileInput || !audioElement || !playButton) return;

        // Handle file selection
        fileInput.addEventListener('change', (e) => {
            const file = e.target.files[0];

            if (file) {
                const url = URL.createObjectURL(file);
                audioElement.src = url;
                playButton.classList.remove('hidden');
                updateFileStatus(fileType, true);

                const statusEl = document.getElementById(`${fileType}-status`);
                if (statusEl) statusEl.title = file.name;

                // Reset icon when new file is loaded
                if (playIcon) playIcon.textContent = 'play_circle';
            } else {
                updateFileStatus(fileType, false);
                playButton.classList.add('hidden');
            }
        });

        // Handle play/pause
        playButton.addEventListener('click', () => {
            if (audioElement.paused) {
                // Pause all other audio elements first
                ['full', 'vocals', 'drums'].forEach(type => {
                    const otherAudio = document.getElementById(`${type}-audio`);
                    const otherIcon = document.getElementById(`play-${type}`)?.querySelector('.material-icons');
                    if (otherAudio && otherAudio !== audioElement && !otherAudio.paused) {
                        otherAudio.pause();
                        if (otherIcon) otherIcon.textContent = 'play_circle';
                    }
                });
                
                audioElement.play();
                if (playIcon) playIcon.textContent = 'pause_circle';
            } else {
                audioElement.pause();
                if (playIcon) playIcon.textContent = 'play_circle';
            }
        });

        // Update icon when audio ends
        audioElement.addEventListener('ended', () => {
            if (playIcon) playIcon.textContent = 'play_circle';
        });
    };

    const init = () => {
        const isSongsPage = !!document.getElementById('songs-grid');

        // View navigation
        document.getElementById('create-song-btn')?.addEventListener('click', () => showEditView(null));
        document.getElementById('back-to-songs-list-btn')?.addEventListener('click', showListView);
        
        // Form actions
        document.getElementById('save-song-btn')?.addEventListener('click', saveSong);
        document.getElementById('delete-song-btn')?.addEventListener('click', deleteSong);

        // Audio preview controls
        setupAudioPreview('full');
        setupAudioPreview('vocals');
        setupAudioPreview('drums');

        initSongSliders();

        if (isSongsPage) {
            showListView();
            loadSongs();
        }
    };

    return {
        init,
        editSong: showEditView,
        loadSongs,
        copyExample
    };
})();

// Make it globally accessible
window.SongsManager = SongsManager;
