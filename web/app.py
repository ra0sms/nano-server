#!/usr/bin/env python3
import asyncio
import glob
import ipaddress
import json
import os
import re
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
    render_template_string,
    request,
    session,
    stream_with_context,
)
from smbus2 import SMBus

# ================= CONFIGURATION =================
app = Flask(__name__)
app.secret_key = "nano_secret_123"

# Password
_PASSWORD_FILE = Path(__file__).with_name("password.txt")
PASSWORD = _PASSWORD_FILE.read_text().strip() if _PASSWORD_FILE.exists() else "1234"

# I2C for relays
try:
    bus = SMBus(0)
except Exception as _e:
    print(f"Warning: could not open I2C bus 0: {_e}")
    bus = None

ADDR1 = 0x20
ADDR2 = 0x21

state1 = 0xFF
state2 = 0xFF

# ================= TRANSFER CONFIG =================
TRX_CONFIG_FILE = Path(__file__).with_name("trx_config.json")

default_trx_config = {
    "serial_port": "/dev/ttyCAT",
    "baudrate": 19200,
    "protocol": "Icom",
    "radio_addr": 0x70,
    "ctrl_addr": 0xE0,
    "tcp_port": 3001,
    "enabled": True,
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
clients = set()
decoder = None
loop = None

# ================= RELAY FUNCTIONS =================


def apply():
    if bus is None:
        return
    bus.write_byte(ADDR1, state1)
    bus.write_byte(ADDR2, state2)


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


# ================= BAND RELAY RULES =================

BAND_RULES_FILE = Path(__file__).with_name("band_rules.json")

# Default rules: one per amateur band
default_band_rules = [
    {"from": 1800, "to": 2000, "relays": []},
    {"from": 3500, "to": 3800, "relays": []},
    {"from": 7000, "to": 7200, "relays": []},
    {"from": 10100, "to": 10150, "relays": []},
    {"from": 14000, "to": 14350, "relays": []},
    {"from": 18068, "to": 18168, "relays": []},
    {"from": 21000, "to": 21450, "relays": []},
    {"from": 24890, "to": 24990, "relays": []},
    {"from": 28000, "to": 29700, "relays": []},
    {"from": 50000, "to": 54000, "relays": []},
]

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


def set_relays_for_frequency(freq_hz):
    """Set all 16 relays according to band rules for the given frequency."""
    global state1, state2, ptt_active
    if not band_relay_enabled:
        return []

    if ptt_active:
        print("🔒 PTT active — band relay switching blocked")
        return []

    target = apply_band_rules(freq_hz)

    # Turn all relays OFF first, then turn ON only the target ones
    state1 = 0xFF
    state2 = 0xFF

    for r in target:
        set_relay(r, True)

    apply()
    return target


# ================= AUDIO & NETWORK CONFIG =================

# Audio/network paths (from web_config_server.py)
PROJECT_DIR = "/home/pi/nano-server"
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


def save_profile(name):
    if not name or not name.replace("_", "").isalnum():
        return False, "Invalid profile name (use letters and numbers only)"
    path = os.path.join(PROFILES_DIR, f"{name}.cfg")
    server_ip = read_config_file(SERVER_IP_FILE)
    client_ip = read_config_file(CLIENT_IP_FILE)
    rate, buffer_time = read_audio_config()
    content = f"[Server]\nIP={server_ip}\n\n[Client]\nIP={client_ip}\n\n[Audio]\nRate={rate}\nLatency={buffer_time}\n"
    try:
        with open(path, "w") as f:
            f.write(content)
        return True, "Profile saved successfully!"
    except Exception as e:
        return False, f"Error saving profile: {str(e)}"


def load_profile(name):
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
    path = os.path.join(PROFILES_DIR, f"{name}.cfg")
    if os.path.exists(path):
        os.remove(path)
        return True, "Profile deleted"
    return False, "Profile not found"


# ================= TRANSFER FUNCTIONS =================


def freq_to_band(freq):
    bands = [
        (1800000, 2000000, "160m"),
        (3500000, 3800000, "80m"),
        (7000000, 7200000, "40m"),
        (10100000, 10150000, "30m"),
        (14000000, 14350000, "20m"),
        (18068000, 18168000, "17m"),
        (21000000, 21450000, "15m"),
        (24890000, 24990000, "12m"),
        (28000000, 29700000, "10m"),
        (50000000, 54000000, "6m"),
    ]
    for start, end, name in bands:
        if start <= freq <= end:
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

        # Frequency response: FAxxxxxxxxxx
        if text.startswith("FA") and len(text) >= 12:
            try:
                freq_hz = int(text[2:12])
                if 100000 <= freq_hz <= 3000000000:
                    radio_state["freq"] = freq_hz
                    radio_state["band"] = freq_to_band(freq_hz)
                    set_relays_for_frequency(freq_hz)
            except ValueError:
                pass

        # Mode response: MDx
        elif text.startswith("MD") and len(text) >= 3:
            mode_map = {
                "1": "LSB",
                "2": "USB",
                "3": "CW",
                "4": "FM",
                "5": "AM",
                "6": "RTTY",
                "7": "CW",
                "8": "FM",
                "9": "FM",
            }
            mode_digit = text[2]
            radio_state["mode"] = mode_map.get(mode_digit, "Unknown")

        # Combined status: IFxxxxxxxxxxyyyyymzzzz;
        # xxxxxxxxxx = 10-digit frequency in Hz
        # yyyyy = 5-digit mode/status
        # m = mode digit
        elif text.startswith("IF") and len(text) >= 20:
            try:
                freq_hz = int(text[2:12])
                if 100000 <= freq_hz <= 3000000000:
                    radio_state["freq"] = freq_hz
                    radio_state["band"] = freq_to_band(freq_hz)
                    set_relays_for_frequency(freq_hz)
            except ValueError:
                pass
            if len(text) >= 18:
                mode_map = {
                    "1": "LSB",
                    "2": "USB",
                    "3": "CW",
                    "4": "FM",
                    "5": "AM",
                    "6": "RTTY",
                    "7": "CW",
                    "8": "FM",
                    "9": "FM",
                }
                mode_digit = text[17]
                radio_state["mode"] = mode_map.get(mode_digit, "Unknown")


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
    global ser, decoder
    try:
        if ser and ser.is_open:
            ser.close()

        ser = serial.Serial(
            trx_config["serial_port"], trx_config["baudrate"], timeout=0.1
        )
        protocol = trx_config.get("protocol", "Icom")
        if protocol == "Kenwood":
            decoder = KenwoodDecoder()
        else:
            decoder = CIVDecoder()
        radio_state["online"] = True
        return True
    except Exception as e:
        radio_state["online"] = False
        print(f"[TRX] Failed: {e}")
        return False


def serial_reader(loop_ref):
    global ser, decoder
    while True:
        if ser and ser.is_open:
            try:
                data = ser.read(1024)
                if data and decoder:
                    decoder.feed(data)
                    if loop_ref:
                        asyncio.run_coroutine_threadsafe(broadcast(data), loop_ref)
            except Exception as e:
                print(f"[TRX] Read error: {e}")
                radio_state["online"] = False
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
    addr = writer.get_extra_info("peername")
    clients.add(writer)
    try:
        while True:
            data = await reader.read(1024)
            if not data:
                break
            if ser and ser.is_open:
                ser.write(data)
    except:
        pass
    clients.discard(writer)
    writer.close()
    await writer.wait_closed()


async def poller():
    while True:
        await asyncio.sleep(2)
        if not trx_config.get("enabled", True):
            continue
        if not ser or not ser.is_open:
            radio_state["online"] = False
            continue

        if time.time() - radio_state["last_rx"] > 5:
            radio_state["online"] = False

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
            ser.write(cmd)
        except:
            pass


async def start_trx_server():
    global loop
    loop = asyncio.get_running_loop()

    thread = threading.Thread(target=serial_reader, args=(loop,), daemon=True)
    thread.start()

    server = await asyncio.start_server(tcp_client, "0.0.0.0", trx_config["tcp_port"])

    asyncio.create_task(poller())

    return server


# ================= AUTH =================

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
        if request.form.get("password") == PASSWORD:
            session["auth"] = True
            return redirect("/")
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

# Упрощенный HTML без сложных конструкций
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NanoPi Controller</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #0f1115;
            color: #e6e6e6;
            font-family: 'Segoe UI', Arial, sans-serif;
            padding: 20px;
        }
        h1, h2, h3 { margin-bottom: 15px; }
        .tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .tab {
            background: #222;
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            transition: 0.2s;
        }
        .tab:hover { background: #2a6fdf; }
        .tab.active { background: #2a6fdf; }
        .panel {
            display: none;
            animation: fadeIn 0.3s;
        }
        .panel.active { display: block; }
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        .group {
            background: #1a1d24;
            border: 1px solid #333;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .relay-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
            gap: 10px;
            margin-top: 15px;
        }
        button {
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: 0.1s;
            font-size: 14px;
        }
        .relay-btn {
            padding: 15px;
            background: #d64545;
            color: white;
        }
        .relay-btn.on { background: #1faa59; }
        .camera-btn, .save-btn, .refresh-btn {
            background: #2d6cdf;
            color: white;
            padding: 10px 20px;
            margin: 5px;
        }
        .trx-panel {
            background: #1a1d24;
            border: 1px solid #333;
            border-radius: 12px;
            padding: 20px;
            text-align: center;
        }
        .freq-display {
            font-size: 48px;
            font-family: monospace;
            background: #000;
            padding: 20px;
            border-radius: 10px;
            margin: 15px 0;
        }
        .status-online { color: #1faa59; }
        .status-offline { color: #d64545; }
        .settings-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }
        .settings-column {
            background: #1a1d24;
            border: 1px solid #333;
            border-radius: 12px;
            padding: 15px;
        }
        .name-row {
            display: flex;
            align-items: center;
            margin-bottom: 8px;
            gap: 10px;
        }
        .name-row input {
            flex: 1;
            background: #2a2d34;
            color: white;
            border: 1px solid #444;
            border-radius: 6px;
            padding: 6px;
        }
        input, select {
            background: #2a2d34;
            color: white;
            border: 1px solid #444;
            border-radius: 6px;
            padding: 8px;
            margin: 5px 0;
        }
        .toast {
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: #1faa59;
            color: white;
            padding: 12px 20px;
            border-radius: 8px;
            opacity: 0;
            transition: 0.3s;
            z-index: 9999;
        }
        .toast.show { opacity: 1; }
        .value-display { font-size: 18px; margin-top: 10px; padding: 10px; background: #2a2d34; border-radius: 6px; text-align: center; }
        .status-display { font-size: 24px; font-weight: bold; text-align: center; margin: 20px 0; }
        .good { color: #1faa59; }
        .warning { color: #f39c12; }
        .bad { color: #d64545; }
        .timestamp { font-size: 14px; color: #7f8c8d; text-align: center; }
        .profile-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }
        .profile-card {
            background: #2a2d34;
            border: 1px solid #444;
            border-radius: 6px;
            padding: 15px;
            text-align: center;
        }
        .profile-name {
            font-weight: bold;
            margin-bottom: 10px;
            display: block;
            color: #e6e6e6;
        }
        .profile-actions {
            display: flex;
            justify-content: center;
            gap: 5px;
        }
        .btn-small {
            padding: 5px 10px;
            font-size: 14px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
        }
        .btn-success { background: #1faa59; color: white; }
        .btn-danger { background: #d64545; color: white; }
        .danger { background: #d64545; color: white; }
        .audio-slider {
            -webkit-appearance: none;
            appearance: none;
            width: 100%;
            height: 12px;
            margin: 15px 0;
            background: #2a2d34;
            border-radius: 6px;
            outline: none;
            cursor: pointer;
        }
        .audio-slider::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 28px;
            height: 28px;
            background: #2d6cdf;
            border-radius: 50%;
            cursor: pointer;
            transition: 0.2s;
        }
        .audio-slider::-webkit-slider-thumb:hover {
            background: #4a8af4;
            transform: scale(1.1);
        }
        .audio-slider::-moz-range-thumb {
            width: 28px;
            height: 28px;
            background: #2d6cdf;
            border: none;
            border-radius: 50%;
            cursor: pointer;
        }
        .audio-slider::-moz-range-track {
            background: #2a2d34;
            border-radius: 6px;
            height: 12px;
        }
        .freq-step-btn {
            padding: 8px 12px;
            border: none;
            border-radius: 6px;
            color: white;
            font-size: 12px;
            font-weight: bold;
            cursor: pointer;
            transition: 0.15s;
        }
        .freq-step-btn:hover {
            transform: scale(1.05);
            filter: brightness(1.2);
        }
        .freq-step-btn:active {
            transform: scale(0.95);
        }
        .band-btn {
            padding: 8px 14px;
            border: 2px solid #2d6cdf;
            border-radius: 6px;
            background: transparent;
            color: #2d6cdf;
            font-size: 13px;
            font-weight: bold;
            cursor: pointer;
            transition: 0.15s;
        }
        .band-btn:hover {
            background: #2d6cdf;
            color: white;
        }
        .band-btn:active {
            transform: scale(0.95);
        }
    </style>
</head>
<body>
    <h1>NanoPi Controller</h1>

    <div class="tabs">
        <div class="tab active" data-tab="main">Main</div>
        <div class="tab" data-tab="trx">TRX</div>
        <div class="tab" data-tab="bandrelay">Band Relay</div>
        <div class="tab" data-tab="audio">Audio</div>
        <div class="tab" data-tab="config">Config</div>
        <div class="tab" data-tab="status">Status</div>
        <div class="tab" data-tab="settings">Settings</div>
        <div class="tab" onclick="location.href='/logout'">Logout</div>
    </div>

    <!-- Main Panel -->
    <div id="main-panel" class="panel active">
        <div class="group">
            <h3>Group 1 (<span id="mode0_label">toggle</span>)</h3>
            <div id="relays1" class="relay-grid"></div>
        </div>

        <div class="group">
            <h3>Group 2 (<span id="mode1_label">toggle</span>)</h3>
            <div id="relays2" class="relay-grid"></div>
        </div>

        <h3>Camera</h3>
        <button class="camera-btn" onclick="toggleCamera()">Show Camera</button>
        <button class="camera-btn" onclick="openCameraWindow()">Fullscreen</button>
        <div id="camera-container" style="margin-top: 20px;"></div>
    </div>

    <!-- TRX Panel -->
    <div id="trx-panel" class="panel">
        <div class="trx-panel">
            <h3>Transceiver Status</h3>
            <div id="trx-status" style="font-size: 18px; margin: 10px;">Loading...</div>
            <div class="freq-display" id="trx-freq">---.--- MHz</div>
            <div id="trx-band">Band: ---</div>
            <div id="trx-mode">Mode: ---</div>

            <!-- Frequency Step Controls -->
            <div style="margin: 15px 0;">
                <div style="display: flex; gap: 6px; justify-content: center; flex-wrap: wrap;">
                    <button class="freq-step-btn" onclick="freqStep(-10000000)" style="background:#8e44ad;">-10 MHz</button>
                    <button class="freq-step-btn" onclick="freqStep(-1000000)" style="background:#8e44ad;">-1 MHz</button>
                    <button class="freq-step-btn" onclick="freqStep(-100000)" style="background:#2980b9;">-100 kHz</button>
                    <button class="freq-step-btn" onclick="freqStep(-10000)" style="background:#2980b9;">-10 kHz</button>
                    <button class="freq-step-btn" onclick="freqStep(-1000)" style="background:#2980b9;">-1 kHz</button>
                    <button class="freq-step-btn" onclick="freqStep(1000)" style="background:#e67e22;">+1 kHz</button>
                    <button class="freq-step-btn" onclick="freqStep(10000)" style="background:#e67e22;">+10 kHz</button>
                    <button class="freq-step-btn" onclick="freqStep(100000)" style="background:#e67e22;">+100 kHz</button>
                    <button class="freq-step-btn" onclick="freqStep(1000000)" style="background:#27ae60;">+1 MHz</button>
                    <button class="freq-step-btn" onclick="freqStep(10000000)" style="background:#27ae60;">+10 MHz</button>
                </div>
            </div>

            <!-- Band Selection -->
            <div style="margin: 15px 0;">
                <div style="display: flex; gap: 6px; justify-content: center; flex-wrap: wrap;">
                    <button class="band-btn" onclick="setBand('160m')">160m</button>
                    <button class="band-btn" onclick="setBand('80m')">80m</button>
                    <button class="band-btn" onclick="setBand('40m')">40m</button>
                    <button class="band-btn" onclick="setBand('30m')">30m</button>
                    <button class="band-btn" onclick="setBand('20m')">20m</button>
                    <button class="band-btn" onclick="setBand('17m')">17m</button>
                    <button class="band-btn" onclick="setBand('15m')">15m</button>
                    <button class="band-btn" onclick="setBand('12m')">12m</button>
                    <button class="band-btn" onclick="setBand('10m')">10m</button>
                    <button class="band-btn" onclick="setBand('6m')">6m</button>
                </div>
            </div>

            <button class="refresh-btn" onclick="loadTrxState()">Refresh</button>
        </div>
    </div>

    <!-- Band Relay Panel -->
    <div id="bandrelay-panel" class="panel">
        <div class="group">
            <h2>Band Relay Rules</h2>
            <p style="margin-bottom:15px;color:#aaa;">Configure which relays activate for each frequency range (in kHz). Rule: <b>from ≤ freq < to</b> (lower bound inclusive, upper bound exclusive). To avoid gaps, set <b>to</b> of one rule equal to <b>from</b> of the next.</p>
            <div style="margin-bottom:15px;padding:10px;background:#2a2d34;border-radius:6px;display:flex;align-items:center;gap:10px;">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:16px;">
                    <input type="checkbox" id="bandrelay-enabled" onchange="toggleBandRelay()" style="width:20px;height:20px;cursor:pointer;" checked>
                    <span>Auto relay switching</span>
                </label>
            </div>
            <div id="bandrelay-status" style="margin-bottom:15px;padding:10px;background:#2a2d34;border-radius:6px;">
                Current: <span id="bandrelay-current-freq">---</span> kHz → Relays: <span id="bandrelay-current-relays">none</span>
            </div>
            <table style="width:100%;border-collapse:collapse;margin-bottom:15px;">
                <thead>
                    <tr style="background:#2a2d34;">
                        <th style="padding:8px;text-align:left;border-bottom:2px solid #444;">From (kHz)</th>
                        <th style="padding:8px;text-align:left;border-bottom:2px solid #444;">To (kHz)</th>
                        <th style="padding:8px;text-align:left;border-bottom:2px solid #444;">Active Relays (1-16)</th>
                        <th style="padding:8px;text-align:center;border-bottom:2px solid #444;">Actions</th>
                    </tr>
                </thead>
                <tbody id="bandrelay-table-body">
                </tbody>
            </table>
            <button class="save-btn" onclick="addBandRule()" style="margin-right:10px;">+ Add Rule</button>
            <button class="save-btn" onclick="saveBandRules()">Save All Rules</button>
            <button class="refresh-btn" onclick="loadBandRules()" style="margin-left:10px;">Refresh</button>
        </div>
    </div>

    <!-- Settings Panel -->
    <div id="settings-panel" class="panel">
        <div class="settings-grid">
            <div class="settings-column">
                <h4>Group 1 Settings</h4>
                Mode:
                <select id="group1_mode">
                    <option value="toggle">Toggle</option>
                    <option value="switch">Switch</option>
                </select>
                <div id="relay-names1" style="margin-top: 15px;"></div>
            </div>

            <div class="settings-column">
                <h4>Group 2 Settings</h4>
                Mode:
                <select id="group2_mode">
                    <option value="toggle">Toggle</option>
                    <option value="switch">Switch</option>
                </select>
                <div id="relay-names2" style="margin-top: 15px;"></div>
            </div>
        </div>

        <div class="settings-grid">
            <div class="settings-column">
                <h4>Transceiver Settings</h4>
                <label>Serial Port:</label>
                <input type="text" id="trx-port" placeholder="/dev/ttyUSB0">
                <label>Baudrate:</label>
                <select id="trx-baudrate">
                    <option value="4800">4800</option>
                    <option value="9600">9600</option>
                    <option value="19200">19200</option>
                    <option value="38400">38400</option>
                    <option value="57600">57600</option>
                    <option value="115200">115200</option>
                </select>
                <label>Protocol:</label>
                <select id="trx-protocol">
                    <option value="Icom">Icom (CI-V)</option>
                    <option value="Kenwood">Kenwood</option>
                </select>
                <label>Transceiver Address (hex):</label>
                <input type="text" id="trx-radio-addr" placeholder="e.g. 0x70" maxlength="6">
                <label>Enabled:</label>
                <select id="trx-enabled">
                    <option value="true">Yes</option>
                    <option value="false">No</option>
                </select>
            </div>

            <div class="settings-column">
                <h4>Actions</h4>
                <button class="save-btn" onclick="saveRelaySettings()" style="width: 100%; margin-bottom: 10px;">Save Relay Settings</button>
                <button class="save-btn" onclick="saveTrxSettings()" style="width: 100%; margin-bottom: 10px;">Save TRX Settings</button>
                <button class="save-btn" onclick="saveAllSettings()" style="width: 100%;">Save All Settings</button>
            </div>
        </div>
    </div>

    <!-- Audio Panel -->
    <div id="audio-panel" class="panel">
        <div class="group">
            <h2>Audio OUT</h2>
            <input type="range" id="speaker-slider" min="0" max="100" value="50" class="audio-slider">
            <div class="value-display" id="speaker-val">50%</div>
            <button class="save-btn" onclick="setSpeaker()">Set Speaker</button>
        </div>
        <div class="group">
            <h2>Audio IN</h2>
            <input type="range" id="mic-slider" min="0" max="100" value="50" class="audio-slider">
            <div class="value-display" id="mic-val">50%</div>
            <button class="save-btn" onclick="setMic()">Set Capture</button>
        </div>
    </div>

    <!-- Config Panel -->
    <div id="config-panel" class="panel">
        <div class="group">
            <h2>Saved Profiles</h2>
            <div id="profile-grid" class="profile-grid"></div>
        </div>
        <div class="group">
            <h2>Server IP Configuration (Local)</h2>
            <textarea id="server-ip-input" style="width:100%;height:40px;padding:10px;border:1px solid #444;border-radius:6px;background:#2a2d34;color:white;font-family:monospace;margin-bottom:15px;resize:vertical;"></textarea>
            <button class="save-btn" onclick="saveServerIp()">Save Server Config</button>
        </div>
        <div class="group">
            <h2>Client IP Configuration (Remote)</h2>
            <textarea id="client-ip-input" style="width:100%;height:40px;padding:10px;border:1px solid #444;border-radius:6px;background:#2a2d34;color:white;font-family:monospace;margin-bottom:15px;resize:vertical;"></textarea>
            <button class="save-btn" onclick="saveClientIp()">Save Client Config</button>
        </div>
        <div class="group">
            <h2>Audio Stream Settings</h2>
            <label>Sample Rate:</label>
            <select id="audio-rate">
                <option value="48000">48000 Hz</option>
                <option value="24000">24000 Hz</option>
            </select>
            <label>Buffer Time (alsasrc):</label>
            <select id="audio-buffer">
                <option value="50000">50 ms (50000 µs)</option>
                <option value="100000">100 ms (100000 µs)</option>
                <option value="200000">200 ms (200000 µs)</option>
                <option value="300000">300 ms (300000 µs)</option>
                <option value="400000">400 ms (400000 µs)</option>
                <option value="500000">500 ms (500000 µs)</option>
            </select>
            <button class="save-btn" onclick="saveAudioSettings()" style="margin-top:10px;">Save Audio Settings</button>
        </div>
        <div class="group">
            <button class="save-btn danger" onclick="restartServices()" style="background:#d64545;">Restart Audio Services</button>
            <button class="save-btn danger" onclick="restartWebPanel()" style="background:#d64545;margin-left:10px;">Restart Web Panel</button>
        </div>
    </div>

    <!-- Status Panel -->
    <div id="status-panel" class="panel">
        <div class="group" style="text-align:center;">
            <h2>Network Information</h2>
            <div class="value-display" id="local-ip"><strong>Local IP:</strong> Loading...</div>
            <h2>Connection to Client</h2>
            <div id="connection-status" class="status-display" style="font-size:24px;font-weight:bold;text-align:center;margin:20px 0;">
                <span id="rtt-value">--</span>
            </div>
            <div id="timestamp" class="timestamp" style="font-size:14px;color:#7f8c8d;text-align:center;">Last updated: --</div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        let relayNames = Array(16).fill().map((_, i) => 'Relay ' + (i+1));
        let relayState = Array(16).fill(0);
        let relayMode = ['toggle', 'toggle'];
        let pttActive = false;

        function showToast(msg, isOk = true) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.style.background = isOk ? '#1faa59' : '#d64545';
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 2000);
        }

        // Tab switching
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', function() {
                const tabName = this.dataset.tab;
                if (!tabName) return;

                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                this.classList.add('active');

                document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
                document.getElementById(`${tabName}-panel`).classList.add('active');

                if (tabName === 'trx') loadTrxState();
            });
        });

        // Relay functions
        function renderRelays() {
            const container1 = document.getElementById('relays1');
            const container2 = document.getElementById('relays2');
            container1.innerHTML = '';
            container2.innerHTML = '';

            for (let i = 0; i < 16; i++) {
                const btn = document.createElement('button');
                btn.className = 'relay-btn' + (relayState[i] ? ' on' : '');
                btn.textContent = relayNames[i];
                btn.onclick = () => toggleRelay(i);
                if (pttActive) {
                    btn.style.opacity = '0.5';
                    btn.style.cursor = 'not-allowed';
                    btn.title = '🔒 PTT active — relay switching blocked';
                }
                if (i < 8) container1.appendChild(btn);
                else container2.appendChild(btn);
            }
        }

        function loadRelays() {
            fetch('/state')
                .then(r => r.json())
                .then(data => {
                    relayState = data.state;
                    relayNames = data.names;
                    relayMode = data.mode;
                    document.getElementById('mode0_label').textContent = relayMode[0];
                    document.getElementById('mode1_label').textContent = relayMode[1];
                    renderRelays();

                    // Settings panel
                    document.getElementById('group1_mode').value = relayMode[0];
                    document.getElementById('group2_mode').value = relayMode[1];

                    const names1 = document.getElementById('relay-names1');
                    const names2 = document.getElementById('relay-names2');
                    names1.innerHTML = '';
                    names2.innerHTML = '';

                    for (let i = 0; i < 16; i++) {
                        const div = document.createElement('div');
                        div.className = 'name-row';
                        div.innerHTML = `<span>${i+1}.</span><input type="text" id="relay_name_${i}" value="${relayNames[i]}">`;
                        if (i < 8) names1.appendChild(div);
                        else names2.appendChild(div);
                    }
                });
        }

        function checkPttStatus() {
            return fetch('/ptt/status')
                .then(r => r.json())
                .then(data => {
                    pttActive = data.active;
                })
                .catch(() => {});
        }

        function toggleRelay(idx) {
            if (pttActive) {
                showToast('🔒 PTT active — relay switching blocked', false);
                return;
            }
            fetch(`/toggle/${idx}`)
                .then(r => r.json())
                .then(data => {
                    relayState = data.state;
                    renderRelays();
                    showToast(`Toggled ${relayNames[idx]}`, true);
                })
                .catch(() => showToast('Failed to toggle relay', false));
        }

        function saveRelaySettings() {
            const newNames = [];
            for (let i = 0; i < 16; i++) {
                const input = document.getElementById(`relay_name_${i}`);
                if (input) newNames.push(input.value);
                else newNames.push(relayNames[i]);
            }

            const data = {
                names: newNames,
                mode: [
                    document.getElementById('group1_mode').value,
                    document.getElementById('group2_mode').value
                ]
            };

            fetch('/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            }).then(r => {
                if (r.ok) {
                    showToast('✅ Relay settings saved successfully!', true);
                    loadRelays();
                } else {
                    showToast('❌ Failed to save relay settings', false);
                }
            }).catch(() => showToast('❌ Network error while saving', false));
        }

        // TRX functions
        function loadTrxState() {
            fetch('/trx/state')
                .then(r => r.json())
                .then(data => {
                    const statusDiv = document.getElementById('trx-status');
                    const freqDiv = document.getElementById('trx-freq');
                    const bandDiv = document.getElementById('trx-band');
                    const modeDiv = document.getElementById('trx-mode');

                    if (data.online) {
                        statusDiv.innerHTML = '🟢 ONLINE';
                        statusDiv.className = 'status-online';
                        freqDiv.textContent = (data.freq / 1000000).toFixed(6) + ' MHz';
                    } else {
                        statusDiv.innerHTML = '🔴 OFFLINE';
                        statusDiv.className = 'status-offline';
                        freqDiv.textContent = '---.--- MHz';
                    }
                    bandDiv.textContent = 'Band: ' + data.band;
                    modeDiv.textContent = 'Mode: ' + data.mode;
                })
                .catch(() => {
                    document.getElementById('trx-status').innerHTML = '🔴 OFFLINE';
                });
        }

        // TRX Control Functions
        function freqStep(step) {
            fetch('/trx/freq_step', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({step: step})
            })
            .then(r => r.json())
            .then(data => {
                const stepLabel = step >= 0 ? '+' + step : '' + step;
                showToast('📡 Freq: ' + (data.freq / 1000000).toFixed(6) + ' MHz (' + stepLabel + ' Hz)', true);
                loadTrxState();
            })
            .catch(() => showToast('❌ Failed to change frequency', false));
        }

        function setBand(band) {
            fetch('/trx/set_band', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({band: band})
            })
            .then(r => {
                if (!r.ok) throw new Error('Band not found');
                return r.json();
            })
            .then(data => {
                showToast('📡 Switched to ' + data.band + ' (' + (data.freq / 1000000).toFixed(6) + ' MHz)', true);
                loadTrxState();
            })
            .catch(() => showToast('❌ Failed to switch band', false));
        }

        function loadTrxConfig() {
            fetch('/trx/config')
                .then(r => r.json())
                .then(cfg => {
                    document.getElementById('trx-port').value = cfg.serial_port;
                    document.getElementById('trx-baudrate').value = cfg.baudrate;
                    document.getElementById('trx-protocol').value = cfg.protocol;
                    document.getElementById('trx-enabled').value = cfg.enabled;
                    // Format radio_addr as hex string
                    document.getElementById('trx-radio-addr').value = '0x' + cfg.radio_addr.toString(16).toUpperCase().padStart(2, '0');
                });
        }

        function saveTrxSettings() {
            // Validate transceiver address
            const addrStr = document.getElementById('trx-radio-addr').value.trim();
            let radioAddr;
            if (!addrStr) {
                showToast('❌ Transceiver address is required', false);
                return;
            }
            const addrMatch = addrStr.match(/^0x([0-9a-fA-F]{1,2})$/);
            if (!addrMatch) {
                showToast('❌ Invalid address format. Use hex format: 0x00-0xFF', false);
                return;
            }
            radioAddr = parseInt(addrStr, 16);
            if (isNaN(radioAddr) || radioAddr < 0 || radioAddr > 255) {
                showToast('❌ Address must be between 0x00 and 0xFF', false);
                return;
            }

            const data = {
                serial_port: document.getElementById('trx-port').value,
                baudrate: parseInt(document.getElementById('trx-baudrate').value),
                protocol: document.getElementById('trx-protocol').value,
                radio_addr: radioAddr,
                enabled: document.getElementById('trx-enabled').value === 'true'
            };

            fetch('/trx/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            }).then(r => {
                if (r.ok) {
                    showToast('✅ TRX settings saved successfully!', true);
                } else {
                    return r.text().then(t => showToast('❌ ' + t, false));
                }
            }).catch(() => showToast('❌ Network error while saving', false));
        }

        function saveAllSettings() {
            saveRelaySettings();
            saveTrxSettings();
            showToast('💾 Saving all settings...', true);
        }

        // Camera functions
        let cameraVisible = false;

        function toggleCamera() {
            const container = document.getElementById('camera-container');
            const btn = event.target;
            if (cameraVisible) {
                container.innerHTML = '';
                btn.textContent = 'Show Camera';
                showToast('Camera hidden', true);
            } else {
                container.innerHTML = '<img src="/stream" style="max-width: 100%; border-radius: 10px;">';
                btn.textContent = 'Hide Camera';
                showToast('Camera shown', true);
            }
            cameraVisible = !cameraVisible;
        }

        function openCameraWindow() {
            window.open('/camera', '_blank');
            showToast('Opening camera in new window', true);
        }

        // Auto-refresh TRX state every 2 seconds when TRX tab is active
        setInterval(() => {
            const activePanel = document.querySelector('.panel.active');
            if (activePanel && activePanel.id === 'trx-panel') {
                loadTrxState();
            }
        }, 2000);

        // ================= Audio functions =================
        function updateSpeakerVal() {
            const s = document.getElementById('speaker-slider');
            document.getElementById('speaker-val').textContent = s.value + '%';
        }
        function updateMicVal() {
            const m = document.getElementById('mic-slider');
            document.getElementById('mic-val').textContent = m.value + '%';
        }

        function loadAudioState() {
            fetch('/audio/state')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('speaker-slider').value = data.speaker;
                    document.getElementById('speaker-val').textContent = data.speaker + '%';
                    document.getElementById('mic-slider').value = data.mic;
                    document.getElementById('mic-val').textContent = data.mic + '%';
                })
                .catch(() => {});
        }

        // Attach live value update on slider input
        document.addEventListener('DOMContentLoaded', function() {
            const sp = document.getElementById('speaker-slider');
            const mc = document.getElementById('mic-slider');
            if (sp) sp.addEventListener('input', updateSpeakerVal);
            if (mc) mc.addEventListener('input', updateMicVal);
        });

        function setSpeaker() {
            const val = document.getElementById('speaker-slider').value;
            fetch('/audio/speaker', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({volume: parseInt(val)})
            }).then(r => {
                if (r.ok) showToast('✅ Speaker volume set to ' + val + '%', true);
                else showToast('❌ Failed to set speaker', false);
            }).catch(() => showToast('❌ Network error', false));
        }

        function setMic() {
            const val = document.getElementById('mic-slider').value;
            fetch('/audio/mic', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({volume: parseInt(val)})
            }).then(r => {
                if (r.ok) showToast('✅ Mic capture set to ' + val + '%', true);
                else showToast('❌ Failed to set mic', false);
            }).catch(() => showToast('❌ Network error', false));
        }

        // ================= Config functions =================
        function loadConfig() {
            fetch('/config/data')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('server-ip-input').value = data.server_ip;
                    document.getElementById('client-ip-input').value = data.client_ip;
                    document.getElementById('audio-rate').value = data.audio_rate;
                    document.getElementById('audio-buffer').value = data.audio_buffer;

                    // Profiles
                    const grid = document.getElementById('profile-grid');
                    grid.innerHTML = '';
                    data.profiles.forEach(name => {
                        const card = document.createElement('div');
                        card.className = 'profile-card';
                        card.innerHTML = `
                            <span class="profile-name">${name}</span>
                            <div class="profile-actions">
                                <button class="btn-small btn-success" onclick="loadProfile('${name}')">Load</button>
                                <button class="btn-small btn-danger" onclick="deleteProfile('${name}')">Del</button>
                            </div>
                        `;
                        grid.appendChild(card);
                    });
                    // Empty slot
                    if (data.profiles.length < 5) {
                        const slot = document.createElement('div');
                        slot.className = 'profile-card';
                        slot.style.borderStyle = 'dashed';
                        slot.innerHTML = `
                            <span class="profile-name" style="color:#666;">Empty Slot</span>
                            <input type="text" id="new-profile-name" placeholder="Profile Name" style="width:100%;padding:5px;margin-bottom:5px;background:#2a2d34;color:white;border:1px solid #444;border-radius:4px;" maxlength="15">
                            <button class="btn-small btn-success" onclick="saveProfile()">Save Current</button>
                        `;
                        grid.appendChild(slot);
                    }
                })
                .catch(() => {});
        }

        function saveServerIp() {
            const ip = document.getElementById('server-ip-input').value.trim();
            fetch('/config/server_ip', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ip: ip})
            }).then(r => {
                if (r.ok) showToast('✅ Server IP saved', true);
                else return r.text().then(t => { showToast('❌ ' + t, false); });
            }).catch(() => showToast('❌ Network error', false));
        }

        function saveClientIp() {
            const ip = document.getElementById('client-ip-input').value.trim();
            fetch('/config/client_ip', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ip: ip})
            }).then(r => {
                if (r.ok) showToast('✅ Client IP saved', true);
                else return r.text().then(t => { showToast('❌ ' + t, false); });
            }).catch(() => showToast('❌ Network error', false));
        }

        function saveAudioSettings() {
            const data = {
                rate: parseInt(document.getElementById('audio-rate').value),
                buffer: parseInt(document.getElementById('audio-buffer').value)
            };
            fetch('/config/audio', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            }).then(r => {
                if (r.ok) showToast('✅ Audio settings saved', true);
                else showToast('❌ Failed to save audio settings', false);
            }).catch(() => showToast('❌ Network error', false));
        }

        function saveProfile() {
            const name = document.getElementById('new-profile-name');
            if (!name || !name.value.trim()) {
                showToast('❌ Enter a profile name', false);
                return;
            }
            fetch('/config/save_profile', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name.value.trim()})
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    showToast('✅ ' + data.message, true);
                    loadConfig();
                } else {
                    showToast('❌ ' + data.message, false);
                }
            }).catch(() => showToast('❌ Network error', false));
        }

        function loadProfile(name) {
            fetch('/config/load_profile', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name})
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    showToast('✅ ' + data.message, true);
                    loadConfig();
                } else {
                    showToast('❌ ' + data.message, false);
                }
            }).catch(() => showToast('❌ Network error', false));
        }

        function deleteProfile(name) {
            if (!confirm('Delete profile ' + name + '?')) return;
            fetch('/config/delete_profile', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name})
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    showToast('✅ ' + data.message, true);
                    loadConfig();
                } else {
                    showToast('❌ ' + data.message, false);
                }
            }).catch(() => showToast('❌ Network error', false));
        }

        function restartServices() {
            if (!confirm('Restart audio services?')) return;
            fetch('/config/restart_services', {method: 'POST'})
                .then(r => {
                    if (r.ok) showToast('✅ Services restarted', true);
                    else showToast('❌ Failed to restart', false);
                }).catch(() => showToast('❌ Network error', false));
        }

        function restartWebPanel() {
            if (!confirm('Restart the web panel? The page will reload after restart.')) return;
            fetch('/config/restart_web', {method: 'POST'})
                .then(r => {
                    if (r.ok) {
                        showToast('🔄 Web panel restarting...', true);
                        setTimeout(() => { location.reload(); }, 3000);
                    } else {
                        showToast('❌ Failed to restart web panel', false);
                    }
                }).catch(() => showToast('❌ Network error', false));
        }

        // ================= Status functions =================
        function loadLocalIp() {
            fetch('/status/local_ip')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('local-ip').innerHTML = '<strong>Local IP:</strong> ' + data.ip;
                })
                .catch(() => {});
        }

        function updateConnectionStatus() {
            fetch('/status/connection')
                .then(r => r.json())
                .then(data => {
                    const statusEl = document.getElementById('connection-status');
                    const valueEl = document.getElementById('rtt-value');
                    const timeEl = document.getElementById('timestamp');

                    if (data.rtt !== null) {
                        valueEl.textContent = data.rtt.toFixed(1) + ' ms';
                        statusEl.className = 'status-display ' + data.status;
                    } else {
                        valueEl.textContent = '--';
                        statusEl.className = 'status-display bad';
                    }
                    timeEl.textContent = 'Last updated: ' + data.timestamp;
                })
                .catch(() => {});
        }

        // Tab switch handler extension
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', function() {
                const tabName = this.dataset.tab;
                if (tabName === 'main') loadRelays();
                if (tabName === 'audio') loadAudioState();
                if (tabName === 'config') loadConfig();
                if (tabName === 'status') { loadLocalIp(); updateConnectionStatus(); }
            });
        });

        // Auto-refresh status every 2 seconds when Status tab is active
        setInterval(() => {
            const activePanel = document.querySelector('.panel.active');
            if (activePanel && activePanel.id === 'status-panel') {
                updateConnectionStatus();
            }
        }, 2000);

        // ================= Band Relay functions =================
        let bandRules = [];

        function loadBandRules() {
            fetch('/bandrelay/rules')
                .then(r => r.json())
                .then(rules => {
                    bandRules = rules;
                    renderBandRules();
                })
                .catch(() => {});
            // Also load current state
            fetch('/bandrelay/state')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('bandrelay-current-freq').textContent =
                        data.freq_khz ? data.freq_khz.toFixed(1) : '---';
                    document.getElementById('bandrelay-current-relays').textContent =
                        data.active_relays.length ? data.active_relays.map(r => r+1).join(', ') : 'none';
                    document.getElementById('bandrelay-enabled').checked = data.enabled;
                })
                .catch(() => {});
        }

        function toggleBandRelay() {
            const enabled = document.getElementById('bandrelay-enabled').checked;
            fetch('/bandrelay/toggle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: enabled})
            }).then(r => {
                if (r.ok) {
                    showToast(enabled ? '🔁 Auto relay switching ON' : '⏸️ Auto relay switching OFF', true);
                }
            }).catch(() => showToast('❌ Network error', false));
        }

        function renderBandRules() {
            const tbody = document.getElementById('bandrelay-table-body');
            tbody.innerHTML = '';
            if (!bandRules.length) {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;padding:20px;color:#666;">No rules configured. Click "+ Add Rule" to create one.</td></tr>';
                return;
            }
            bandRules.forEach((rule, idx) => {
                const tr = document.createElement('tr');
                tr.style.borderBottom = '1px solid #333';
                tr.innerHTML = `
                    <td style="padding:6px;"><input type="number" class="br-from" value="${rule.from}" min="0" max="60000" style="width:100px;"></td>
                    <td style="padding:6px;"><input type="number" class="br-to" value="${rule.to}" min="0" max="60000" style="width:100px;"></td>
                    <td style="padding:6px;"><input type="text" class="br-relays" value="${rule.relays.map(r => r+1).join(',')}" placeholder="e.g. 1,2,16" style="width:100%;"></td>
                    <td style="padding:6px;text-align:center;">
                        <button class="btn-small btn-danger" onclick="deleteBandRule(${idx})">✕</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }

        function addBandRule() {
            bandRules.push({from: 7000, to: 7300, relays: []});
            renderBandRules();
            showToast('➕ New rule added. Set values and click "Save All Rules".', true);
        }

        function deleteBandRule(idx) {
            bandRules.splice(idx, 1);
            renderBandRules();
            showToast('🗑️ Rule removed. Click "Save All Rules" to persist.', true);
        }

        function saveBandRules() {
            // Read values from inputs
            const rows = document.querySelectorAll('#bandrelay-table-body tr');
            const newRules = [];
            let hasError = false;
            rows.forEach((tr, idx) => {
                const fromInput = tr.querySelector('.br-from');
                const toInput = tr.querySelector('.br-to');
                const relaysInput = tr.querySelector('.br-relays');
                if (!fromInput || !toInput || !relaysInput) return;
                const fromVal = parseInt(fromInput.value);
                const toVal = parseInt(toInput.value);
                if (isNaN(fromVal) || isNaN(toVal) || fromVal < 0 || toVal < 0 || fromVal >= toVal) {
                    showToast(`❌ Rule ${idx+1}: invalid frequency range`, false);
                    hasError = true;
                    return;
                }
                const relays = relaysInput.value.split(',')
                    .map(s => parseInt(s.trim()))
                    .filter(n => !isNaN(n) && n >= 1 && n <= 16)
                    .map(n => n - 1); // convert to 0-based
                newRules.push({from: fromVal, to: toVal, relays: relays});
            });
            if (hasError) return;

            fetch('/bandrelay/rules', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(newRules)
            }).then(r => {
                if (r.ok) {
                    showToast('✅ Band relay rules saved!', true);
                    bandRules = newRules;
                    loadBandRules(); // refresh
                } else {
                    return r.text().then(t => showToast('❌ ' + t, false));
                }
            }).catch(() => showToast('❌ Network error', false));
        }

        // Tab switch handler extension for bandrelay
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', function() {
                const tabName = this.dataset.tab;
                if (tabName === 'bandrelay') loadBandRules();
            });
        });

        // Auto-refresh bandrelay state when tab is active
        setInterval(() => {
            const activePanel = document.querySelector('.panel.active');
            if (activePanel && activePanel.id === 'bandrelay-panel') {
                fetch('/bandrelay/state')
                    .then(r => r.json())
                    .then(data => {
                        document.getElementById('bandrelay-current-freq').textContent =
                            data.freq_khz ? data.freq_khz.toFixed(1) : '---';
                        document.getElementById('bandrelay-current-relays').textContent =
                            data.active_relays.length ? data.active_relays.map(r => r+1).join(', ') : 'none';
                    })
                    .catch(() => {});
            }
        }, 2000);

        // Auto-refresh relay state + PTT status on Main tab every 2 seconds
        setInterval(() => {
            const activePanel = document.querySelector('.panel.active');
            if (activePanel && activePanel.id === 'main-panel') {
                Promise.all([
                    fetch('/state').then(r => r.json()),
                    fetch('/ptt/status').then(r => r.json()),
                ])
                    .then(([stateData, pttData]) => {
                        relayState = stateData.state;
                        relayNames = stateData.names;
                        relayMode = stateData.mode;
                        pttActive = pttData.active;
                        document.getElementById('mode0_label').textContent = relayMode[0];
                        document.getElementById('mode1_label').textContent = relayMode[1];
                        renderRelays();
                    })
                    .catch(() => {});
            }
        }, 2000);

        // Initialize
        loadRelays();
        loadTrxConfig();
        checkPttStatus();
        showToast('🎉 Welcome to NanoPi Controller!', true);
    </script>
</body>
</html>
"""


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
    return render_template_string(HTML_TEMPLATE)


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

    trx_config.update(data)
    save_trx_config()

    if (
        old_port != trx_config["serial_port"]
        or old_baud != trx_config["baudrate"]
        or old_protocol != trx_config.get("protocol", "Icom")
    ):
        init_serial()

    return "ok"


# ================= TRX CONTROL API =================

# Amateur band definitions (Hz)
# Each entry: (from_khz, to_khz, name, target_freq_hz)
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
        ser.write(cmd.encode("ascii"))
        return True
    except Exception as e:
        print(f"[TRX] Kenwood send error: {e}")
        return False


def _is_kenwood():
    return trx_config.get("protocol", "Icom") == "Kenwood"


@app.route("/trx/set_freq", methods=["POST"])
def trx_set_freq():
    """Set transceiver frequency (Hz)."""
    if not auth():
        return "no auth", 403
    data = request.json
    freq_hz = data.get("freq", 0)
    if freq_hz < 100000 or freq_hz > 3000000000:
        return "Invalid frequency", 400

    if _is_kenwood():
        cmd = f"FA{freq_hz:011d};"
        _send_kenwood_cmd(cmd)
    else:
        # Icom CI-V: set frequency command 0x05
        bcd = bytearray(5)
        temp = freq_hz
        for i in range(5):
            low = temp % 10
            temp //= 10
            high = temp % 10
            temp //= 10
            bcd[i] = (high << 4) | low
        cmd = bytes([0x05]) + bytes(bcd)
        _send_civ_cmd(cmd)

    radio_state["freq"] = freq_hz
    radio_state["band"] = freq_to_band(freq_hz)
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

    # Send the new frequency to the radio
    if _is_kenwood():
        cmd = f"FA{new_freq:011d};"
        _send_kenwood_cmd(cmd)
    else:
        bcd = bytearray(5)
        temp = new_freq
        for i in range(5):
            low = temp % 10
            temp //= 10
            high = temp % 10
            temp //= 10
            bcd[i] = (high << 4) | low
        cmd = bytes([0x05]) + bytes(bcd)
        _send_civ_cmd(cmd)

    radio_state["freq"] = new_freq
    radio_state["band"] = freq_to_band(new_freq)
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
            # Send frequency
            if _is_kenwood():
                cmd = f"FA{target_freq:011d};"
                _send_kenwood_cmd(cmd)
            else:
                bcd = bytearray(5)
                temp = target_freq
                for i in range(5):
                    low = temp % 10
                    temp //= 10
                    high = temp % 10
                    temp //= 10
                    bcd[i] = (high << 4) | low
                cmd = bytes([0x05]) + bytes(bcd)
                _send_civ_cmd(cmd)

            radio_state["freq"] = target_freq
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
        # Kenwood: AG command
        _send_kenwood_cmd(f"AG{gain:03d};")
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


@app.route("/audio/speaker", methods=["POST"])
def audio_set_speaker():
    if not auth():
        return "no auth", 403
    data = request.json
    vol = data.get("volume", 50)
    subprocess.run(["amixer", "-c", ALSA_CARD, "set", SPEAKER, f"{vol}%"], timeout=5)
    return "ok"


@app.route("/audio/mic", methods=["POST"])
def audio_set_mic():
    if not auth():
        return "no auth", 403
    data = request.json
    vol = data.get("volume", 50)
    alsa_value = percent_to_alsa(vol)
    subprocess.run(["amixer", "-c", ALSA_CARD, "cset", MIC, str(alsa_value)], timeout=5)
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
    script_path = "/home/pi/nano-server/restart_services_on_server.sh"
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
