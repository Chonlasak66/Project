from pms5003 import PMS5003
import time

# สร้างอินสแตนซ์ของ PMS5003
pms5003 = PMS5003()

try:
    while True:
        # อ่านข้อมูลจากเซ็นเซอร์
        data = pms5003.read()
        
        # แสดงผลข้อมูล PM1.0, PM2.5, PM10
        print(f"PM1.0: {data.pm_ug_per_m3(1.0)} µg/m³")
        print(f"PM2.5: {data.pm_ug_per_m3(2.5)} µg/m³")
        print(f"PM10: {data.pm_ug_per_m3(10)} µg/m³")
        
        # รอ 10 วินาทีก่อนอ่านครั้งถัดไป
        time.sleep(5)

except KeyboardInterrupt:
    print("หยุดการทำงาน")
finally:
    pms5003.close()