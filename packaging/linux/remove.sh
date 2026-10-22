#!/bin/sh
# Stops and removes the service and the Python environment (run as root).
# --purge also deletes the data (database, logs, uploaded plugins) and the user.
set -e
if [ -d /run/systemd/system ]; then
    systemctl disable --now lan-streamer.service >/dev/null 2>&1 || true
fi
rm -f /etc/systemd/system/lan-streamer.service
if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi
rm -rf /opt/lan-streamer/venv
if [ "$1" = --purge ]; then
    rm -rf /var/lib/lan-streamer /var/cache/lan-streamer
    if id lanstreamer >/dev/null 2>&1; then userdel lanstreamer; fi
fi
