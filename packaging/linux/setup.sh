#!/bin/sh
# Finishes an installation in /opt/lan-streamer (run as root by the .deb's
# postinst and by install.sh): service user, Python environment from the bundled
# wheels (no internet needed), systemd service started.
set -e
ROOT=/opt/lan-streamer

if ! id lanstreamer >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/lan-streamer --no-create-home \
        --shell /usr/sbin/nologin --comment "lan-streamer service" lanstreamer
fi
for group in video render; do  # GPU encoding (NVIDIA, VA-API for AMD/Intel)
    if getent group "$group" >/dev/null; then usermod -aG "$group" lanstreamer; fi
done

# PyGObject comes from the distribution (python3-gi): --system-site-packages
python3 -m venv --clear --system-site-packages "$ROOT/venv"
"$ROOT/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    --no-index --find-links "$ROOT/wheels" lanstreamer

install -m 644 "$ROOT/share/lan-streamer.service" /etc/systemd/system/lan-streamer.service
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemctl enable lan-streamer.service >/dev/null 2>&1
    systemctl restart --no-block lan-streamer.service
    echo "lan-streamer is running: http://$(hostname -I 2>/dev/null | cut -d' ' -f1 || echo localhost):8080"
fi
