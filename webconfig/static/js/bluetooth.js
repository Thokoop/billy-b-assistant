window.BluetoothSettings = (() => {
    let cleanup = () => {};

    function init() {
        cleanup();
        const root = document.getElementById('bluetooth-settings');
        if (!root) return;
        if (root.dataset.bluetoothBound === 'true') {
            // Reconnect initialization reuses the current controls and polling.
            cleanup = root.bluetoothCleanup;
            root.bluetoothResume();
            return;
        }
        root.dataset.bluetoothBound = 'true';
        const el = (id) => root.querySelector(`#${id}`);
        let pending = null;
        let busy = false;
        let polling = false;

        async function act(action, data = {}) {
            if (busy) return;
            busy = true;
            root.querySelectorAll('button').forEach((button) => { button.disabled = true; });
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
                root.querySelectorAll('button').forEach((button) => { button.disabled = false; });
                await refresh();
            }
        }

        async function refresh() {
            if (polling || busy || document.hidden || !root.isConnected) return;
            polling = true;
            try {
                const response = await fetch('/api/bluetooth', { signal: AbortSignal.timeout(18000) });
                const state = await response.json();
                if (!root.isConnected) return;
                if (!response.ok) throw new Error(state.error || 'Bluetooth unavailable');
                const setup = state.setup || {};
                el('bt-setup').textContent = setup.message || '';
                el('bt-setup').hidden = setup.state === 'ready';
                el('bt-setup-retry').hidden = setup.state !== 'error';
                root.querySelectorAll('button').forEach((button) => {
                    button.disabled = setup.state !== 'ready' && button.id !== 'bt-setup-retry'
                        && button.dataset.action !== 'test-stop';
                });
                if (setup.state !== 'ready') {
                    el('bt-status').textContent = 'Bluetooth will be available when setup completes.';
                    el('bt-pending').hidden = true;
                    el('bt-pair-hint').hidden = true;
                    el('bt-pair-stop').hidden = true;
                    pending = null;
                    return;
                }
                // Stopping pairing and the "go to your device's settings" hint
                // only mean anything while the pairing window is open.
                const pairing = state.pairing_seconds > 0;
                el('bt-status').textContent = !state.adapter ? 'No Bluetooth adapter found.'
                    : pairing ? `Visible as Billy Bass for ${state.pairing_seconds} seconds`
                    : state.adapter.Powered ? 'Ready to pair.' : 'Bluetooth is off.';
                el('bt-pair-hint').hidden = !pairing;
                el('bt-pair-stop').hidden = !pairing;
                pending = state.pending;
                el('bt-pending').hidden = !pending;
                if (pending) el('bt-request').textContent = pending.passkey
                    ? `${pending.name}: does code ${pending.passkey} match your phone?`
                    : `Allow ${pending.name} to pair?`;
                const list = el('bt-devices');
                list.replaceChildren();
                for (const device of state.devices.filter((d) => d.Paired || d.Connected)) {
                    const connected = Boolean(device.Connected);
                    const state_label = connected ? 'Connected' : 'Disconnected';
                    const item = document.createElement('li');
                    item.className = 'flex items-center gap-3';

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
                    item.append(orb);

                    const name = document.createElement('span');
                    name.className = 'grow truncate';
                    name.textContent = device.Alias || device.Address;
                    item.append(name);

                    for (const action of [connected ? 'disconnect' : 'connect', 'forget']) {
                        const button = document.createElement('button');
                        button.type = 'button';
                        button.className = 'secondary-action p-2 shrink-0';
                        button.textContent = action[0].toUpperCase() + action.slice(1);
                        button.addEventListener('click', () => act(action, { address: device.Address }));
                        item.append(button);
                    }
                    list.append(item);
                }
                if (!list.children.length) {
                    const empty = document.createElement('li');
                    empty.textContent = 'No devices paired yet.';
                    list.append(empty);
                }
                const audio = state.audio || {};
                // Report what the runtime actually did, not what call audio
                // usually does: echo cancellation can be turned off.
                const labels = { waiting: 'Waiting for phone audio', media_audio: 'Media audio',
                    call_audio: `Call audio · echo cancellation ${audio.aec ? 'active' : 'off'}`,
                    error: 'Audio test failed', stopped: 'Stopped' };
                el('bt-audio').textContent = state.service === 'active'
                    ? (labels[audio.state] || 'Starting audio test…') : `Audio test ${state.service || 'not installed'}`;
                if (audio.state === 'error') el('bt-audio').textContent += `: ${audio.error}`;
            } catch (error) {
                el('bt-status').textContent = `Bluetooth unavailable: ${error.message}`;
                el('bt-pending').hidden = true;
                pending = null;
            } finally {
                polling = false;
            }
        }
        root.querySelectorAll('[data-action]').forEach((button) => {
            button.addEventListener('click', () => act(button.dataset.action));
        });
        el('bt-accept').addEventListener('click', () => pending && act('confirm', { id: pending.id, accepted: true }));
        el('bt-reject').addEventListener('click', () => pending && act('confirm', { id: pending.id, accepted: false }));
        refresh();
        let timer = setInterval(refresh, 2000);
        cleanup = () => clearInterval(timer);
        root.bluetoothCleanup = cleanup;
        root.bluetoothResume = () => {
            refresh();
            timer = setInterval(refresh, 2000);
        };
    }

    return { init };
})();
