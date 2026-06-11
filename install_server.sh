#!/bin/bash
# Install required packages and configure environment for SERVER part

# ANSI color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

# Change to the script's own directory so all relative paths work correctly
cd "$(dirname "$(realpath "$0")")"

handle_error() {
    echo -e "${RED}Error in command: $1${NC}"
    echo -e "${RED}Exiting script...${NC}"
    exit 1
}

set -e
trap 'handle_error "$BASH_COMMAND"' ERR

echo -e "${GREEN}Updating package lists...${NC}"
apt-get update || { echo -e "${RED}Failed to update packages${NC}"; exit 1; }

echo -e "${GREEN}Installing required packages...${NC}"
apt-get install -y \
  make gcc python3 python3-pip swig python3-dev python3-setuptools mc socat avahi-daemon \
  python3-flask python3-waitress gstreamer1.0-plugins-base gstreamer1.0-alsa \
  cmake build-essential libjpeg-dev libv4l-dev \
  gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  python3-libgpiod python3-smbus2 python3-requests python3-serial ||
  { echo -e "${RED}Failed to install packages${NC}"; exit 1; }

echo -e "${GREEN}Building mjpg-streamer...${NC}"
git clone https://github.com/jacksonliam/mjpg-streamer.git
cd mjpg-streamer/mjpg-streamer-experimental
make -j || { echo -e "${RED}Failed to build mjpg-streamer${NC}"; exit 1; }
make install || { echo -e "${RED}Failed to install mjpg-streamer${NC}"; exit 1; }
cd ../..

echo -e "${GREEN}Cleaning up...${NC}"
apt-get autoremove -y && apt-get autoclean -y
rm -rf mjpg-streamer

echo -e "${GREEN}Setting permissions...${NC}"
usermod -a -G dialout pi || { echo -e "${RED}Failed to modify user groups${NC}"; exit 1; }
usermod -a -G i2c pi || { echo -e "${RED}Failed to add pi to i2c group${NC}"; exit 1; }
chmod +x ./network/combined_ptt_service.py

# Create config files owned by pi (not root) if they don't exist yet
echo -e "${GREEN}Creating config files...${NC}"

if [ ! -f ./client_ip.cfg ]; then
  sudo -u pi bash -c "echo '0.0.0.0' > ./client_ip.cfg"
  echo -e "${GREEN}Created client_ip.cfg — set the client IP before reboot.${NC}"
fi

if [ ! -f ./server_ip.cfg ]; then
  sudo -u pi bash -c "echo '0.0.0.0' > ./server_ip.cfg"
  echo -e "${GREEN}Created server_ip.cfg — set the server IP before reboot.${NC}"
fi

if [ ! -f ./web/password.txt ]; then
  sudo -u pi bash -c "echo '1234' > ./web/password.txt"
  echo -e "${GREEN}Created web/password.txt with default password '1234'. Change it after install.${NC}"
fi

echo -e "${GREEN}Setting hostname...${NC}"
hostnamectl set-hostname nano-server-2 || { echo -e "${RED}Failed to set hostname${NC}"; exit 1; }

echo -e "${GREEN}Configuring hardware...${NC}"
chmod +x ./setup_armbian_env.sh
bash ./setup_armbian_env.sh

/usr/sbin/alsactl -f /var/lib/alsa/asound.state store || true

setup_service() {
  local service_name=$1
  cp ./systemd/${service_name}.service /etc/systemd/system/ ||
    { echo -e "${RED}Failed to copy ${service_name}.service${NC}"; exit 1; }
  systemctl daemon-reload
  systemctl start ${service_name}.service ||
    { echo -e "${RED}Failed to start ${service_name}.service${NC}"; exit 1; }
  systemctl enable ${service_name}.service ||
    { echo -e "${RED}Failed to enable ${service_name}.service${NC}"; exit 1; }
  echo -e "${GREEN}${service_name}.service configured successfully${NC}"
}

services=("ptt_server" "audio_server" "audio_client_on_server"
          "web_config_server" "alsa_restore" "mjpeg-streamer" "relay-web")

for service in "${services[@]}"; do
  setup_service "$service"
done

sudoers_entry="pi ALL=(ALL) NOPASSWD: /home/pi/nano-server/restart_services_on_server.sh"
echo "$sudoers_entry" | tee /etc/sudoers.d/nano-server > /dev/null
chmod 0440 /etc/sudoers.d/nano-server

echo -e "${GREEN}Configuration completed successfully. Please edit client_ip.cfg and server_ip.cfg, then reboot (sudo reboot).${NC}"
