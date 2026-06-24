#!/bin/bash

[ "$EUID" -ne 0 ] && exec sudo "$0" "$@"

LOG_TAG="BillyWiFiCheck"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
ONBOARDING_FLAG="$PROJECT_ROOT/setup/.wifi_onboarding_active"
LEGACY_CAPTIVE_PORTAL_DNSMASQ_CONF="/etc/dnsmasq.d/billy-captive-portal.conf"
NM_DNSMASQ_SHARED_DIR="/etc/NetworkManager/dnsmasq-shared.d"
UNIFIED_CAPTIVE_PORTAL_DNSMASQ_CONF="$NM_DNSMASQ_SHARED_DIR/billy-captive-portal.conf"
UNIFIED_HOTSPOT_CON_NAME="Billy-Onboarding-Hotspot"
UNIFIED_HOTSPOT_SSID="Billy_Bassistant"
UNIFIED_HOTSPOT_IP="10.42.0.1/24"

# Test override: set TEST_FORCE_OFFLINE=1 to force the "no internet" branch
FORCE_OFFLINE=0

# CLI flag
for arg in "$@"; do
  case "$arg" in
    --force-offline) FORCE_OFFLINE=1 ;;
  esac
done

# env var override
if [ "${TEST_FORCE_OFFLINE:-0}" -eq 1 ]; then
  FORCE_OFFLINE=1
fi

write_legacy_captive_portal_dnsmasq_conf() {
  sudo tee "$LEGACY_CAPTIVE_PORTAL_DNSMASQ_CONF" >/dev/null <<EOF
address=/#/192.168.4.1
dhcp-option=option:router,192.168.4.1
dhcp-option=option:dns-server,192.168.4.1
dhcp-option=114,http://192.168.4.1/
EOF
}

write_unified_captive_portal_dnsmasq_conf() {
  sudo mkdir -p "$NM_DNSMASQ_SHARED_DIR"
  sudo tee "$UNIFIED_CAPTIVE_PORTAL_DNSMASQ_CONF" >/dev/null <<EOF
address=/#/10.42.0.1
dhcp-option=option:router,10.42.0.1
dhcp-option=option:dns-server,10.42.0.1
dhcp-option=114,http://10.42.0.1/
EOF
}

remove_captive_portal_dnsmasq_conf() {
  sudo rm -f "$LEGACY_CAPTIVE_PORTAL_DNSMASQ_CONF" "$UNIFIED_CAPTIVE_PORTAL_DNSMASQ_CONF"
}

ensure_unified_hotspot_profile() {
  if sudo nmcli con show "$UNIFIED_HOTSPOT_CON_NAME" >/dev/null 2>&1; then
    sudo nmcli con delete "$UNIFIED_HOTSPOT_CON_NAME" >/dev/null 2>&1 || true
  fi

  sudo nmcli con add type wifi ifname wlan0 mode ap con-name "$UNIFIED_HOTSPOT_CON_NAME" \
    ssid "$UNIFIED_HOTSPOT_SSID" autoconnect no \
    ipv4.method shared ipv4.addresses "$UNIFIED_HOTSPOT_IP" >/dev/null

  sudo nmcli con modify "$UNIFIED_HOTSPOT_CON_NAME" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg >/dev/null

  sudo nmcli con modify "$UNIFIED_HOTSPOT_CON_NAME" \
    remove 802-11-wireless-security >/dev/null 2>&1 || true
}

activate_unified_hotspot() {
  sudo systemctl stop hostapd >/dev/null 2>&1 || true
  sudo systemctl stop dnsmasq >/dev/null 2>&1 || true
  sudo systemctl mask --runtime hostapd >/dev/null 2>&1 || true
  sudo systemctl mask --runtime dnsmasq >/dev/null 2>&1 || true
  sudo systemctl start NetworkManager
  write_unified_captive_portal_dnsmasq_conf
  ensure_unified_hotspot_profile
  sudo nmcli dev set wlan0 managed yes >/dev/null 2>&1 || true
  sudo nmcli con down "$UNIFIED_HOTSPOT_CON_NAME" >/dev/null 2>&1 || true
  sudo nmcli dev disconnect wlan0 >/dev/null 2>&1 || true
  sudo nmcli con up "$UNIFIED_HOTSPOT_CON_NAME" >/dev/null
}

echo "[$LOG_TAG] Checking internet connectivity..."
WIFI_ONBOARDING_MODE="legacy"
if [ -f "$ENV_FILE" ]; then
    env_mode=$(grep -E '^WIFI_ONBOARDING_MODE=' "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr '[:upper:]' '[:lower:]')
    if [ -n "$env_mode" ]; then
        WIFI_ONBOARDING_MODE="$env_mode"
    fi
fi

has_active_wifi=0
if nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null | grep -qE '^[^:]+:(wifi|802-11-wireless):wlan0$'; then
    has_active_wifi=1
fi

should_start_onboarding=0
if [ "$WIFI_ONBOARDING_MODE" = "unified" ]; then
    if [ "$has_active_wifi" -eq 0 ]; then
        should_start_onboarding=1
    fi
else
    # Legacy behavior: treat missing internet access as "offline"
    if ! [ "$FORCE_OFFLINE" -eq 0 ] || ! ping -c 1 -W 3 8.8.8.8 &> /dev/null; then
        should_start_onboarding=1
    fi
fi

if [ "$should_start_onboarding" -eq 0 ]; then
    echo "[$LOG_TAG] Connectivity check passed."
    remove_captive_portal_dnsmasq_conf
    rm -f "$ONBOARDING_FLAG"
    sudo systemctl stop billy-wifi-setup.service
else
    echo "[$LOG_TAG] No internet connection. Starting onboarding flow..."
    touch "$ONBOARDING_FLAG"
    if [ "$WIFI_ONBOARDING_MODE" = "unified" ]; then
        remove_captive_portal_dnsmasq_conf
        activate_unified_hotspot
        echo "[$LOG_TAG] Unified hotspot active via NetworkManager."
    else
        sudo systemctl unmask --runtime hostapd >/dev/null 2>&1 || true
        sudo systemctl unmask --runtime dnsmasq >/dev/null 2>&1 || true
        # Stop conflicting services for legacy mode.
        sudo systemctl stop NetworkManager

        # Bring wlan0 down and back up with static IP
        ip link set wlan0 down
        ip addr flush dev wlan0
        ip link set wlan0 up
        sleep 1
        ip addr add 192.168.4.1/24 dev wlan0

        echo "[$LOG_TAG] IP on wlan0:"
        ip a show wlan0

        write_legacy_captive_portal_dnsmasq_conf

        # Restart services
        sudo systemctl restart dnsmasq
        sudo systemctl restart hostapd

        # Start the Flask onboarding app (in service)
        sudo systemctl restart billy-wifi-setup.service
        echo "[$LOG_TAG] Legacy onboarding Flask app launched."
    fi

    if [ "$WIFI_ONBOARDING_MODE" = "unified" ]; then
        sudo systemctl restart billy-webconfig.service
        sudo systemctl stop billy-wifi-setup.service
        echo "[$LOG_TAG] Unified onboarding UI launched on the main web interface."
    fi
fi
