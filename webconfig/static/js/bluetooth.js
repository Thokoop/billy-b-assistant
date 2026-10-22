window.BluetoothSettings = (() => {
    // Rose for the one that throws work away, amber for the reversible
    // interruption, emerald for the one that restores a connection.
    const DEVICE_ACTIONS = {
        connect: {
            icon: 'bluetooth_connected',
            hover: 'secondary-action--hover--emerald',
            title: (name) => `Connect to ${name}`,
        },
        disconnect: {
            icon: 'bluetooth_disabled',
            hover: 'secondary-action--hover--amber',
            title: (name) => `Disconnect ${name}, keeping it paired`,
        },
        forget: {
            icon: 'delete',
            hover: 'secondary-action--hover--rose',
            title: (name) => `Forget ${name}, and pair again to use it`,
        },
    };

    // The status file names the PipeWire node, which carries the phone's
    // address with underscores: bluez_input.1C_3C_78_C9_7C_61.2
    function audioDeviceAddress(audio) {
        const match = /^bluez_(?:input|output)\.([0-9A-Fa-f_]+)\./.exec(audio.source || '');
        return match ? match[1].replace(/_/g, ':').toUpperCase() : null;
    }

    function audioDeviceName(state, audio) {
        const address = audioDeviceAddress(audio);
        if (!address) return '';
        const device = (state.devices || []).find(
            (d) => (d.Address || '').toUpperCase() === address,
        );
        return device ? ` from ${device.Alias || device.Address}` : '';
    }

    const PLAYING_STATES = new Set(['media_audio', 'call_audio']);

    // Keep disabled controls visually distinct, including during setup.
    function setDisabled(button, disabled) {
        button.disabled = disabled;
        button.classList.toggle('opacity-50', disabled);
        button.classList.toggle('cursor-not-allowed', disabled);
    }

    let cleanup = () => {};

    function init() {
        cleanup();
        const root = document.getElementById('bluetooth-settings');
        if (!root) return;
        if (root.dataset.bluetoothBound === 'true') {
            // Reconnect initialization reuses the controls and status subscription.
            cleanup = root.bluetoothCleanup;
            root.bluetoothResume();
            return;
        }
        root.dataset.bluetoothBound = 'true';
        const el = (id) => root.querySelector(`#${id}`);
        let pending = null;
        let busy = false;
        let polling = false;
        let active = false;
        let revision = 0;
        let actionRevision = 0;
        let latestSample = -Infinity;
        let refreshQueued = false;
        const deviceRows = new Map();
        let emptyDevicesRow = null;

        async function act(action, data = {}) {
            if (busy) return;
            busy = true;
            revision += 1;
            actionRevision += 1;
            root.querySelectorAll('button, input').forEach((button) => { button.disabled = true; });
            el('bt-error').textContent = '';
            try {
                const response = await fetch(`/api/bluetooth/${action}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Bluetooth-Token': root.dataset.token },
                    body: JSON.stringify(data), signal: AbortSignal.timeout(45000),
                });
                const result = await response.json();
                if (!response.ok) throw new Error(result.error || 'Bluetooth action failed');
            } catch (error) {
                el('bt-error').textContent = error.message;
            } finally {
                busy = false;
                root.querySelectorAll('button, input').forEach((button) => { button.disabled = false; });
                // One fresh snapshot after an action; do not reuse a pre-action cache.
                await refresh(true);
            }
        }

        function showError(error) {
            el('bt-audio').textContent = `Bluetooth unavailable: ${error.message}`;
            el('bt-pending').hidden = true;
            pending = null;
        }

        function render(state) {
            // HTTP and the shared WebSocket sampler can finish out of order.
            // Compare acquisition times, not arrival order in this browser.
            if (Number.isFinite(state.sampled_at)) {
                if (state.sampled_at < latestSample) return;
                latestSample = state.sampled_at;
            }
            if (state.error) {
                showError(new Error(state.error));
                return;
            }
            const setup = state.setup || {};
            el('bt-setup').textContent = setup.message || '';
            el('bt-setup').hidden = setup.state === 'ready';
            el('bt-setup-retry').hidden = true;
            el('bt-components-help').hidden = setup.state === 'ready';
            root.querySelectorAll('button, input').forEach((button) => {
                // bt-pair is decided below, by whether pairing is possible.
                if (button.id === 'bt-pair') return;
                setDisabled(button, setup.state !== 'ready' && button.id !== 'bt-setup-retry');
            });
            setDisabled(el('bt-pair'), setup.state !== 'ready');
            if (setup.state !== 'ready') {
                el('bt-audio').textContent = 'Bluetooth will be available when setup completes.';
                el('bt-pending').hidden = true;
                el('bt-pair-hint').hidden = true;
                el('bt-pair-stop').hidden = true;
                el('bt-pair-status').hidden = true;
                el('bt-pair').hidden = false;
                el('bt-devices-title').hidden = true;
                pending = null;
                return;
            }
            // Stopping pairing and the "go to your device's settings" hint
            // only mean anything while the pairing window is open.
            const pairing = state.pairing_seconds > 0;
            // "Ready to pair." said nothing a disabled button cannot say
            // better. What is left - no adapter, or a powered-down one - is
            // a fault rather than a status, so it goes in the one status
            // line at the top, and the button explains itself on hover.
            const blocked = !state.adapter ? 'No Bluetooth adapter found.'
                : state.adapter.Powered ? '' : 'Bluetooth is off.';
            setDisabled(el('bt-pair'), Boolean(blocked) || pairing);
            el('bt-pair').title = blocked || (pairing
                ? 'Already visible to nearby devices'
                : 'Make Billy visible to nearby devices');
            el('bt-pair-status').textContent = pairing
                ? `Visible for ${state.pairing_seconds}s` : '';
            el('bt-pair-status').hidden = !pairing;
            el('bt-pair-hint').hidden = !pairing;
            el('bt-pair-stop').hidden = !pairing;
            el('bt-pair').hidden = pairing;
            pending = state.pending;
            el('bt-pending').hidden = !pending;
            el('bt-code').textContent = pending?.passkey || '';
            el('bt-code').hidden = !pending?.passkey;
            if (pending) {
                el('bt-request').textContent = pending.name;
                el('bt-confirm-hint').textContent = pending.passkey
                    ? 'Check the code and tap Pair on your phone.'
                    : 'Complete pairing on your phone.';
            }
            const audioState = state.audio || {};
            const list = el('bt-devices');
            const rows = [];
            const addresses = new Set();
            for (const device of state.devices.filter((d) => d.Paired || d.Connected)) {
                const connected = Boolean(device.Connected);
                const playing = PLAYING_STATES.has(audioState.state)
                    && audioDeviceAddress(audioState) === (device.Address || '').toUpperCase()
                    ? audioState.state : null;
                // Only visible row changes replace DOM. Sampling timestamps,
                // transport metadata and pairing countdowns do not affect it.
                const signature = JSON.stringify([
                    device.Alias || device.Address, connected, Boolean(device.Paired), playing,
                ]);
                addresses.add(device.Address);
                const cached = deviceRows.get(device.Address);
                if (cached?.signature === signature) {
                    // Also restore the switch after a rejected save, without
                    // removing its focus or replacing the row's event handlers.
                    if (cached.checkbox) cached.checkbox.checked = Boolean(device.AutoConnect);
                    rows.push(cached.item);
                    continue;
                }
                let autoConnectCheckbox = null;
                const state_label = connected
                    ? (device.Paired ? 'Connected' : 'Pairing…') : 'Disconnected';
                const item = document.createElement('li');
                item.className = 'bt-device';

                const orb = document.createElement('span');
                // Written as whole class names so Tailwind's scan finds
                // them, and only ones already in the built styles.css, so
                // this needs no CSS rebuild.
                orb.className = 'inline-block h-3 w-3 shrink-0 rounded-full '
                    + (connected ? 'bg-emerald-500' : 'bg-zinc-500');
                // Colour alone must not be the only carrier of the state.
                orb.setAttribute('role', 'img');
                orb.setAttribute('aria-label', state_label);
                orb.title = state_label;
                const identity = document.createElement('div');
                identity.className = 'bt-device-identity';
                identity.append(orb);
                item.append(identity);
                const details = document.createElement('div');
                details.className = 'bt-device-details';
                identity.append(details);

                const name = document.createElement('span');
                name.className = 'bt-device-name';
                name.textContent = device.Alias || device.Address;
                details.append(name);
                const connection = document.createElement('span');
                connection.className = 'bt-device-state';
                connection.textContent = state_label;
                details.append(connection);


                // Which phone owns the speaker right now. Worth saying on
                // the row rather than only in the status line: select_route()
                // fails closed on two streaming phones, and this explains
                // the refusal before it happens.
                if (playing) {
                    const badge = document.createElement('span');
                    badge.className = 'bt-device-badge';
                    badge.textContent = audioState.state === 'call_audio' ? 'on a call' : 'playing';
                    connection.textContent = connected ? 'Connected · ' : '';
                    connection.append(badge);
                }

                const actions = document.createElement('div');
                actions.className = 'bt-device-actions';
                item.append(actions);
                const deviceActions = connected
                    ? ['disconnect', 'forget']
                    : ['connect', 'forget'];
                for (const action of deviceActions) {
                    const button = document.createElement('button');
                    button.type = 'button';
                    button.className = `secondary-action ${DEVICE_ACTIONS[action].hover} bt-device-action`;
                    const revealLabel = action === 'disconnect' || action === 'forget';
                    if (revealLabel) button.classList.add('button-reveal');
                    if (action === 'forget') button.classList.add('bt-forget');
                    const icon = document.createElement('span');
                    icon.className = 'material-icons text-base';
                    icon.textContent = DEVICE_ACTIONS[action].icon;
                    // The label already says it; the icon is decoration.
                    icon.setAttribute('aria-hidden', 'true');
                    button.append(icon);
                    const label = document.createElement('span');
                    if (revealLabel) label.className = 'button-reveal-label';
                    label.textContent = action[0].toUpperCase() + action.slice(1);
                    button.append(label);
                    button.title = DEVICE_ACTIONS[action].title(name.textContent);
                    button.setAttribute('aria-label', button.title);
                    button.addEventListener('click', () => {
                        // Forgetting drops the pairing on Billy's side
                        // only: the phone still lists him, and it has to be
                        // forgotten there too before pairing again. Every
                        // other destructive action in the UI confirms.
                        if (action === 'forget' && !window.confirm(
                            `Forget ${name.textContent}? You will need to pair it again, `
                            + 'and remove Billy from the phone\u2019s Bluetooth settings first.',
                        )) return;
                        act(action, { address: device.Address });
                    });
                    actions.append(button);
                }
                if (device.Paired) {
                    const label = document.createElement('label');
                    label.className = 'wf-toggle gap-2 text-sm w-full';
                    const checkbox = document.createElement('input');
                    checkbox.type = 'checkbox';
                    checkbox.setAttribute('role', 'switch');
                    checkbox.checked = Boolean(device.AutoConnect);
                    autoConnectCheckbox = checkbox;
                    checkbox.setAttribute('aria-label', `Auto-connect to ${name.textContent} on startup`);
                    checkbox.addEventListener('change', () => act('auto-connect', {
                        address: device.Address, enabled: checkbox.checked,
                    }));
                    const caption = document.createElement('span');
                    caption.textContent = 'Auto-connect';
                    label.title = 'When Billy starts, try selected devices until one connects.';
                    const track = document.createElement('span');
                    track.className = 'wf-toggle-track';
                    track.setAttribute('aria-hidden', 'true');
                    const thumb = document.createElement('span');
                    thumb.className = 'wf-toggle-thumb';
                    track.append(thumb);
                    label.append(checkbox, track, caption);
                    actions.append(label);
                }

                deviceRows.set(device.Address, {signature, item, checkbox: autoConnectCheckbox});
                rows.push(item);
            }
            for (const address of deviceRows.keys()) {
                if (!addresses.has(address)) deviceRows.delete(address);
            }
            // With rows on screen the heading only repeats what they
            // already show; with none it is what labels the empty state.
            const empty_list = !rows.length;
            el('bt-devices-title').hidden = !empty_list;
            if (empty_list) {
                if (!emptyDevicesRow) {
                    emptyDevicesRow = document.createElement('li');
                    emptyDevicesRow.className = 'text-slate-400';
                    emptyDevicesRow.textContent = 'No devices paired yet.';
                }
                rows.push(emptyDevicesRow);
            }
            // Leave unchanged nodes attached so hover and focus survive live
            // updates, including when another phone changes or disappears.
            const retained = new Set(rows);
            for (const child of Array.from(list.children)) {
                if (!retained.has(child)) list.removeChild(child);
            }
            rows.forEach((row, index) => {
                if (list.children[index] !== row) {
                    list.insertBefore(row, list.children[index] || null);
                }
            });
            const audio = audioState;
            // Switching is automatic, so this reports rather than offers:
            // what Billy's audio path is doing right now. Billy writes it
            // from inside billy.service as the mode changes; it is not a
            // property of any service being up, which is why nothing here
            // consults systemd any more.
            //
            // Echo cancellation is named because it can be turned off, and
            // a call that sounds echoey is the first thing to check.
            const idleLabel = state.devices.some((device) => device.Connected)
                ? 'No Bluetooth audio playing.' : 'No phone connected.';
            const labels = {
                idle: idleLabel,
                waiting: 'Waiting for phone audio',
                media_audio: `Playing media${audioDeviceName(state, audio) || ''}`,
                call_audio: `On a call${audioDeviceName(state, audio) || ''} · echo cancellation ${audio.aec ? 'active' : 'off'}`,
                error: 'Bluetooth audio failed',
                stopped: 'Stopped',
            };
            el('bt-audio').textContent = blocked || labels[audio.state] || labels.idle;
            if (audio.state === 'error') el('bt-audio').textContent += `: ${audio.error}`;
        }

        async function refresh(force = false) {
            if (force && polling) {
                refreshQueued = true;
                return;
            }
            if (!active || polling || busy || document.hidden || !root.isConnected) return;
            polling = true;
            const receivedRevision = revision;
            const receivedActionRevision = actionRevision;
            try {
                const response = await fetch('/api/bluetooth', { signal: AbortSignal.timeout(18000) });
                const state = await response.json();
                if (!active || !root.isConnected) return;
                if (!response.ok) throw new Error(state.error || 'Bluetooth unavailable');
                if (!busy && receivedActionRevision === actionRevision
                    && (Number.isFinite(state.sampled_at) || receivedRevision === revision)) render(state);
            } catch (error) {
                if (active && root.isConnected && receivedRevision === revision) showError(error);
            } finally {
                polling = false;
                if (refreshQueued) {
                    refreshQueued = false;
                    refresh();
                }
            }
        }
        root.querySelectorAll('[data-action]').forEach((button) => {
            button.addEventListener('click', () => act(button.dataset.action));
        });
        let timer = null;
        const stopPolling = () => {
            if (timer !== null) clearInterval(timer);
            timer = null;
        };
        const onStatus = (event) => {
            revision += 1;
            stopPolling();
            if (active && !busy && !document.hidden && root.isConnected) render(event.detail);
        };
        const resume = () => {
            if (!active || !root.isConnected || document.hidden) {
                stopPolling();
                return;
            }
            const snapshot = window.billyWebSocket?.getBluetoothStatus?.();
            if (snapshot) {
                stopPolling();
                if (!busy) render(snapshot);
            } else if (timer === null) {
                // Initial load, older backends and a disconnected socket only.
                refresh();
                timer = setInterval(refresh, 2000);
            }
        };
        const onDisconnect = () => {
            latestSample = -Infinity;
            resume();
        };
        cleanup = () => {
            active = false;
            revision += 1;
            stopPolling();
            window.removeEventListener('billy:bluetooth:status', onStatus);
            window.removeEventListener('billy:websocket:disconnected', onDisconnect);
            document.removeEventListener('visibilitychange', resume);
        };
        root.bluetoothCleanup = cleanup;
        root.bluetoothResume = () => {
            active = true;
            window.addEventListener('billy:bluetooth:status', onStatus);
            window.addEventListener('billy:websocket:disconnected', onDisconnect);
            document.addEventListener('visibilitychange', resume);
            resume();
        };
        root.bluetoothResume();
    }

    return { init };
})();
