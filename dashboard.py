import tkinter as tk
from tkinter import ttk
import matplotlib
matplotlib.rcParams['font.family'] = 'DejaVu Sans'  # English-safe font
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt
from datetime import datetime
import serial

# ---------------- Serial Config (no CSV) ----------------
# Adjust ports if different on your hardware
INDOOR_PORT = "/dev/ttyAMA0"   # GPIO14/15
OUTDOOR_PORT = "/dev/ttyAMA2"  # GPIO4/5
BAUDRATE = 9600
TIMEOUT = 1

# PMS3005 reader
class PMSReader:
    def __init__(self, port: str):
        try:
            self.ser = serial.Serial(port, baudrate=BAUDRATE, timeout=TIMEOUT)
            self.ok = True
        except Exception as e:
            print(f"[WARN] Cannot open serial {port}: {e}")
            self.ser = None
            self.ok = False

    def read_once(self):
        """Return dict {pm1, pm25, pm10} or fallback zeros if not available."""
        if not self.ok:
            return {"pm1": 0.0, "pm25": 0.0, "pm10": 0.0}
        try:
            data = self.ser.read(32)
            if len(data) == 32 and data[0] == 0x42 and data[1] == 0x4D:
                pm1   = int.from_bytes(data[4:6],  'big')
                pm25  = int.from_bytes(data[6:8],  'big')
                pm10  = int.from_bytes(data[8:10], 'big')
                return {"pm1": float(pm1), "pm25": float(pm25), "pm10": float(pm10)}
        except Exception as e:
            print(f"[WARN] Serial read error: {e}")
        return {"pm1": 0.0, "pm25": 0.0, "pm10": 0.0}

    def close(self):
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass

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
        # Use explicit bg to avoid ttk background issues
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

class PMDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Air Quality Dashboard")
        self.root.geometry("1280x820")
        self.job = None
        self._setup_style()

        # Serial readers (no CSV)
        self.reader_indoor = PMSReader(INDOOR_PORT)
        self.reader_outdoor = PMSReader(OUTDOOR_PORT)

        # Header
        header = ttk.Frame(root, padding=(16, 12))
        header.pack(fill="x")
        title = ttk.Label(header, text="Indoor & Outdoor Air Quality", font=("Kanit", 28, "bold"))
        self.last_lbl = ttk.Label(header, text="Last update: -", font=("Kanit", 12))
        title.pack(side="left")
        self.last_lbl.pack(side="right")

        # Content
        content = ttk.Frame(root, padding=(12, 0))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        self.indoor = Section(content, "Indoor")
        self.outdoor = Section(content, "Outdoor")
        self.indoor.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        self.outdoor.grid(row=0, column=1, padx=8, pady=8, sticky="nsew")

        # Trend Chart (English labels)
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

        # schedule updates
        self.update_data()  # immediate first draw
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
        style.configure("TProgressbar", troughcolor="#1c1c2b", background="#00bcd4", bordercolor="#1c1c2b", lightcolor="#00bcd4", darkcolor="#00bcd4")

    def _update_cards(self, section: Section, data: dict):
        section.pm1.value_lbl.config(text=f"{data['pm1']:.1f} µg/m³")
        section.pm25.value_lbl.config(text=f"{data['pm25']:.1f} µg/m³")
        section.pm10.value_lbl.config(text=f"{data['pm10']:.1f} µg/m³")
        section.badge.update_badge(data['pm25'])

    def update_data(self):
        # Read from serial directly
        indoor = self.reader_indoor.read_once()
        outdoor = self.reader_outdoor.read_once()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_lbl.config(text=f"Last update: {ts}")
        self._update_cards(self.indoor, indoor)
        self._update_cards(self.outdoor, outdoor)

        # Keep history
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

        # Reschedule safely
        if self.root.winfo_exists():
            self.job = self.root.after(5000, self.update_data)

    def on_close(self):
        # Cancel scheduled job to avoid "invalid command name ...update_data"
        if self.job is not None:
            try:
                self.root.after_cancel(self.job)
            except Exception:
                pass
        # Close serials
        self.reader_indoor.close()
        self.reader_outdoor.close()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = PMDashboard(root)
    root.mainloop()
