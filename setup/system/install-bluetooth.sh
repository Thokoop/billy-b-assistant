#!/usr/bin/env bash
# Called automatically by webconfig after an update, as the Billy service user.
# Python dependencies are installed by the existing requirements.txt workflow.
set -euo pipefail
billy_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
billy_user="$(id -un)"
billy_uid="$(id -u)"
if [[ "$billy_uid" == 0 || ! "$billy_root" =~ ^/[a-zA-Z0-9_./-]+$ || ! "$billy_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]]; then
    echo 'Bluetooth setup requires a non-root Billy user and an installation path without special characters.' >&2
    exit 1
fi
if [[ "$(systemctl show billy.service -p User --value)" != "$billy_user" ]]; then
    echo 'The web interface and billy.service must run as the same user.' >&2
    exit 1
fi
# Never prompt for a password on an unattended Pi.
sudo -n true
# Configure both supported versions before packages can start WirePlumber.
# Only Billy-owned fragments are written; ALSA remains owned by Billy.
billy_config="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber"
mkdir -p "$billy_config/wireplumber.conf.d" "$billy_config/bluetooth.lua.d" "$billy_config/main.lua.d"
install -m 644 "$billy_root/setup/system/51-billy-bluetooth.conf" "$billy_config/wireplumber.conf.d/51-billy-bluetooth.conf"
install -m 644 "$billy_root/setup/system/51-billy-bluetooth.lua" "$billy_config/bluetooth.lua.d/51-billy-bluetooth.lua"
printf 'alsa_monitor.enabled = false\n' > "$billy_config/main.lua.d/51-billy-alsa.lua"
export DEBIAN_FRONTEND=noninteractive
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 update
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends \
    bluez pipewire pipewire-bin wireplumber libspa-0.2-bluetooth dbus-user-session
billy_wp_version="$(wireplumber --version | sed -n 's/.*wireplumber \([0-9][0-9.]*\).*/\1/p' | head -1)"
if ! dpkg --compare-versions "${billy_wp_version:-0}" ge 0.4; then
    echo 'Bluetooth requires WirePlumber 0.4 or newer from the operating system repositories.' >&2
    exit 1
fi
billy_tmp="$(mktemp -d)"
trap 'rm -rf "$billy_tmp"' EXIT
# billy.service on devices already in the field cannot be rewritten, so grant
# the audio stack real-time scheduling with a drop-in instead of editing the
# unit. PortAudio raises its capture callback thread to real-time priority and
# a systemd unit cannot unless LimitRTPRIO allows it; without this the
# microphone loses about 0.7 frames per second of speech under load, and
# PortAudio reports no overflow, so nothing in the logs shows it. Additive and
# safe on any existing install; takes effect on the next billy.service start.
printf '[Service]\nLimitRTPRIO=95\nLimitMEMLOCK=infinity\n' > "$billy_tmp/realtime"
sudo -n mkdir -p /etc/systemd/system/billy.service.d
sudo -n install -m 644 "$billy_tmp/realtime" \
    /etc/systemd/system/billy.service.d/10-billy-realtime.conf

sed -e "s|@ROOT@|$billy_root|g" -e "s|@USER@|$billy_user|g" -e "s|@UID@|$billy_uid|g" \
    "$billy_root/setup/system/billy-bluetooth.service.in" > "$billy_tmp/unit"
sudo -n install -m 644 "$billy_tmp/unit" /etc/systemd/system/billy-bluetooth.service
billy_systemctl="$(command -v systemctl)"
printf '%s ALL=(root) NOPASSWD: %s start billy-bluetooth.service, %s stop billy-bluetooth.service, %s start billy.service\n' \
    "$billy_user" "$billy_systemctl" "$billy_systemctl" "$billy_systemctl" > "$billy_tmp/sudoers"
sudo -n visudo -cf "$billy_tmp/sudoers"
sudo -n install -m 440 "$billy_tmp/sudoers" /etc/sudoers.d/billy-bluetooth
# A user-specific policy works in the already-running web process, without
# requiring logout or a reboot to acquire new supplementary groups.
cat > "$billy_tmp/dbus" <<EOF
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><policy user="$billy_user">
  <allow send_destination="org.bluez"/>
</policy></busconfig>
EOF
sudo -n install -m 644 "$billy_tmp/dbus" /etc/dbus-1/system.d/billy-bluetooth.conf
sudo -n loginctl enable-linger "$billy_user"
sudo -n systemctl start "user@$billy_uid.service"
# iOS decides what a device is from Class of Device as well as SDP. With the
# stock Raspberry Pi class it lists Billy as an audio output but refuses to
# stream to it. 0x200414 = Audio service, Audio/Video major, Loudspeaker minor.
# Phones cache the class from pairing time, so an already-paired phone must be
# forgotten and paired again after this first lands.
billy_main_conf=/etc/bluetooth/main.conf
if [[ ! -f "$billy_main_conf" ]]; then
    printf '[General]\nClass = 0x200414\n' | sudo -n tee "$billy_main_conf" >/dev/null
elif grep -qE '^[[:space:]]*Class[[:space:]]*=' "$billy_main_conf"; then
    sudo -n sed -i -E 's|^[[:space:]]*Class[[:space:]]*=.*|Class = 0x200414|' "$billy_main_conf"
elif grep -qE '^[[:space:]]*#[[:space:]]*Class[[:space:]]*=' "$billy_main_conf"; then
    sudo -n sed -i -E '0,/^[[:space:]]*#[[:space:]]*Class[[:space:]]*=.*/s||Class = 0x200414|' "$billy_main_conf"
elif grep -qE '^\[General\]' "$billy_main_conf"; then
    sudo -n sed -i '0,/^\[General\]/s//[General]\nClass = 0x200414/' "$billy_main_conf"
else
    printf '\n[General]\nClass = 0x200414\n' | sudo -n tee -a "$billy_main_conf" >/dev/null
fi
sudo -n systemctl enable bluetooth.service
# Restart rather than enable --now: the adapter class is only read at start.
sudo -n systemctl restart bluetooth.service
sudo -n systemctl daemon-reload
export XDG_RUNTIME_DIR="/run/user/$billy_uid"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
systemctl --user enable --now pipewire.socket wireplumber.service
systemctl --user restart wireplumber.service
systemctl --user is-active --quiet pipewire.socket wireplumber.service
echo 'Bluetooth setup complete. Audio test remains off until started in System Settings.'
echo 'Phones paired before this version must be forgotten and paired again.'
