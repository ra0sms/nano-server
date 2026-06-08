import json
import os
from pathlib import Path

import requests
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

app = Flask(__name__)
app.secret_key = "nano_secret_123"

# Password is read from password.txt next to app.py.
# To change: echo 'newpassword' > /home/pi/nano-server/web/password.txt
_PASSWORD_FILE = Path(__file__).with_name("password.txt")
PASSWORD = _PASSWORD_FILE.read_text().strip() if _PASSWORD_FILE.exists() else "1234"

try:
    bus = SMBus(0)
except Exception as _e:
    print(f"Warning: could not open I2C bus 0: {_e}")
    bus = None

# PCF8574 at 0x20 (A2=A1=A0=0) and 0x21 (A0=1)
ADDR1 = 0x20
ADDR2 = 0x21

state1 = 0xFF  # all relays OFF (active-low: HIGH = OFF)
state2 = 0xFF


# ================= CONFIG =================

CONFIG_FILE = Path(__file__).with_name("config.json")

default_config = {
    "names": [f"Relay {i + 1}" for i in range(16)],
    "group_mode": ["toggle", "toggle"],
}

config = {}


def load_config():
    global config

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)

            if "names" not in config:
                config = default_config.copy()

            if "group_mode" not in config:
                config["group_mode"] = ["toggle", "toggle"]

        except Exception:
            config = default_config.copy()
    else:
        config = default_config.copy()
        save_config()


def save_config():
    tmp = CONFIG_FILE.with_suffix(".tmp")

    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)

    os.replace(tmp, CONFIG_FILE)


# ================= RELAYS =================


def apply():
    """Write current relay state to both PCF8574T chips."""
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

    # SWITCH MODE (radio behavior inside group)
    if mode == "switch":
        global state1, state2

        if group == 0:
            state1 = 0xFF & ~(1 << bit)
        else:
            state2 = 0xFF & ~(1 << bit)

        return

    # TOGGLE MODE
    set_relay(n, not bits[n])


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


# ================= UI =================

HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<style>
body {
    margin:0;
    background:#0f1115;
    color:#e6e6e6;
    font-family:Arial;
    text-align:center;
}

h2 { margin:10px; }

.tabs {
    display:flex;
    justify-content:center;
    gap:10px;
    margin:10px;
}

.tab {
    background:#222;
    padding:10px 15px;
    border-radius:6px;
    cursor:pointer;
}

.panel {
    display:grid;
    grid-template-columns:repeat(auto-fit, minmax(120px,1fr));
    gap:8px;
}

button.relay {
    width:100%;
    height:60px;
    margin:0;
    border:none;
    border-radius:8px;
    font-size:14px;
}


.group {
    background:#1a1d24;
    border:1px solid #333;
    border-radius:12px;
    padding:15px;
    margin:15px auto;
    max-width:600px;
}

.camera-btn {
    background:#2d6cdf;
    color:white;
    padding:12px 20px;
    border:none;
    border-radius:8px;
    cursor:pointer;
    font-size:16px;
}

.settings {
    display:none;
    max-width:900px;
    margin:auto;
    padding:15px;
}

.settings-grid {
    display:grid;
    grid-template-columns: 1fr 1fr;
    gap:20px;
    margin-top:20px;
}

.settings-column {
    background:#1a1d24;
    border:1px solid #333;
    border-radius:12px;
    padding:15px;
}

.settings-column h4 {
    margin-top:0;
}

.name-row {
    display:flex;
    align-items:center;
    margin-bottom:8px;
}

.name-row span {
    width:40px;
    text-align:right;
    margin-right:8px;
    color:#aaa;
}

.name-row input {
    flex:1;
    background:#2a2d34;
    color:white;
    border:1px solid #444;
    border-radius:6px;
    padding:6px;
}

.save-btn {
    margin-top:20px;
    padding:12px 25px;
    border:none;
    border-radius:8px;
    background:#2d6cdf;
    color:white;
    font-size:16px;
    cursor:pointer;
}

#toast {
    position: fixed;
    bottom: 20px;
    left: 50%;
    transform: translateX(-50%);
    background: #1faa59;
    color: white;
    padding: 12px 20px;
    border-radius: 8px;
    font-size: 14px;
    opacity: 0;
    pointer-events: none;
    transition: 0.3s;
    z-index: 9999;
}

#toast.show {
    opacity: 1;
}

.on { background:#1faa59; color:white; }
.off { background:#d64545; color:white; }

button:hover { transform:scale(1.05); }

img {
    max-width:95%;
    border-radius:10px;
    border:2px solid #333;
}

.settings { display:none; max-width:600px; margin:auto; }

input, select {
    padding:6px;
    margin:3px;
}
</style>
</head>

<body>

<h2>NanoPi Relay Panel</h2>

<div id="toast"></div>

<div class="tabs">
    <div class="tab" onclick="show('main')">Main</div>
    <div class="tab" onclick="show('settings')">Settings</div>
    <div class="tab" onclick="location.href='/logout'">Logout</div>
</div>

	<div id="main">

    <div class="group">
        <h3 id="group1_title">Group 1</h3>
        <div class="panel" id="group1"></div>
    </div>

    <div class="group">
        <h3 id="group2_title">Group 2</h3>
        <div class="panel" id="group2"></div>
    </div>

    <h3>Camera</h3>

    <button
        id="cameraButton"
        class="camera-btn"
        onclick="toggleCamera()">
        Show Camera
    </button>

    <button
        class="camera-btn"
        onclick="openCameraWindow()">
        Fullscreen
    </button>

    <br><br>

    <div id="cameraContainer"></div>

</div>

<!-- ЗАКРЫЛИ MAIN -->

<div class="settings" id="settings">

<h3>Relay Configuration</h3>

<div class="settings-grid">

    <div class="settings-column">
        <h4>Group 1</h4>

        Mode:
        <select id="mode0">
            <option value="toggle">toggle</option>
            <option value="switch">switch</option>
        </select>

        <br><br>

        <div id="names1"></div>
    </div>

    <div class="settings-column">
        <h4>Group 2</h4>

        Mode:
        <select id="mode1">
            <option value="toggle">toggle</option>
            <option value="switch">switch</option>
        </select>

        <br><br>

        <div id="names2"></div>
    </div>

</div>

<button class="save-btn" onclick="save()">Save Configuration</button>
</div>

<script>

let state = [];
let names = [];
let mode = [];

const buttons = [];

function showToast(msg, ok=true){
    const t = document.getElementById("toast");

    t.innerText = msg;

    t.style.background = ok ? "#1faa59" : "#d64545";

    t.classList.add("show");

    setTimeout(() => {
        t.classList.remove("show");
    }, 2000);
}


function show(t){
    document.getElementById("main").style.display = (t==="main")?"block":"none";
    document.getElementById("settings").style.display = (t==="settings")?"block":"none";
}

function initButtons(){

    const g1 = document.getElementById("group1");
    const g2 = document.getElementById("group2");

    g1.innerHTML = "";
    g2.innerHTML = "";

    buttons.length = 0;

    for(let i=0;i<16;i++){

        let b = document.createElement("button");

        b.className = "relay off";
        b.innerText = "Relay " + (i + 1);

        b.onclick = () => toggle(i);

        buttons.push(b);

        if(i < 8)
            g1.appendChild(b);
        else
            g2.appendChild(b);
    }
}
function renderState(){
    for(let i=0;i<16;i++){
        if(state[i] === 1){
            buttons[i].classList.remove("off");
            buttons[i].classList.add("on");
        } else {
            buttons[i].classList.remove("on");
            buttons[i].classList.add("off");
        }

        buttons[i].innerText = names[i] || ("Relay " + (i+1));
    }
}

function load(){
    fetch("/state")
    .then(r => r.json())
    .then(d => {

        state = d.state;
        names = d.names;
        mode = d.mode;
        document.getElementById("group1_title").innerText =
            "Group 1 (" + mode[0] + ")";

        document.getElementById("group2_title").innerText =
            "Group 2 (" + mode[1] + ")";

        renderState();

        document.getElementById("mode0").value = mode[0];
        document.getElementById("mode1").value = mode[1];

let n1 = document.getElementById("names1");
let n2 = document.getElementById("names2");

n1.innerHTML = "";
n2.innerHTML = "";

for(let i=0;i<16;i++){

    let row = document.createElement("div");
    row.className = "name-row";

    let num = document.createElement("span");
    num.innerText = (i + 1);

    let inp = document.createElement("input");
    inp.value = names[i];

    inp.oninput = () => names[i] = inp.value;

    row.appendChild(num);
    row.appendChild(inp);

    if(i < 8)
        n1.appendChild(row);
    else
        n2.appendChild(row);
}
    });
}

function toggle(i){
    fetch("/toggle/"+i)
    .then(r => r.json())
    .then(d => {
        state = d.state;
        names = d.names;
        mode = d.mode;

        renderState();
    });
}

function save(){
    fetch("/settings", {
        method:"POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
            names: names,
            mode: [
                document.getElementById("mode0").value,
                document.getElementById("mode1").value
            ]
        })
    })
    .then(r => {
        if(r.ok){
            showToast("Settings saved");
            load();
        } else {
            showToast("Save failed", false);
        }
    })
    .catch(() => showToast("Network error", false));
}

function openCameraWindow(){
    window.open("/camera", "_blank");
}

let cameraVisible = false;

function toggleCamera(){

    const div = document.getElementById("cameraContainer");
    const btn = document.getElementById("cameraButton");

    if(cameraVisible){

        div.innerHTML = "";
        btn.innerText = "Show Camera";

    } else {

        div.innerHTML =
            '<img src="/stream" style="max-width:95%;border-radius:10px;border:2px solid #333;">';

        btn.innerText = "Hide Camera";
    }

    cameraVisible = !cameraVisible;
}
window.addEventListener("beforeunload", () => {
    const div = document.getElementById("cameraContainer");
    if(div) div.innerHTML = "";
});

initButtons();
load();
show("main");

</script>
</body>
</html>
"""


# ================= ROUTES =================


@app.route("/camera")
def camera():

    if not auth():
        return redirect("/login")

    ip = os.popen("hostname -I | awk '{print $1}'").read().strip()

    return render_template_string(
        """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<title>Camera</title>

<style>
body {
    margin: 0;
    background: black;

    /* центрируем всё */
    display: flex;
    justify-content: center;
    align-items: center;

    height: 100vh;
    overflow: hidden;
}

/* верхняя панель */
.topbar {
    position: center;
    top: 10px;
    left: 10px;
    z-index: 10;
}

/* кнопка */
button {
    padding: 10px 15px;
    font-size: 14px;
    border: none;
    border-radius: 8px;
    cursor: pointer;
}

/* камера */
img {
    max-width: 95vw;
    max-height: 95vh;
    width: auto;
    height: auto;
    object-fit: contain;
    border-radius: 10px;
    border: 2px solid #333;
}
</style>

</head>
<body>

<img src="/stream">

</body>
</html>
""",
        ip=ip,
    )


@app.route("/")
def index():
    if not auth():
        return redirect("/login")

    ip = os.popen("hostname -I | awk '{print $1}'").read().strip()
    return render_template_string(HTML, ip=ip)


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

    save_config()

    return "ok"


# ================= START =================

if __name__ == "__main__":
    load_config()
    apply()  # write initial state (all relays OFF)
    app.run(host="0.0.0.0", port=5050)
