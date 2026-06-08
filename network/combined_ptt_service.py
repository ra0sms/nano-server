#!/usr/bin/python3
import os
import signal
import socket
import sys
import threading
import time

import gpiod

# Absolute path to client_ip.cfg regardless of working directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_IP_FILE = os.path.join(SCRIPT_DIR, "..", "client_ip.cfg")

# === GPIO ===
CHIP = "gpiochip0"
LINE_CON = 66
LINE_PTT = 67
CONSUMER = "ptt_combined"

# === UDP ===
SERVER_IP = "0.0.0.0"
SERVER_PORT = 5001  # for PTT commands
PING_PORT = 5002  # for ping replies
TIMEOUT = 1.0
CHECK_INTERVAL = 0.3
MAGIC_PHRASE = b"PING_RESPONSE"

# === State ===
ptt_state = 0  # текущее физическое состояние PTT
client_ip = "0.0.0.0"
need_ser2net_reboot = False
shutdown_flag = threading.Event()

# === GPIO Setup ===
try:
    chip = gpiod.Chip(CHIP)
    line_con = chip.get_line(LINE_CON)
    line_ptt = chip.get_line(LINE_PTT)
    line_con.request(consumer=CONSUMER, type=gpiod.LINE_REQ_DIR_OUT)
    line_ptt.request(consumer=CONSUMER, type=gpiod.LINE_REQ_DIR_OUT)
    print("GPIO lines initialized.")
except Exception as e:
    print(f"❌ GPIO initialization failed: {e}")
    sys.exit(1)


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
            line_ptt.set_value(value)
            ptt_state = value
            print(f"📡 PTT {'ON' if value else 'OFF'} (GPIO={value})")
        except Exception as e:
            print(f"⚠️ GPIO error: {e}")


def ptt_server():
    """UDP server listening for '0'/'1' commands"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((SERVER_IP, SERVER_PORT))
    print(f"📡 UDP server listening on {SERVER_IP}:{SERVER_PORT}...")

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
                    print(f"⚠️ UDP server error: {e}")
    finally:
        sock.close()


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
            line_con.set_value(1)
            if need_ser2net_reboot:
                print("🔄 Client back online — restarting ser2net...")
                os.system("systemctl restart ser2net.service")
                need_ser2net_reboot = False
                # NOTE: no need to restart self — we’re already running!
        else:
            line_con.set_value(0)
            set_ptt(0)  # 🔒 FAIL-SAFE: disable PTT when client is gone
            need_ser2net_reboot = True

        if online != prev_online:
            status = "✅ online" if online else "❌ offline"
            print(f"🌐 Client {client_ip} is {status}")
            prev_online = online

        time.sleep(CHECK_INTERVAL)


def signal_handler(sig, frame):
    print("\n🛑 Shutdown requested...")
    shutdown_flag.set()
    time.sleep(0.2)  # let threads notice flag
    # Ensure PTT is OFF on exit
    set_ptt(0)
    line_con.set_value(0)
    # Release GPIO resources so the service can restart cleanly
    line_ptt.release()
    line_con.release()
    chip.close()
    print("👋 Goodbye.")
    sys.exit(0)


# === MAIN ===
if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    read_allowed_ip(CLIENT_IP_FILE)

    # Start threads
    server_thread = threading.Thread(target=ptt_server, daemon=True)
    monitor_thread = threading.Thread(target=client_monitor, daemon=True)

    server_thread.start()
    monitor_thread.start()

    print("✅ Combined PTT service started.")
    print("   • UDP PTT control on port 5001")
    print("   • Client ping monitoring on port 5002")
    print("   • Press Ctrl+C to exit")

    try:
        while not shutdown_flag.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(None, None)
