#!/usr/bin/env python3
import asyncio
import json
import os
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
    bits = get_state()
    group = 0 if n < 8 else 1
    bit = n if n < 8 else n - 8
    mode = config["group_mode"][group]

    if mode == "switch":
        global state1, state2
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
                    print(f"[TRX] Freq={freq / 1000000:.6f} MHz")

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
        decoder = CIVDecoder()
        radio_state["online"] = True
        print(f"[TRX] Connected to {trx_config['serial_port']}")
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
    print(f"[TCP] Client: {addr}")
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
    print(f"[TRX] TCP port {trx_config['tcp_port']}")

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
    </style>
</head>
<body>
    <h1>NanoPi Controller</h1>

    <div class="tabs">
        <div class="tab active" data-tab="main">Main</div>
        <div class="tab" data-tab="trx">TRX</div>
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
            <button class="refresh-btn" onclick="loadTrxState()">Refresh</button>
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
                    <option value="switch">Switch (radio)</option>
                </select>
                <div id="relay-names1" style="margin-top: 15px;"></div>
            </div>

            <div class="settings-column">
                <h4>Group 2 Settings</h4>
                Mode:
                <select id="group2_mode">
                    <option value="toggle">Toggle</option>
                    <option value="switch">Switch (radio)</option>
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

    <div id="toast" class="toast"></div>

    <script>
        let relayNames = Array(16).fill().map((_, i) => 'Relay ' + (i+1));
        let relayState = Array(16).fill(0);
        let relayMode = ['toggle', 'toggle'];

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

        function toggleRelay(idx) {
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

        function loadTrxConfig() {
            fetch('/trx/config')
                .then(r => r.json())
                .then(cfg => {
                    document.getElementById('trx-port').value = cfg.serial_port;
                    document.getElementById('trx-baudrate').value = cfg.baudrate;
                    document.getElementById('trx-protocol').value = cfg.protocol;
                    document.getElementById('trx-enabled').value = cfg.enabled;
                });
        }

        function saveTrxSettings() {
            const data = {
                serial_port: document.getElementById('trx-port').value,
                baudrate: parseInt(document.getElementById('trx-baudrate').value),
                protocol: document.getElementById('trx-protocol').value,
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
                    showToast('❌ Failed to save TRX settings', false);
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
        let trxInterval = null;
        setInterval(() => {
            const activePanel = document.querySelector('.panel.active');
            if (activePanel && activePanel.id === 'trx-panel') {
                loadTrxState();
            }
        }, 2000);

        // Initialize
        loadRelays();
        loadTrxConfig();
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
    old_port = trx_config["serial_port"]
    old_baud = trx_config["baudrate"]

    trx_config.update(data)
    save_trx_config()

    if old_port != trx_config["serial_port"] or old_baud != trx_config["baudrate"]:
        init_serial()

    return "ok"


# ================= MAIN =================


def start_flask():
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)


async def main():
    load_relay_config()
    load_trx_config()

    if trx_config.get("enabled", True):
        init_serial()

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
