#!/bin/bash
# Run as root (sudo) by the web panel: "Restart Audio Services" and Update.

# Install changed unit files first, so an update can change how a service
# runs (only units that install_server.sh already installed).
UNIT_DIR=/etc/systemd/system
SRC_DIR="$(dirname "$(realpath "$0")")/systemd"
changed=0
for unit in "$SRC_DIR"/*.service; do
    name=$(basename "$unit")
    [ -f "$UNIT_DIR/$name" ] || continue
    if ! cmp -s "$unit" "$UNIT_DIR/$name"; then
        install -m 644 "$unit" "$UNIT_DIR/$name"
        echo "Updated $name"
        changed=1
    fi
done
if [ "$changed" -eq 1 ]; then
    systemctl daemon-reload
fi

systemctl restart audio_server.service
systemctl restart audio_client_on_server.service
systemctl restart mjpeg-streamer.service
systemctl restart ptt_server.service
