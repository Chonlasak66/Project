import serial
import time
import csv
from datetime import datetime
import logging

# Setup logging
logging.basicConfig(filename='pms_backend.log',
                    level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Serial setup สำหรับ PMS3005
ser_indoor = serial.Serial("/dev/ttyAMA0", baudrate=9600, timeout=1)  # GPIO14/15
ser_outdoor = serial.Serial("/dev/ttyAMA2", baudrate=9600, timeout=1) # GPIO4/5

CSV_FILE = "pms3005_dual.csv"

# สร้าง CSV ถ้าไฟล์ไม่อยู่
with open(CSV_FILE, mode='a', newline='') as file:
    writer = csv.writer(file)
    if file.tell() == 0:
        writer.writerow([
            "timestamp",
            "sensor_indoor_PM1.0", "sensor_indoor_PM2.5", "sensor_indoor_PM10",
            "sensor_outdoor_PM1.0", "sensor_outdoor_PM2.5", "sensor_outdoor_PM10"
        ])

def read_pms(ser):
    """อ่านข้อมูลจาก PMS3005 คืนค่า (pm1, pm2_5, pm10) หรือ NaN ถ้าไม่มีข้อมูล"""
    data = ser.read(32)
    if len(data) == 32 and data[0] == 0x42 and data[1] == 0x4d:
        pm1 = int.from_bytes(data[4:6], 'big')
        pm2_5 = int.from_bytes(data[6:8], 'big')
        pm10 = int.from_bytes(data[8:10], 'big')
        return pm1, pm2_5, pm10
    else:
        return float('nan'), float('nan'), float('nan')

print("Starting PMS3005 dual sensor logging... (Press Ctrl+C to stop)")

try:
    while True:
        vals_indoor = read_pms(ser_indoor)
        vals_outdoor = read_pms(ser_outdoor)

        # Timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Log warnings ถ้ามีค่า NaN
        if any([v != v for v in vals_indoor + vals_outdoor]):  # NaN check
            logging.warning("Missing or invalid sensor data: Indoor=%s, Outdoor=%s", vals_indoor, vals_outdoor)
        else:
            logging.info("Sensor read successful: Indoor=%s, Outdoor=%s", vals_indoor, vals_outdoor)

        # แจ้งเตือน PM2.5 สูง
        if vals_indoor[1] > 50:
            logging.warning("High PM2.5 indoor: %.2f µg/m³", vals_indoor[1])
        if vals_outdoor[1] > 50:
            logging.warning("High PM2.5 outdoor: %.2f µg/m³", vals_outdoor[1])

        # บันทึกลง CSV
        row = [timestamp, *vals_indoor, *vals_outdoor]
        with open(CSV_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(row)

        # แสดงผลบนหน้าจอ
        print(f"{timestamp} | Indoor PM2.5={vals_indoor[1]} | Outdoor PM2.5={vals_outdoor[1]}")

        time.sleep(10)  # อ่านทุก 10 วินาที

except KeyboardInterrupt:
    print("\nLogging stopped by user.")
    logging.info("Program stopped by user")

finally:
    ser_indoor.close()
    ser_outdoor.close()
