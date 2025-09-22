# -*- coding: utf-8 -*-
import tkinter as tk
from tkinter import ttk
import matplotlib
matplotlib.rcParams['font.family'] = 'DejaVu Sans'
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt
from datetime import datetime
import time
import serial

# ================== Serial Config ==================
INDOOR_PORT = "/dev/ttyAMA0"   # ปรับตามบอร์ด
OUTDOOR_PORT = "/dev/ttyAMA2"  # ปรับตามบอร์ด
BAUDRATE = 9600
TIMEOUT = 0  # non-blocking

# ================== GPIO / Relay Config ==================
ACTIVE_LOW = False
RELAY_PINS = [17, 18, 27, 22]  # BCM numbering
RELAY_NAMES = {17: "Purifier", 18: "Fan", 27: "Vent", 22: "Spare"}

# พยายามใช้ gpiozero (รองรับ Pi 5 + lgpio); ตกไป RPi.GPIO; สุดท้าย mock
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

# ================== UI Tunables ==================
UI_UPDATE_MS = 500          # อัปเดต UI ทุก 0.5s
CHART_EVERY_N_TICKS = 2     # วาดกราฟทุก 1s (เบาเครื่อง)
HISTORY_MAX = 120           # เก็บล่าสุด ~60 วินาที (ที่ 0.5s/จุด)

# ================== UI Helpers ==================
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

# ================== GPIO Relay Controller ==================
class RelayController:
    def __init__(self, pins, active_low=True):
        self.pins = list(pins)
        self.active_low = bool(active_low)
        self.states = {p: False for p in pins}
        self.backend = None
        self._devices = {}
        # gpiozero
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
        # RPi.GPIO
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
        # mock
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
                # สำคัญ: ปิดโรงงานขา ป้องกัน "GPIO busy" บน Pi 5
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

# ================== PMS Reader (non-blocking, ATM) ==================
class PMSReader:
    """
    อ่าน PMSx003/x3005 แบบ non-blocking (timeout=0) แล้ว parse ในบัฟเฟอร์เอง
    ใช้ค่า Atmospheric (ATM): bytes 10..15 -> PM1/PM2.5/PM10 (big-endian)
    """
    def __init__(self, port: str):
        try:
            self.ser = serial.Serial(port, baudrate=BAUDRATE, timeout=TIMEOUT)
            try: self.ser.reset_input_buffer()
            except: pass
            self.buf = bytearray()
            self.last = {"pm1": 0.0, "pm25": 0.0, "pm10": 0.0}
            self.last_ts = 0.0
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
                # เก็บท้ายไว้ 1 ไบต์เผื่อหัวเฟรมขาด
                if len(self.buf) > 1:
                    self.buf = self.buf[-1:]
                break
            if len(self.buf) - j < 32:
                # รอข้อมูลเพิ่ม
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
                self.last_ts = time.time()
            i = 0

    def poll(self):
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

    def read_once(self):
        return self.poll()

    def close(self):
        try:
            if self.ser: self.ser.close()
        except Exception:
            pass

# ================== Main App ==================
class PMDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Air Quality Dashboard")
        self.root.geometry("1280x900")
        self._setup_style()

        # Serial readers
        self.reader_indoor = PMSReader(INDOOR_PORT)
        self.reader_outdoor = PMSReader(OUTDOOR_PORT)

        # Relay controller
        self.relays = RelayController(RELAY_PINS, active_low=ACTIVE_LOW)

        # Auto control vars
        self.auto_enabled = tk.BooleanVar(value=False)
        self.auto_source = tk.StringVar(value="Indoor")  # Indoor/Outdoor
        self.auto_on_threshold = tk.DoubleVar(value=35.0)
        self.auto_hysteresis = tk.DoubleVar(value=5.0)

        # Header
        header = ttk.Frame(root, padding=(16, 12))
        header.pack(fill="x")
        title = ttk.Label(header, text="Indoor & Outdoor Air Quality", font=("Kanit", 28, "bold"))
        self.last_lbl = ttk.Label(header, text="Last update: -", font=("Kanit", 12))
        title.pack(side="left"); self.last_lbl.pack(side="right")

        # Sections
        content = ttk.Frame(root, padding=(12, 0)); content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1); content.columnconfigure(1, weight=1)
        self.indoor = Section(content, "Indoor"); self.outdoor = Section(content, "Outdoor")
        self.indoor.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        self.outdoor.grid(row=0, column=1, padx=8, pady=8, sticky="nsew")

        # Relay Control panel
        self._build_relay_panel()

        # Trend Chart
        chart_frame = ttk.Frame(root, padding=(12, 4)); chart_frame.pack(fill="both", expand=True)
        self.indoor_history, self.outdoor_history, self.time_history = [], [], []
        self.fig, self.ax = plt.subplots(figsize=(10, 4), facecolor="#0F0F1A")
        self.ax.set_facecolor("#0F0F1A")
        self.ax.tick_params(colors='white')
        self.ax.set_title("PM2.5 Trend (last ~60s)", color="white", fontsize=14)
        self.ax.set_ylabel("µg/m³", color="white"); self.ax.set_xlabel("Time", color="white")
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # Safe timer: ตัวเดียวถาวร
        self._running = True
        self._tick = 0
        self._after_cb = self._on_timer
        self.job = self.root.after(UI_UPDATE_MS, self._after_cb)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _setup_style(self):
        self.root.configure(bg="#0F0F1A")
        style = ttk.Style(self.root)
        try: style.theme_use('clam')
        except tk.TclError: pass
        style.configure("TFrame", background="#0F0F1A")
        style.configure("TLabelframe", background="#0F0F1A", foreground="white", font=("Kanit", 16, "bold"))
        style.configure("TLabelframe.Label", background="#0F0F1A", foreground="#e0e0e0")
        style.configure("TLabel", background="#0F0F1A", foreground="white")
        style.configure("TButton", padding=8)
        style.configure("TCheckbutton", background="#0F0F1A", foreground="white")
        style.configure("TMenubutton", background="#0F0F1A", foreground="white")
        style.configure("TSpinbox", fieldbackground="#1c1c2b", foreground="white", background="#0F0F1A")
        style.configure("TProgressbar", troughcolor="#1c1c2b", background="#00bcd4",
                        bordercolor="#1c1c2b", lightcolor="#00bcd4", darkcolor="#00bcd4")

    def _build_relay_panel(self):
        panel = ttk.Labelframe(self.root, text="Relay Control", padding=12); panel.pack(fill="x", padx=12, pady=6)
        for i in range(len(RELAY_PINS) + 4): panel.columnconfigure(i, weight=1)
        self.relay_btns = {}
        for idx, pin in enumerate(RELAY_PINS):
            name = RELAY_NAMES.get(pin, f"Pin {pin}")
            b = ttk.Button(panel, text=f"{name} (Pin {pin}): OFF", command=lambda p=pin: self._toggle_relay(p))
            b.grid(row=0, column=idx, padx=6, pady=6, sticky="ew"); self.relay_btns[pin] = b
        ttk.Button(panel, text="All ON", command=lambda: self._set_all_relays(True)).grid(row=0, column=len(RELAY_PINS), padx=6, pady=6, sticky="ew")
        ttk.Button(panel, text="All OFF", command=lambda: self._set_all_relays(False)).grid(row=0, column=len(RELAY_PINS)+1, padx=6, pady=6, sticky="ew")

        auto = ttk.Frame(panel); auto.grid(row=1, column=0, columnspan=len(RELAY_PINS)+2, sticky="ew", pady=(8,0))
        for i in range(10): auto.columnconfigure(i, weight=1)
        ttk.Checkbutton(auto, text="Auto mode", variable=self.auto_enabled).grid(row=0, column=0, sticky="w")
        ttk.Label(auto, text="Source:").grid(row=0, column=1, sticky="e", padx=(12,4))
        ttk.OptionMenu(auto, self.auto_source, self.auto_source.get(), "Indoor", "Outdoor").grid(row=0, column=2, sticky="w")
        ttk.Label(auto, text="On threshold (µg/m³):").grid(row=0, column=3, sticky="e", padx=(12,4))
        ttk.Spinbox(auto, from_=0, to=500, increment=1, textvariable=self.auto_on_threshold, width=6).grid(row=0, column=4, sticky="w")
        ttk.Label(auto, text="Hysteresis (µg/m³):").grid(row=0, column=5, sticky="e", padx=(12,4))
        ttk.Spinbox(auto, from_=0, to=100, increment=1, textvariable=self.auto_hysteresis, width=6).grid(row=0, column=6, sticky="w")
        self.auto_state_lbl = ttk.Label(auto, text="Auto state: idle"); self.auto_state_lbl.grid(row=0, column=9, sticky="e")

    # ---------- Timer driver ----------
    def _on_timer(self):
        """ตัวจับเวลาถาวร: เรียก update_data แล้วนัดรอบถัดไปด้วย callback ตัวเดิม"""
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

    # ---------- Relay helpers ----------
    def _toggle_relay(self, pin):
        self.relays.toggle(pin); self._refresh_relay_text(pin)

    def _set_all_relays(self, state: bool):
        self.relays.set_all(state)
        for pin in RELAY_PINS: self._refresh_relay_text(pin)

    def _refresh_relay_text(self, pin):
        state = self.relays.states.get(pin, False); name = RELAY_NAMES.get(pin, f"Pin {pin}")
        self.relay_btns[pin].config(text=f"{name} (Pin {pin}): {'ON' if state else 'OFF'}")

    # ---------- Sensor/Chart update ----------
    def _update_cards(self, section, data: dict):
        section.pm1.value_lbl.config(text=f"{data['pm1']:.1f} µg/m³")
        section.pm25.value_lbl.config(text=f"{data['pm25']:.1f} µg/m³")
        section.pm10.value_lbl.config(text=f"{data['pm10']:.1f} µg/m³")
        section.badge.update_badge(data['pm25'])

    def update_data(self):
        indoor = self.reader_indoor.read_once()
        outdoor = self.reader_outdoor.read_once()

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_lbl.config(text=f"Last update: {ts}")

        self._update_cards(self.indoor, indoor)
        self._update_cards(self.outdoor, outdoor)

        # Auto control (hysteresis)
        self._auto_control(indoor, outdoor)

        # History for chart
        current_time = datetime.now().strftime("%H:%M:%S")
        self.time_history.append(current_time)
        self.indoor_history.append(indoor['pm25'])
        self.outdoor_history.append(outdoor['pm25'])
        if len(self.time_history) > HISTORY_MAX:
            self.time_history.pop(0); self.indoor_history.pop(0); self.outdoor_history.pop(0)

        # Draw chart every N ticks (ลดภาระ)
        # ----- วางแทนบล็อควาดกราฟเดิมใน update_data() -----
        self._tick += 1
        if self._tick % CHART_EVERY_N_TICKS == 0:
            self.ax.clear()
            self.ax.set_facecolor("#0F0F1A")
            self.ax.tick_params(colors='white')
            self.ax.grid(True, linestyle='--', alpha=0.3, color="#555555")
            self.ax.set_title("PM2.5 Trend (last ~60s)", color="white", fontsize=14)
            self.ax.set_ylabel("µg/m³", color="white")
            self.ax.set_xlabel("Time", color="white")

            # 1) ใช้แกน X เป็น index 0..N-1 เพื่อคุม tick ได้แม่น
            x = list(range(len(self.time_history)))

            # 2) วาดเส้น ลด marker ให้ขึ้นทุก k จุด (อ่านง่ายกว่า)
            MARK_EVERY = 6  # แสดง marker ทุก ~3 วินาที (ที่ 0.5s/จุด)
            self.ax.plot(x, self.indoor_history, linewidth=2, label="Indoor",
                        marker="o", markevery=MARK_EVERY, markersize=4)
            self.ax.plot(x, self.outdoor_history, linewidth=2, label="Outdoor",
                        marker="o", markevery=MARK_EVERY, markersize=4)

            # 3) จำกัดจำนวนฉลากแกน X (เช่น 8 ช่อง)
            n = len(x)
            if n > 0:
                TICKS = min(8, n)                          # จำนวนฉลากที่ต้องการ
                if TICKS == 1:
                    idxs = [0]
                else:
                    idxs = [round(i*(n-1)/(TICKS-1)) for i in range(TICKS)]
                self.ax.set_xticks(idxs)
                self.ax.set_xticklabels([self.time_history[i] for i in idxs],
                                        rotation=0, ha='center', color='white')

            self.ax.legend(facecolor="#0F0F1A", edgecolor="white",
                        fontsize=10, labelcolor="white")
            self.ax.margins(x=0)  # ไม่ให้เหลือขอบซ้าย/ขวาเยอะ
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
        self.auto_state_lbl.config(text=f"Auto state: {'ON' if desired_on else 'OFF'} | {source} PM2.5={pm:.1f} ≥ {on_th:.1f} (ON) / < {off_th:.1f} (OFF)")

    def on_close(self):
        # ยกเลิก timer ก่อน
        self._running = False
        if getattr(self, "job", None) is not None:
            try:
                self.root.after_cancel(self.job)
            except Exception:
                pass
            self.job = None
        # ปิด Serial/GPIO
        try: self.reader_indoor.close()
        except Exception: pass
        try: self.reader_outdoor.close()
        except Exception: pass
        try: self.relays.cleanup()
        except Exception: pass
        # ปิดหน้าต่าง
        try: self.root.destroy()
        except Exception: pass

if __name__ == "__main__":
    root = tk.Tk()
    app = PMDashboard(root)
    root.mainloop()
