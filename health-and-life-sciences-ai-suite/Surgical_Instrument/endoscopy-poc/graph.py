#!/usr/bin/env python3

import sys
import time
import threading
import signal
from collections import deque
import matplotlib
from matplotlib.widgets import Button

# Use Qt5Agg; fallback to TkAgg if needed
try:
    matplotlib.use("Qt5Agg")
except Exception:
    matplotlib.use("TkAgg")

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from serial import Serial
from serial.tools import list_ports
import argparse

# ------------------ Config ------------------
BAUD = 9600
MAX_SAMPLES = 1000
Y_AXIS_MAX_CAP = 500
PORT_PROBE_TIMEOUT = 5.0  # seconds

# ------------------ Auto-detect port ------------------
def detect_serial_port():
    ports = [p.device for p in list_ports.comports()]
    if not ports:
        print("No serial ports found.")
        sys.exit(1)

    for port in ports:
        print(f"Checking {port} for activity...")
        try:
            with Serial(port, BAUD, timeout=1) as ser:
                ser.reset_input_buffer()
                start = time.time()
                while time.time() - start < PORT_PROBE_TIMEOUT:
                    if ser.in_waiting:
                        line = ser.readline().decode(errors="ignore").strip()
                        if line:
                            print(f"Detected serial activity on {port}")
                            return port
                    time.sleep(0.1)
        except Exception:
            continue

    print("No active Arduino found.")
    sys.exit(1)

# ------------------ Serial Reader Thread ------------------
class SerialReader(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.ser = Serial(port, BAUD, timeout=0.5)
        self.samples = deque(maxlen=MAX_SAMPLES)
        self.lock = threading.Lock()
        self.running = True

    def reset_data(self):
        with self.lock:
            self.samples.clear()

    def run(self):
        print("SerialReader started")
        print("clock_time,ms")
        while self.running:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors="ignore").strip()
                    if line.isdigit():
                        value = int(line)
                        assume = 1 <= value <= Y_AXIS_MAX_CAP
                        if assume:
                            print(f"/{time.time():.3f},{value}")
                            with self.lock:
                                self.samples.append(value)
            except Exception as e:
                print(f"Reader error: {e}")
                continue

    def get_data(self):
        with self.lock:
            return list(self.samples)

    def stop(self):
        self.running = False
        try:
            self.ser.close()
        except Exception:
            pass

# ------------------ Live Plot ------------------
def plot_live(reader: SerialReader):
    print("Starting plot")
    fig, ax = plt.subplots()
    fig.canvas.manager.set_window_title("Physically Measured Latency")

    reset_ax = plt.axes([0.81, 0.01, 0.1, 0.05])
    reset_btn = Button(reset_ax, 'Reset')
    reset_btn.on_clicked(lambda event: reader.reset_data())

    fig.suptitle("Photon to Pixel Latency")
    line, = ax.plot([], [], lw=1)
    ax.set_xlabel("Sample Number")
    ax.set_ylabel("Latency (ms)")
    ax.set_ylim(0, Y_AXIS_MAX_CAP)
    ax.set_xlim(1, 100)
    ax.grid(True)
    fig.tight_layout(rect=[0, 0.05, 1, 0.95])  # leave room for reset button


    def update(_):
        y = reader.get_data()
        if not y:
            return line,

        x = list(range(1, len(y) + 1))
        line.set_data(x, y)

        right = x[-1]
        left = max(1, right - MAX_SAMPLES + 1)
        ax.set_xlim(left, max(100, right))

        actual_y = [val for val in y if 1 <= val <= Y_AXIS_MAX_CAP]
        if actual_y:
            y_max = min(max(actual_y), Y_AXIS_MAX_CAP)
            ax.set_ylim(0, max(100, y_max))
        else:
            ax.set_ylim(0, 100)

        return line,

    ani = FuncAnimation(fig, update, interval=100, blit=False, cache_frame_data=False)


    try:
        plt.show()
    except KeyboardInterrupt:
        pass

    print("Plot ended")

# ------------------ Main ------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", help="Specify serial port manually (e.g., /dev/ttyACM0)")
    args = parser.parse_args()

    port = args.port if args.port else detect_serial_port()
    reader = SerialReader(port)

    def signal_handler(sig, frame):
        print("\nInterrupt received, shutting down...")
        reader.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    reader.start()

    try:
        plot_live(reader)
    finally:
        reader.stop()

if __name__ == "__main__":
    main()
