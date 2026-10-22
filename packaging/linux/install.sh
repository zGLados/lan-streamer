#!/bin/sh
# Installs lan-streamer from this folder (the unpacked .tar.gz) to /opt/lan-streamer
# and starts it as a systemd service. Run as root:  sudo ./install.sh
# On Debian/Ubuntu the needed packages are installed with apt-get; elsewhere
# install the equivalents of depends.txt yourself first.
#   sudo ./install.sh --uninstall [--purge]   removes it (--purge: also the data)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
if [ "$(id -u)" != 0 ]; then echo "Please run as root: sudo $0 $*" >&2; exit 1; fi

if [ "$1" = --uninstall ]; then
    "$HERE/opt/lan-streamer/lib/remove.sh" "$2"
    rm -rf /opt/lan-streamer /usr/local/bin/lan-streamer
    echo "lan-streamer removed."
    exit 0
fi

if command -v apt-get >/dev/null; then
    packages=$(grep -v '^#' "$HERE/depends.txt" | sed 's/ *(.*)//' | tr '\n' ' ')
    apt-get update -q
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q $packages
else
    echo "No apt-get: make sure the packages in depends.txt (or their equivalents) are installed."
fi

rm -rf /opt/lan-streamer
cp -a "$HERE/opt/lan-streamer" /opt/lan-streamer
ln -sf /opt/lan-streamer/bin/lan-streamer /usr/local/bin/lan-streamer
/opt/lan-streamer/lib/setup.sh
