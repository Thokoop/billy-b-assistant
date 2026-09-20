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

    // .secondary-action has no :disabled rule in the built stylesheet, so a
    // disabled button is indistinguishable from a live one. The knowledge and
    // song panels carry the state as utility classes for the same reason; both
    // are already in styles.css, so this needs no CSS rebuild.
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
                el('bt-pair-hint').textContent = pairing
                    ? `Visible as Billy Bass for ${state.pairing_seconds} seconds. `
                        + 'Select Billy Bass in your device\u2019s Bluetooth settings, '
                        + 'then confirm the request here.'
                    : '';
                el('bt-pair-hint').hidden = !pairing;
                el('bt-pair-stop').hidden = !pairing;
                pending = state.pending;
                el('bt-pending').hidden = !pending;
                if (pending) el('bt-request').textContent = pending.passkey
                    ? `${pending.name}: does code ${pending.passkey} match your phone?`
                    : `Allow ${pending.name} to pair?`;
                const audioState = state.audio || {};
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

                    // Which phone owns the speaker right now. Worth saying on
                    // the row rather than only in the status line: select_route()
                    // fails closed on two streaming phones, and this explains
                    // the refusal before it happens.
                    if (
                        PLAYING_STATES.has(audioState.state)
                        && audioDeviceAddress(audioState) === (device.Address || '').toUpperCase()
                    ) {
                        const badge = document.createElement('span');
                        badge.className = 'text-emerald-400 text-sm shrink-0';
                        badge.textContent = audioState.state === 'call_audio' ? 'on a call' : 'playing';
                        item.append(badge);
                    }

                    for (const action of [connected ? 'disconnect' : 'connect', 'forget']) {
                        const button = document.createElement('button');
                        button.type = 'button';
                        // Same shape as the knowledge and wake-up row actions:
                        // fixed height, a hover colour carrying the weight of
                        // the action, and an icon beside the label. Only class
                        // names already in the built styles.css, so this needs
                        // no CSS rebuild.
                        button.className = `secondary-action ${DEVICE_ACTIONS[action].hover} h-11 px-3 shrink-0`;
                        const icon = document.createElement('span');
                        icon.className = 'material-icons text-base';
                        icon.textContent = DEVICE_ACTIONS[action].icon;
                        // The label already says it; the icon is decoration.
                        icon.setAttribute('aria-hidden', 'true');
                        button.append(icon);
                        const label = document.createElement('span');
                        label.textContent = action[0].toUpperCase() + action.slice(1);
                        button.append(label);
                        button.title = DEVICE_ACTIONS[action].title(name.textContent);
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
                        item.append(button);
                    }
                    list.append(item);
                }
                // With rows on screen the heading only repeats what they
                // already show; with none it is what labels the empty state.
                const empty_list = !list.children.length;
                el('bt-devices-title').hidden = !empty_list;
                if (empty_list) {
                    const empty = document.createElement('li');
                    empty.className = 'text-slate-400';
                    empty.textContent = 'No devices paired yet.';
                    list.append(empty);
                }
                const audio = audioState;
                // Switching is automatic, so this reports rather than offers:
                // what Billy's audio path is doing right now. Billy writes it
                // from inside billy.service as the mode changes; it is not a
                // property of any service being up, which is why nothing here
                // consults systemd any more.
                //
                // Echo cancellation is named because it can be turned off, and
                // a call that sounds echoey is the first thing to check.
                const labels = {
                    idle: 'Not playing. Billy takes over automatically when your phone does.',
                    waiting: 'Waiting for phone audio',
                    media_audio: `Playing media${audioDeviceName(state, audio) || ''}`,
                    call_audio: `On a call${audioDeviceName(state, audio) || ''} · echo cancellation ${audio.aec ? 'active' : 'off'}`,
                    error: 'Bluetooth audio failed',
                    stopped: 'Stopped',
                };
                el('bt-audio').textContent = blocked || labels[audio.state] || labels.idle;
                if (audio.state === 'error') el('bt-audio').textContent += `: ${audio.error}`;
            } catch (error) {
                el('bt-audio').textContent = `Bluetooth unavailable: ${error.message}`;
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
