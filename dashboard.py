import tkinter as tk
from tkinter import ttk
import matplotlib
# Force an English-safe font to avoid Thai glyph warnings in Matplotlib
matplotlib.rcParams['font.family'] = 'DejaVu Sans'
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt
from datetime import datetime

# ---------------- Serial (PMS3005) ----------------
import serial

INDOOR_PORT = "/dev/ttyAMA0"   # change if needed
OUTDOOR_PORT = "/dev/ttyAMA2"  # change if needed
BAUDRATE = 9600
TIMEOUT = 1

class PMSReader:
    """Read PMS3005 from a given serial port. Returns dict with pm1, pm25, pm10."""
    def __init__(self, port: str):
        self.port = port
        try:
            self.ser = serial.Serial(port, baudrate=BAUDRATE, timeout=TIMEOUT)
            self.ok = True
            print(f"[PMS] Opened {port}")
        except Exception as e:
            print(f"[WARN] Cannot open serial {port}: {e}")
            self.ser = None
            self.ok = False

    def read_once(self):
        if not self.ok:
            return {"pm1": 0.0, "pm25": 0.0, "pm10": 0.0}
        try:
            data = self.ser.read(32)
            if len(data) == 32 and data[0] == 0x42 and data[1] == 0x4D:
                pm1  = int.from_bytes(data[4:6],  'big')
                pm25 = int.from_bytes(data[6:8],  'big')
                pm10 = int.from_bytes(data[8:10], 'big')
                return {"pm1": float(pm1), "pm25": float(pm25), "pm10": float(pm10)}
        except Exception as e:
            print(f"[WARN] Serial read error on {self.port}: {e}")
        return {"pm1": 0.0, "pm25": 0.0, "pm10": 0.0}

    def close(self):
        try:
            if self.ser:
                self.ser.close()
                print(f"[PMS] Closed {self.port}")
        except Exception:
            pass

# ---------------- GPIO (Relays) ----------------
ACTIVE_LOW = True                   # Most relay boards are active LOW; set True to make ON=LOW
RELAY_PINS = [17, 18, 27, 22]       # BCM pin numbers

# Prefer gpiozero with lgpio on Pi 5 / Bookworm
try:
    from gpiozero import DigitalOutputDevice, Device
    try:
        from gpiozero.pins.lgpio import LGPIOFactory
        Device.pin_factory = LGPIOFactory()
        _gpio_backend = 'gpiozero(lgpio)'
    except Exception:
        _gpio_backend = 'gpiozero(auto)'
    _gpio_available = True
except Exception as e:
    print(f"[WARN] gpiozero not available: {e}")
    _gpio_available = False
    _gpio_backend = 'mock'

class RelayManager:
    def __init__(self, pins, active_low=True):
        self.active_low = active_low
        self.devs = {}
        self.mock = not _gpio_available
        if self.mock:
            print("[GPIO] Using MOCK backend (no hardware)")
        else:
            print(f"[GPIO] Using {_gpio_backend}")
        for p in pins:
            if self.mock:
                self.devs[p] = False  # False=OFF, True=ON
            else:
                dev = DigitalOutputDevice(p, active_high=(not active_low), initial_value=False)
                self.devs[p] = dev

    def set(self, pin, on: bool):
        if self.mock:
            self.devs[pin] = on
            print(f"[MOCK] Pin {pin} => {'ON' if on else 'OFF'}")
        else:
            d = self.devs[pin]
            d.on() if on else d.off()

    def toggle(self, pin):
        if self.mock:
            self.set(pin, not self.devs[pin])
        else:
            d = self.devs[pin]
            d.off() if d.value else d.on()

    def all_on(self):
        for p in list(self.devs.keys()):
            self.set(p, True)

    def all_off(self):
        for p in list(self.devs.keys()):
            self.set(p, False)

    def is_on(self, pin):
        if self.mock:
            return bool(self.devs[pin])
        return bool(self.devs[pin].value)

    def close(self):
        if not self.mock:
            for dev in self.devs.values():
                try: dev.close()
                except: pass
            try: Device.pin_factory.close()
            except: pass

# ---------------- UI Helpers ----------------
PM25_BANDS = [
    (0.0, 12.0, "Good", "#2ecc71"),
    (12.1, 35.4, "Moderate", "#f1c40f"),
    (35.5, 55.4, "USG", "#e67e22"),
    (55.5, 150.4, "Unhealthy", "#e74c3c"),
    (150.5, 250.4, "Very Unhealthy", "#8e44ad"),
    (250.5, float("inf"), "Hazardous", "#7f0000"),
]

def pm25_category(val: float):
    for lo, hi, label, color in PM25_BANDS:
        if lo <= val <= hi:
            return label, color
    return "-", "#7f8c8d"

class StatCard(ttk.Frame):
    def __init__(self, master, title: str):
        super().__init__(master, padding=10)
        self.columnconfigure(0, weight=1)
        self.title_lbl = ttk.Label(self, text=title, font=("Kanit", 14))
        self.value_lbl = ttk.Label(self, text="-- µg/m³", font=("Kanit", 26, "bold"))
        self.title_lbl.grid(row=0, column=0, sticky="w")
        self.value_lbl.grid(row=1, column=0, sticky="ew")

class PM25Badge(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=(10, 6))
        self.columnconfigure(1, weight=1)
        self.bgcolor = "#0F0F1A"
        self.dot = tk.Canvas(self, width=14, height=14, highlightthickness=0, bg=self.bgcolor)
        self.label = ttk.Label(self, text="-", font=("Kanit", 12, "bold"))
        self.bar = ttk.Progressbar(self, orient="horizontal", mode="determinate", maximum=250)
        self.dot.grid(row=0, column=0, padx=(0, 8))
        self.label.grid(row=0, column=1, sticky="w")
        self.bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
    def update_badge(self, value: float):
        text, color = pm25_category(value)
        self.label.config(text=text)
        self.bar['value'] = min(max(value, 0), 250)
        self.dot.delete("all")
        self.dot.create_oval(2, 2, 12, 12, fill=color, outline=color)

class Section(ttk.Labelframe):
    def __init__(self, master, title: str):
        super().__init__(master, text=title, padding=12)
        for i in range(3):
            self.columnconfigure(i, weight=1, uniform="col")
        self.pm1 = StatCard(self, "PM1.0")
        self.pm25 = StatCard(self, "PM2.5")
        self.pm10 = StatCard(self, "PM10")
        self.badge = PM25Badge(self)
        self.pm1.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        self.pm25.grid(row=0, column=1, padx=6, pady=6, sticky="nsew")
        self.pm10.grid(row=0, column=2, padx=6, pady=6, sticky="nsew")
        self.badge.grid(row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 6))

# ---------------- Main App ----------------
class PMDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Air Quality Dashboard")
        self.root.geometry("1280x860")
        self.job = None
        self.auto_on = False   # current auto relay state (are relays ON due to auto?)

        self._setup_style()

        # Serial readers
        self.reader_indoor = PMSReader(INDOOR_PORT)
        self.reader_outdoor = PMSReader(OUTDOOR_PORT)

        # Relays
        self.relays = RelayManager(RELAY_PINS, active_low=ACTIVE_LOW)

        # Header
        header = ttk.Frame(root, padding=(16, 12))
        header.pack(fill="x")
        title = ttk.Label(header, text="Indoor & Outdoor Air Quality", font=("Kanit", 28, "bold"))
        self.last_lbl = ttk.Label(header, text="Last update: -", font=("Kanit", 12))
        title.pack(side="left")
        self.last_lbl.pack(side="right")

        # Content (two sections)
        content = ttk.Frame(root, padding=(12, 0))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        self.indoor = Section(content, "Indoor")
        self.outdoor = Section(content, "Outdoor")
        self.indoor.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        self.outdoor.grid(row=0, column=1, padx=8, pady=8, sticky="nsew")

        # Trend chart
        chart_frame = ttk.Frame(root, padding=(12, 4))
        chart_frame.pack(fill="both", expand=True)
        self.indoor_history, self.outdoor_history, self.time_history = [], [], []
        self.fig, self.ax = plt.subplots(figsize=(10, 4), facecolor="#0F0F1A")
        self.ax.set_facecolor("#0F0F1A")
        self.ax.tick_params(colors='white')
        self.ax.set_title("PM2.5 Trend (last 20 points)", color="white", fontsize=14)
        self.ax.set_ylabel("µg/m³", color="white")
        self.ax.set_xlabel("Time", color="white")
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # Relay control panel
        self._build_relay_panel()

        # Auto control panel
        self._build_auto_panel()

        # Start updates
        self.update_data()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _setup_style(self):
        self.root.configure(bg="#0F0F1A")
        style = ttk.Style(self.root)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure("TFrame", background="#0F0F1A")
        style.configure("TLabelframe", background="#0F0F1A", foreground="white", font=("Kanit", 16, "bold"))
        style.configure("TLabelframe.Label", background="#0F0F1A", foreground="#e0e0e0")
        style.configure("TLabel", background="#0F0F1A", foreground="white")
        style.configure("TButton", background="#1e1e2e", foreground="white")
        style.configure("TCheckbutton", background="#0F0F1A", foreground="white")
        style.configure("TProgressbar", troughcolor="#1c1c2b", background="#00bcd4", bordercolor="#1c1c2b", lightcolor="#00bcd4", darkcolor="#00bcd4")

    def _build_relay_panel(self):
        panel = ttk.Labelframe(self.root, text="Relay Control", padding=12)
        panel.pack(fill="x", padx=12, pady=(0, 8))
        row = 0
        self.relay_btns = {}
        for idx, pin in enumerate(RELAY_PINS):
            btn = ttk.Button(panel, text=f"GPIO {pin}: OFF", command=lambda p=pin: self._toggle_pin(p))
            btn.grid(row=row, column=idx, padx=6, pady=6, sticky="ew")
            self.relay_btns[pin] = btn
        row += 1
        ttk.Button(panel, text="All ON", command=self._all_on).grid(row=row, column=0, padx=6, pady=6, sticky="ew")
        ttk.Button(panel, text="All OFF", command=self._all_off).grid(row=row, column=1, padx=6, pady=6, sticky="ew")
        self.backend_lbl = ttk.Label(panel, text=f"GPIO backend: {_gpio_backend} | ActiveLow={ACTIVE_LOW}")
        self.backend_lbl.grid(row=row, column=2, columnspan=2, sticky="e", padx=6)

    def _build_auto_panel(self):
        panel = ttk.Labelframe(self.root, text="Auto Control (by PM2.5)", padding=12)
        panel.pack(fill="x", padx=12, pady=(0, 12))

        self.auto_enabled = tk.BooleanVar(value=False)
        self.auto_source = tk.StringVar(value="Indoor")  # Indoor or Outdoor
        self.auto_on_th = tk.DoubleVar(value=45.0)        # ON when >= th
        self.auto_hyst = tk.DoubleVar(value=5.0)          # OFF when <= th - hyst
        self.auto_state_lbl = ttk.Label(panel, text="Auto state: OFF")

        ttk.Checkbutton(panel, text="Enable auto mode", variable=self.auto_enabled).grid(row=0, column=0, sticky="w")
        ttk.Label(panel, text="Source:").grid(row=0, column=1, sticky="e")
        ttk.OptionMenu(panel, self.auto_source, self.auto_source.get(), "Indoor", "Outdoor").grid(row=0, column=2, sticky="w")

        ttk.Label(panel, text="On threshold (µg/m³):").grid(row=1, column=0, sticky="e", pady=6)
        ttk.Entry(panel, textvariable=self.auto_on_th, width=8).grid(row=1, column=1, sticky="w", pady=6)
        ttk.Label(panel, text="Hysteresis (µg/m³):").grid(row=1, column=2, sticky="e", pady=6)
        ttk.Entry(panel, textvariable=self.auto_hyst, width=8).grid(row=1, column=3, sticky="w", pady=6)
        self.auto_state_lbl.grid(row=0, column=3, sticky="e")

    # ---------------- Relay actions ----------------
    def _toggle_pin(self, pin):
        self.relays.toggle(pin)
        self._refresh_relay_labels()

    def _all_on(self):
        self.relays.all_on()
        self._refresh_relay_labels()

    def _all_off(self):
        self.relays.all_off()
        self._refresh_relay_labels()
        self.auto_on = False  # reset auto state as well
        self.auto_state_lbl.config(text="Auto state: OFF")

    def _refresh_relay_labels(self):
        for pin, btn in self.relay_btns.items():
            state = self.relays.is_on(pin)
            btn.config(text=f"GPIO {pin}: {'ON' if state else 'OFF'}")

    # ---------------- Data & chart ----------------
    def _update_cards(self, section: Section, data: dict):
        section.pm1.value_lbl.config(text=f"{data['pm1']:.1f} µg/m³")
        section.pm25.value_lbl.config(text=f"{data['pm25']:.1f} µg/m³")
        section.pm10.value_lbl.config(text=f"{data['pm10']:.1f} µg/m³")
        section.badge.update_badge(data['pm25'])

    def _auto_logic(self, indoor_pm25: float, outdoor_pm25: float):
        if not self.auto_enabled.get():
            return
        try:
            th = float(self.auto_on_th.get())
            hy = float(self.auto_hyst.get())
        except Exception:
            return
        src = self.auto_source.get()
        pm = indoor_pm25 if src == 'Indoor' else outdoor_pm25
        # ON when pm >= th; OFF when pm <= th - hy
        if not self.auto_on and pm >= th:
            self.relays.all_on()
            self.auto_on = True
            self.auto_state_lbl.config(text=f"Auto state: ON ({src} {pm:.1f} ≥ {th})")
            self._refresh_relay_labels()
        elif self.auto_on and pm <= (th - hy):
            self.relays.all_off()
            self.auto_on = False
            self.auto_state_lbl.config(text=f"Auto state: OFF ({src} {pm:.1f} ≤ {th - hy})")
            self._refresh_relay_labels()

    def update_data(self):
        indoor = self.reader_indoor.read_once()
        outdoor = self.reader_outdoor.read_once()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_lbl.config(text=f"Last update: {ts}")

        # Update sections
        self._update_cards(self.indoor, indoor)
        self._update_cards(self.outdoor, outdoor)

        # Auto control
        self._auto_logic(indoor.get('pm25', 0.0), outdoor.get('pm25', 0.0))

        # History
        current_time = datetime.now().strftime("%H:%M:%S")
        self.time_history.append(current_time)
        self.indoor_history.append(indoor['pm25'])
        self.outdoor_history.append(outdoor['pm25'])
        if len(self.time_history) > 20:
            self.time_history.pop(0)
            self.indoor_history.pop(0)
            self.outdoor_history.pop(0)

        # Redraw chart
        self.ax.clear()
        self.ax.set_facecolor("#0F0F1A")
        self.ax.tick_params(colors='white')
        self.ax.grid(True, linestyle='--', alpha=0.3, color="#555555")
        self.ax.set_title("PM2.5 Trend (last 20 points)", color="white", fontsize=14)
        self.ax.set_ylabel("µg/m³", color="white")
        self.ax.set_xlabel("Time", color="white")
        self.ax.plot(self.time_history, self.indoor_history, color="#FFA500", marker="o", linewidth=2, label="Indoor")
        self.ax.plot(self.time_history, self.outdoor_history, color="#00FFFF", marker="o", linewidth=2, label="Outdoor")
        self.ax.legend(facecolor="#0F0F1A", edgecolor="white", fontsize=10, labelcolor="white")
        self.fig.autofmt_xdate()
        self.canvas.draw()

        # Schedule next
        if self.root.winfo_exists():
            self.job = self.root.after(5000, self.update_data)

    def on_close(self):
        if self.job is not None:
            try: self.root.after_cancel(self.job)
            except Exception: pass
        try: self.reader_indoor.close()
        except Exception: pass
        try: self.reader_outdoor.close()
        except Exception: pass
        try: self.relays.close()
        except Exception: pass
        self.root.destroy()

# ---------------- Run ----------------
if __name__ == "__main__":
    root = tk.Tk()
    app = PMDashboard(root)
    root.mainloop()
