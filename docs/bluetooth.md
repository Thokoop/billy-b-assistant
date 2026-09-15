# Bluetooth installation and updates

Use the existing Software Update or Reinstall current version button. Python
packages, including `dbus-next`, are installed through the root `requirements.txt`.
There is no second pip installation or manual Bluetooth setup command.

After the updater restarts webconfig, the new application starts Bluetooth system
setup in the background. This also works when upgrading from a release whose
updater has never heard of Bluetooth. The existing update endpoints are unchanged.
The web interface remains available while installation runs.

System Settings contains the Bluetooth section directly after Audio Settings:
installation status and retry, pairing confirmation, saved devices, and the
phase 1/2 audio test. `/bluetooth` redirects to this section. The section works
with the existing page navigation and stops polling when you leave Settings.

The setup script and configuration files live in `setup/system/`. The script
installs BlueZ, PipeWire, WirePlumber, the Bluetooth SPA plugin and a user D-Bus
session from the configured operating-system repositories. It supplies both
WirePlumber 0.4 Lua configuration and 0.5 configuration fragments. It does not
upgrade the OS or add package repositories. The configuration keeps physical
ALSA devices with Billy; the PipeWire connection carries Bluetooth audio.
See the [upstream configuration migration documentation](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/migration.html)
for the difference between these configuration formats.

The internal `billy-bluetooth.service` is installed automatically. Its conflict
with `billy.service` enforces exclusive audio/motor ownership during the manual
audio test. It is not enabled at boot. Return to AI stops it before starting
Billy. Automatic media/call switching remains outside this prototype.

A successful setup stores a fingerprint in `state/bluetooth-setup.json`. Unchanged
installations skip setup on subsequent starts; changed installer assets trigger
setup again. Failed setup does not write a success marker, does not fail the
software update or disable the web interface, and can be retried in Settings.
Details are available in the section and in `state/bluetooth-setup.log`.

## Existing installation requirements

The web service must run as the same non-root user as `billy.service`. That user
needs passwordless sudo for system package/service installation (as on typical
Raspberry Pi OS installations). Permission errors are shown in Settings instead
of opening an unattended password prompt. Restricted sudo installations cannot
gain additional privileges through a software update; those permissions must
already be provisioned. No login or reboot is needed just to gain a new Bluetooth
group: setup installs a user-specific D-Bus policy instead.

## Audio roles

`bluez5.roles` names describe the **local** machine, not the remote device:

    bluez5.roles = [ a2dp_sink hfp_hf ]

Billy is the A2DP sink (the phone streams media to Billy) and the HFP hands-free
unit (Billy is the headset). `a2dp_source` / `hfp_ag` are the opposite direction
and would make Billy drive headphones. Verified on hardware: setting
`[ a2dp_source ]` makes the adapter advertise Audio Source (0x110a) instead of
Audio Sink (0x110b).

This reads backwards if you reason from `endpoint_to_profile()` in PipeWire's
`bluez5-dbus.c`, which maps a local endpoint path to the *remote* profile it
serves. Do not "correct" the value from that function; check the adapter instead.

## Adapter Class of Device

iOS uses Class of Device alongside SDP to decide what a device is, and will list
a device as an audio output while refusing to stream to it if the class is wrong.
A default Raspberry Pi adapter reports a Miscellaneous major class.
`install-bluetooth.sh` sets, in `/etc/bluetooth/main.conf` under `[General]`:

    Class = 0x200414

That is Audio service + Audio/Video major + Loudspeaker minor. The installer
rewrites an existing `Class` line, uncomments a commented one, or inserts one
under `[General]`, then restarts `bluetooth.service` (the class is only read at
start). It is idempotent, so Retry and repeated updates are safe.

**Phones paired before this version must be forgotten and paired again.** iOS
caches both the class and the service record from pairing time, so an upgraded
device will keep behaving as it did until the phone re-pairs. The installer
prints a reminder on completion.

## Checking what Billy advertises

    bluetoothctl show | grep -iE 'uuid|class'

Correct, as speaker/headset:

    Class: 0x00240414
    UUID: Audio Sink   (0000110b-...)
    UUID: Handsfree    (0000111e-...)

Wrong direction, presenting as the phone side:

    UUID: Audio Source            (0000110a-...)
    UUID: Handsfree Audio Gateway (0000111f-...)

The adapter UUID list is live and updates on a `wireplumber` restart without
restarting `bluetoothd`, so this is a fast feedback loop.

## Transport

`transport.py` matches nodes by the `bluez_input.` / `bluez_output.` prefix.
Observed node name for A2DP media on PipeWire 1.2.7 is
`bluez_input.<MAC>.2` — **not** `bluez_input.<MAC>.a2dp-source`. Do not key on
the profile suffix.

`pw-cat` in PipeWire 1.2.7 has no `--raw` option; raw PCM is what it writes to
stdout for `-`. Passing `--raw` makes every invocation exit immediately with
`unrecognized option`, which surfaces in the UI only as the generic
"PipeWire audio stream ended; reconnect or retry".

Both `pw-cat` processes inherit stderr, so their real errors appear in
`journalctl -u billy-bluetooth`, not in the UI.

`runtime.main()` retries a `StreamRestart` up to `MAX_CONSECUTIVE_RESTARTS`
times before failing the test, and requires a discovered route to survive a
short settle interval, because a freshly configured A2DP transport exposes its
node before the AVDTP Open/Start exchange completes.

## Mouth articulation

Bluetooth audio drives the mouth from the same envelope follower as everything
else: `movements.flap_from_pcm_chunk()`. It is RMS-based, not speech analysis.
RMS below `threshold / 2` stops the mouth, RMS above `threshold` (1500) triggers
a flap, and a flap fires at most every `min_flap_gap` (0.15 s). Motor speed and
flap duration both scale with normalised RMS, speed is multiplied by
`MOUTH_BOOST`, and duration by `1 + level * 0.5`. `runtime.session()` calls
`stop_mouth()` when no frame has arrived for 150 ms.

Songs drive the mouth from an isolated vocals stem. Bluetooth media is a full
mix, so a raw envelope flaps on bass and kick as hard as on voice.
`audio.voice_band()` is the real-time stand-in: a 4th-order Butterworth
band-pass over `VOICE_BAND_HZ` (300-3400 Hz) applied to the flap signal only,
never to what is played. Measured response: 60 Hz retained at 0.3%, 8 kHz at
1.7%, 1-2 kHz at ~100%. Filter state carries across frames, so 20 ms chunks are
bit-identical to one long pass; `audio.reset_voice_band()` clears it and
`runtime.session()` calls it per session.

`render_external_pcm()` passes `chunk_ms=CHUNK_MS` rather than the 20 ms frame
length, so flap duration can reach the same ceiling as the song path instead of
being clamped shorter.

Articulation level comes from the persona's `meta.mouth_articulation` when it
sets one, otherwise from `MOUTH_ARTICULATION` (0-10, default 5). Before this
version `_articulation_multiplier()` fell back to a hardcoded `5` and the
environment setting was inert.

## Diagnostics

    bluetoothctl show | grep -iE 'uuid|class'     # what Billy advertises
    bluetoothctl info <phone-MAC>                 # Paired / Trusted / Connected
    pw-dump | grep -a bluez                       # profile, connection, nodes
    wpctl status                                  # devices and source nodes
    cat /run/user/$(id -u)/billy-bluetooth.json   # runtime state
    sudo journalctl -u billy-bluetooth -n 60      # runtime + pw-cat stderr
    sudo journalctl -u bluetooth -b               # endpoint registration
    sudo btmon                                    # AVDTP negotiation

`bluez5.profile: off` with `api.bluez5.connection: disconnected` means the phone
is not streaming; there is nothing wrong on Billy's side to fix. With no phone
connected in an enabled role, `pw-dump | grep -c bluez` returning 0 is normal.

There is no persistent user journal on a default Raspberry Pi OS Lite install,
so `journalctl --user -u wireplumber` returns nothing. Run WirePlumber in the
foreground instead, redirecting to a file (piping to `grep` block-buffers and
loses the output):

    systemctl --user stop wireplumber
    timeout 8 env WIREPLUMBER_DEBUG=D wireplumber > /tmp/wp.log 2>&1
    systemctl --user start wireplumber

## Which UI button restarts what

Restart touches only `billy.service`, plus `billy-webconfig.service` on the full
restart. It does not restart `wireplumber` or `billy-bluetooth.service`, so it
never picks up audio configuration changes, and it ends a running audio test
because of the `Conflicts=billy.service`.

Retry in the Bluetooth section re-runs `install-bluetooth.sh`, which reinstalls
the configuration fragments **from the repository checkout on the Pi** and
restarts `wireplumber`. This is the only UI path that reloads audio
configuration. Hand-editing `~/.config/wireplumber/...` and then pressing Retry
silently reverts the edit; edit the repository copy instead.

## Validation

Mocked tests cover first installation after restart, completed/changed migrations,
failure and retry, request authorization, both configuration formats, and retained
AI/Bluetooth service ordering. JavaScript checks cover navigation and reconnect
without duplicate polling or handlers.

Validated on hardware (Raspberry Pi, WirePlumber 0.4.13, PipeWire 1.2.7,
BlueZ, iPhone) on 2026-09-14: package installation, pairing, A2DP media audio
through Billy's speaker, and mouth articulation.

Not yet re-validated on hardware since that session: the installer's Class of
Device step (it was applied by hand when validated), the `voice_band` flap
filter, and the `MOUTH_ARTICULATION` fallback.

Still unvalidated on hardware: HFP call audio, AEC behaviour during calls, and
mouth timing during calls.
