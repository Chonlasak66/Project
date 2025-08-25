import serial
import time

# PMS3005 ตัวแรก: UART0
ser1 = serial.Serial('/dev/ttyAMA0', baudrate=9600, timeout=2)

# PMS3005 ตัวที่สอง: UART2
ser2 = serial.Serial('/dev/ttyAMA2', baudrate=9600, timeout=2)

def read_pms(ser):
    start_time = time.time()
    while ser.in_waiting < 32:
        if time.time() - start_time > 3:  # Timeout 3 วินาที
            return None
        time.sleep(0.1)

    data = ser.read(32)

    if len(data) == 32 and data[0] == 0x42 and data[1] == 0x4D:
        pm1_0 = (data[10] << 8) | data[11]
        pm2_5 = (data[12] << 8) | data[13]
        pm10  = (data[14] << 8) | data[15]
        return pm1_0, pm2_5, pm10
    return None


while True:
    vals1 = read_pms(ser1)
    vals2 = read_pms(ser2)

    if vals1:
        print(f"Sensor1 (/dev/ttyAMA0) → PM1.0: {vals1[0]}, PM2.5: {vals1[1]}, PM10: {vals1[2]}")
    if vals2:
        print(f"Sensor2 (/dev/ttyAMA2) → PM1.0: {vals2[0]}, PM2.5: {vals2[1]}, PM10: {vals2[2]}")

    print("-" * 50)
    time.sleep(1)
