#!/bin/bash
# Fix ALSA card order: make C-Media USB Audio Device card 0, webcam card 2
# Run as root
# Based on lsusb output:
#   C-Media USB Audio: 0d8c:0012 -> card 0
#   Full HD webcam:     1bcf:2283 -> card 2

CONF_FILE="/etc/modprobe.d/alsa-card-order.conf"

cat > "$CONF_FILE" << 'EOF'
# USB audio card order fix
# C-Media USB Audio Device (main sound card) -> index 0
options snd-usb-audio index=0 vid=0x0d8c pid=0x0012
# Full HD webcam -> index 2 (so it doesn't interfere)
options snd-usb-audio index=2 vid=0x1bcf pid=0x2283
EOF

echo "Created $CONF_FILE"
echo ""
echo "Current card order:"
cat /proc/asound/cards
echo ""
echo "Reboot required for changes to take effect."
echo "After reboot, verify with: cat /proc/asound/cards"