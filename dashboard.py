import tkinter as tk
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt
from datetime import datetime

CSV_FILE = "pms3005_dual.csv"

def get_sensor_data():
    try:
        df = pd.read_csv(CSV_FILE)
        last_row = df.iloc[-1]  # ดึงแถวล่าสุด
        return {
            "indoor": {
                "pm1": float(last_row["sensor1_PM1.0"]),
                "pm25": float(last_row["sensor1_PM2.5"]),
                "pm10": float(last_row["sensor1_PM10"]),
            },
            "outdoor": {
                "pm1": float(last_row["sensor2_PM1.0"]),
                "pm25": float(last_row["sensor2_PM2.5"]),
                "pm10": float(last_row["sensor2_PM10"]),
            }
        }
    except Exception as e:
        print("Error reading CSV:", e)
        return {
            "indoor": {"pm1": 0, "pm25": 0, "pm10": 0},
            "outdoor": {"pm1": 0, "pm25": 0, "pm10": 0}
        }

class PMDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Air Quality Dashboard")
        self.root.geometry("1200x800")
        self.root.configure(bg="#0F0F1A")

        self.indoor_history = []
        self.outdoor_history = []
        self.time_history = []

        self.create_widgets()
        self.update_data()

    def create_widgets(self):
        title_font = ("Kanit", 28, "bold")
        value_font = ("Kanit", 28, "bold")

        tk.Label(self.root, text="แสดงข้อมูลคุณภาพอากาศ (Indoor & Outdoor)",
                 font=title_font, fg="#00FFFF", bg="#0F0F1A").pack(pady=10)

        # Frame หลัก
        self.frame = tk.Frame(self.root, bg="#0F0F1A")
        self.frame.pack(pady=10)

        # Indoor
        self.indoor_frame = tk.LabelFrame(self.frame, text="Indoor",
                                          font=("Kanit", 18), bg="#0F0F1A", fg="white")
        self.indoor_frame.pack(side="left", padx=30)

        # Outdoor
        self.outdoor_frame = tk.LabelFrame(self.frame, text="Outdoor",
                                           font=("Kanit", 18), bg="#0F0F1A", fg="white")
        self.outdoor_frame.pack(side="left", padx=30)

        # Indoor cards
        self.indoor_pm1 = self.create_data_card(self.indoor_frame, "PM1.0", value_font, "#FF8C00")
        self.indoor_pm25 = self.create_data_card(self.indoor_frame, "PM2.5", value_font, "#FF8C00")
        self.indoor_pm10 = self.create_data_card(self.indoor_frame, "PM10", value_font, "#FF8C00")

        # Outdoor cards
        self.outdoor_pm1 = self.create_data_card(self.outdoor_frame, "PM1.0", value_font, "#1E90FF")
        self.outdoor_pm25 = self.create_data_card(self.outdoor_frame, "PM2.5", value_font, "#1E90FF")
        self.outdoor_pm10 = self.create_data_card(self.outdoor_frame, "PM10", value_font, "#1E90FF")

        # กราฟแนวโน้ม
        self.fig, self.ax = plt.subplots(figsize=(9, 4), facecolor="#0F0F1A")
        self.ax.set_facecolor("#0F0F1A")
        self.ax.tick_params(colors='white')
        self.ax.set_title("PM2.5 Trend", color="white", fontsize=16)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(pady=20)

    def create_data_card(self, parent, title, font, bg_color):
        frame = tk.Frame(parent, bg=bg_color, width=250, height=120, bd=2, relief="groove")
        frame.pack(padx=10, pady=10)
        frame.pack_propagate(False)

        tk.Label(frame, text=title, font=("Kanit", 16),
                 bg=bg_color, fg="white").pack(pady=(10, 0))
        value_label = tk.Label(frame, text="-- µg/m³", font=font,
                               bg=bg_color, fg="white")
        value_label.pack()

        return value_label

    def update_data(self):
        try:
            data = get_sensor_data()

            # Update indoor
            self.indoor_pm1.config(text=f"{data['indoor']['pm1']:.1f} µg/m³")
            self.indoor_pm25.config(text=f"{data['indoor']['pm25']:.1f} µg/m³")
            self.indoor_pm10.config(text=f"{data['indoor']['pm10']:.1f} µg/m³")

            # Update outdoor
            self.outdoor_pm1.config(text=f"{data['outdoor']['pm1']:.1f} µg/m³")
            self.outdoor_pm25.config(text=f"{data['outdoor']['pm25']:.1f} µg/m³")
            self.outdoor_pm10.config(text=f"{data['outdoor']['pm10']:.1f} µg/m³")

            # เก็บประวัติกราฟ
            current_time = datetime.now().strftime("%H:%M:%S")
            self.time_history.append(current_time)
            self.indoor_history.append(data['indoor']['pm25'])
            self.outdoor_history.append(data['outdoor']['pm25'])

            if len(self.time_history) > 20:
                self.time_history.pop(0)
                self.indoor_history.pop(0)
                self.outdoor_history.pop(0)

            # วาดกราฟ
            self.ax.clear()
            self.ax.plot(self.time_history, self.indoor_history,
                         color="#FFA500", marker="o", linewidth=2, label="Indoor")
            self.ax.plot(self.time_history, self.outdoor_history,
                         color="#00FFFF", marker="o", linewidth=2, label="Outdoor")

            # ตกแต่งแกนและกราฟ
            self.ax.set_title("PM2.5 Trend", color="white", fontsize=14)
            self.ax.set_facecolor("#0F0F1A")
            self.ax.tick_params(colors="white")
            self.ax.set_ylabel("µg/m³", color="white", fontsize=12)
            self.ax.set_xlabel("Time", color="white", fontsize=12)

            # Grid line
            self.ax.grid(True, linestyle="--", alpha=0.3, color="#555555")

            # Legend
            self.ax.legend(facecolor="#0F0F1A", edgecolor="white",
                           fontsize=10, labelcolor="white")

            self.fig.autofmt_xdate()
            self.canvas.draw()

        except Exception as e:
            print("Error in update:", e)

        if self.root.winfo_exists():
            self.root.after(5000, self.update_data)

if __name__ == "__main__":
    root = tk.Tk()
    app = PMDashboard(root)
    root.mainloop()
