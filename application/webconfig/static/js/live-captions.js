// CC opens the Live subtitle panel; its fullscreen button provides a larger view.
(() => {
    const button = document.getElementById('toggle-captions-btn');
    const panel = document.getElementById('live-captions');
    const panelHost = document.getElementById('live-captions-panel');
    const content = document.getElementById('live-captions-content');
    const transcriptContent = document.getElementById('live-transcript-content');
    const transcriptTurns = document.getElementById('live-transcript-turns');
    const transcriptEmpty = document.getElementById('live-transcript-empty');
    const title = document.getElementById('live-captions-title');
    const dialog = document.getElementById('live-captions-dialog');
    const expandButton = document.getElementById('expand-captions-btn');
    const expandIcon = document.getElementById('captions-expand-icon');
    if (!button || !panel || !panelHost || !content || !dialog || !expandButton || !expandIcon
        || !transcriptContent || !transcriptTurns || !transcriptEmpty || !title) return;
    const deviceName = panel.dataset.deviceName || 'Billy';
    const empty = document.getElementById('live-captions-empty');
    const rows = Object.fromEntries(['user', 'billy'].map(speaker => [speaker, {
        node: document.getElementById(`live-caption-${speaker}`),
    }]));
    const textLimits = {user: 160, billy: 512}; // Match the bounded server caption windows.
    const key = 'billy:live-captions';
    let expanded = false;
    try { expanded = localStorage.getItem(key) === 'true'; } catch (_) { /* Private browsing. */ }
    let available = false;
    let socket = null;
    let retry = null;
    let session = null;
    let transcriptRows = [];
    let hasTranscript = false;
    let followTranscript = true;

    function clear() {
        for (const row of Object.values(rows)) {
            row.node.hidden = true;
            row.node.querySelector('.live-caption-text').textContent = '';
        }
        empty.hidden = false;
        session = null;
        transcriptTurns.replaceChildren();
        transcriptRows = [];
        hasTranscript = false;
        followTranscript = true;
        transcriptEmpty.hidden = false;
        transcriptEmpty.textContent = 'Waiting for speech…';
        scrollToLatest();
    }

    function setFullscreen(fullscreen) {
        const wasOpen = dialog.open;
        if (fullscreen) {
            if (panel.hidden || !expanded || wasOpen) return;
            // Move the live view, keeping one set of rows and one subscription.
            dialog.append(panel);
            dialog.showModal();
        } else {
            if (wasOpen) dialog.close();
            if (panel.parentElement !== panelHost) panelHost.append(panel);
        }
        content.hidden = fullscreen;
        transcriptContent.hidden = !fullscreen;
        title.textContent = fullscreen ? 'Session transcript' : 'Live subtitles';
        title.hidden = !fullscreen;
        if (fullscreen) followTranscript = true;
        requestView();
        expandButton.setAttribute('aria-expanded', String(fullscreen));
        expandIcon.textContent = fullscreen ? 'fullscreen_exit' : 'fullscreen';
        scrollToLatest();
        if (fullscreen || (wasOpen && !panel.hidden && expanded)) expandButton.focus();
        else if (wasOpen && !button.hidden) button.focus();
    }

    function requestView() {
        if (socket?.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({view: dialog.open ? 'transcript' : 'captions'}));
        }
    }

    function scrollToLatest() {
        const active = dialog.open ? transcriptContent : content;
        const overflow = active.scrollHeight > active.clientHeight + 1;
        const label = dialog.open ? 'Return to subtitle panel'
            : overflow ? 'Open session transcript to read more' : 'Open session transcript';
        expandButton.title = label;
        expandButton.setAttribute('aria-label', label);
        expandButton.dataset.overflow = String(overflow && !dialog.open);
        if (!panel.hidden && (!dialog.open || followTranscript)) active.scrollTop = active.scrollHeight;
    }

    function renderTranscript(turns) {
        // Stable rows preserve selection and reading position as words stream in.
        turns.forEach((turn, index) => {
            let row = transcriptRows[index];
            if (!row) {
                const node = document.createElement('div');
                node.className = 'live-caption-row';
                const speaker = document.createElement('span');
                speaker.className = 'live-caption-speaker';
                const text = document.createElement('span');
                text.className = 'live-caption-text';
                node.append(speaker, text);
                transcriptTurns.append(node);
                row = {node, speaker, text};
                transcriptRows.push(row);
            }
            row.node.dataset.speaker = turn.speaker;
            row.speaker.textContent = turn.speaker === 'user' ? 'You' : deviceName;
            const text = turn.text.trim();
            if (row.text.textContent !== text) row.text.textContent = text;
            row.node.hidden = !text;
        });
        transcriptRows.slice(turns.length).forEach(row => row.node.remove());
        transcriptRows.length = turns.length;
        hasTranscript = true;
        transcriptEmpty.hidden = transcriptRows.some(row => !row.node.hidden);
        transcriptEmpty.textContent = 'Waiting for speech…';
    }

    function receive(data) {
        if (!data || typeof data.session !== 'string' || !Array.isArray(data.lines)) {
            clear(); return;
        }
        if (data.session !== session) clear();
        session = data.session;
        if (Array.isArray(data.transcript) && data.transcript.every(turn =>
            turn && ['user', 'billy'].includes(turn.speaker) && typeof turn.text === 'string')) {
            renderTranscript(data.transcript);
        } else if (data.transcript_error && !hasTranscript) {
            // Keep this session's last complete snapshot during publication
            // races; the server retries. A new session is cleared above.
            transcriptEmpty.hidden = false;
            transcriptEmpty.textContent = 'Session transcript is temporarily unavailable.';
        } else if (!hasTranscript) {
            transcriptEmpty.textContent = 'Loading session transcript…';
        }
        // The server retains each speaker's last turn and clears ended/stale
        // sessions. Do not expire a question while Billy is still answering it.
        for (const [speaker, row] of Object.entries(rows)) {
            const line = data.lines.find(item => item?.speaker === speaker);
            const text = typeof line?.text === 'string'
                ? Array.from(line.text.trim()).slice(-textLimits[speaker]).join('') : '';
            row.node.querySelector('.live-caption-text').textContent = text;
            row.node.hidden = !text;
        }
        // The snapshot follows speech turns, not fragment arrival times.
        const first = rows[data.lines[0]?.speaker]?.node;
        if (first && content.firstElementChild !== first) content.prepend(first);
        empty.hidden = Object.values(rows).some(row => !row.node.hidden);
        scrollToLatest();
    }

    function connect() {
        if (!available || !expanded || socket) return;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const current = new WebSocket(`${protocol}//${window.location.host}/ws/live-captions`);
        socket = current;
        current.onopen = () => { if (socket === current) requestView(); };
        current.onmessage = event => {
            if (socket !== current) return;
            try { receive(JSON.parse(event.data)); } catch (_) { clear(); }
        };
        current.onclose = () => {
            if (socket !== current) return;
            socket = null;
            clear();
            if (available && expanded) retry = setTimeout(connect, 3000);
        };
    }

    function stop() {
        clearTimeout(retry);
        retry = null;
        const previous = socket;
        socket = null;
        previous?.close();
        clear();
    }

    function render() {
        const visible = available && expanded;
        button.hidden = !available;
        button.setAttribute('aria-expanded', String(visible));
        button.title = visible ? 'Hide subtitles' : 'Show subtitles';
        panel.hidden = !visible;
        panelHost.hidden = !visible;
        if (visible) connect();
        else {
            setFullscreen(false);
            stop();
        }
        scrollToLatest();
    }

    expandButton.addEventListener('click', () => setFullscreen(!dialog.open));
    dialog.addEventListener('cancel', event => {
        event.preventDefault();
        setFullscreen(false);
    });
    dialog.addEventListener('click', event => {
        if (event.target === dialog) setFullscreen(false);
    });
    dialog.addEventListener('close', () => {
        if (!dialog.open && panel.parentElement === dialog) setFullscreen(false);
    });
    if (typeof ResizeObserver !== 'undefined') {
        const observer = new ResizeObserver(scrollToLatest);
        observer.observe(content);
        observer.observe(transcriptContent);
    }
    transcriptContent.addEventListener('scroll', () => {
        if (dialog.open) {
            followTranscript = transcriptContent.scrollHeight - transcriptContent.clientHeight
                - transcriptContent.scrollTop <= 24;
        }
    });
    button.addEventListener('click', () => {
        expanded = !expanded;
        try { localStorage.setItem(key, String(expanded)); } catch (_) { /* Optional preference. */ }
        render();
    });
    window.addEventListener('billy:service:status', event => {
        available = event.detail?.live_captions === true;
        render();
    });
    window.addEventListener('resize', scrollToLatest);
    window.addEventListener('billy:websocket:disconnected', () => {
        available = false;
        render();
    });
    render();
})();
