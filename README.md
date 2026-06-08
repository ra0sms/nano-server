# CAESAR — Remote Radio Station Server

**CAESAR** (Client Audio Equipment Server And Remote) is a server-side component of a remote ham radio station system. It runs on a **NanoPi NEO** single-board computer with **Armbian** and provides audio streaming, PTT control via GPIO, video streaming, relay switching, and a web-based configuration interface.

---

## Features

- 🎙️ **Bidirectional audio** over UDP (GStreamer + Opus codec)
- 📡 **PTT control** via GPIO with client reachability monitoring
- 📷 **Video streaming** via mjpg-streamer (MJPEG over HTTP)
- 🔌 **Relay control** via I2C (2× MCP23017, 16 relays total)
- 🌐 **Web configuration** — IP addresses, audio settings, volume, profiles
- 🔗 **CAT interface** forwarding over UDP via ser2net
- 🔒 **Fail-safe** — PTT is forced OFF when client disconnects

---

## Hardware

| Component | Details |
|---|---|
| SBC | NanoPi NEO (Allwinner H3) |
| OS | Armbian (Debian Bookworm or later) |
| Audio | USB sound card (card index 0) |
| Camera | USB webcam on `/dev/video1` |
| Relay board | 2× I2C GPIO expander at `0x20` / `0x21` |
| CAT interface | USB-to-Serial on `/dev/ttyCAT` (via udev symlink) |
| CON LED | GPIO PC2 (line 66) |
| PTT output | GPIO PC3 (line 67) |

---

## Architecture

```
Client PC                          NanoPi NEO (Server)
─────────────────────────────────────────────────────────
                    UDP :5000  ←──  Audio TX (mic → client)
                    UDP :5000  ──→  Audio RX (client → speaker)
                    UDP :5001  ──→  PTT commands (0/1)
                    UDP :5002  ←→   Ping / RTT monitoring
                    TCP :8080  ←──  Web config UI (Flask)
                    TCP :5050  ←──  Relay control UI (Flask)
                    TCP :8081  ←──  MJPEG video stream
                    UDP :3001  ←──  CAT / ser2net
```

### Systemd services

| Service | Description |
|---|---|
| `ptt_server` | PTT GPIO control + client monitor + ping responder |
| `audio_server` | GStreamer audio TX (server mic → client) |
| `audio_client_on_server` | GStreamer audio RX (client → server speaker) |
| `web_config_server` | Web UI: volume, IP config, audio settings, profiles |
| `relay-web` | Web UI: relay switching via I2C |
| `mjpeg-streamer` | MJPEG video stream from USB camera |
| `alsa_restore` | Restores ALSA mixer state at boot |

---

## Installation

### 1. Flash Armbian to SD card and boot NanoPi NEO

### 2. Clone the repository

```bash
git clone https://github.com/youruser/nano-server.git /home/pi/nano-server
cd /home/pi/nano-server
```

### 3. Run the install script as root

```bash
sudo bash install_server.sh
```

The script will:
- Install all required packages (GStreamer, Flask, gpiod, ser2net, mjpg-streamer, etc.)
- Build and install **mjpg-streamer** from source
- Create `client_ip.cfg`, `server_ip.cfg`, and `web/password.txt` with defaults
- Configure hardware overlays in `/boot/armbianEnv.txt`
- Register and start all systemd services
- Add a sudoers entry for `restart_services_on_server.sh`

### 4. Set IP addresses

```bash
echo '192.168.1.100' > /home/pi/nano-server/client_ip.cfg   # IP of the client PC
echo '192.168.1.10'  > /home/pi/nano-server/server_ip.cfg   # IP of this server
```

Or use the web configuration interface at `http://<server-ip>:8080/config`.

### 5. Change the default password for the relay web panel

```bash
echo 'yourpassword' > /home/pi/nano-server/web/password.txt
```

### 6. Set up the CAT USB port udev symlink (with transceiver connected)

```bash
sudo bash /home/pi/nano-server/fix_usb_ports.sh
```

### 7. Configure ser2net

```bash
sudo bash /home/pi/nano-server/create_ser2net_yaml.sh
```

### 8. Reboot

```bash
sudo reboot
```

---

## Armbian Hardware Configuration

The script `setup_armbian_env.sh` patches `/boot/armbianEnv.txt` to enable the required device tree overlays. It **never modifies** `rootdev` or `rootfstype` so the system remains bootable.

To apply manually on a running system:

```bash
sudo bash /home/pi/nano-server/setup_armbian_env.sh
sudo reboot
```

Required overlays: `i2c0 uart1 uart2 uart3 usbhost0 usbhost1 usbhost2 usbhost3 w1-gpio`

---

## Web Interfaces

### Configuration & Volume — port 8080

| Page | URL | Description |
|---|---|---|
| Volume | `http://<ip>:8080/` | Speaker and mic level control |
| Config | `http://<ip>:8080/config` | IP addresses, audio settings, profiles |
| Status | `http://<ip>:8080/status` | Client RTT / connection status |

### Relay Control — port 5050

Access at `http://<ip>:5050/` (password protected).  
Supports 16 relays in two groups of 8 with **toggle** and **switch** (radio) modes.  
Default password: `1234` — change it in `web/password.txt`.

---

## Profiles

Up to 5 configuration profiles (server IP, client IP, audio settings) can be saved and loaded from the web configuration interface. Profile files are stored in `profiles/`.

---

## Restarting Services

To restart all runtime services at once:

```bash
sudo /home/pi/nano-server/restart_services_on_server.sh
```

Or restart individual services:

```bash
sudo systemctl restart ptt_server.service
sudo systemctl restart audio_server.service
```

To reload `client_ip.cfg` without restarting the PTT service:

```bash
sudo systemctl kill -s SIGHUP ptt_server.service
```

---

## Audio Configuration

Edit `audio/audio_config.cfg`:

```ini
RATE=48000       # sample rate: 48000 or 24000 Hz
LATENCY=100000   # buffer time in microseconds (100 ms)
```

Or use the **Configuration** page in the web UI.

---

## Project Structure

```
nano-server/
├── install_server.sh              # Main installation script
├── setup_armbian_env.sh           # Patches /boot/armbianEnv.txt safely
├── restart_services_on_server.sh  # Restart all runtime services
├── fix_usb_ports.sh               # Create /dev/ttyCAT udev symlink
├── create_ser2net_yaml.sh         # Generate ser2net config
├── client_ip.cfg                  # Client IP (git-ignored)
├── server_ip.cfg                  # Server IP (git-ignored)
├── armbianEnv.txt                 # Reference overlay config
├── audio/
│   ├── audio_server.sh            # GStreamer TX pipeline
│   ├── audio_client_on_server.sh  # GStreamer RX pipeline
│   └── audio_config.cfg           # Rate and buffer settings
├── network/
│   └── combined_ptt_service.py    # PTT + ping monitor + ping responder
├── web/
│   ├── web_config_server.py       # Config/volume/status web UI
│   ├── app.py                     # Relay control web UI
│   ├── config.json                # Relay names and group modes
│   └── password.txt               # Relay UI password (git-ignored)
├── systemd/                       # Service unit files
├── profiles/                      # Saved configuration profiles (git-ignored)
└── docs/
    ├── gpio-info.txt              # GPIO line numbers reference
    └── nano-pinout.jpg            # NanoPi NEO pinout diagram
```

---

## License

MIT License

Copyright (c) RA0SMS 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
