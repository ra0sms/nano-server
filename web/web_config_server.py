import glob
import ipaddress
import os
import re
import socket
import subprocess
import threading
import time

from flask import Flask, jsonify, request

app = Flask(__name__)
# use amixer -c 0 controls to find the correct control numbers
SPEAKER = "Speaker"
MIC = "numid=8"

# Paths
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

# Global variables for status
current_rtt = None
last_update = None
status_active = False
status_thread = None
status_lock = threading.Lock()

# Ensure profiles directory exists
if not os.path.exists(PROFILES_DIR):
    os.makedirs(PROFILES_DIR)

COMMON_STYLE = """
<style>
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background-color: #f5f5f5; color: #333; }
    h1 { color: #2c3e50; text-align: center; margin-bottom: 30px; }
    .control-group { background-color: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
    h2 { color: #3498db; margin-top: 0; }
    h3 { color: #7f8c8d; margin-top: 15px; font-size: 1.1em; }
    input[type="range"] { width: 100%; height: 10px; margin: 15px 0; -webkit-appearance: none; background: #ecf0f1; border-radius: 5px; }
    input[type="range"]::-webkit-slider-thumb { -webkit-appearance: none; width: 25px; height: 25px; background: #3498db; border-radius: 50%; cursor: pointer; }
    button { background-color: #3498db; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 16px; transition: background-color 0.3s; margin-right: 10px; margin-bottom: 5px;}
    button:hover { background-color: #2980b9; }
    button.small { padding: 5px 10px; font-size: 14px; }
    button.danger { background-color: #e74c3c; }
    button.danger:hover { background-color: #c0392b; }
    button.success { background-color: #27ae60; }
    button.success:hover { background-color: #219150; }

    .value-display { font-size: 18px; margin-top: 10px; padding: 10px; background-color: #ecf0f1; border-radius: 5px; text-align: center; }
    .container { display: flex; flex-direction: column; gap: 20px; }
    textarea { width: 100%; height: 40px; padding: 10px; border: 1px solid #ddd; border-radius: 5px; font-family: monospace; margin-bottom: 15px; resize: vertical;}
    select { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; font-size: 16px; margin-bottom: 15px; background: white; }
    .nav { display: flex; justify-content: center; margin-bottom: 20px; flex-wrap: wrap; gap: 5px;}
    .nav-button { padding: 10px 20px; margin: 0 5px; }
    .active { background-color: #2980b9; font-weight: bold; }
    .service-button { background-color: #e74c3c; }
    .service-button:hover { background-color: #c0392b; }
    .status-display { font-size: 24px; font-weight: bold; text-align: center; margin: 20px 0; }
    .good { color: #27ae60; }
    .warning { color: #f39c12; }
    .bad { color: #e74c3c; }
    .timestamp { font-size: 14px; color: #7f8c8d; text-align: center; }
    label { font-weight: bold; display: block; margin-bottom: 5px; }

    .profile-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
        gap: 15px;
        margin-top: 15px;
    }
    .profile-card {
        background: #f9f9f9;
        border: 1px solid #ddd;
        border-radius: 6px;
        padding: 15px;
        text-align: center;
    }
    .profile-name {
        font-weight: bold;
        margin-bottom: 10px;
        display: block;
        color: #2c3e50;
    }
    .profile-actions {
        display: flex;
        justify-content: center;
        gap: 5px;
    }
</style>
"""


def get_local_ip():
    """Return the local IP by routing-table lookup (no packets sent)."""
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
    """Return True if ip_str is a valid IPv4 or IPv6 address."""
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
                current_rtt = rtt  # None means unreachable — always update
                last_update = time.strftime("%H:%M:%S")
        else:
            with status_lock:
                current_rtt = None
                last_update = "No client IP configured"
        time.sleep(CHECK_INTERVAL)


def get_volume_template(speaker_volume, mic_capture, active_page="volume"):
    active_volume = "active" if active_page == "volume" else ""
    active_config = "active" if active_page == "config" else ""
    active_status = "active" if active_page == "status" else ""

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CAESAR Volume Control</title>
    {COMMON_STYLE}
</head>
<body>
    <div class="container">
        <h1>CAESAR Control Panel (Server)</h1>
        <div class="nav">
            <a href="/"><button class="nav-button {active_volume}">Volume Control</button></a>
            <a href="/config"><button class="nav-button {active_config}">Configuration</button></a>
            <a href="/status"><button class="nav-button {active_status}">Status</button></a>
        </div>
        <form method="post">
            <div class="control-group">
                <h2>Audio OUT</h2>
                <input type="range" name="speaker_volume" min="0" max="100" value="{speaker_volume}">
                <div class="value-display">{speaker_volume}%</div>
                <button type="submit" name="action" value="set_speaker">Set Speaker</button>
            </div>
            <div class="control-group">
                <h2>Audio IN</h2>
                <input type="range" name="mic_capture" min="0" max="100" value="{mic_capture}">
                <div class="value-display">{mic_capture}%</div>
                <button type="submit" name="action" value="set_mic">Set Capture</button>
            </div>
        </form>
    </div>
</body>
</html>
"""


def get_config_template(
    server_config,
    client_config,
    audio_rate,
    audio_buffer,
    profiles,
    active_page="config",
    message=None,
    msg_type="info",
):
    active_volume = "active" if active_page == "volume" else ""
    active_config = "active" if active_page == "config" else ""
    active_status = "active" if active_page == "status" else ""

    color = (
        "#27ae60"
        if msg_type == "success"
        else "#e74c3c"
        if msg_type == "error"
        else "#3498db"
    )
    message_html = (
        f'<div class="value-display" style="color:{color};margin-bottom:20px;">{message}</div>'
        if message
        else ""
    )

    # Rate options
    rate_options = ""
    for r in [48000, 24000]:
        selected = "selected" if str(r) == str(audio_rate) else ""
        rate_options += f'<option value="{r}" {selected}>{r} Hz</option>'

    # Buffer options (in microseconds, displayed as ms)
    buffer_options = ""
    for b in [50000, 100000, 200000, 300000, 400000, 500000]:
        selected = "selected" if str(b) == str(audio_buffer) else ""
        ms_val = b // 1000
        buffer_options += (
            f'<option value="{b}" {selected}>{ms_val} ms ({b} µs)</option>'
        )

    profile_cards = ""
    for p_name in profiles:
        profile_cards += f"""
        <div class="profile-card">
            <span class="profile-name">{p_name}</span>
            <div class="profile-actions">
                <button class="small success" onclick="location.href='/load_profile?name={p_name}'">Load</button>
                <button class="small danger" onclick="if(confirm('Delete profile {p_name}?')) location.href='/delete_profile?name={p_name}'">Del</button>
            </div>
        </div>
        """

    if len(profiles) < 5:
        profile_cards += """
        <div class="profile-card" style="border-style: dashed; display:flex; align-items:center; justify-content:center; flex-direction:column;">
            <span class="profile-name" style="color:#aaa;">Empty Slot</span>
            <form method="post" style="display:flex; gap:5px; flex-direction:column; width:100%;">
                <input type="text" name="new_profile_name" placeholder="Profile Name" required style="padding:5px; border-radius:4px; border:1px solid #ccc;" maxlength="15">
                <button type="submit" name="action" value="save_profile" class="small">Save Current</button>
            </form>
        </div>
        """

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CAESAR Configuration</title>
    {COMMON_STYLE}
</head>
<body>
    <div class="container">
        <h1>CAESAR Control Panel (Server)</h1>
        <div class="nav">
            <a href="/"><button class="nav-button {active_volume}">Volume Control</button></a>
            <a href="/config"><button class="nav-button {active_config}">Configuration</button></a>
            <a href="/status"><button class="nav-button {active_status}">Status</button></a>
        </div>
        {message_html}

        <div class="control-group">
            <h2>Saved Profiles ({len(profiles)}/5)</h2>
            <div class="profile-grid">
                {profile_cards}
            </div>
        </div>

        <form method="post">
            <div class="control-group">
                <h2>Server IP Configuration (Local)</h2>
                <textarea name="server_config">{server_config}</textarea>
                <button type="submit" name="action" value="save_server">Save Server Config</button>
            </div>

            <div class="control-group">
                <h2>Client IP Configuration (Remote)</h2>
                <textarea name="client_config">{client_config}</textarea>
                <button type="submit" name="action" value="save_client">Save Client Config</button>
            </div>

            <div class="control-group">
                <h2>Audio Stream Settings</h2>

                <label for="audio_rate">Sample Rate:</label>
                <select name="audio_rate" id="audio_rate">
                    {rate_options}
                </select>

                <label for="audio_buffer">Buffer Time (alsasrc):</label>
                <select name="audio_buffer" id="audio_buffer">
                    {buffer_options}
                </select>

                <button type="submit" name="action" value="save_audio">Save Audio Settings</button>
            </div>

            <div class="control-group">
                <button type="submit" name="action" value="restart_services" class="service-button">Restart Services</button>
            </div>
        </form>
    </div>
</body>
</html>
"""


def get_status_template(active_page="status"):
    active_volume = "active" if active_page == "volume" else ""
    active_config = "active" if active_page == "config" else ""
    active_status = "active" if active_page == "status" else ""

    local_ip = get_local_ip()
    target_ip = get_ip_from_file(CLIENT_IP_FILE) or "Not configured"

    init_rtt = current_rtt if current_rtt else "--"
    init_status = (
        "good"
        if current_rtt and current_rtt < 50
        else "warning"
        if current_rtt and current_rtt < 100
        else "bad"
        if current_rtt
        else "unknown"
    )

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CAESAR Status</title>
    {COMMON_STYLE}
    <script>
        function updateStatus() {{
            fetch('/get_status')
                .then(response => response.json())
                .then(data => {{
                    const statusElement = document.getElementById('connection-status');
                    const valueElement = document.getElementById('rtt-value');
                    const timeElement = document.getElementById('timestamp');

                    if (data.rtt !== null) {{
                        valueElement.textContent = data.rtt.toFixed(1) + ' ms';
                        statusElement.className = 'status-display ' + data.status;
                    }} else {{
                        valueElement.textContent = '--';
                        statusElement.className = 'status-display bad';
                    }}
                    timeElement.textContent = 'Last updated: ' + data.timestamp;
                    setTimeout(updateStatus, 1000);
                }})
                .catch(error => {{
                    console.error('Status update error:', error);
                    setTimeout(updateStatus, 2000);
                }});
        }}
        document.addEventListener('DOMContentLoaded', updateStatus);
    </script>
</head>
<body>
    <div class="container">
        <h1>CAESAR Control Panel (Server)</h1>
        <div class="nav">
            <a href="/"><button class="nav-button {active_volume}">Volume</button></a>
            <a href="/config"><button class="nav-button {active_config}">Configuration</button></a>
            <a href="/status"><button class="nav-button {active_status}">Status</button></a>
        </div>
        <div class="control-group">
            <h2>Network Information</h2>
            <div class="value-display"><strong>Local IP:</strong> {local_ip}</div>
            <h2>Connection to Client ({target_ip})</h2>
            <div id="connection-status" class="status-display {init_status}">
                <span id="rtt-value">{init_rtt if isinstance(init_rtt, float) else "--"}</span>
            </div>
            <div id="timestamp" class="timestamp">Last updated: {last_update}</div>
        </div>
    </div>
</body>
</html>
"""


def percent_to_alsa(vol_percent):
    return min(35, max(0, round(float(vol_percent) * 35 / 100)))


def alsa_to_percent(alsa_value):
    return min(100, max(0, round(float(alsa_value) * 100 / 35)))


def get_mic_value():
    try:
        result = subprocess.run(
            ["amixer", "-c0", "cget", MIC], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "values=" in line:
                val = line.split("values=")[1].split(",")[0].strip()
                return int(val) if val.isdigit() else 8
    except Exception:
        pass
    return 8


def get_speaker_volume():
    try:
        result = subprocess.run(
            ["amixer", "-c0", "get", SPEAKER], capture_output=True, text=True, timeout=5
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
    # Defaults: 48kHz, 100000us (100ms)
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


@app.route("/", methods=["GET", "POST"])
def index():
    speaker_vol = get_speaker_volume()
    mic_alsa = get_mic_value()
    mic_capture_display = alsa_to_percent(mic_alsa)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "set_speaker":
            speaker_vol = request.form.get("speaker_volume", speaker_vol)
            subprocess.run(
                ["amixer", "-c0", "set", SPEAKER, f"{speaker_vol}%"], timeout=5
            )
            subprocess.run(
                ["/usr/sbin/alsactl", "-f", "/var/lib/alsa/asound.state", "store"],
                timeout=5,
            )
            speaker_vol = get_speaker_volume()
        elif action == "set_mic":
            mic_capture_display = request.form.get("mic_capture", mic_capture_display)
            alsa_value = percent_to_alsa(mic_capture_display)
            subprocess.run(["amixer", "-c0", "cset", MIC, str(alsa_value)], timeout=5)
            subprocess.run(
                ["/usr/sbin/alsactl", "-f", "/var/lib/alsa/asound.state", "store"],
                timeout=5,
            )
            mic_capture_display = alsa_to_percent(alsa_value)

    return get_volume_template(speaker_vol, mic_capture_display, "volume")


@app.route("/config", methods=["GET", "POST"])
def config_editor():
    server_config = read_config_file(SERVER_IP_FILE)
    client_config = read_config_file(CLIENT_IP_FILE)
    audio_rate, audio_buffer = read_audio_config()
    profiles = get_profiles_list()

    message = None
    msg_type = "info"

    if request.method == "POST":
        action = request.form.get("action")
        if action == "save_server":
            new_ip = request.form.get("server_config", "").strip()
            if not is_valid_ip(new_ip):
                message = f"Invalid IP address: '{new_ip}'"
                msg_type = "error"
            else:
                server_config = new_ip
                write_config_file(SERVER_IP_FILE, server_config)
                message = "Server configuration saved successfully!"
                msg_type = "success"
        elif action == "save_client":
            new_ip = request.form.get("client_config", "").strip()
            if not is_valid_ip(new_ip):
                message = f"Invalid IP address: '{new_ip}'"
                msg_type = "error"
            else:
                client_config = new_ip
                write_config_file(CLIENT_IP_FILE, client_config)
                message = "Client configuration saved successfully!"
                msg_type = "success"
        elif action == "save_audio":
            new_rate = request.form.get("audio_rate", 48000)
            new_buffer = request.form.get("audio_buffer", 100000)
            write_audio_config(new_rate, new_buffer)
            ms_val = int(new_buffer) // 1000
            message = f"Audio settings saved! Rate: {new_rate}, Buffer: {ms_val}ms ({new_buffer}µs)."
            msg_type = "success"
        elif action == "save_profile":
            name = request.form.get("new_profile_name", "").strip()
            if not name:
                message = "Profile name cannot be empty"
                msg_type = "error"
            elif len(profiles) >= 5:
                message = "Maximum 5 profiles allowed. Delete one first."
                msg_type = "error"
            else:
                success, msg = save_profile(name)
                message = msg
                msg_type = "success" if success else "error"
                if success:
                    profiles = get_profiles_list()
        elif action == "restart_services":
            script_path = "/home/pi/nano-server/restart_services_on_server.sh"
            try:
                subprocess.run(["sudo", script_path], timeout=30, check=True)
                message = "Services restarted successfully!"
                msg_type = "success"
            except subprocess.SubprocessError as e:
                message = f"Failed to restart services: {e}"
                msg_type = "error"

    return get_config_template(
        server_config,
        client_config,
        audio_rate,
        audio_buffer,
        profiles,
        "config",
        message,
        msg_type,
    )


@app.route("/load_profile")
def load_profile_route():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "No name provided"}), 400

    success, msg = load_profile(name)
    alert_msg = msg.replace("'", "\\'")
    html = f"""
    <script>
        alert('{alert_msg}');
        window.location.href = '/config';
    </script>
    Loading profile...
    """
    return html


@app.route("/delete_profile")
def delete_profile_route():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "No name provided"}), 400

    success, msg = delete_profile(name)
    alert_msg = msg.replace("'", "\\'")
    html = f"""
    <script>
        alert('{alert_msg}');
        window.location.href = '/config';
    </script>
    Deleting profile...
    """
    return html


@app.route("/status")
def status_page():
    return get_status_template("status")


@app.route("/get_status")
def get_status():
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


# Start background monitoring when the module is loaded (works with waitress and gunicorn)
start_status_monitoring()
