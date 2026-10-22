#!/bin/sh
# CI ONLY: never use this script to provision or upgrade a customer Pi.
set -eu
if [ ! -f /.dockerenv ] || [ "$(id -u)" != 0 ]; then
    echo 'This helper requires a disposable root Docker container.' >&2
    exit 1
fi
codename=$(python -c 'import platform; print(platform.freedesktop_os_release()["VERSION_CODENAME"])')
case "$codename" in bookworm|trixie) ;; *) exit 1 ;; esac
# liblgpio is supplied by Raspberry Pi OS, not the base Debian archive.
# Use only its official signed repository, with the reviewed public key.
cp dependencies/raspberrypi-archive.gpg /usr/share/keyrings/billy-ci-raspberrypi.gpg
cat > /etc/apt/sources.list.d/billy-ci-raspberrypi.sources <<SOURCES
Types: deb
URIs: https://archive.raspberrypi.com/debian/
Suites: $codename
Components: main
Architectures: arm64
Signed-By: /usr/share/keyrings/billy-ci-raspberrypi.gpg
SOURCES
apt-get update -qq
python -c 'import json; m=json.load(open("dependencies/system-packages.json")); print("\n".join(sorted(set(m["build"] + m["runtime"] + m["bluetooth"]))))' > /tmp/billy-system-packages
DEBIAN_FRONTEND=noninteractive xargs apt-get install -qq --yes --no-install-recommends < /tmp/billy-system-packages
