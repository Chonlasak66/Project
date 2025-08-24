import RPi.GPIO as GPIO
import time

# กำหนดขา GPIO ที่จะใช้ (เช่น 4 ขา)
RELAY_PINS = [17, 18, 27, 22]

# ตั้งค่า GPIO
GPIO.setmode(GPIO.BCM)
for pin in RELAY_PINS:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.HIGH)  # เริ่มต้นปิด relay

def relay_on(index):
    """เปิด relay ตาม index (0-3)"""
    GPIO.output(RELAY_PINS[index], GPIO.LOW)  # relay active-low
    print(f"Relay {index+1} ON")

def relay_off(index):
    """ปิด relay ตาม index (0-3)"""
    GPIO.output(RELAY_PINS[index], GPIO.HIGH)
    print(f"Relay {index+1} OFF")

def cleanup():
    GPIO.cleanup()
