#!/usr/bin/python3
import asyncio
import glob
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests
import serial
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    session,
    stream_with_context,
)
from smbus2 import SMBus

# ================= CONFIGURATION =================
app = Flask(__name__)

# Secret key used to sign the session cookie. A hardcoded key baked into
# the source would let anyone who reads the code forge a valid "auth"
# session cookie and bypass /login entirely, so it's generated once and
# persisted next to password.txt instead.
_SECRET_KEY_FILE = Path(__file__).with_name("secret_key.txt")
if _SECRET_KEY_FILE.exists():
    app.secret_key = _SECRET_KEY_FILE.read_text().strip()
else:
    app.secret_key = secrets.token_hex(32)
    _SECRET_KEY_FILE.write_text(app.secret_key)
    try:
        os.chmod(_SECRET_KEY_FILE, 0o600)
    except Exception:
        pass

# Password
_PASSWORD_FILE = Path(__file__).with_name("password.txt")
PASSWORD = _PASSWORD_FILE.read_text().strip() if _PASSWORD_FILE.exists() else "1234"

# I2C for relays (PCF8574T expanders). The relay board is optional: the web
# panel must keep running even when no I2C module is connected (e.g. during a
# bare-NanoPi setup). So opening the bus AND probing both expander addresses
# are wrapped in try/except — if anything fails, `bus` stays None and every
# relay write is skipped instead of crashing the service.
ADDR1 = 0x20
ADDR2 = 0x21

bus = None
try:
    _probe_bus = SMBus(0)
    # Probing writes the initial all-off state, so this is safe on real hardware.
    _probe_bus.write_byte(ADDR1, 0xFF)
    _probe_bus.write_byte(ADDR2, 0xFF)
    bus = _probe_bus
    print("[relay] I2C OK: relay board detected")
except Exception as _e:
    print(f"Warning: I2C / relay board not available ({_e}); relay control disabled")

state1 = 0xFF
state2 = 0xFF

# ================= TRANSFER CONFIG =================
TRX_CONFIG_FILE = Path(__file__).with_name("trx_config.json")

default_trx_config = {
    # The CAT serial port is chosen by the user on the web Settings tab; an
    # empty default means the TRX stays offline until a port is selected.
    "serial_port": "",
    "baudrate": 19200,
    "protocol": "Icom",
    "radio_addr": 0x70,
    "ctrl_addr": 0xE0,
    "tcp_port": 3001,
    "enabled": True,
    "uart1_enabled": True,
    "uart1_port": "/dev/ttyS1",
}

trx_config = {}

# Radio state
radio_state = {
    "freq": 0,
    "band": "Unknown",
    "online": False,
    "last_rx": 0,
    "mode": "Unknown",
}

# Serial and async components
ser = None
ser_uart1 = None
clients = set()
decoder = None
loop = None

# Timestamp (time.time()) of the most recent CAT frame an external controller
# sent toward the radio — either through the UART1 transparent relay or a TCP
# client. The poller uses this to tell whether an external program currently
# owns the CAT port, so the server knows when it is safe to run its own
# CI-V / IF polling without corrupting the stream.
external_cat_time = 0.0

# How long (seconds) an external controller may stay silent before the server
# considers the CAT port free and resumes its own polling.
EXTERNAL_CAT_TIMEOUT = 5.0

# Serialize writes to the CAT port (ser). pyserial write() is NOT thread-safe:
# multiple threads (uart1_reader, tcp_client, poller) write to the same port,
# and interleaved writes can corrupt the CAT frame and desynchronize the
# transceiver. A single lock prevents mid-byte interleaving and also guards
# the close+reopen cycle during automatic error recovery.
ser_lock = threading.Lock()

# ================= RELAY FUNCTIONS =================


def apply():
    """Write the relay states to the I2C expanders. No-op if the I2C bus was not
    available at startup, and tolerant of a mid-run bus error (e.g. the relay
    board being unplugged), so a missing/disconnected I2C module never crashes
    the web service."""
    if bus is None:
        return
    try:
        bus.write_byte(ADDR1, state1)
        bus.write_byte(ADDR2, state2)
    except Exception as e:
        print(f"[relay] I2C write failed: {e} (relay board disconnected?)")


def get_state():
    bits = []
    for i in range(8):
        bits.append(1 if (state1 & (1 << i)) == 0 else 0)
    for i in range(8):
        bits.append(1 if (state2 & (1 << i)) == 0 else 0)
    return bits


def set_relay(n, on):
    global state1, state2
    if n < 8:
        if on:
            state1 &= ~(1 << n)
        else:
            state1 |= 1 << n
    else:
        n -= 8
        if on:
            state2 &= ~(1 << n)
        else:
            state2 |= 1 << n


def toggle_relay(n):
    global state1, state2, ptt_active
    if ptt_active:
        print("🔒 PTT active — relay toggle blocked")
        return

    bits = get_state()
    group = 0 if n < 8 else 1
    bit = n if n < 8 else n - 8
    mode = config["group_mode"][group]

    if mode == "switch":
        if group == 0:
            state1 = 0xFF & ~(1 << bit)
        else:
            state2 = 0xFF & ~(1 << bit)
        return

    set_relay(n, not bits[n])


# ================= RELAY CONFIG =================

CONFIG_FILE = Path(__file__).with_name("config.json")

default_config = {
    "names": [f"Relay {i + 1}" for i in range(16)],
    "group_mode": ["toggle", "toggle"],
}

config = {}


def load_relay_config():
    global config
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
            if "names" not in config:
                config["names"] = default_config["names"]
            if "group_mode" not in config:
                config["group_mode"] = default_config["group_mode"]
        except Exception:
            config = default_config.copy()
    else:
        config = default_config.copy()
        save_relay_config()


def save_relay_config():
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ================= AMATEUR BAND DEFINITIONS =================
# Single source of truth for band ranges: used by the default band relay
# rules, freq_to_band() and the TRX "set band" control, so they can't drift
# out of sync with each other.
# Each entry: (from_khz, to_khz, name, default_target_freq_hz)
AMATEUR_BANDS = [
    (1800, 2000, "160m", 1840000),
    (3500, 3800, "80m", 3573000),
    (7000, 7200, "40m", 7074000),
    (10100, 10150, "30m", 10136000),
    (14000, 14350, "20m", 14074000),
    (18068, 18168, "17m", 18100000),
    (21000, 21450, "15m", 21074000),
    (24890, 24990, "12m", 24915000),
    (28000, 29700, "10m", 28074000),
    (50000, 54000, "6m", 50313000),
]

# ================= BAND RELAY RULES =================

BAND_RULES_FILE = Path(__file__).with_name("band_rules.json")

# Default rules: one per amateur band
default_band_rules = [{"from": f, "to": t, "relays": []} for f, t, _, _ in AMATEUR_BANDS]

band_rules = []
band_relay_enabled = True  # Global toggle for automatic relay switching


def load_band_rules():
    global band_rules
    if BAND_RULES_FILE.exists():
        try:
            with open(BAND_RULES_FILE, "r") as f:
                band_rules = json.load(f)
            # Validate structure
            for rule in band_rules:
                if "from" not in rule or "to" not in rule or "relays" not in rule:
                    raise ValueError("Invalid rule structure")
        except Exception:
            band_rules = default_band_rules.copy()
            save_band_rules()
    else:
        band_rules = default_band_rules.copy()
        save_band_rules()


def save_band_rules():
    tmp = BAND_RULES_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(band_rules, f, indent=2)
    os.replace(tmp, BAND_RULES_FILE)


def apply_band_rules(freq_hz):
    """Check band rules and activate relays for the given frequency (in Hz).
    Returns the list of relay indices that should be ON.
    Rules: from <= freq_khz < to (lower bound inclusive, upper bound exclusive).
    """
    if not band_rules or not freq_hz:
        return []

    # Convert Hz to kHz for rule matching
    freq_khz = freq_hz / 1000

    # Find matching rules (a frequency can match multiple rules)
    active_relays = set()
    for rule in band_rules:
        if rule["from"] <= freq_khz < rule["to"]:
            for r in rule.get("relays", []):
                if 0 <= r <= 15:
                    active_relays.add(r)

    return sorted(active_relays)


def _managed_relay_groups():
    """Return the set of relay groups (0 = relays 1-8, 1 = relays 9-16) that are
    referenced by any band rule. Groups not referenced by any rule are left
    completely untouched by automatic switching, so their relays can still be
    selected manually without being reset on every frequency change."""
    managed = set()
    for rule in band_rules:
        for r in rule.get("relays", []):
            if 0 <= r <= 15:
                managed.add(0 if r < 8 else 1)
    return managed


def set_relays_for_frequency(freq_hz):
    """Set relays according to band rules for the given frequency.

    Only the relay group(s) actually referenced by the band rules are reset and
    re-selected (auto-managed). Relays in any other group keep their current
    state, so e.g. if auto-switching only uses relays 1-8, then relays 9-16 are
    never touched automatically and can be selected manually.
    """
    global state1, state2, ptt_active
    if not band_relay_enabled:
        return []

    if ptt_active:
        print("🔒 PTT active — band relay switching blocked")
        return []

    target = apply_band_rules(freq_hz)
    managed = _managed_relay_groups()

    # Only reset the groups that are auto-managed by the band rules. The other
    # group byte is left as-is so a manually-selected relay there is preserved.
    if 0 in managed:
        state1 = 0xFF
    if 1 in managed:
        state2 = 0xFF

    for r in target:
        set_relay(r, True)

    apply()
    return target


# ================= AUDIO & NETWORK CONFIG =================

# Audio/network paths (from web_config_server.py)
# Derive the project root from this file's own location so paths stay correct
# regardless of where the repo is installed (e.g. /home/pi/nano-server).
PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
SERVER_IP_FILE = os.path.join(PROJECT_DIR, "server_ip.cfg")
CLIENT_IP_FILE = os.path.join(PROJECT_DIR, "client_ip.cfg")
AUDIO_CONFIG_FILE = os.path.join(PROJECT_DIR, "audio/audio_config.cfg")
PROFILES_DIR = os.path.join(PROJECT_DIR, "profiles")

# UDP Ping configuration
UDP_PORT = 5002
TIMEOUT = 1.0
CHECK_INTERVAL = 0.3
MAGIC_PHRASE = b"PING_RESPONSE"

# PTT status from combined_ptt_service (via UDP broadcast on port 5004)
PTT_STATUS_PORT = 5004
ptt_active = False

# Global variables for status
current_rtt = None
last_update = None
status_active = False
status_thread = None
status_lock = threading.Lock()

# Ensure profiles directory exists
if not os.path.exists(PROFILES_DIR):
    os.makedirs(PROFILES_DIR)
# Ensure the profiles directory and its contents are writable by the process
if not os.access(PROFILES_DIR, os.W_OK):
    print(f"WARNING: {PROFILES_DIR} is not writable — profile save/delete will fail.")
    print("Fix: sudo chown -R pi:pi /home/pi/nano-server && sudo chmod -R u+rw /home/pi/nano-server")

# Audio ALSA controls — auto-detected

def _find_alsa_card():
    """Find the C-Media USB Audio Device card identifier.
    Returns the card ID (e.g., 'Device') or index (e.g., '1') as fallback.
    """
    try:
        with open("/proc/asound/cards", "r") as f:
            content = f.read()
        for line in content.splitlines():
            # Look for "C-Media" USB audio device first
            if "C-Media" in line:
                # Line format: " 1 [Device         ]: USB-Audio - ..."
                m = re.search(r'\[(\w+)\]', line)
                if m:
                    card_id = m.group(1)
                    return card_id
        # Second pass: any USB Audio device that isn't webcam
        for line in content.splitlines():
            if "USB Audio" in line and "webcam" not in line.lower():
                m = re.search(r'\[(\w+)\]', line)
                if m:
                    card_id = m.group(1)
                    return card_id
    except Exception as e:
        print(f"[audio] Error reading /proc/asound/cards: {e}")

    # Fallback: try to find any card that isn't audiocodec or webcam
    try:
        with open("/proc/asound/cards", "r") as f:
            content = f.read()
        for line in content.splitlines():
            m = re.search(r'\[(\w+)\]', line)
            if m:
                card_id = m.group(1)
                if card_id not in ("audiocodec", "webcam"):
                    return card_id
    except Exception:
        pass

    print("[audio] WARNING: using card 0 as fallback")
    return "0"


def _find_speaker_control():
    """Find the first playback simple control with a percentage value."""
    # First try: list all simple controls and pick the first playback one
    try:
        r = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "scontrols"],
            capture_output=True, text=True, timeout=3
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                m = re.search(r"Simple mixer control '(.+?)'", line)
                if m:
                    name = m.group(1)
                    try:
                        r2 = subprocess.run(
                            ["amixer", "-c", ALSA_CARD, "get", name],
                            capture_output=True, text=True, timeout=3
                        )
                        if r2.returncode == 0 and "%" in r2.stdout:
                            if "Playback" in name or "Speaker" in name or "PCM" in name or "Master" in name or "Headphone" in name:
                                return name
                    except Exception:
                        pass
    except Exception as e:
        print(f"[audio] scontrols failed: {e}")

    # Second try: fallback to known names
    for name in ["Speaker", "PCM", "Headphone", "Master"]:
        try:
            r = subprocess.run(
                ["amixer", "-c", ALSA_CARD, "get", name],
                capture_output=True, text=True, timeout=3
            )
            if r.returncode == 0 and "%" in r.stdout:
                return name
        except Exception:
            pass

    # Last resort: try to find ANY playback control from contents
    try:
        r = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "contents"],
            capture_output=True, text=True, timeout=3
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                m = re.search(r"numid=(\d+),.*name='(.+?)'", line)
                if m:
                    numid = m.group(1)
                    name = m.group(2)
                    if "Playback" in name or "Speaker" in name or "PCM" in name:
                        return f"numid={numid}"
    except Exception:
        pass

    print("[audio] WARNING: no speaker control found, falling back to 'Speaker'")
    return "Speaker"


# MIC is hardcoded to numid=8 (Mic Capture Volume) for the fixed C-Media USB Audio card
MIC = "numid=8"

ALSA_CARD = _find_alsa_card()
SPEAKER = _find_speaker_control()


def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception as e:
        print(f"Error getting local IP: {e}")
        return "Not available"


def start_status_monitoring():
    global status_active, status_thread
    if not status_active:
        status_active = True
        status_thread = threading.Thread(target=update_status, daemon=True)
        status_thread.start()


def is_valid_ip(ip_str):
    try:
        ipaddress.ip_address(ip_str.strip())
        return True
    except ValueError:
        return False


def get_ip_from_file(filepath):
    try:
        with open(filepath, "r") as f:
            ip = f.read().strip()
            if not ip:
                raise ValueError("IP address is empty")
            return ip
    except Exception:
        return None


def measure_udp_rtt(ip):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(TIMEOUT)
            start = time.perf_counter()
            s.sendto(b"PING_REQUEST", (ip, UDP_PORT))
            data, addr = s.recvfrom(1024)
            if data == MAGIC_PHRASE and addr[0] == ip:
                return (time.perf_counter() - start) * 1000
    except Exception:
        return None


def update_status():
    global current_rtt, last_update, status_active
    while status_active:
        ip = get_ip_from_file(CLIENT_IP_FILE)
        if ip:
            rtt = measure_udp_rtt(ip)
            with status_lock:
                current_rtt = rtt
                last_update = time.strftime("%H:%M:%S")
        else:
            with status_lock:
                current_rtt = None
                last_update = "No client IP configured"
        time.sleep(CHECK_INTERVAL)


def ptt_status_listener():
    """Listen for PTT status broadcasts from combined_ptt_service on UDP port 5004."""
    global ptt_active
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", PTT_STATUS_PORT))
    sock.settimeout(0.5)
    print(f"[PTT] 📡 Listening for PTT status on 127.0.0.1:{PTT_STATUS_PORT}...")
    while True:
        try:
            data, _ = sock.recvfrom(1024)
            ptt_active = (data[0] == 1)
        except socket.timeout:
            continue
        except Exception:
            break
    sock.close()


def percent_to_alsa(vol_percent):
    return min(35, max(0, round(float(vol_percent) * 35 / 100)))


def alsa_to_percent(alsa_value):
    return min(100, max(0, round(float(alsa_value) * 100 / 35)))


def get_mic_value():
    try:
        result = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "cget", MIC], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if line.strip().startswith(": values="):
                val = line.strip().split("values=")[1].split(",")[0].strip()
                return int(val) if val.isdigit() else 8
    except Exception:
        pass
    return 8


def get_speaker_volume():
    try:
        result = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "get", SPEAKER], capture_output=True, text=True, timeout=5
        )
        m = re.search(r"(\d+)%", result.stdout)
        return m.group(1) if m else "50"
    except Exception:
        return "50"


def read_config_file(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def write_config_file(path, content):
    with open(path, "w") as f:
        f.write(content.strip())


def read_audio_config():
    defaults = (48000, 100000)
    try:
        with open(AUDIO_CONFIG_FILE, "r") as f:
            lines = f.readlines()
            rate = defaults[0]
            buffer_time = defaults[1]
            for line in lines:
                line = line.strip()
                if line.startswith("RATE="):
                    rate = int(line.split("=")[1])
                elif line.startswith("LATENCY="):
                    buffer_time = int(line.split("=")[1])
            return rate, buffer_time
    except Exception as e:
        print(f"Error reading audio config: {e}")
        return defaults


def write_audio_config(rate, buffer_time):
    content = f"RATE={rate}\nLATENCY={buffer_time}\n"
    write_config_file(AUDIO_CONFIG_FILE, content)


def get_profiles_list():
    files = glob.glob(os.path.join(PROFILES_DIR, "*.cfg"))
    names = []
    for f in files:
        names.append(os.path.basename(f).replace(".cfg", ""))
    return sorted(names)


def _is_valid_profile_name(name):
    """Names come from the client and are used to build filesystem paths
    (see save/load/delete_profile below), so every path must be rejected
    here first, not just at save time — otherwise a name like '../../etc/x'
    can escape PROFILES_DIR when loading or deleting a profile."""
    return bool(name) and name.replace("_", "").isalnum()


def save_profile(name):
    if not _is_valid_profile_name(name):
        return False, "Invalid profile name (use letters and numbers only)"
    path = os.path.join(PROFILES_DIR, f"{name}.cfg")
    server_ip = read_config_file(SERVER_IP_FILE)
    client_ip = read_config_file(CLIENT_IP_FILE)
    rate, buffer_time = read_audio_config()
    content = f"[Server]\nIP={server_ip}\n\n[Client]\nIP={client_ip}\n\n[Audio]\nRate={rate}\nLatency={buffer_time}\n"
    try:
        # Atomic write: write to a temp file in the same directory, then replace.
        # This avoids partial writes and matches the pattern used elsewhere.
        tmp = os.path.join(PROFILES_DIR, f".{name}.cfg.tmp")
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, path)
        return True, "Profile saved successfully!"
    except Exception as e:
        return False, f"Error saving profile: {str(e)}"


def load_profile(name):
    if not _is_valid_profile_name(name):
        return False, "Invalid profile name"
    path = os.path.join(PROFILES_DIR, f"{name}.cfg")
    if not os.path.exists(path):
        return False, "Profile not found"
    try:
        with open(path, "r") as f:
            content = f.read()
        server_ip = ""
        client_ip = ""
        rate = 48000
        buffer_time = 100000
        parts = content.split("[")
        for part in parts:
            if part.startswith("Server]"):
                for line in part.split("\n"):
                    if line.startswith("IP="):
                        server_ip = line.split("=")[1].strip()
            elif part.startswith("Client]"):
                for line in part.split("\n"):
                    if line.startswith("IP="):
                        client_ip = line.split("=")[1].strip()
            elif part.startswith("Audio]"):
                for line in part.split("\n"):
                    if line.startswith("Rate="):
                        rate = int(line.split("=")[1].strip())
                    if line.startswith("Latency="):
                        buffer_time = int(line.split("=")[1].strip())
        write_config_file(SERVER_IP_FILE, server_ip)
        write_config_file(CLIENT_IP_FILE, client_ip)
        write_audio_config(rate, buffer_time)
        return True, f"Profile '{name}' loaded successfully! Restart services to apply."
    except Exception as e:
        return False, f"Error loading profile: {str(e)}"


def delete_profile(name):
    if not _is_valid_profile_name(name):
        return False, "Invalid profile name"
    path = os.path.join(PROFILES_DIR, f"{name}.cfg")
    if os.path.exists(path):
        os.remove(path)
        return True, "Profile deleted"
    return False, "Profile not found"


# ================= TRANSFER FUNCTIONS =================


def freq_to_band(freq):
    freq_khz = freq / 1000
    for start_khz, end_khz, name, _ in AMATEUR_BANDS:
        if start_khz <= freq_khz <= end_khz:
            return name
    return "Unknown"


def decode_bcd_freq(data):
    if len(data) != 5:
        return None
    freq = 0
    for i, b in enumerate(data):
        low = b & 0x0F
        high = (b >> 4) & 0x0F
        freq += low * (10 ** (i * 2))
        freq += high * (10 ** (i * 2 + 1))
    return freq


class CIVDecoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        while True:
            try:
                start = self.buffer.index(b"\xfe\xfe")
            except ValueError:
                self.buffer.clear()
                return
            try:
                end = self.buffer.index(0xFD, start)
            except ValueError:
                return
            frame = bytes(self.buffer[start : end + 1])
            del self.buffer[: end + 1]
            self.process_frame(frame)

    def process_frame(self, frame):
        if len(frame) < 6:
            return

        radio_state["last_rx"] = time.time()
        radio_state["online"] = True

        cmd = frame[4]

        if cmd == 0x03:
            payload = frame[5:-1]
            if len(payload) == 5:
                freq = decode_bcd_freq(payload)
                if freq:
                    radio_state["freq"] = freq
                    radio_state["band"] = freq_to_band(freq)
                    set_relays_for_frequency(freq)

        elif cmd == 0x04 and len(frame) >= 7:
            mode_byte = frame[5]
            modes = {
                0x00: "LSB",
                0x01: "USB",
                0x02: "AM",
                0x03: "CW",
                0x04: "RTTY",
                0x05: "FM",
            }
            radio_state["mode"] = modes.get(mode_byte, "Unknown")




# Per the Kenwood PC control command reference (MD command): 0 and 8 are
# both "None (setting failure)" (not real modes, so intentionally absent
# here — .get() below falls back to "Unknown"), and 9 is FSK-R, not FM.
KENWOOD_MODE_MAP = {
    "1": "LSB",
    "2": "USB",
    "3": "CW",
    "4": "FM",
    "5": "AM",
    "6": "RTTY",
    "7": "CW",
    "9": "RTTY",
}


class KenwoodDecoder:
    """Decoder for Kenwood CAT protocol (ASCII-based, terminated by ';')."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        while True:
            try:
                end = self.buffer.index(0x3B)  # ';'
            except ValueError:
                # Keep buffer, wait for more data
                return
            frame = bytes(self.buffer[: end + 1])
            del self.buffer[: end + 1]
            self.process_frame(frame)

    def process_frame(self, frame):
        if len(frame) < 3:
            return

        radio_state["last_rx"] = time.time()
        radio_state["online"] = True

        try:
            text = frame.decode("ascii", errors="replace").strip()
        except Exception:
            return

        if not text.endswith(";"):
            return
        text = text[:-1]  # strip ';'

        # Frequency response: FAxxxxxxxxxxx
        # Kenwood sends the frequency as an 11-digit, zero-padded Hz value
        # (e.g. 14.074 MHz -> "00014074000"), i.e. text[2:13].
        if text.startswith("FA") and len(text) >= 13:
            try:
                freq_hz = int(text[2:13])
                if 100000 <= freq_hz <= 3000000000:
                    radio_state["freq"] = freq_hz
                    radio_state["band"] = freq_to_band(freq_hz)
                    set_relays_for_frequency(freq_hz)
            except ValueError:
                pass

        # Mode response: MDx
        elif text.startswith("MD") and len(text) >= 3:
            mode_digit = text[2]
            radio_state["mode"] = KENWOOD_MODE_MAP.get(mode_digit, "Unknown")

        # Combined status: IF<freq:11><space:5><RIT/XIT freq:5><RIT:1><XIT:1>
        # <ch bank:1><ch num:2><TX/RX:1><mode:1>... (Kenwood PC control command
        # reference, "IF" command) — the mode digit is P9, at offset 29, not 18
        # (18 is the sign character of the RIT/XIT offset field).
        elif text.startswith("IF") and len(text) >= 13:
            try:
                freq_hz = int(text[2:13])
                if 100000 <= freq_hz <= 3000000000:
                    radio_state["freq"] = freq_hz
                    radio_state["band"] = freq_to_band(freq_hz)
                    set_relays_for_frequency(freq_hz)
            except ValueError:
                pass
            if len(text) >= 30:
                mode_digit = text[29]
                radio_state["mode"] = KENWOOD_MODE_MAP.get(mode_digit, "Unknown")


def load_trx_config():
    global trx_config
    if TRX_CONFIG_FILE.exists():
        try:
            with open(TRX_CONFIG_FILE, "r") as f:
                trx_config = json.load(f)
            for key, value in default_trx_config.items():
                if key not in trx_config:
                    trx_config[key] = value
        except Exception:
            trx_config = default_trx_config.copy()
    else:
        trx_config = default_trx_config.copy()
        save_trx_config()


def save_trx_config():
    tmp = TRX_CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(trx_config, f, indent=2)
    os.replace(tmp, TRX_CONFIG_FILE)


def init_serial():
    global ser, ser_uart1, decoder
    try:
        if ser and ser.is_open:
            ser.close()
        if ser_uart1 and ser_uart1.is_open:
            ser_uart1.close()

        ser = serial.Serial(
            trx_config["serial_port"], trx_config["baudrate"], timeout=0.1
        )
        protocol = trx_config.get("protocol", "Icom")
        if protocol == "Kenwood":
            decoder = KenwoodDecoder()
        else:
            decoder = CIVDecoder()
        # NOTE: we deliberately do NOT set online=True here. Opening the serial
        # port only means the USB/RS232 adapter is present, not that a
        # transceiver is attached and responding. "online" is set to True by
        # the decoder when a valid CAT frame is received, and cleared by the
        # poller when the radio stops answering.

        # Open UART1 for transparent CAT relay to local computer
        if trx_config.get("uart1_enabled", True):
            try:
                uart1_port = trx_config.get("uart1_port", "/dev/ttyS1")
                ser_uart1 = serial.Serial(uart1_port, trx_config["baudrate"], timeout=0.1)
                print(f"[TRX] UART1 relay opened on {uart1_port} at {trx_config['baudrate']} baud")
            except Exception as e:
                print(f"[TRX] UART1 relay failed: {e}")
                ser_uart1 = None
        else:
            ser_uart1 = None

        return True
    except Exception as e:
        radio_state["online"] = False
        print(f"[TRX] Failed: {e}")
        return False


def serial_reader(loop_ref):
    """Read data from the CAT port: decode for the web UI, broadcast to TCP
    clients, and relay to UART1. On a serial error the port is automatically
    reopened (with retries) so a temporary USB hiccup or transceiver power-off
    does not require a full service restart."""
    global ser, ser_uart1, decoder
    while True:
        if ser and ser.is_open:
            try:
                data = ser.read(1024)
                if data:
                    # Decode for web UI
                    if decoder:
                        decoder.feed(data)
                    # Broadcast to TCP clients
                    if loop_ref:
                        asyncio.run_coroutine_threadsafe(broadcast(data), loop_ref)
                    # Relay to UART1 (local computer)
                    if ser_uart1 and ser_uart1.is_open:
                        try:
                            ser_uart1.write(data)
                        except Exception as e:
                            print(f"[TRX] UART1 write error: {e}")
            except Exception as e:
                print(f"[TRX] Read error: {e}")
                radio_state["online"] = False
                # Try to reopen the serial ports after a short delay.
                time.sleep(1)
                with ser_lock:
                    try:
                        if ser and ser.is_open:
                            ser.close()
                        if ser_uart1 and ser_uart1.is_open:
                            ser_uart1.close()
                    except Exception:
                        pass
                    if not init_serial():
                        print("[TRX] Auto-reconnect failed; will retry in 5s")
                        time.sleep(5)
        else:
            time.sleep(1)


def uart1_reader():
    """Read data from UART1 and write to the CAT serial port (transparent relay)."""
    global ser, ser_uart1, external_cat_time
    while True:
        if ser_uart1 and ser_uart1.is_open and ser and ser.is_open:
            try:
                data = ser_uart1.read(1024)
                if data:
                    # This frame came FROM the external program (through UART1)
                    # and is headed TO the radio: remember that the external
                    # program is actively using the CAT port.
                    external_cat_time = time.time()
                    with ser_lock:
                        ser.write(data)
            except Exception as e:
                print(f"[TRX] UART1 read error: {e}")
                time.sleep(1)
        else:
            time.sleep(1)


async def broadcast(data):
    dead = []
    for w in clients:
        try:
            w.write(data)
            await w.drain()
        except:
            dead.append(w)
    for w in dead:
        clients.discard(w)


async def tcp_client(reader, writer):
    global external_cat_time
    addr = writer.get_extra_info("peername")
    clients.add(writer)
    try:
        while True:
            data = await reader.read(1024)
            if not data:
                break
            if ser and ser.is_open:
                # A TCP client acts like an external controller: mark the CAT
                # port as busy so the poller backs off while it's active.
                external_cat_time = time.time()
                with ser_lock:
                    ser.write(data)
    except:
        pass
    clients.discard(writer)
    writer.close()
    await writer.wait_closed()


async def poller():
    while True:
        # Run frequently: the faster we sweep stale bytes, the sooner a
        # restarted JTDX session re-syncs. 0.5s is short enough to clear a
        # stuck partial command quickly, yet cheap in CPU.
        await asyncio.sleep(0.5)
        if not trx_config.get("enabled", True):
            continue
        if not ser or not ser.is_open:
            radio_state["online"] = False
            continue

        # Mark the transceiver offline if it hasn't answered recently. This
        # check must run in BOTH modes (UART1 relay on/off): opening the serial
        # port does not prove a radio is attached — only an actual CAT response
        # (which updates radio_state["last_rx"]) does.
        if time.time() - radio_state["last_rx"] > 5:
            radio_state["online"] = False

        # Transparent-relay ownership check. When the UART1 relay (and/or a TCP
        # client) is enabled, an external program on the PC is meant to own the
        # CAT port, and our own IF;/CI-V queries must NOT interleave with its
        # active traffic — that would corrupt the response stream and confuse
        # the PC software (flrig/JTDX/TR4W etc.), exactly like the old
        # reset_input_buffer() watchdog did.
        #
        # BUT if the external program is silent or absent (it hasn't sent any
        # frame toward the radio for EXTERNAL_CAT_TIMEOUT seconds), the port is
        # free: the server takes over and polls the radio itself so the web UI
        # can still show the live frequency/mode. As soon as external traffic
        # resumes, this check backs off and hands the port back to the external
        # program within one poll cycle.
        if trx_config.get("uart1_enabled", True) and (
            time.time() - external_cat_time
        ) <= EXTERNAL_CAT_TIMEOUT:
            continue

        protocol = trx_config.get("protocol", "Icom")
        if protocol == "Kenwood":
            # Kenwood CAT: IF; returns frequency + mode of the active VFO (A or B)
            cmd = b"IF;"
        else:
            # Icom CI-V: poll frequency
            cmd = bytes(
                [0xFE, 0xFE, trx_config["radio_addr"], trx_config["ctrl_addr"], 0x03, 0xFD]
            )
        try:
            with ser_lock:
                ser.write(cmd)
        except:
            pass


async def start_trx_server():
    global loop
    loop = asyncio.get_running_loop()

    thread = threading.Thread(target=serial_reader, args=(loop,), daemon=True)
    thread.start()

    # Start UART1 reader thread for transparent relay
    uart1_thread = threading.Thread(target=uart1_reader, daemon=True)
    uart1_thread.start()

    server = await asyncio.start_server(tcp_client, "0.0.0.0", trx_config["tcp_port"])

    asyncio.create_task(poller())

    return server


# ================= AUTH =================

# Simple in-memory login rate limiting: after LOGIN_MAX_ATTEMPTS failed
# attempts from the same IP, block further attempts from it for
# LOGIN_LOCKOUT_SECONDS. This is a basic guard against online brute-forcing
# of the panel password (which is often short and numeric).
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 60
_login_attempts = {}
_login_attempts_lock = threading.Lock()


def _login_locked_out(ip):
    with _login_attempts_lock:
        entry = _login_attempts.get(ip)
        return bool(entry and entry["locked_until"] > time.time())


def _record_login_failure(ip):
    with _login_attempts_lock:
        entry = _login_attempts.setdefault(ip, {"count": 0, "locked_until": 0})
        entry["count"] += 1
        if entry["count"] >= LOGIN_MAX_ATTEMPTS:
            entry["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
            entry["count"] = 0


def _record_login_success(ip):
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)


LOGIN_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body { background:#0f1115; color:white; font-family:Arial; text-align:center; padding-top:120px; }
input, button { font-size:18px; padding:10px; margin:5px; }
</style>
</head>
<body>
<h2>Login</h2>
<form method="post">
<input type="password" name="password" placeholder="Password">
<br>
<button type="submit">Enter</button>
</form>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr
        if _login_locked_out(ip):
            return f"Too many attempts. Try again in {LOGIN_LOCKOUT_SECONDS} seconds.", 429
        if hmac.compare_digest(request.form.get("password", "").encode(), PASSWORD.encode()):
            session["auth"] = True
            _record_login_success(ip)
            return redirect("/")
        _record_login_failure(ip)
        return "Wrong password"
    return LOGIN_HTML


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


def auth():
    return session.get("auth", False)


@app.route("/stream")
def stream():
    if not auth():
        return "no auth", 403
    mjpg_url = "http://127.0.0.1:8081/?action=stream"
    try:
        r = requests.get(mjpg_url, stream=True)

        def generate():
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            content_type=r.headers.get(
                "Content-Type", "multipart/x-mixed-replace; boundary=--frame"
            ),
        )
    except:
        return "Camera not available", 503


# ================= UI =================



@app.route("/camera")
def camera():
    if not auth():
        return redirect("/login")
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Camera</title>
    <style>
        body { margin:0; background:black; display:flex; justify-content:center; align-items:center; height:100vh; }
        img { max-width:95vw; max-height:95vh; border-radius:10px; }
    </style>
</head>
<body>
    <img src="/stream">
</body>
</html>
""")


@app.route("/")
def index():
    if not auth():
        return redirect("/login")
    return render_template("index.html")


@app.route("/state")
def state():
    if not auth():
        return jsonify({})
    return jsonify(
        {"state": get_state(), "names": config["names"], "mode": config["group_mode"]}
    )


@app.route("/toggle/<int:n>")
def toggle(n):
    if not auth():
        return jsonify({"error": "no auth"})
    if n < 0 or n > 15:
        return jsonify({"error": "invalid relay index"}), 400
    toggle_relay(n)
    apply()
    return jsonify(
        {"state": get_state(), "names": config["names"], "mode": config["group_mode"]}
    )


@app.route("/settings", methods=["POST"])
def settings():
    if not auth():
        return "no"
    data = request.json
    config["names"] = data["names"]
    config["group_mode"] = data["mode"]
    save_relay_config()
    return "ok"


# ================= BAND RELAY API =================


@app.route("/bandrelay/rules")
def bandrelay_get_rules():
    if not auth():
        return jsonify([])
    return jsonify(band_rules)


@app.route("/bandrelay/rules", methods=["POST"])
def bandrelay_save_rules():
    if not auth():
        return "no auth", 403
    global band_rules
    data = request.json
    if not isinstance(data, list):
        return "Invalid data: expected array", 400
    # Validate
    for rule in data:
        if "from" not in rule or "to" not in rule or "relays" not in rule:
            return "Invalid rule structure", 400
        if not isinstance(rule["relays"], list):
            return "relays must be an array", 400
        for r in rule["relays"]:
            if not isinstance(r, int) or r < 0 or r > 15:
                return f"Invalid relay index: {r}", 400
    band_rules = data
    save_band_rules()
    return "ok"


@app.route("/bandrelay/apply")
def bandrelay_apply():
    """Manually apply band rules for the current frequency."""
    if not auth():
        return "no auth", 403
    freq = radio_state.get("freq", 0)
    if freq:
        relays = set_relays_for_frequency(freq)
        return jsonify({"relays": relays, "freq_khz": freq / 1000})
    return jsonify({"relays": [], "freq_khz": 0})


@app.route("/bandrelay/toggle", methods=["POST"])
def bandrelay_toggle():
    """Enable or disable automatic relay switching."""
    if not auth():
        return "no auth", 403
    global band_relay_enabled
    data = request.json
    band_relay_enabled = data.get("enabled", True)
    return jsonify({"enabled": band_relay_enabled})


@app.route("/bandrelay/state")
def bandrelay_state():
    """Return current band relay state."""
    if not auth():
        return jsonify({})
    freq = radio_state.get("freq", 0)
    active = apply_band_rules(freq) if freq else []
    return jsonify({
        "freq_khz": freq / 1000 if freq else 0,
        "active_relays": active,
        "rules_count": len(band_rules),
        "enabled": band_relay_enabled,
    })


@app.route("/trx/state")
def trx_state():
    if not auth():
        return jsonify({})
    return jsonify(
        {
            "freq": radio_state["freq"],
            "band": radio_state["band"],
            "online": radio_state["online"],
            "mode": radio_state["mode"],
            "last_rx": radio_state["last_rx"],
        }
    )


@app.route("/trx/ports")
def trx_ports():
    """Scan for available serial ports (ttyUSB* and ttyACM*) plus UART1."""
    ports = []
    for pattern in ["/dev/ttyUSB*", "/dev/ttyACM*"]:
        for p in glob.glob(pattern):
            ports.append(p)
    # Include /dev/ttyS1 (UART1 on NanoPi, used for the transparent CAT relay)
    if os.path.exists("/dev/ttyS1"):
        if "/dev/ttyS1" not in ports:
            ports.append("/dev/ttyS1")
    return jsonify(sorted(ports))


@app.route("/trx/reinit", methods=["POST"])
def trx_reinit():
    """Re-initialize the serial connection without restarting the service."""
    if not auth():
        return "no auth", 403
    try:
        global ser, ser_uart1
        with ser_lock:
            # Close existing connections
            if ser and ser.is_open:
                ser.close()
            if ser_uart1 and ser_uart1.is_open:
                ser_uart1.close()
            # Re-init with current config
            success = init_serial()
        if success:
            return jsonify({"status": "ok", "online": radio_state["online"], "port": trx_config["serial_port"]})
        else:
            return jsonify({"status": "error", "online": False, "message": "Failed to open port"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/trx/config", methods=["GET", "POST"])
def trx_config_route():
    if not auth():
        return "no auth", 403

    if request.method == "GET":
        return jsonify(trx_config)

    data = request.json

    # Validate radio_addr if provided
    if "radio_addr" in data:
        addr = data["radio_addr"]
        if not isinstance(addr, int) or addr < 0 or addr > 255:
            return "Invalid transceiver address: must be 0-255 (0x00-0xFF)", 400

    old_port = trx_config["serial_port"]
    old_baud = trx_config["baudrate"]
    old_protocol = trx_config.get("protocol", "Icom")
    old_uart1 = trx_config.get("uart1_enabled", True)

    trx_config.update(data)
    save_trx_config()

    if (
        old_port != trx_config["serial_port"]
        or old_baud != trx_config["baudrate"]
        or old_protocol != trx_config.get("protocol", "Icom")
        or old_uart1 != trx_config.get("uart1_enabled", True)
    ):
        with ser_lock:
            init_serial()

    return "ok"


# ================= TRX CONTROL API =================


def _send_civ_cmd(payload: bytes, to_addr=None):
    """Send a CI-V command frame and return True if sent successfully.
    Frame format: FE FE <to_addr> <from_addr> <cmd_data> FD
    For Xiegu G90: to_addr=radio_addr, from_addr=ctrl_addr
    If to_addr is None, uses radio_addr from config.
    """
    if not ser or not ser.is_open:
        return False
    try:
        if to_addr is None:
            to_addr = trx_config["radio_addr"]
        frame = bytes([0xFE, 0xFE, to_addr, trx_config["ctrl_addr"]]) + payload + bytes([0xFD])
        with ser_lock:
            ser.write(frame)
        return True
    except Exception as e:
        print(f"[TRX] CIV send error: {e}")
        return False


def _send_kenwood_cmd(cmd: str):
    """Send a Kenwood CAT command string."""
    if not ser or not ser.is_open:
        return False
    try:
        with ser_lock:
            ser.write(cmd.encode("ascii"))
        return True
    except Exception as e:
        print(f"[TRX] Kenwood send error: {e}")
        return False


def _is_kenwood():
    return trx_config.get("protocol", "Icom") == "Kenwood"


def _freq_to_civ_bcd(freq_hz):
    """Encode a frequency in Hz as the 5-byte BCD payload used by Icom CI-V
    'set frequency' commands (least-significant digit pair first)."""
    bcd = bytearray(5)
    temp = freq_hz
    for i in range(5):
        low = temp % 10
        temp //= 10
        high = temp % 10
        temp //= 10
        bcd[i] = (high << 4) | low
    return bytes(bcd)


def _set_transceiver_freq(freq_hz):
    """Send a 'set frequency' command to the transceiver (in the configured
    protocol) and update radio_state accordingly."""
    if _is_kenwood():
        _send_kenwood_cmd(f"FA{freq_hz:011d};")
    else:
        # Icom CI-V: set frequency command 0x05
        _send_civ_cmd(bytes([0x05]) + _freq_to_civ_bcd(freq_hz))

    radio_state["freq"] = freq_hz
    radio_state["band"] = freq_to_band(freq_hz)


@app.route("/trx/set_freq", methods=["POST"])
def trx_set_freq():
    """Set transceiver frequency (Hz)."""
    if not auth():
        return "no auth", 403
    data = request.json
    freq_hz = data.get("freq", 0)
    if freq_hz < 100000 or freq_hz > 3000000000:
        return "Invalid frequency", 400

    _set_transceiver_freq(freq_hz)
    return jsonify({"freq": freq_hz, "band": radio_state["band"]})


@app.route("/trx/freq_step", methods=["POST"])
def trx_freq_step():
    """Change frequency by a step in Hz (positive or negative)."""
    if not auth():
        return "no auth", 403
    data = request.json
    step = data.get("step", 0)
    current_freq = radio_state.get("freq", 0)
    if current_freq == 0:
        current_freq = 7100000  # default to 40m if unknown
    new_freq = current_freq + step
    # Clamp to valid range
    new_freq = max(100000, min(3000000000, new_freq))

    _set_transceiver_freq(new_freq)
    return jsonify({"freq": new_freq, "band": radio_state["band"]})


@app.route("/trx/set_band", methods=["POST"])
def trx_set_band():
    """Set frequency to the configured target frequency for an amateur band."""
    if not auth():
        return "no auth", 403
    data = request.json
    band_name = data.get("band", "")

    for start, end, name, target_freq in AMATEUR_BANDS:
        if name == band_name:
            _set_transceiver_freq(target_freq)
            # Use the canonical band name rather than freq_to_band()'s lookup,
            # matching prior behavior exactly.
            radio_state["band"] = band_name
            return jsonify({"freq": target_freq, "band": band_name})

    return f"Unknown band: {band_name}", 400


@app.route("/trx/set_power", methods=["POST"])
def trx_set_power():
    """Set transceiver output power (0-100%)."""
    if not auth():
        return "no auth", 403
    data = request.json
    power = data.get("power", 50)
    power = max(0, min(100, int(power)))

    if _is_kenwood():
        # Kenwood: PC command (some models support it)
        _send_kenwood_cmd(f"PC{power:03d};")
    else:
        # Icom CI-V: power setting command 0x14
        # Value 0-255 maps to 0-100%
        pwr_byte = max(0, min(255, int(power * 255 / 100)))
        cmd = bytes([0x14, pwr_byte])
        _send_civ_cmd(cmd)

    radio_state["power"] = power
    return jsonify({"power": power})


@app.route("/trx/set_af_gain", methods=["POST"])
def trx_set_af_gain():
    """Set transceiver AF gain (0-100%)."""
    if not auth():
        return "no auth", 403
    data = request.json
    gain = data.get("gain", 50)
    gain = max(0, min(100, int(gain)))

    if _is_kenwood():
        # Kenwood AG command: "AG" + P1(1 digit, always 0) + P2(3-digit
        # level, 000-255) — sending our 0-100% value as the 3-digit P2
        # directly (with no P1 digit) both breaks the frame length the
        # radio expects and caps the audible range at ~100/255 (~39%).
        level_255 = round(gain * 255 / 100)
        _send_kenwood_cmd(f"AG0{level_255:03d};")
    else:
        # Icom CI-V: AF gain command 0x14 with sub-command 0x01
        gain_byte = max(0, min(255, int(gain * 255 / 100)))
        cmd = bytes([0x14, 0x01, gain_byte])
        _send_civ_cmd(cmd)

    radio_state["af_gain"] = gain
    return jsonify({"af_gain": gain})


# ================= AUDIO API =================


@app.route("/audio/state")
def audio_state():
    if not auth():
        return jsonify({})
    speaker = get_speaker_volume()
    mic_alsa = get_mic_value()
    mic_pct = alsa_to_percent(mic_alsa)
    return jsonify({"speaker": int(speaker), "mic": int(mic_pct)})


_ALSA_STATE_FILE = "/var/lib/alsa/asound.state"


def alsa_save_state():
    """Persist current ALSA mixer levels to the state file that
    alsa_restore.service restores at boot. Called after every level change
    so Audio IN/OUT settings survive a reboot."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "alsactl", "store", "-f", _ALSA_STATE_FILE],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"[audio] alsactl store failed: {result.stderr.decode(errors='replace')}")
    except Exception as e:
        print(f"[audio] alsactl store failed: {e}")


@app.route("/audio/speaker", methods=["POST"])
def audio_set_speaker():
    if not auth():
        return "no auth", 403
    data = request.json
    vol = data.get("volume", 50)
    subprocess.run(["amixer", "-c", ALSA_CARD, "set", SPEAKER, f"{vol}%"], timeout=5)
    alsa_save_state()
    return "ok"


@app.route("/audio/mic", methods=["POST"])
def audio_set_mic():
    if not auth():
        return "no auth", 403
    data = request.json
    vol = data.get("volume", 50)
    alsa_value = percent_to_alsa(vol)
    subprocess.run(["amixer", "-c", ALSA_CARD, "cset", MIC, str(alsa_value)], timeout=5)
    alsa_save_state()
    return "ok"


# ================= CONFIG API =================


@app.route("/config/data")
def config_data():
    if not auth():
        return jsonify({})
    server_ip = read_config_file(SERVER_IP_FILE)
    client_ip = read_config_file(CLIENT_IP_FILE)
    audio_rate, audio_buffer = read_audio_config()
    profiles = get_profiles_list()
    return jsonify({
        "server_ip": server_ip,
        "client_ip": client_ip,
        "audio_rate": audio_rate,
        "audio_buffer": audio_buffer,
        "profiles": profiles,
    })


@app.route("/config/server_ip", methods=["POST"])
def config_set_server_ip():
    if not auth():
        return "no auth", 403
    data = request.json
    ip = data.get("ip", "").strip()
    if not is_valid_ip(ip):
        return f"Invalid IP address: '{ip}'", 400
    write_config_file(SERVER_IP_FILE, ip)
    return "ok"


@app.route("/config/client_ip", methods=["POST"])
def config_set_client_ip():
    if not auth():
        return "no auth", 403
    data = request.json
    ip = data.get("ip", "").strip()
    if not is_valid_ip(ip):
        return f"Invalid IP address: '{ip}'", 400
    write_config_file(CLIENT_IP_FILE, ip)
    return "ok"


@app.route("/config/audio", methods=["POST"])
def config_set_audio():
    if not auth():
        return "no auth", 403
    data = request.json
    rate = data.get("rate", 48000)
    buffer_time = data.get("buffer", 100000)
    write_audio_config(rate, buffer_time)
    return "ok"


@app.route("/config/save_profile", methods=["POST"])
def config_save_profile():
    if not auth():
        return jsonify({"success": False, "message": "no auth"}), 403
    data = request.json
    name = data.get("name", "").strip()
    profiles = get_profiles_list()
    if len(profiles) >= 5:
        return jsonify({"success": False, "message": "Maximum 5 profiles allowed"})
    success, msg = save_profile(name)
    return jsonify({"success": success, "message": msg})


@app.route("/config/load_profile", methods=["POST"])
def config_load_profile():
    if not auth():
        return jsonify({"success": False, "message": "no auth"}), 403
    data = request.json
    name = data.get("name", "").strip()
    success, msg = load_profile(name)
    return jsonify({"success": success, "message": msg})


@app.route("/config/delete_profile", methods=["POST"])
def config_delete_profile():
    if not auth():
        return jsonify({"success": False, "message": "no auth"}), 403
    data = request.json
    name = data.get("name", "").strip()
    success, msg = delete_profile(name)
    return jsonify({"success": success, "message": msg})


@app.route("/config/restart_services", methods=["POST"])
def config_restart_services():
    if not auth():
        return "no auth", 403
    script_path = os.path.join(PROJECT_DIR, "restart_services_on_server.sh")
    try:
        subprocess.run(["sudo", script_path], timeout=30, check=True)
        return "ok"
    except subprocess.SubprocessError as e:
        return f"Failed: {e}", 500


@app.route("/config/restart_web", methods=["POST"])
def config_restart_web():
    """Restart the relay-web systemd service (self-restart)."""
    if not auth():
        return "no auth", 403
    try:
        # Run restart in background so the HTTP response can be sent first
        subprocess.Popen(
            ["sudo", "systemctl", "restart", "relay-web"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "ok"
    except Exception as e:
        return f"Failed: {e}", 500


# ================= UPDATE API =================

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse_version(tag):
    """Parse a 'vX.Y.Z' tag into a comparable (X, Y, Z) tuple, or None."""
    if not tag:
        return None
    m = _VERSION_RE.match(tag.strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def _run_git(args, timeout=20):
    return subprocess.run(
        ["git", "-C", PROJECT_DIR] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def get_current_version():
    """Latest version tag reachable from HEAD, or 'v0.0.0' if none."""
    try:
        r = _run_git(["describe", "--tags", "--abbrev=0"])
    except subprocess.SubprocessError:
        return "v0.0.0"
    tag = r.stdout.strip()
    if r.returncode == 0 and _parse_version(tag):
        return tag
    return "v0.0.0"


def get_latest_tag():
    """Fetch tags from origin and return the highest 'vX.Y.Z' tag, or None."""
    try:
        _run_git(["fetch", "--tags", "--force", "origin"], timeout=30)
        r = _run_git(["tag", "--list", "v*"])
    except subprocess.SubprocessError:
        return None
    if r.returncode != 0:
        return None
    tags = [t for t in r.stdout.splitlines() if _parse_version(t)]
    if not tags:
        return None
    tags.sort(key=lambda t: _parse_version(t) or (0, 0, 0))
    return tags[-1]


def get_changelog_for_new_versions(current_version, latest_tag):
    """Changelog sections (## vX.Y.Z ...) for versions newer than current_version.

    Reads CHANGELOG.txt as it exists at latest_tag (via `git show`), not the
    copy on disk: the working tree still reflects whatever commit is
    currently checked out, so a fetched-but-not-yet-pulled tag's changelog
    entries wouldn't be visible there yet.
    """
    try:
        r = _run_git(["show", f"{latest_tag}:CHANGELOG.txt"])
    except subprocess.SubprocessError:
        return ""
    if r.returncode != 0:
        return ""
    content = r.stdout
    cur_v = _parse_version(current_version) or (0, 0, 0)
    sections = re.split(r"(?m)^(?=## v\d+\.\d+\.\d+)", content)
    entries = []
    for section in sections:
        m = re.match(r"^## (v\d+\.\d+\.\d+)", section)
        if not m:
            continue
        v = _parse_version(m.group(1))
        if v and v > cur_v:
            entries.append(section.strip())
    return "\n\n".join(entries)


@app.route("/update/current")
def update_current():
    """Local-only version lookup (no network), for showing it as soon as the Update tab opens."""
    if not auth():
        return jsonify({}), 403
    return jsonify({"current": get_current_version()})


@app.route("/update/check")
def update_check():
    if not auth():
        return jsonify({}), 403
    current = get_current_version()
    latest = get_latest_tag()
    if latest is None:
        return jsonify({
            "current": current, "latest": None, "update_available": False,
            "error": "Could not reach GitHub or no version tags found.",
        })
    update_available = (_parse_version(latest) or (0, 0, 0)) > (_parse_version(current) or (0, 0, 0))
    changelog = get_changelog_for_new_versions(current, latest) if update_available else ""
    return jsonify({
        "current": current,
        "latest": latest,
        "update_available": update_available,
        "changelog": changelog,
    })


# Files the running app itself rewrites at runtime (relay names/state, saved
# audio/TRX/band-relay settings). Local edits to these are expected, not a
# sign of a manually-modified checkout, so they must never block an update.
_RUNTIME_STATE_FILES = [
    "web/config.json",
    "audio/audio_config.cfg",
    "web/trx_config.json",
    "web/band_rules.json",
]


@app.route("/update/apply", methods=["POST"])
def update_apply():
    if not auth():
        return jsonify({"success": False, "message": "no auth"}), 403
    runtime_snapshots = {}
    try:
        status = _run_git(["status", "--porcelain"])
        if status.returncode != 0:
            return jsonify({"success": False, "message": "git status failed"}), 500

        unexpected = []
        for line in status.stdout.splitlines():
            if not line.strip():
                continue
            code, path = line[:2], line[3:].strip()
            if path in _RUNTIME_STATE_FILES:
                abs_path = os.path.join(PROJECT_DIR, path)
                if os.path.exists(abs_path):
                    with open(abs_path, "rb") as f:
                        runtime_snapshots[path] = f.read()
                if code != "??":
                    # Tracked + modified: reset to HEAD so git status is clean
                    # for this path; the saved bytes are written back below,
                    # after the pull, regardless of what the pull does to it.
                    _run_git(["checkout", "--", path])
            else:
                unexpected.append(path)
        if unexpected:
            return jsonify({
                "success": False,
                "message": "Repository has local changes, refusing to update: " + ", ".join(unexpected),
            }), 409

        pull = _run_git(["pull", "--ff-only"], timeout=60)
        if pull.returncode != 0:
            return jsonify({"success": False, "message": pull.stderr.strip() or "git pull failed"}), 500
    except subprocess.SubprocessError as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        for path, content in runtime_snapshots.items():
            abs_path = os.path.join(PROJECT_DIR, path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "wb") as f:
                f.write(content)

    try:
        subprocess.run(
            ["sudo", os.path.join(PROJECT_DIR, "restart_services_on_server.sh")],
            timeout=30,
        )
    except subprocess.SubprocessError as e:
        print(f"[update] restart_services_on_server.sh failed: {e}")

    # Restart relay-web last and in the background, since it kills this process.
    subprocess.Popen(
        ["sudo", "systemctl", "restart", "relay-web"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return jsonify({"success": True, "message": "Updated, services are restarting..."})


# ================= STATUS API =================


@app.route("/status/local_ip")
def status_local_ip():
    if not auth():
        return jsonify({"ip": "Not available"})
    return jsonify({"ip": get_local_ip()})


@app.route("/status/connection")
def status_connection():
    if not auth():
        return jsonify({"rtt": None, "timestamp": "--", "status": "unknown"})
    with status_lock:
        status = (
            "good"
            if current_rtt and current_rtt < 50
            else "warning"
            if current_rtt and current_rtt < 100
            else "bad"
            if current_rtt is not None
            else "unknown"
        )
        return jsonify({"rtt": current_rtt, "timestamp": last_update, "status": status})


# ================= PTT STATUS API =================


@app.route("/ptt/status")
def ptt_status_api():
    """Return current PTT state (from combined_ptt_service broadcast)."""
    return jsonify({"active": ptt_active})


# ================= LIVE STATUS (SSE) =================


def _status_snapshot():
    """Build a single aggregated snapshot of the live web-UI state. Sent to the
    browser over SSE so the page can update instantly instead of polling the
    server from multiple per-tab intervals."""
    with status_lock:
        rtt = current_rtt
        ts = last_update

    freq = radio_state.get("freq", 0)
    bfreq = freq / 1000 if freq else 0

    return {
        "ptt_active": ptt_active,
        "relay_state": get_state(),
        "names": config.get("names", default_config["names"]),
        "mode": config.get("group_mode", default_config["group_mode"]),
        "trx": {
            "freq": radio_state.get("freq", 0),
            "band": radio_state.get("band", "Unknown"),
            "mode": radio_state.get("mode", "Unknown"),
            "online": radio_state.get("online", False),
        },
        "connection": {"rtt": rtt, "timestamp": ts},
        "bandrelay": {
            "freq_khz": round(bfreq, 1),
            "active_relays": apply_band_rules(freq) if freq else [],
            "enabled": band_relay_enabled,
        },
    }


@app.route("/events")
def sse_events():
    """Server-Sent Events stream pushing the live state snapshot to the web UI.
    Replaces the per-tab HTTP polling (PTT, TRX, relays, client RTT, band-relay
    current state), so changes arrive immediately and the device is polled less.

    A snapshot is emitted only when it actually changed (compared as JSON), so
    an idle UI receives one small message per second at most.
    """
    if not auth():
        return Response(status=403)

    def generate():
        last = None
        while True:
            try:
                data = json.dumps(_status_snapshot())
                if data != last:
                    last = data
                    yield f"data: {data}\n\n"
                time.sleep(1)
            except GeneratorExit:
                break
            except Exception as e:
                print(f"[sse] error: {e}")
                time.sleep(1)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ================= MAIN =================


def start_flask():
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)


async def main():
    load_relay_config()
    load_trx_config()
    load_band_rules()

    if trx_config.get("enabled", True):
        init_serial()

    start_status_monitoring()

    # Start PTT status listener (UDP broadcast from combined_ptt_service)
    ptt_thread = threading.Thread(target=ptt_status_listener, daemon=True)
    ptt_thread.start()

    # Start auto-reconnect thread for TRX serial port
    def auto_reconnect():
        """Periodically check if the serial port is available and reconnect."""
        while True:
            time.sleep(5)
            if not trx_config.get("enabled", True):
                continue
            port = trx_config.get("serial_port", "")
            if not port:
                continue
            # If serial is not open but the port device exists, try to reconnect
            if (not ser or not ser.is_open) and os.path.exists(port):
                print(f"[TRX] Auto-reconnect: {port} appeared, reinitializing...")
                init_serial()
            # If serial is open but port disappeared, mark offline
            elif ser and ser.is_open and not os.path.exists(port):
                radio_state["online"] = False

    reconnect_thread = threading.Thread(target=auto_reconnect, daemon=True)
    reconnect_thread.start()

    apply()

    trx_server = await start_trx_server()

    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    print("\n=== NanoPi Controller Started ===")
    print(f"Web interface: http://0.0.0.0:5050")
    print(f"TRX TCP proxy: port {trx_config['tcp_port']}\n")

    async with trx_server:
        await trx_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
