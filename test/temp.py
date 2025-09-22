import sys, time, math

def fmt(x, unit):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return f"- {unit}"
    return f"{x:.2f} {unit}"

def try_adafruit(addr):
    try:
        import board, busio
        import adafruit_bme280
        i2c = busio.I2C(board.SCL, board.SDA)
        bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=addr)
        return ("adafruit", bme)
    except Exception as e:
        return (None, e)

def try_smbus2(addr):
    try:
        import smbus2, bme280
        bus = smbus2.SMBus(1)
        cal = bme280.load_calibration_params(bus, addr)
        return ("smbus2", (bus, addr, cal))
    except Exception as e:
        return (None, e)

def read_smbus2(pack):
    import bme280
    bus, addr, cal = pack
    s = bme280.sample(bus, addr, cal)
    return float(s.temperature), float(s.humidity), float(s.pressure)

def main():
    addr = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x76

    backend, obj = try_adafruit(addr)
    if backend is None:
        backend, obj = try_smbus2(addr)

    if backend is None:
        print("[ERROR] ไม่พบ backend สำหรับ BME280:", obj)
        print("  - เช็กว่ามี 0x76/0x77 ใน i2cdetect หรือไม่")
        print("  - ติดตั้งไลบรารี adafruit-circuitpython-bme280 หรือ smbus2/RPi.bme280")
        sys.exit(1)

    print(f"[OK] ใช้ backend: {backend} @ 0x{addr:02X}")
    if backend == "adafruit":
        bme = obj
        while True:
            t = float(bme.temperature)      # °C
            h = float(bme.humidity)         # %
            p = float(bme.pressure)         # hPa
            print(f"T={fmt(t,'°C')}  RH={fmt(h,'%')}  P={fmt(p,'hPa')}")
            time.sleep(1)
    else:
        pack = obj
        while True:
            t, h, p = read_smbus2(pack)
            print(f"T={fmt(t,'°C')}  RH={fmt(h,'%')}  P={fmt(p,'hPa')}")
            time.sleep(1)

if __name__ == "__main__":
    main()
