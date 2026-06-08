#!/bin/bash
# Configure /boot/armbianEnv.txt for NanoPi NEO (Allwinner H3 / sun8i)
#
# WHAT THIS SCRIPT CHANGES:
#   overlay_prefix  = sun8i-h3
#   overlays        = i2c0 uart1 uart2 uart3 usbhost0 usbhost1 usbhost2 w1-gpio
#   verbosity       = 1
#   bootlogo        = false
#   console         = serial
#   disp_mode       = 1920x1080p60
#   usbstoragequirks= 0x2537:0x1066:u,0x2537:0x1068:u
#
# WHAT THIS SCRIPT NEVER TOUCHES:
#   rootdev, rootfstype  — device-specific, wrong value = unbootable system

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ARMBIAN_ENV="/boot/armbianEnv.txt"

# --- Sanity checks ---

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}Run as root (sudo).${NC}"
    exit 1
fi

if [ ! -f "$ARMBIAN_ENV" ]; then
    echo -e "${RED}$ARMBIAN_ENV not found. Is this Armbian?${NC}"
    exit 1
fi

# --- Backup ---

BACKUP="${ARMBIAN_ENV}.bak"
cp "$ARMBIAN_ENV" "$BACKUP"
echo -e "${GREEN}Backup saved: $BACKUP${NC}"

# --- Helper: set or append a key=value ---

set_param() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$ARMBIAN_ENV"; then
        local old
        old=$(grep "^${key}=" "$ARMBIAN_ENV" | head -1)
        sed -i "s|^${key}=.*|${key}=${value}|" "$ARMBIAN_ENV"
        echo -e "  ${YELLOW}updated${NC}  ${key}: $(echo "$old" | cut -d= -f2-) → ${value}"
    else
        echo "${key}=${value}" >> "$ARMBIAN_ENV"
        echo -e "  ${GREEN}added${NC}    ${key}=${value}"
    fi
}

# --- Apply settings ---

echo -e "${GREEN}Patching $ARMBIAN_ENV ...${NC}"

set_param "overlay_prefix"   "sun8i-h3"
set_param "overlays"         "i2c0 uart1 uart2 uart3 usbhost0 usbhost1 usbhost2 usbhost3 w1-gpio"
set_param "verbosity"        "1"
set_param "bootlogo"         "false"
set_param "console"          "serial"
set_param "disp_mode"        "1920x1080p60"
set_param "usbstoragequirks" "0x2537:0x1066:u,0x2537:0x1068:u"

# --- Show result ---

echo ""
echo -e "${GREEN}Result:${NC}"
cat "$ARMBIAN_ENV"

echo ""
echo -e "${GREEN}Done. Reboot to apply: sudo reboot${NC}"
