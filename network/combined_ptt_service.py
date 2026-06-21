#!/usr/bin/python3
"""
Combined service: PTT + Client Monitor + Ping Responder + CW Keyer (Winkeyer)
=============================================================================
All GPIO lines are requested in a single gpiod.request_lines() call to avoid
conflicts between separate processes on the same gpiochip.

GPIO lines:
  - PC1 (line 65) — CW Keyer output (Winkeyer)
  - PC2 (line 66) — CON LED (client reachability)
  - PC3 (line 67) — PTT output

UDP ports:
  - 5001 — PTT commands ('0' / '1')
  - 5002 — Ping / RTT monitoring
  - 5003 — Winkeyer CW Keyer protocol

XML-RPC (compatible with PyWinKeyerSerial by K6GTE):
  - 8000 — k1elsendstring, setspeed, sendblended, tuneon/tuneoff, clearbuffer
"""

import os
import signal
import socket
import sys
import threading
import time
from xmlrpc.server import SimpleXMLRPCServer
from xmlrpc.server import SimpleXMLRPCRequestHandler

import gpiod
from gpiod.line import Direction, Value

# ================= CONFIGURATION =================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_IP_FILE = os.path.join(SCRIPT_DIR, "..", "client_ip.cfg")

# === GPIO ===
CHIP_PATH = "/dev/gpiochip0"
LINE_CW = 65    # PC1 — CW Keyer output
LINE_CON = 66   # PC2 — CON LED
LINE_PTT = 67   # PC3 — PTT output
CONSUMER = "nano_server_combined"

# === UDP ===
SERVER_IP = "0.0.0.0"
PTT_PORT = 5001       # PTT commands
PING_PORT = 5002      # Ping / RTT
WK_PORT = 5003        # Winkeyer CW Keyer
XMLRPC_PORT = 6789    # XML-RPC (PyWinKeyerSerial compatible, default in not1mm)
TIMEOUT = 1.0
CHECK_INTERVAL = 0.3
MAGIC_PHRASE = b"PING_RESPONSE"

# === CW defaults ===
WPM_DEFAULT = 20
WPM_MIN = 5
WPM_MAX = 99
MAX_BUFFER = 255

# === State ===
ptt_state = 0
client_ip = "0.0.0.0"
need_ser2net_reboot = False
shutdown_flag = threading.Event()

# CW state
wpm = WPM_DEFAULT
cw_buffer = bytearray()
cw_sending = False
cw_send_lock = threading.Lock()

# === GPIO Setup (gpiod v2 API) — single request for all lines ===
try:
    gpio_request = gpiod.request_lines(
        CHIP_PATH,
        consumer=CONSUMER,
        config={
            LINE_CW: gpiod.LineSettings(
                direction=Direction.OUTPUT,
                output_value=Value.INACTIVE,
            ),
            LINE_CON: gpiod.LineSettings(
                direction=Direction.OUTPUT,
                output_value=Value.INACTIVE,
            ),
            LINE_PTT: gpiod.LineSettings(
                direction=Direction.OUTPUT,
                output_value=Value.INACTIVE,
            ),
        },
    )
    print("✅ GPIO lines initialized (CW=PC1, CON=PC2, PTT=PC3).")
except Exception as e:
    print(f"❌ GPIO initialization failed: {e}")
    sys.exit(1)


# ================= PTT FUNCTIONS =================


def read_allowed_ip(filename):
    global client_ip
    try:
        with open(filename, "r") as f:
            ip = f.read().strip()
            if not ip:
                print("⚠️ client_ip.cfg is empty — using 0.0.0.0")
                ip = "0.0.0.0"
            client_ip = ip
            print(f"✅ Allowed client IP: {client_ip}")
            return ip
    except Exception as e:
        print(f"❌ Error reading {filename}: {e}")
        client_ip = "0.0.0.0"
        return client_ip


def set_ptt(value: int):
    """Thread-safe PTT update with hardware write"""
    global ptt_state
    if value not in (0, 1):
        return
    if value != ptt_state:
        try:
            gpio_request.set_value(LINE_PTT, Value.ACTIVE if value else Value.INACTIVE)
            ptt_state = value
            print(f"📡 PTT {'ON' if value else 'OFF'} (GPIO={value})")
        except Exception as e:
            print(f"⚠️ GPIO error: {e}")


def ptt_server():
    """UDP server listening for '0'/'1' commands on port 5001"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((SERVER_IP, PTT_PORT))
    print(f"📡 PTT UDP server listening on {SERVER_IP}:{PTT_PORT}...")

    try:
        while not shutdown_flag.is_set():
            try:
                sock.settimeout(0.5)
                data, addr = sock.recvfrom(1024)
                sender_ip, _ = addr
                cmd = data.decode().strip()

                if sender_ip == client_ip:
                    if cmd == "1":
                        set_ptt(1)
                    elif cmd == "0":
                        set_ptt(0)
                    else:
                        print(f"❓ Unknown command from {sender_ip}: {repr(cmd)}")
                else:
                    print(f"🔒 Ignored command from unauthorized IP: {sender_ip}")
            except socket.timeout:
                continue
            except Exception as e:
                if not shutdown_flag.is_set():
                    print(f"⚠️ PTT UDP server error: {e}")
    finally:
        sock.close()


# ================= PING / CLIENT MONITOR =================


def send_ping(ip):
    """Send ping request and wait for magic reply"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(TIMEOUT)
        sock.sendto(b"PING_REQUEST", (ip, PING_PORT))
        data, addr = sock.recvfrom(1024)
        ok = data == MAGIC_PHRASE and addr[0] == ip
        sock.close()
        return ok
    except socket.timeout:
        return False
    except Exception as e:
        print(f"⚠️ Ping error to {ip}: {e}")
        return False


def client_monitor():
    """Monitor client reachability and manage CON/PTT state"""
    global need_ser2net_reboot
    prev_online = False

    while not shutdown_flag.is_set():
        online = send_ping(client_ip) if client_ip != "0.0.0.0" else True

        if online:
            gpio_request.set_value(LINE_CON, Value.ACTIVE)
            if need_ser2net_reboot:
                print("🔄 Client back online — restarting ser2net...")
                os.system("systemctl restart ser2net.service")
                need_ser2net_reboot = False
        else:
            gpio_request.set_value(LINE_CON, Value.INACTIVE)
            set_ptt(0)  # 🔒 FAIL-SAFE: disable PTT when client is gone
            need_ser2net_reboot = True

        if online != prev_online:
            status = "✅ online" if online else "❌ offline"
            print(f"🌐 Client {client_ip} is {status}")
            prev_online = online

        time.sleep(CHECK_INTERVAL)


def ping_responder():
    """Reply PING_RESPONSE to any PING_REQUEST — lets the client measure RTT."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PING_PORT))
    print(f"🏓 Ping responder listening on port {PING_PORT}...")
    while not shutdown_flag.is_set():
        try:
            sock.settimeout(0.5)
            data, addr = sock.recvfrom(1024)
            if data == b"PING_REQUEST":
                sock.sendto(b"PING_RESPONSE", addr)
        except socket.timeout:
            continue
        except Exception as e:
            if not shutdown_flag.is_set():
                print(f"⚠️ Ping responder error: {e}")
    sock.close()


# ================= CW KEYER (Winkeyer Protocol) =================


def cw_set(value: int):
    """Set CW key line (1 = key down / mark, 0 = key up / space)."""
    try:
        gpio_request.set_value(LINE_CW, Value.ACTIVE if value else Value.INACTIVE)
    except Exception as e:
        print(f"[CW] ⚠️ GPIO error: {e}")


def dit_ms():
    """Duration of a dit in milliseconds."""
    return max(10, int(1200 / wpm))


def dah_ms():
    """Duration of a dah in milliseconds."""
    return max(30, int(3 * 1200 / wpm))


# Morse code table: ASCII char -> (bits, length)
# bits: 1 = mark (key down), 0 = space (key up), MSB first
# Intra-character gap (between dit/dah) = 1 bit (0)
# Inter-character gap is handled in send_char()
MORSE = {
    "A": (0b10111, 5),        # .-
    "B": (0b111010101, 9),    # -...
    "C": (0b11101011101, 11), # -.-.
    "D": (0b1110101, 7),      # -..
    "E": (0b1, 1),            # .
    "F": (0b101011101, 9),    # ..-.
    "G": (0b111011101, 9),    # --.
    "H": (0b1010101, 7),      # ....
    "I": (0b101, 3),          # ..
    "J": (0b1011101110111, 13), # .---
    "K": (0b111010111, 9),    # -.-
    "L": (0b101110101, 9),    # .-..
    "M": (0b1110111, 7),      # --
    "N": (0b11101, 5),        # -.
    "O": (0b11101110111, 11), # ---
    "P": (0b10111011101, 11), # .--.
    "Q": (0b1110111010111, 13), # --.-
    "R": (0b1011101, 7),      # .-.
    "S": (0b10101, 5),        # ...
    "T": (0b111, 3),          # -
    "U": (0b1010111, 7),      # ..-
    "V": (0b101010111, 9),    # ...-
    "W": (0b101110111, 9),    # .--
    "X": (0b11101010111, 11), # -..-
    "Y": (0b1110101110111, 13), # -.--
    "Z": (0b11101110101, 11), # --..
    "0": (0b1110111011101110111, 19), # -----
    "1": (0b10111011101110111, 17),   # .----
    "2": (0b101011101110111, 15),     # ..---
    "3": (0b1010101110111, 13),       # ...--
    "4": (0b10101010111, 11),         # ....-
    "5": (0b101010101, 9),            # .....
    "6": (0b11101010101, 11),         # -....
    "7": (0b1110111010101, 13),       # --...
    "8": (0b111011101110101, 15),     # ---..
    "9": (0b11101110111011101, 17),   # ----.
    ".": (0b10111010111010111, 17),   # .-.-.-
    ",": (0b1110111010101110111, 19), # --..--
    "?": (0b101011101110101, 15),     # ..--..
    "/": (0b1110101011101, 13),       # -..-.
    "@": (0b10111011101011101, 17),   # .--.-.
    "!": (0b1110101110101110111, 19), # -.-.--
    "-": (0b111010101010111, 15),     # -....-
    ";": (0b11101011101011101, 17),   # -.-.-.
    ":": (0b11101110111010101, 17),   # ---...
    "'": (0b1011101110111011101, 19), # .----.
    '"': (0b101110101011101, 15),     # .-..-.
    "(": (0b111010111011101, 15),     # -.--.
    ")": (0b1110101110111010111, 19), # -.--.-
    "=": (0b1110101010111, 13),       # -...-
    "+": (0b1011101011101, 13),       # .-.-.
    "_": (0b10101110111010111, 17),   # ..--.-
    "$": (0b10101011101010111, 17),   # ...-..-
    "&": (0b10111010101, 11),         # .-...
    " ": (0b0, 0),  # word space (handled separately)
}


def char_to_cw_bits(char: str):
    """Convert a character to (bits, length) or None if not mappable."""
    upper = char.upper()
    if upper in MORSE:
        return MORSE[upper]
    return None


def send_char(char: str):
    """Send a single character as CW, blocking."""
    if char == " ":
        time.sleep(dit_ms() * 7 / 1000)
        return False

    morse = char_to_cw_bits(char)
    if morse is None:
        return False

    bits, length = morse
    dit = dit_ms()
    dah = dah_ms()

    for i in range(length):
        if shutdown_flag.is_set():
            return False

        bit = (bits >> (length - 1 - i)) & 1

        if bit:
            cw_set(1)
            time.sleep(dit / 1000)
        else:
            cw_set(0)
            time.sleep(dit / 1000)

    # Inter-character gap: key up for 2 dit (1 dit already from last space)
    cw_set(0)
    time.sleep(dit * 2 / 1000)

    return True


def cw_send_buffer_thread():
    """Background thread that sends the CW buffer."""
    global cw_sending, cw_buffer

    with cw_send_lock:
        cw_sending = True
        buf = bytes(cw_buffer)
        cw_buffer.clear()

    print(f"[CW] ▶️ Sending: {buf.decode('ascii', errors='replace')}")

    try:
        for char in buf.decode("ascii", errors="replace"):
            if shutdown_flag.is_set():
                break
            if cw_sending is False:  # stopped by 0x1B
                break
            send_char(char)
    finally:
        cw_set(0)
        with cw_send_lock:
            cw_sending = False
        print("[CW] ✅ Done sending")


def cw_start_sending():
    """Start sending the CW buffer in a background thread."""
    global cw_send_thread
    with cw_send_lock:
        if cw_sending:
            print("[CW] ⏳ Already sending, ignoring")
            return
        if not cw_buffer:
            print("[CW] 📭 Buffer empty, nothing to send")
            return

    cw_send_thread = threading.Thread(target=cw_send_buffer_thread, daemon=True)
    cw_send_thread.start()


def cw_stop_sending():
    """Immediately stop sending."""
    global cw_sending
    with cw_send_lock:
        cw_sending = False
    cw_set(0)
    print("[CW] ⏹️ Stopped")


def cw_clear_buffer():
    """Clear the buffer and stop sending."""
    global cw_buffer
    cw_stop_sending()
    with cw_send_lock:
        cw_buffer.clear()
    print("[CW] 🗑️ Buffer cleared")


def handle_winkeyer_command(data: bytes, sock: socket.socket, addr: tuple):
    """Process a Winkeyer protocol datagram."""
    global wpm, cw_buffer

    i = 0
    while i < len(data):
        byte = data[i]

        if byte == 0x00:
            # Set speed: next byte is WPM
            if i + 1 < len(data):
                new_wpm = data[i + 1]
                if WPM_MIN <= new_wpm <= WPM_MAX:
                    wpm = new_wpm
                    print(f"[CW] ⏩ Speed set to {wpm} WPM")
                i += 2
            else:
                i += 1

        elif byte == 0x01:
            # Key immediate dit (not implemented in keyer-only mode)
            i += 1

        elif byte == 0x02:
            # Start sending buffer
            cw_start_sending()
            i += 1

        elif byte == 0x03:
            # Clear buffer / reset
            cw_clear_buffer()
            i += 1

        elif byte in (0x04, 0x05, 0x06, 0x07, 0x09, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,
                      0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x1E):
            # Configuration commands with 1 data byte — ignore
            i += 2

        elif byte == 0x0A:
            # LF — start sending buffer
            cw_start_sending()
            i += 1

        elif byte == 0x1B:
            # Immediate stop
            cw_stop_sending()
            i += 1

        elif byte == 0x1C:
            # PTT on — log only, PTT is handled by the PTT server
            print("[CW] 📡 PTT ON requested via Winkeyer (use PTT port 5001)")
            i += 1

        elif byte == 0x1D:
            # PTT off
            print("[CW] 📡 PTT OFF requested via Winkeyer (use PTT port 5001)")
            i += 1

        elif byte == 0x1F:
            # Request status
            with cw_send_lock:
                status = 0x01 if cw_sending else 0x00
            try:
                sock.sendto(bytes([status]), addr)
            except Exception:
                pass
            i += 1

        elif byte == 0x7F:
            # Backspace: remove last character from buffer
            with cw_send_lock:
                if cw_buffer:
                    cw_buffer.pop()
            i += 1

        elif 0x20 <= byte <= 0x7E:
            # ASCII character — add to buffer
            with cw_send_lock:
                if len(cw_buffer) < MAX_BUFFER:
                    cw_buffer.append(byte)
            i += 1

        else:
            # Unknown byte — skip
            i += 1


def winkeyer_server():
    """UDP server listening for Winkeyer protocol commands on port 5003."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((SERVER_IP, WK_PORT))
    print(f"[CW] 📡 Winkeyer UDP server listening on {SERVER_IP}:{WK_PORT}...")
    print(f"[CW] ⏩ Default speed: {wpm} WPM")
    print(f"[CW] 🔌 GPIO: PC1 (line {LINE_CW})")

    try:
        while not shutdown_flag.is_set():
            try:
                sock.settimeout(0.5)
                data, addr = sock.recvfrom(1024)
                if data:
                    handle_winkeyer_command(data, sock, addr)
            except socket.timeout:
                continue
            except Exception as e:
                if not shutdown_flag.is_set():
                    print(f"[CW] ⚠️ UDP error: {e}")
    finally:
        sock.close()


# ================= XML-RPC (PyWinKeyerSerial compatible) =================
# Programs like N1MM, DXLog, etc. connect via XML-RPC to send CW.
# Compatible with the K6GTE PyWinKeyerSerial XML-RPC interface on port 8000.


def rpc_k1elsendstring(text: str) -> bool:
    """Send a string to the CW buffer and start sending (compatible with PyWinKeyerSerial)."""
    global cw_buffer
    with cw_send_lock:
        for char in text.upper():
            if len(cw_buffer) < MAX_BUFFER:
                cw_buffer.append(ord(char))
    cw_start_sending()
    print(f"[XMLRPC] k1elsendstring: '{text}'")
    return True


def rpc_setspeed(speed: int) -> bool:
    """Set CW speed in WPM."""
    global wpm
    if WPM_MIN <= speed <= WPM_MAX:
        wpm = speed
        print(f"[XMLRPC] setspeed: {wpm} WPM")
        return True
    return False


def rpc_sendblended(msg: str) -> bool:
    """Send a blended (prosign) message — same as k1elsendstring."""
    return rpc_k1elsendstring(msg)


def rpc_tuneon() -> bool:
    """Key down and hold (tune mode)."""
    cw_set(1)
    print("[XMLRPC] tuneon")
    return True


def rpc_tuneoff() -> bool:
    """Stop key down (tune mode off)."""
    cw_set(0)
    print("[XMLRPC] tuneoff")
    return True


def rpc_clearbuffer() -> bool:
    """Clear the CW buffer and stop sending."""
    cw_clear_buffer()
    print("[XMLRPC] clearbuffer")
    return True


def xmlrpc_server():
    """XML-RPC server compatible with PyWinKeyerSerial on port 8000."""
    class RequestHandler(SimpleXMLRPCRequestHandler):
        rpc_paths = ("/RPC2",)

    try:
        with SimpleXMLRPCServer(
            ("0.0.0.0", XMLRPC_PORT),
            requestHandler=RequestHandler,
            allow_none=True,
            logRequests=False,
        ) as server:
            server.register_function(rpc_k1elsendstring, "k1elsendstring")
            server.register_function(rpc_setspeed, "setspeed")
            server.register_function(rpc_sendblended, "sendblended")
            server.register_function(rpc_tuneon, "tuneon")
            server.register_function(rpc_tuneoff, "tuneoff")
            server.register_function(rpc_clearbuffer, "clearbuffer")
            server.register_introspection_functions()
            print(f"[XMLRPC] 📡 Server listening on 0.0.0.0:{XMLRPC_PORT}...")
            while not shutdown_flag.is_set():
                server.timeout = 0.5
                server.handle_request()
    except Exception as e:
        if not shutdown_flag.is_set():
            print(f"[XMLRPC] ⚠️ Server error: {e}")


# ================= SIGNAL HANDLING =================


def reload_config(sig, frame):
    """Reload client_ip.cfg on SIGHUP — no restart needed after IP change."""
    print("🔄 SIGHUP received — reloading client IP...")
    read_allowed_ip(CLIENT_IP_FILE)


def signal_handler(sig, frame):
    print("\n🛑 Shutdown requested...")
    shutdown_flag.set()
    time.sleep(0.2)
    # Ensure PTT and CW are OFF on exit
    set_ptt(0)
    cw_set(0)
    gpio_request.set_value(LINE_CON, Value.INACTIVE)
    # Release GPIO resources so the service can restart cleanly
    gpio_request.release()
    print("👋 Goodbye.")
    sys.exit(0)


# ================= MAIN =================

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGHUP, reload_config)

    read_allowed_ip(CLIENT_IP_FILE)

    # Start threads
    server_thread = threading.Thread(target=ptt_server, daemon=True)
    monitor_thread = threading.Thread(target=client_monitor, daemon=True)
    responder_thread = threading.Thread(target=ping_responder, daemon=True)
    cw_thread = threading.Thread(target=winkeyer_server, daemon=True)
    xmlrpc_thread = threading.Thread(target=xmlrpc_server, daemon=True)

    server_thread.start()
    monitor_thread.start()
    responder_thread.start()
    cw_thread.start()
    xmlrpc_thread.start()

    print("✅ Combined service started.")
    print(f"   • PTT receiver        : UDP port {PTT_PORT}")
    print(f"   • Ping responder      : UDP port {PING_PORT}")
    print(f"   • CW Keyer (Winkeyer) : UDP port {WK_PORT}")
    print(f"   • XML-RPC (logging)   : TCP port {XMLRPC_PORT}")
    print(f"   • Client monitor      : pings {client_ip}:{PING_PORT} every {CHECK_INTERVAL}s")
    print(f"   • GPIO lines          : CW=PC1({LINE_CW}), CON=PC2({LINE_CON}), PTT=PC3({LINE_PTT})")
    print("   • Send SIGHUP to reload client_ip.cfg without restart")
    print("   • Press Ctrl+C to exit")

    try:
        while not shutdown_flag.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(None, None)
