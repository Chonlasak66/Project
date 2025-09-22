
# -*- coding: utf-8 -*-
"""
PM2.5 Backend (RAW PM + BME280) with Thai Time
- PMSx003/3005 2 ตัว (INDOOR/OUTDOOR), non-blocking, ใช้ค่า ATM (bytes 10..15)
- บันทึก CSV แบบหมุนไฟล์รายวันตาม "วันที่ไทย" (Asia/Bangkok) => CSV_DIR/YYYY-MM-DD.csv
- ส่งขึ้น Firebase Realtime DB แบบบัฟเฟอร์ (ถ้ามี firebase-admin และ RTDB URL)
- ปัดทศนิยม: PM เป็นจำนวนเต็ม, BME (T/RH/Pressure) 1 ตำแหน่ง
"""
# auto-load .env if present (no crash if package missing)
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(), override=False)  # ค่าใน ENV ระบบจะ "ชนะ" .env
except Exception:
    pass
import os, sys, time, signal, atexit, csv, logging, math, socket
from pathlib import Path
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    # Python เก่ากว่า: pip install backports.zoneinfo
    from backports.zoneinfo import ZoneInfo

TH_TZ = ZoneInfo("Asia/Bangkok")

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pm-backend")

# ---------- Config (ENV for general settings; TIMEZONE is fixed to TH_TZ) ----------
def env_str(key, default=""):
    return os.getenv(key, default)
def env_int(key, default=0):
    try: return int(os.getenv(key, str(default)))
    except Exception: return default
def env_float(key, default=0.0):
    try: return float(os.getenv(key, str(default)))
    except Exception: return default
def env_bool(key, default=True):
    val = os.getenv(key)
    if val is None: return default
    return val.strip().lower() in ("1","true","yes","on")
def env_hex(key, default="0x76"):
    try: return int(os.getenv(key, default), 16)
    except Exception: return int(default, 16)

CSV_DIR           = env_str("CSV_DIR", "csv_logs")
INDOOR_PORT       = env_str("INDOOR_PORT", "/dev/ttyAMA0")
OUTDOOR_PORT      = env_str("OUTDOOR_PORT", "/dev/ttyAMA2")
BAUDRATE          = env_int("BAUDRATE", 9600)
TIMEOUT           = 0  # non-blocking serial
READ_INTERVAL_SEC = env_int("READ_INTERVAL_SEC", 1)

BME280_ADDR       = env_hex("BME280_ADDR", "0x76")
BME280_ENABLED    = env_bool("BME280_ENABLED", True)

FIREBASE_CREDENTIALS = env_str("FIREBASE_CREDENTIALS", "/etc/pm25/firebase-adminsdk.json")
FIREBASE_RTDB_URL    = env_str("FIREBASE_RTDB_URL", "")  # ถ้าเว้นว่างจะไม่ส่ง RTDB
RTDB_ROOT            = env_str("RTDB_ROOT", "pm_readings")
RTDB_BATCH_SIZE      = env_int("RTDB_BATCH_SIZE", 50)
RTDB_FLUSH_SECS      = env_int("RTDB_FLUSH_SECS", 3)
SINK_MAX_BUFFER      = env_int("SINK_MAX_BUFFER", 5000)

DEVICE_ID            = env_str("DEVICE_ID", "")
if not DEVICE_ID:
    # หา DEVICE_ID อัตโนมัติ
    DEVICE_ID = (socket.gethostname() or "unknown").strip()

# ---------- Helpers ----------
def _r0(x):
    return x if (isinstance(x, float) and math.isnan(x)) else int(round(float(x)))
def _r1(x):
    return x if (isinstance(x, float) and math.isnan(x)) else round(float(x), 1)

# ---------- CSV ----------
def ensure_csv_header(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode='a', newline='') as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow([
                "timestamp_iso_th",
                "indoor_PM1.0","indoor_PM2.5","indoor_PM10",
                "outdoor_PM1.0","outdoor_PM2.5","outdoor_PM10",
                "bme_temp_C","bme_rh_%","bme_pressure_hPa",
                "device_id"
            ])

def append_csv(path, row):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode='a', newline='') as f:
        csv.writer(f).writerow(row)

def csv_path_for(dt):
    """
    คืน path ของไฟล์ CSV รายวัน: CSV_DIR/YYYY-MM-DD.csv
    ใช้ "วันที่ไทย" (Asia/Bangkok) เป็นเกณฑ์ในการหมุนไฟล์
    """
    d = dt.astimezone(TH_TZ).date()
    return os.path.join(CSV_DIR, f"{d.isoformat()}.csv")

CURRENT_CSV_FILE = None

# ---------- Optional Firebase deps ----------
try:
    import firebase_admin
    from firebase_admin import credentials, db as rtdb
    _fb_ok = True
except Exception as e:
    log.warning(f"firebase-admin not available: {e}")
    _fb_ok = False

# ---------- PMS Reader (non-blocking, ATM) ----------
try:
    import serial  # pyserial
except Exception as e:
    log.warning(f"pyserial not available: {e}")
    serial = None

class PMSStreamReader:
    def __init__(self, port: str):
        self.port = port
        self.ser = None
        self.buf = bytearray()
        self.last = (float('nan'), float('nan'), float('nan'))
        self._open()
    def _open(self):
        if serial is None:
            log.warning("serial module missing; PMS will remain NaN")
            return
        try:
            self.ser = serial.Serial(self.port, baudrate=BAUDRATE, timeout=TIMEOUT)
            try: self.ser.reset_input_buffer()
            except Exception: pass
            log.info(f"Opened serial {self.port}")
        except Exception as e:
            log.warning(f"Open serial failed on {self.port}: {e}")
            self.ser = None
    def _reopen_if_needed(self):
        if self.ser is None or not self.ser.is_open:
            self._open()
    def read(self):
        """Return (pm1, pm25, pm10) ATM as floats; NaN if unavailable"""
        try:
            self._reopen_if_needed()
            if self.ser is None: return self.last
            n = self.ser.in_waiting
            if n <= 0:
                return self.last
            self.buf.extend(self.ser.read(n))
            # หา header 0x42 0x4D
            while True:
                idx = self.buf.find(b"\x42\x4D")
                if idx < 0:
                    self.buf.clear()
                    break
                # ต้องมีอย่างน้อย 32 bytes
                if len(self.buf) - idx < 32:
                    # รออ่านเพิ่ม
                    if idx > 0: del self.buf[:idx]
                    break
                pkt = self.buf[idx:idx+32]
                # เอาเฉพาะ ATM PM1/2.5/10 (bytes 10..15)
                pm1  = (pkt[10] << 8) | pkt[11]
                pm25 = (pkt[12] << 8) | pkt[13]
                pm10 = (pkt[14] << 8) | pkt[15]
                self.last = (float(pm1), float(pm25), float(pm10))
                del self.buf[:idx+32]
                break
            return self.last
        except Exception as e:
            log.warning(f"PMS read error on {self.port}: {e}")
            self.last = (float('nan'), float('nan'), float('nan'))
            try:
                if self.ser: self.ser.close()
            except Exception: pass
            self.ser = None
            return self.last

# ---------- BME280 Reader ----------
class BME280Reader:
    """Optional BME280 reader. Tries Adafruit backend first, then smbus2.
    Returns (temp_C, rh_%, pressure_hPa) as floats; NaN if unavailable."""
    def __init__(self, addr=0x76, enabled=True):
        self.addr = addr
        self.enabled = enabled
        self.backend = None   # ("adafruit", bme) or ("smbus2", (bus, addr, cal))
        self._warned = False
        if enabled:
            self._init_backend()

    def _init_backend(self):
        # Try Adafruit
        try:
            import board, busio
            import adafruit_bme280
            i2c = busio.I2C(board.SCL, board.SDA)
            bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=self.addr)
            self.backend = ("adafruit", bme)
            log.info(f"BME280 via Adafruit backend @ 0x{self.addr:02X}")
            return
        except Exception:
            pass
        # Try smbus2
        try:
            import smbus2, bme280 as bme280_mod
            bus = smbus2.SMBus(1)
            cal = bme280_mod.load_calibration_params(bus, self.addr)
            self.backend = ("smbus2", (bus, self.addr, cal))
            log.info(f"BME280 via smbus2 backend @ 0x{self.addr:02X}")
            return
        except Exception:
            self.backend = None
            log.warning("BME280 backend not available")

    def read(self):
        if not self.enabled:
            return float('nan'), float('nan'), float('nan')
        if self.backend is None:
            if not self._warned:
                log.warning("BME280 not initialized; will retry. Set BME280_ENABLED=0 to disable.")
                self._warned = True
            self._init_backend()
            if self.backend is None:
                return float('nan'), float('nan'), float('nan')
        kind, obj = self.backend
        try:
            if kind == "adafruit":
                bme = obj
                t = float(bme.temperature)
                h = float(bme.humidity)
                p = float(bme.pressure)
                return t, h, p
            else:
                import bme280 as bme280_mod
                bus, addr, cal = obj
                s = bme280_mod.sample(bus, addr, cal)
                return float(s.temperature), float(s.humidity), float(s.pressure)
        except Exception as e:
            log.warning(f"BME280 read error: {e}")
            return float('nan'), float('nan'), float('nan')

# ---------- RTDB Sink (pm_readings) ----------
class RTDBSink:
    """
    เขียน Realtime Database แบบบัฟเฟอร์ → update หลายพาธครั้งเดียว
    path: /<root>/<DEVICE_ID>/<sensor>/<YYYYMMDD>/<HHMMSS> => { ts, pm1, pm25, pm10, device_id, ... }
    """
    def __init__(self, cred_path, db_url, root, batch_size=50, flush_secs=3, max_buffer=5000):
        self.enabled = _fb_ok and bool(db_url)
        self.root = root.strip("/")
        self.buffer, self.last_flush = [], time.time()
        self.batch_size, self.flush_secs = batch_size, flush_secs
        self.max_buffer = max_buffer
        self._ref = None

        if self.enabled:
            try:
                if not firebase_admin._apps:
                    cred = credentials.Certificate(cred_path)
                    firebase_admin.initialize_app(cred, {"databaseURL": db_url})
                self._ref = rtdb.reference("/")
                log.info("Firebase RTDB initialized")
            except Exception as e:
                log.warning(f"Init Firebase failed: {e}")
                self.enabled = False

    def put(self, row: dict):
        if not self.enabled:
            return
        if len(self.buffer) >= self.max_buffer:
            # ตัดทิ้งจากหัว (กัน RAM พอง)
            self.buffer = self.buffer[-self.max_buffer//2:]
        self.buffer.append(row)
        now = time.time()
        if len(self.buffer) >= self.batch_size or (now - self.last_flush) >= self.flush_secs:
            self.flush()

    def flush(self):
        if not self.enabled or not self.buffer:
            return
        batch = self.buffer; self.buffer = []
        try:
            updates = {}
            for row in batch:
                ts_iso = row.get("ts","")
                sensor = row.get("sensor","misc")
                # path ตามเวลา "ไทย"
                try:
                    dt = datetime.fromisoformat(ts_iso)
                except Exception:
                    dt = datetime.now(TH_TZ)
                d = dt.astimezone(TH_TZ)
                ymd = f"{d.year:04d}{d.month:02d}{d.day:02d}"
                hms = f"{d.hour:02d}{d.minute:02d}{d.second:02d}"
                path = f"/{self.root}/{DEVICE_ID}/{sensor}/{ymd}/{hms}"
                data = dict(row)
                data["device_id"] = DEVICE_ID
                updates[path] = data
            if self._ref is not None:
                self._ref.update(updates)
            self.last_flush = time.time()
        except Exception as e:
            log.warning(f"RTDB flush failed: {e}")

# ---------- Cleanup ----------
def cleanup():
    try:
        pass
    finally:
        log.info("Cleanup done.")

atexit.register(cleanup)
signal.signal(signal.SIGTERM, lambda s,f: sys.exit(0))

# ---------- Main ----------
def main():
    print("[CONFIG]")
    print("  DEVICE_ID         =", DEVICE_ID)
    print("  CSV_DIR           =", CSV_DIR)
    print("  INDOOR_PORT       =", INDOOR_PORT)
    print("  OUTDOOR_PORT      =", OUTDOOR_PORT)
    print("  BME280_ADDR       = 0x%02X" % BME280_ADDR)
    print("  READ_INTERVAL     =", READ_INTERVAL_SEC, "s")
    print("  FIREBASE_URL      =", FIREBASE_RTDB_URL or "(disabled)")

    reader_in  = PMSStreamReader(INDOOR_PORT)
    reader_out = PMSStreamReader(OUTDOOR_PORT)
    bme_reader = BME280Reader(addr=BME280_ADDR, enabled=BME280_ENABLED)

    rtdb_sink = RTDBSink(
        cred_path=FIREBASE_CREDENTIALS,
        db_url=FIREBASE_RTDB_URL,
        root=RTDB_ROOT,
        batch_size=RTDB_BATCH_SIZE,
        flush_secs=RTDB_FLUSH_SECS,
        max_buffer=SINK_MAX_BUFFER,
    )

    print(f"Starting PMS backend → CSV + RTDB (RAW PM + BME280 every {READ_INTERVAL_SEC}s, Thai time) as DEVICE_ID='{DEVICE_ID}'. Ctrl+C to stop.")

    global CURRENT_CSV_FILE
    try:
        while True:
            pm_in  = reader_in.read()
            pm_out = reader_out.read()
            bme_t, bme_h, bme_p = bme_reader.read()

            # Thai time
            ts_dt  = datetime.now(TH_TZ)
            ts_iso = ts_dt.isoformat(timespec='seconds')

            # Daily rotation (Thai date)
            path_today = csv_path_for(ts_dt)
            if CURRENT_CSV_FILE != path_today:
                ensure_csv_header(path_today)
                CURRENT_CSV_FILE = path_today

            append_csv(CURRENT_CSV_FILE, [
                ts_iso,
                _r0(pm_in[0]), _r0(pm_in[1]), _r0(pm_in[2]),
                _r0(pm_out[0]), _r0(pm_out[1]), _r0(pm_out[2]),
                _r1(bme_t), _r1(bme_h), _r1(bme_p),
                DEVICE_ID
            ])

            # RTDB puts (indoor/outdoor always if valid; BME only if not NaN)
            if all(not math.isnan(x) for x in pm_in):
                rtdb_sink.put({"ts": ts_iso, "sensor": "indoor",
                               "pm1": _r0(pm_in[0]), "pm25": _r0(pm_in[1]), "pm10": _r0(pm_in[2]),
                               "n": 1, "min25": _r0(pm_in[1]), "max25": _r0(pm_in[1])})
            if all(not math.isnan(x) for x in pm_out):
                rtdb_sink.put({"ts": ts_iso, "sensor": "outdoor",
                               "pm1": _r0(pm_out[0]), "pm25": _r0(pm_out[1]), "pm10": _r0(pm_out[2]),
                               "n": 1, "min25": _r0(pm_out[1]), "max25": _r0(pm_out[1])})
            if not math.isnan(bme_t) and not math.isnan(bme_h):
                rtdb_sink.put({"ts": ts_iso, "sensor": "bme280",
                               "temp_c": _r1(bme_t), "rh": _r1(bme_h), "pressure_hPa": _r1(bme_p)})

            # Console line
            print(f"{ts_iso} | In PM2.5={_r0(pm_in[1])} | Out PM2.5={_r0(pm_out[1])} | T={_r1(bme_t)}°C RH={_r1(bme_h)}%")
            time.sleep(READ_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\nStopped by user.")

if __name__ == "__main__":
    main()
