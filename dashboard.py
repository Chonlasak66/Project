
# -*- coding: utf-8 -*-
"""
Air Quality Dashboard - Overlay Gap (no spacer column)
- Removes the 4th "blank" column inside Indoor/Outdoor sections
- Left stack becomes narrower; right chart widens to fill the freed space
- Keeps compact_fix_v2 safeguards (hero min-height + chart anti-spill + size sync)
"""
import tkinter as tk
from tkinter import ttk
import matplotlib
matplotlib.rcParams['font.family'] = 'DejaVu Sans'
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt
from datetime import datetime
import serial
from typing import Optional

INDOOR_PORT = "/dev/ttyAMA0"
OUTDOOR_PORT = "/dev/ttyAMA2"
BAUDRATE = 9600
TIMEOUT = 0

ACTIVE_LOW = True
RELAY_PINS = [17, 18, 27, 22]
RELAY_NAMES = {17: "Purifier", 18: "Fan", 27: "Vent", 22: "Spare"}

try:
    from gpiozero import DigitalOutputDevice as _GpioZeroDevice, Device
    try:
        from gpiozero.pins.lgpio import LGPIOFactory
        Device.pin_factory = LGPIOFactory()
        _gpiozero_backend_name = 'lgpio'
    except Exception:
        _gpiozero_backend_name = 'auto'
    _gpiozero_available = True
except Exception as e:
    print(f"[WARN] gpiozero not available: {e}")
    _gpiozero_available = False
    Device = None

try:
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    _rpi_gpio_available = True
except Exception as e:
    print(f"[WARN] RPi.GPIO not available: {e}")
    _rpi_gpio_available = False
    GPIO = None

UI_UPDATE_MS = 600
HISTORY_MAX = 180
CHART_EVERY_N_TICKS = 2

COL_BG = "#0B0F1A"
COL_SURFACE = "#121826"
COL_SURFACE_MUTED = "#0f1522"
COL_TEXT = "#FFFFFF"
COL_TEXT_MUTED = "#B8C1CC"
COL_ACCENT = "#49B6FF"
COL_OK = "#2ecc71"
COL_WARN = "#f1c40f"
COL_USG = "#e67e22"
COL_BAD = "#e74c3c"
COL_VBAD = "#8e44ad"
COL_HAZ = "#7f0000"
GRID_COLOR = "#38465a"

PM25_BANDS = [
    (0.0, 12.0, "Good", COL_OK),
    (12.1, 35.4, "Moderate", COL_WARN),
    (35.5, 55.4, "USG", COL_USG),
    (55.5, 150.4, "Unhealthy", COL_BAD),
    (150.5, 250.4, "Very Unhealthy", COL_VBAD),
    (250.5, float("inf"), "Hazardous", COL_HAZ),
]

def pm25_band(value: float):
    for lo, hi, label, color in PM25_BANDS:
        if lo <= (value if value is not None else -1) <= hi:
            return label, color
    return "-", COL_TEXT_MUTED

def _hex_to_rgb(h: str):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _rgb_to_hex(rgb):
    return '#%02x%02x%02x' % rgb

def mix_color(fg: str, bg: str, t: float = 0.30) -> str:
    r1, g1, b1 = _hex_to_rgb(fg)
    r2, g2, b2 = _hex_to_rgb(bg)
    r = int(r1*t + r2*(1-t))
    g = int(g1*t + g2*(1-t))
    b = int(b1*t + b2*(1-t))
    return _rgb_to_hex((r, g, b))

class RelayController:
    def __init__(self, pins, active_low=True):
        self.pins = list(pins)
        self.active_low = bool(active_low)
        self.states = {p: False for p in pins}
        self.backend = None
        self._devices = {}
        if _gpiozero_available:
            try:
                for p in pins:
                    dev = _GpioZeroDevice(p, active_high=(not self.active_low), initial_value=False)
                    self._devices[p] = dev
                self.backend = 'gpiozero'
                print(f"[GPIO] Using gpiozero backend ({_gpiozero_backend_name})")
            except Exception as e:
                print(f"[WARN] gpiozero init failed: {e}")
                self.backend = None
        if self.backend is None and _rpi_gpio_available:
            try:
                GPIO.setmode(GPIO.BCM)
                for p in pins:
                    GPIO.setup(p, GPIO.OUT)
                for p in pins:
                    self._apply_pin_rpigpio(p, False)
                self.backend = 'RPi.GPIO'
                print('[GPIO] Using RPi.GPIO backend')
            except Exception as e:
                print(f"[WARN] RPi.GPIO init failed: {e}")
                self.backend = None
        if self.backend is None:
            self.backend = 'mock'
            print('[GPIO] Using MOCK backend (no hardware)')

    def _apply_pin_gpiozero(self, pin, state):
        dev = self._devices.get(pin)
        if dev:
            dev.on() if state else dev.off()
        self.states[pin] = state

    def _apply_pin_rpigpio(self, pin, state):
        if self.active_low:
            level = GPIO.LOW if state else GPIO.HIGH
        else:
            level = GPIO.HIGH if state else GPIO.LOW
        GPIO.output(pin, level)
        self.states[pin] = state

    def _apply_pin(self, pin, state):
        if self.backend == 'gpiozero':
            self._apply_pin_gpiozero(pin, state)
        elif self.backend == 'RPi.GPIO':
            self._apply_pin_rpigpio(pin, state)
        else:
            print(f"[MOCK GPIO] Pin {pin} -> {'ON' if state else 'OFF'}")
            self.states[pin] = state

    def set(self, pin, state: bool):
        if pin in self.pins:
            self._apply_pin(pin, bool(state))

    def toggle(self, pin):
        self.set(pin, not self.states.get(pin, False))

    def set_all(self, state: bool):
        for p in self.pins:
            self._apply_pin(p, bool(state))

    def cleanup(self):
        try:
            if self.backend == 'gpiozero':
                for dev in self._devices.values():
                    try: dev.off()
                    except: pass
                    try: dev.close()
                    except: pass
                try:
                    if Device and getattr(Device, "pin_factory", None):
                        Device.pin_factory.close()
                except Exception as e:
                    print(f"[WARN] pin_factory.close() failed: {e}")
            elif self.backend == 'RPi.GPIO':
                try:
                    for p in self.pins:
                        self._apply_pin_rpigpio(p, False)
                except Exception: pass
                try:
                    GPIO.cleanup()
                except Exception: pass
        except Exception:
            pass

class PMSReader:
    def __init__(self, port: str):
        try:
            self.ser = serial.Serial(port, baudrate=BAUDRATE, timeout=TIMEOUT)
            try: self.ser.reset_input_buffer()
            except: pass
            self.buf = bytearray()
            self.last = {"pm1": 0.0, "pm25": 0.0, "pm10": 0.0}
            self.ok = True
        except Exception as e:
            print(f"[WARN] Cannot open serial {port}: {e}")
            self.ser = None
            self.ok = False

    def _parse_frames(self):
        i = 0
        while True:
            j = self.buf.find(b'\x42\x4D', i)
            if j < 0:
                if len(self.buf) > 1:
                    self.buf = self.buf[-1:]
                break
            if len(self.buf) - j < 32:
                if j > 0:
                    self.buf = self.buf[j:]
                break
            frame = self.buf[j:j+32]
            self.buf = self.buf[j+32:]
            if frame[0] == 0x42 and frame[1] == 0x4D:
                pm1  = int.from_bytes(frame[10:12], 'big')
                pm25 = int.from_bytes(frame[12:14], 'big')
                pm10 = int.from_bytes(frame[14:16], 'big')
                self.last = {"pm1": float(pm1), "pm25": float(pm25), "pm10": float(pm10)}
            i = 0

    def read_once(self):
        if not self.ok:
            return self.last
        try:
            n = self.ser.in_waiting
            if n:
                self.buf += self.ser.read(n)
                self._parse_frames()
        except Exception as e:
            print(f"[WARN] Serial read error: {e}")
        return self.last

    def close(self):
        try:
            if self.ser: self.ser.close()
        except Exception:
            pass

class EnvReader:
    def __init__(self, addr=0x76):
        self.addr = addr
        self.backend = None
        self.kind = None
        self.obj = None
        try:
            import board, busio
            import adafruit_bme280
            i2c = busio.I2C(board.SCL, board.SDA)
            self.obj = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=self.addr)
            self.backend = 'adafruit_bme280'
            self.kind = 'BME280'
            return
        except Exception:
            pass
        try:
            import board, busio
            import adafruit_bmp280
            i2c = busio.I2C(board.SCL, board.SDA)
            self.obj = adafruit_bmp280.Adafruit_BMP280_I2C(i2c, address=self.addr)
            self.backend = 'adafruit_bmp280'
            self.kind = 'BMP280'
            return
        except Exception:
            pass
        try:
            import smbus2, bme280
            bus = smbus2.SMBus(1)
            cal = bme280.load_calibration_params(bus, self.addr)
            self.obj = (bus, self.addr, cal)
            self.backend = 'smbus2_bme280'
            self.kind = 'BME280'
            return
        except Exception:
            pass
        print("[WARN] No BME280/BMP280 backend available.")

    def read_once(self):
        try:
            if self.backend == 'adafruit_bme280':
                b = self.obj
                return {"temp": float(b.temperature), "humid": float(b.humidity), "press": float(b.pressure)}
            elif self.backend == 'adafruit_bmp280':
                b = self.obj
                return {"temp": float(b.temperature), "humid": None, "press": float(b.pressure)}
            elif self.backend == 'smbus2_bme280':
                import bme280
                bus, addr, cal = self.obj
                s = bme280.sample(bus, addr, cal)
                return {"temp": float(s.temperature), "humid": float(s.humidity), "press": float(s.pressure)}
        except Exception as e:
            print(f"[WARN] ENV read error: {e}")
        return {"temp": None, "humid": None, "press": None}

class KPIHero(ttk.Frame):
    def __init__(self, master, title: str, **kw):
        super().__init__(master, **kw)
        self.configure(style="Surface.TFrame")
        self.accent = tk.Frame(self, width=8, height=1, bg=COL_SURFACE)
        self.accent.grid(row=0, column=0, rowspan=2, sticky="nsw")
        wrap = ttk.Frame(self, padding=(12, 10), style="Surface.TFrame")
        wrap.grid(row=0, column=1, sticky="nsew")
        self.columnconfigure(1, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.title = ttk.Label(wrap, text=title, style="Muted.TLabel")
        self.value = ttk.Label(wrap, text="--", style="Hero.TLabel")
        self.chip = ttk.Label(wrap, text="-", style="Chip.TLabel")
        self.title.grid(row=0, column=0, sticky="w")
        self.value.grid(row=1, column=0, sticky="w")
        self.chip.grid(row=0, column=1, sticky="e")

    def update(self, val: Optional[float], unit: str = ""):
        txt = "--" if val is None else f"{val:.1f}{(' ' + unit) if unit else ''}"
        self.value.configure(text=txt)
        if unit == "µg/m³":
            label, color = pm25_band(val if val is not None else -1)
            self.chip.configure(text=label)
            muted = mix_color(color, COL_SURFACE, t=0.30)
            try:
                self.accent.configure(bg=muted)
            except Exception:
                pass

class StatCard(ttk.Frame):
    def __init__(self, master, title: str, unit: str):
        super().__init__(master, padding=(12, 10))
        self.configure(style="SurfaceMuted.TFrame")
        self.title = ttk.Label(self, text=title, style="Muted.TLabel")
        self.value = ttk.Label(self, text=f"-- {unit}", style="KPINum.TLabel")
        self.title.pack(anchor="w")
        self.value.pack(anchor="w")
        self.unit = unit

    def set(self, val: Optional[float]):
        if val is None:
            self.value.configure(text=f"- {self.unit}")
        else:
            self.value.configure(text=f"{val:.1f} {self.unit}")

class Section(ttk.Labelframe):
    """NOW 3 columns (no spacer)."""
    def __init__(self, master, title: str, show_climate: bool=False):
        super().__init__(master, text=title, padding=12, style="Group.TLabelframe")
        for i in range(3):
            self.columnconfigure(i, weight=1, uniform="col")
        self.rowconfigure(0, minsize=84)
        self.hero = KPIHero(self, "PM2.5", height=84)
        self.hero.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=4, pady=(0,8))
        self.pm1 = StatCard(self, "PM1.0", "µg/m³")
        self.pm25 = StatCard(self, "PM2.5", "µg/m³")
        self.pm10 = StatCard(self, "PM10", "µg/m³")
        self.pm1.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.pm25.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)
        self.pm10.grid(row=1, column=2, sticky="nsew", padx=4, pady=4)
        if show_climate:
            self.temp = StatCard(self, "Temperature", "°C")
            self.humid = StatCard(self, "Humidity", "%")
            self.press = StatCard(self, "Pressure", "hPa")
            self.temp.grid(row=2, column=0, sticky="nsew", padx=4, pady=(4,0))
            self.humid.grid(row=2, column=1, sticky="nsew", padx=4, pady=(4,0))
            self.press.grid(row=2, column=2, sticky="nsew", padx=4, pady=(4,0))

class PMDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Air Quality Dashboard")
        self.root.geometry("1280x900")
        self._setup_style()

        self.reader_indoor = PMSReader(INDOOR_PORT)
        self.reader_outdoor = PMSReader(OUTDOOR_PORT)
        self.env = EnvReader(addr=0x76)

        self.relays = RelayController(RELAY_PINS, active_low=ACTIVE_LOW)

        self.auto_enabled = tk.BooleanVar(value=False)
        self.auto_source = tk.StringVar(value="Indoor")
        self.auto_on_threshold = tk.DoubleVar(value=35.0)
        self.auto_hysteresis = tk.DoubleVar(value=5.0)

        top = ttk.Frame(root, padding=(16, 12), style="Top.TFrame"); top.pack(fill="x")
        self.title_lbl = ttk.Label(top, text="Indoor & Outdoor Air Quality", style="Title.TLabel")
        self.time_lbl = tk.Label(top, text="0000-00-00 00:00:00", font=("DejaVu Sans Mono", 12), bg=COL_BG, fg=COL_TEXT, width=19, anchor="e")
        self.title_lbl.pack(side="left")
        self.time_lbl.pack(side="right")

        content = ttk.Frame(root, padding=12, style="BG.TFrame"); content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=2)
        content.columnconfigure(1, weight=5)
        content.rowconfigure(0, weight=1)

        self.left_stack = ttk.Frame(content, style="BG.TFrame")
        self.left_stack.grid(row=0, column=0, sticky="nsew")
        self.indoor = Section(self.left_stack, "Indoor", show_climate=True)
        self.outdoor = Section(self.left_stack, "Outdoor", show_climate=False)
        self.indoor.pack(fill="x", expand=False, padx=(0,8), pady=(0,8))
        self.outdoor.pack(fill="x", expand=False, padx=(0,8), pady=(0,8))

        self.right_side = ttk.Frame(content, style="BG.TFrame")
        self.right_side.grid(row=0, column=1, sticky="nsew")
        self.right_side.grid_propagate(False)

        self.chart_card = ttk.Frame(self.right_side, padding=12, style="Surface.TFrame")
        self.chart_card.pack(fill="both", expand=True)
        self.chart_card.pack_propagate(False)

        ttl = ttk.Label(self.chart_card, text="PM2.5 Exceedances (last ~100s)", style="Muted.TLabel"); ttl.pack(anchor="w")
        self.indoor_history, self.outdoor_history, self.time_history = [], [], []
        self.fig = plt.Figure(figsize=(8, 3.6), facecolor=COL_SURFACE)
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.95, bottom=0.20)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.chart_card)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill="both", expand=True)

        self.left_stack.bind("<Configure>", self._sync_sizes)
        self.right_side.bind("<Configure>", self._on_right_resize)

        ctrl = ttk.Labelframe(root, text="Controls", padding=12, style="Group.TLabelframe")
        ctrl.pack(fill="x", padx=12, pady=(8,8))
        for i in range(12): ctrl.columnconfigure(i, weight=1)
        ttk.Checkbutton(ctrl, text="Auto mode", variable=self.auto_enabled, style="TCheckbutton").grid(row=0, column=0, sticky="w")
        ttk.Label(ctrl, text="Source:", style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=(12,4))
        ttk.OptionMenu(ctrl, self.auto_source, self.auto_source.get(), "Indoor", "Outdoor").grid(row=0, column=2, sticky="w")
        ttk.Label(ctrl, text="On threshold (µg/m³):", style="Muted.TLabel").grid(row=0, column=3, sticky="e", padx=(12,4))
        ttk.Spinbox(ctrl, from_=0, to=500, increment=1, textvariable=self.auto_on_threshold, width=6).grid(row=0, column=4, sticky="w")
        ttk.Label(ctrl, text="Hysteresis (µg/m³):", style="Muted.TLabel").grid(row=0, column=5, sticky="e", padx=(12,4))
        ttk.Spinbox(ctrl, from_=0, to=100, increment=1, textvariable=self.auto_hysteresis, width=6).grid(row=0, column=6, sticky="w")
        self.auto_state_lbl = ttk.Label(ctrl, text="Auto state: idle", style="Caption.TLabel"); self.auto_state_lbl.grid(row=0, column=11, sticky="e")

        relay_box = ttk.Labelframe(root, text="Relay Controls", padding=12, style="Group.TLabelframe")
        relay_box.pack(fill="x", padx=12, pady=(0,12))
        for i in range(len(RELAY_PINS) + 3): relay_box.columnconfigure(i, weight=1)
        self.relay_btns = {}
        for idx, pin in enumerate(RELAY_PINS):
            name = RELAY_NAMES.get(pin, f"Pin {pin}")
            b = ttk.Button(relay_box, text=f"{name}: OFF", command=lambda p=pin: self._toggle_relay(p))
            b.grid(row=0, column=idx, padx=6, pady=6, sticky="ew"); self.relay_btns[pin] = b
        ttk.Button(relay_box, text="All ON", command=lambda: self._set_all_relays(True)).grid(row=0, column=len(RELAY_PINS), padx=6, pady=6, sticky="ew")
        ttk.Button(relay_box, text="All OFF", command=lambda: self._set_all_relays(False)).grid(row=0, column=len(RELAY_PINS)+1, padx=6, pady=6, sticky="ew")

        self._running = True
        self._tick = 0
        self._after_cb = self._on_timer
        self.job = self.root.after(UI_UPDATE_MS, self._after_cb)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self._sync_sizes)

    def _setup_style(self):
        self.root.configure(bg=COL_BG)
        style = ttk.Style(self.root)
        try: style.theme_use('clam')
        except tk.TclError: pass
        style.configure("BG.TFrame", background=COL_BG)
        style.configure("Top.TFrame", background=COL_BG)
        style.configure("Surface.TFrame", background=COL_SURFACE)
        style.configure("SurfaceMuted.TFrame", background=COL_SURFACE_MUTED)
        style.configure("Group.TLabelframe", background=COL_BG, foreground=COL_TEXT, font=("Kanit", 16, "bold"))
        style.configure("Group.TLabelframe.Label", background=COL_BG, foreground=COL_TEXT)
        style.configure("TLabel", background=COL_BG, foreground=COL_TEXT)
        style.configure("Title.TLabel", background=COL_BG, foreground=COL_TEXT, font=("Kanit", 26, "bold"))
        style.configure("Hero.TLabel", background=COL_SURFACE, foreground=COL_TEXT, font=("Kanit", 34, "bold"))
        style.configure("KPINum.TLabel", background=COL_SURFACE_MUTED, foreground=COL_TEXT, font=("Kanit", 22, "bold"))
        style.configure("Caption.TLabel", background=COL_BG, foreground=COL_TEXT_MUTED, font=("Kanit", 10))
        style.configure("Muted.TLabel", background=COL_BG, foreground=COL_TEXT_MUTED, font=("Kanit", 12))
        style.configure("Chip.TLabel", background=COL_SURFACE, foreground=COL_TEXT, font=("Kanit", 10, "bold"))
        style.configure("TButton", padding=8)
        style.configure("TCheckbutton", background=COL_BG, foreground=COL_TEXT)
        style.configure("TSpinbox", fieldbackground=COL_SURFACE_MUTED, foreground=COL_TEXT, background=COL_BG)
        style.configure("TProgressbar", troughcolor=COL_SURFACE_MUTED, background=COL_ACCENT,
                        bordercolor=COL_SURFACE_MUTED, lightcolor=COL_ACCENT, darkcolor=COL_ACCENT)

    def _on_timer(self):
        if not self._running or not self.root.winfo_exists():
            return
        try:
            self.update_data()
        finally:
            if self._running and self.root.winfo_exists():
                try:
                    self.job = self.root.after(UI_UPDATE_MS, self._after_cb)
                except Exception:
                    pass

    def _toggle_relay(self, pin):
        self.relays.toggle(pin); self._refresh_relay_text(pin)

    def _set_all_relays(self, state: bool):
        self.relays.set_all(state)
        for pin in RELAY_PINS: self._refresh_relay_text(pin)

    def _refresh_relay_text(self, pin):
        state = self.relays.states.get(pin, False); name = RELAY_NAMES.get(pin, f"Pin {pin}")
        self.relay_btns[pin].config(text=f"{name}: {'ON' if state else 'OFF'}")

    def _update_section(self, section: 'Section', data: dict):
        section.hero.update(data['pm25'], unit="µg/m³")
        section.pm1.set(data['pm1'])
        section.pm25.set(data['pm25'])
        section.pm10.set(data['pm10'])

    def _update_climate(self, section: 'Section', env: Optional[dict]):
        if not hasattr(section, 'temp'):
            return
        if not env:
            section.temp.set(None); section.humid.set(None); section.press.set(None)
            return
        section.temp.set(env.get('temp'))
        section.humid.set(env.get('humid'))
        section.press.set(env.get('press'))

    def update_data(self):
        indoor = self.reader_indoor.read_once()
        outdoor = self.reader_outdoor.read_once()
        env = self.env.read_once()

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.time_lbl.configure(text=ts)

        self._update_section(self.indoor, indoor)
        self._update_section(self.outdoor, outdoor)
        self._update_climate(self.indoor, env)

        current_time = datetime.now().strftime("%H:%M:%S")
        self.time_history.append(current_time)
        self.indoor_history.append(indoor['pm25'])
        self.outdoor_history.append(outdoor['pm25'])
        if len(self.time_history) > HISTORY_MAX:
            self.time_history.pop(0); self.indoor_history.pop(0); self.outdoor_history.pop(0)

        self._tick = getattr(self, "_tick", 0) + 1
        if self._tick % CHART_EVERY_N_TICKS == 0:
            self._draw_chart()
        self._auto_control(indoor, outdoor)

    def _draw_chart(self):
        self.fig.clf()
        ax = self.fig.add_subplot(111)
        ax.set_facecolor(COL_SURFACE)
        ax.tick_params(colors=COL_TEXT)
        ax.grid(True, linestyle='--', alpha=0.3, color=GRID_COLOR)
        ax.set_ylabel("µg/m³", color=COL_TEXT)
        ax.set_xlabel("Time", color=COL_TEXT)

        x = list(range(len(self.time_history)))
        y_in = self.indoor_history[:]
        y_out = self.outdoor_history[:]

        ax.plot(x, y_in, linewidth=2, label="Indoor")
        ax.plot(x, y_out, linewidth=2, label="Outdoor")

        th = float(self.auto_on_threshold.get())
        def fill_exceed(series, color_hex):
            muted = mix_color(color_hex, COL_SURFACE, t=0.28)
            above = [ (v is not None) and (v >= th) for v in series ]
            start = None
            for i, flag in enumerate(above + [False]):
                if flag and start is None:
                    start = i
                elif not flag and start is not None:
                    end = i - 1
                    xs = list(range(start, end+1))
                    ys = series[start:end+1]
                    ax.fill_between(xs, ys, [th]*len(xs), alpha=0.26, color=muted, step=None)
                    start = None
        fill_exceed(y_in, COL_BAD)
        fill_exceed(y_out, COL_WARN)

        ax.axhline(th, linestyle=':', color=COL_ACCENT, linewidth=1)

        vals = [v for v in (y_in + y_out + [th]) if v is not None]
        if not vals: vals = [0.0, 1.0]
        ymin = max(0.0, min(vals) - 5.0)
        ymax = max(vals) * 1.15 + 5.0
        if ymax <= ymin: ymax = ymin + 10.0
        prev_min, prev_max = getattr(self, "_ylim", (ymin, ymax))
        alpha = 0.25
        new_min = prev_min*(1-alpha) + ymin*alpha
        new_max = prev_max*(1-alpha) + ymax*alpha
        self._ylim = (new_min, new_max)
        ax.set_ylim(new_min, new_max)

        n = len(x)
        if n > 0:
            ticks = min(8, n)
            idxs = [round(i*(n-1)/(ticks-1)) for i in range(ticks)] if ticks > 1 else [0]
            ax.set_xticks(idxs)
            ax.set_xticklabels([self.time_history[i] for i in idxs], rotation=0, ha='center', color=COL_TEXT)

        leg = ax.legend(loc='upper left', bbox_to_anchor=(0.01, 0.99),
                        facecolor=COL_SURFACE, edgecolor=COL_TEXT, fontsize=10)
        for text in leg.get_texts(): text.set_color(COL_TEXT)

        self.ax = ax
        self.canvas.draw_idle()

    def _auto_control(self, indoor, outdoor):
        if not self.auto_enabled.get():
            self.auto_state_lbl.config(text="Auto state: idle"); return
        source = self.auto_source.get()
        pm = indoor['pm25'] if source == 'Indoor' else outdoor['pm25']
        on_th = float(self.auto_on_threshold.get()); hyster = float(self.auto_hysteresis.get())
        off_th = max(0.0, on_th - hyster)
        currently_on = any(self.relays.states.values())
        desired_on = (pm >= on_th) if not currently_on else (pm >= off_th)
        self.relays.set_all(desired_on)
        for pin in RELAY_PINS: self._refresh_relay_text(pin)
        self.auto_state_lbl.config(text=f"Auto state: {'ON' if desired_on else 'OFF'} | {source} PM2.5={pm:.1f} ≥ {on_th:.1f} / < {off_th:.1f}")

    def _sync_sizes(self, event=None):
        try:
            left_h = self.left_stack.winfo_height()
            if left_h > 0:
                self.right_side.configure(height=left_h)
                self.chart_card.configure(height=left_h)
        except Exception:
            pass

    def _on_right_resize(self, event=None):
        try:
            self.canvas_widget.configure(width=self.right_side.winfo_width(), height=self.right_side.winfo_height())
        except Exception:
            pass

    def on_close(self):
        self._running = False
        if getattr(self, "job", None) is not None:
            try: self.root.after_cancel(self.job)
            except Exception: pass
            self.job = None
        try: self.reader_indoor.close()
        except Exception: pass
        try: self.reader_outdoor.close()
        except Exception: pass
        try: self.relays.cleanup()
        except Exception: pass
        try: self.root.destroy()
        except Exception: pass

if __name__ == "__main__":
    root = tk.Tk()
    app = PMDashboard(root)
    root.mainloop()
