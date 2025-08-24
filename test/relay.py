import RPi.GPIO as GPIO
import time

# ✅ กำหนด GPIO ขาที่ต่อกับ SSR
relay_pins = [17, 18, 27, 22]  # เปลี่ยนได้ตามการต่อจริง

# ✅ ตั้งค่า GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for pin in relay_pins:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)  # เริ่มต้น ปิด

print("🔁 เริ่มทดสอบรีเลย์: ทีละตัว และพร้อมกัน...")

try:
    while True:
        # 🔹 ทดสอบเปิดทีละตัว
        print("🔹 เปิด/ปิด ทีละตัว")
        for i, pin in enumerate(relay_pins):
            print(f"  👉 เปิด SSR {i+1} (GPIO {pin})")
            GPIO.output(pin, GPIO.HIGH)
            time.sleep(1)
            print(f"  ❌ ปิด SSR {i+1}")
            GPIO.output(pin, GPIO.LOW)
            time.sleep(0.5)

        # 🔸 ทดสอบเปิดพร้อมกัน
        print("🔸 เปิด SSR ทั้งหมดพร้อมกัน")
        for pin in relay_pins:
            GPIO.output(pin, GPIO.HIGH)
        time.sleep(2)

        print("❌ ปิด SSR ทั้งหมด")
        for pin in relay_pins:
            GPIO.output(pin, GPIO.LOW)
        time.sleep(2)

except KeyboardInterrupt:
    print("\n🛑 หยุดทดสอบด้วยคีย์บอร์ด")

finally:
    GPIO.cleanup()
    print("✅ เคลียร์ GPIO เรียบร้อยแล้ว")
