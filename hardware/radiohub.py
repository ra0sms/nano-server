import asyncio
import threading
import time

import serial

SERIAL_PORT = "/dev/ttyCAT"
BAUDRATE = 19200

TCP_PORT = 3001

RADIO_ADDR = 0x70
CTRL_ADDR = 0xE0


clients = set()

radio_state = {"freq": 0, "band": "Unknown", "last_rx": 0}


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

        dst = frame[2]
        src = frame[3]
        cmd = frame[4]

        #
        # Частота
        #
        if cmd == 0x03:
            payload = frame[5:-1]

            if len(payload) == 5:
                freq = decode_bcd_freq(payload)

                if freq:
                    radio_state["freq"] = freq
                    radio_state["band"] = freq_to_band(freq)

                    print(f"Freq={freq} Hz Band={radio_state['band']}")


decoder = CIVDecoder()


ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=0.1)


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


def serial_reader(loop):

    while True:
        data = ser.read(1024)

        if not data:
            continue

        decoder.feed(data)

        asyncio.run_coroutine_threadsafe(broadcast(data), loop)


async def tcp_client(reader, writer):

    addr = writer.get_extra_info("peername")

    print("Client connected:", addr)

    clients.add(writer)

    try:
        while True:
            data = await reader.read(1024)

            if not data:
                break

            ser.write(data)

    except:
        pass

    clients.discard(writer)

    writer.close()

    await writer.wait_closed()

    print("Client disconnected:", addr)


async def poller():

    while True:
        await asyncio.sleep(2)

        if time.time() - radio_state["last_rx"] < 2:
            continue

        cmd = bytes([0xFE, 0xFE, RADIO_ADDR, CTRL_ADDR, 0x03, 0xFD])

        ser.write(cmd)


async def main():

    loop = asyncio.get_running_loop()

    threading.Thread(target=serial_reader, args=(loop,), daemon=True).start()

    server = await asyncio.start_server(tcp_client, "0.0.0.0", TCP_PORT)

    print(f"Listening TCP {TCP_PORT}")

    asyncio.create_task(poller())

    async with server:
        await server.serve_forever()


asyncio.run(main())
