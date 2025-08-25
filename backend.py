# -*- coding: utf-8 -*-
"""
PM2.5 Backend (RTDB only: pm_readings)
- PMSx003/3005 2 ตัว (INDOOR/OUTDOOR), non-blocking, ใช้ค่า ATM (bytes 10..15)
- CSV สำรอง
- RTDB (pm_readings) แบบลดความถี่: avg ทุก RTDB_PUSH_SECS หรือเมื่อ ΔPM2.5 ≥ RTDB_CHANGE_DELTA
- ไม่มี pm_readings_1m

ENV (ตัวอย่าง):
  FIREBASE_CREDENTIALS=/etc/pm25/firebase-adminsdk.json
  FIREBASE_RTDB_URL=https://<project>-default-rtdb.<region>.firebasedatabase.app
  READ_INTERVAL_SEC=1
  RTDB_PUSH_SECS=5
  RTDB_CHANGE_DELTA=3.0
  RTDB_BATCH_SIZE=50
  RTDB_FLUSH_SECS=3
  SINK_MAX_BUFFER=5000
  CSV_FILE=pms3005_dual.csv
  INDOOR_PORT=/dev/ttyAMA0
  OUTDOOR_PORT=/dev/ttyAMA2
  DEVICE_ID=<ถ้าอยากกำหนดเอง>
"""
import os, sys, time, signal, atexit, csv, logging, re, math, socket
from pathlib import Path
from datetime import datetime, timezone
import serial

# ---------- Logging ----------
logging.basicConfig(filename='pms_backend.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("pms-backend")

# ---------- Config ----------
INDOOR_PORT = os.getenv("INDOOR_PORT", "/dev/ttyAMA0")
OUTDOOR_PORT = os.getenv("OUTDOOR_PORT", "/dev/ttyAMA2")
BAUDRATE = int(os.getenv("BAUDRATE", "9600"))
TIMEOUT = 0  # non-blocking
READ_INTERVAL_SEC = float(os.getenv("READ_INTERVAL_SEC", "1.0"))

CSV_FILE = os.getenv("CSV_FILE", "pms3005_dual.csv")

def _auto_device_id():
    v = os.getenv("DEVICE_ID")
    if v and v.strip(): return v.strip()
    try:
        hn = socket.gethostname().strip()
        if hn: return hn
    except Exception:
        pass
    try:
        mid = Path("/etc/machine-id").read_text().strip()
        if mid: return mid[:8]
    except Exception:
        pass
    return "unknown"

DEVICE_ID = _auto_device_id()

FIREBASE_CREDS = os.getenv("FIREBASE_CREDENTIALS") or str(Path(__file__).with_name("firebase-adminsdk.json"))
FIREBASE_RTDB_URL = os.getenv("FIREBASE_RTDB_URL", "")
RTDB_ROOT = os.getenv("RTDB_ROOT", "pm_readings")
RTDB_PUSH_SECS = float(os.getenv("RTDB_PUSH_SECS", "5"))
RTDB_CHANGE_DELTA = float(os.getenv("RTDB_CHANGE_DELTA", "3.0"))
RTDB_BATCH_SIZE = int(os.getenv("RTDB_BATCH_SIZE", "50"))
RTDB_FLUSH_SECS = int(os.getenv("RTDB_FLUSH_SECS", "3"))
SINK_MAX_BUFFER = int(os.getenv("SINK_MAX_BUFFER", "5000"))

# ---------- CSV ----------
def ensure_csv_header(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode='a', newline='') as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow([
                "timestamp_iso_utc",
                "indoor_PM1.0","indoor_PM2.5","indoor_PM10",
                "outdoor_PM1.0","outdoor_PM2.5","outdoor_PM10",
                "device_id"
            ])
def append_csv(path, row):
    with open(path, mode='a', newline='') as f:
        csv.writer(f).writerow(row)
ensure_csv_header(CSV_FILE)

# ---------- Optional Firebase deps ----------
try:
    import firebase_admin
    from firebase_admin import credentials, db as rtdb
    _fb_ok = True
except Exception as e:
    log.warning(f"firebase-admin not available: {e}")
    _fb_ok = False

# ---------- PMS Reader (non-blocking, ATM) ----------
class PMSStreamReader:
    def __init__(self, port: str):
        self.port = port
        self.ser = None
        self.buf = bytearray()
        self.last = (float('nan'), float('nan'), float('nan'))
        self._open()
    def _open(self):
        try:
            self.ser = serial.Serial(self.port, baudrate=BAUDRATE, timeout=TIMEOUT)
            try: self.ser.reset_input_buffer()
            except Exception: pass
            log.info(f"Serial opened: {self.port}")
        except Exception as e:
            self.ser = None
            log.warning(f"Cannot open serial {self.port}: {e}")
    def _parse_frames(self):
        i = 0
        while True:
            j = self.buf.find(b'\x42\x4D', i)
            if j < 0:
                if len(self.buf) > 1: self.buf = self.buf[-1:]
                break
            if len(self.buf) - j < 32:
                if j > 0: self.buf = self.buf[j:]
                break
            frame = self.buf[j:j+32]; self.buf = self.buf[j+32:]
            if frame[0] == 0x42 and frame[1] == 0x4D:
                pm1  = int.from_bytes(frame[10:12], 'big')
                pm25 = int.from_bytes(frame[12:14], 'big')
                pm10 = int.from_bytes(frame[14:16], 'big')
                self.last = (float(pm1), float(pm25), float(pm10))
            i = 0
    def read(self):
        if self.ser is None:
            self._open(); return self.last
        try:
            n = self.ser.in_waiting
            if n:
                self.buf += self.ser.read(n)
                self._parse_frames()
        except Exception as e:
            log.warning(f"Serial read error on {self.port}: {e}")
            try: self.ser.close()
            except Exception: pass
            self.ser = None
        return self.last
    def close(self):
        try:
            if self.ser: self.ser.close()
        except Exception: pass

# ---------- RTDB Sink (pm_readings) ----------
class RTDBSink:
    """
    เขียน Realtime Database แบบบัฟเฟอร์ → update หลายพาธครั้งเดียว
    path: /<root>/<DEVICE_ID>/<sensor>/<YYYYMMDD>/<HHMMSS> => { ts, pm1, pm25, pm10, device_id, n, min25, max25 }
    """
    def __init__(self, cred_path, db_url, root, batch_size=50, flush_secs=3, max_buffer=5000):
        self.enabled = _fb_ok and bool(db_url)
        self.root = root.strip("/")
        self.buffer, self.last_flush = [], time.time()
        self.batch_size, self.flush_secs = batch_size, flush_secs
        self.max_buffer = max_buffer
        self._ref = None
        if not self.enabled:
            log.warning("RTDB sink disabled: check firebase-admin/URL"); return
        try:
            if not firebase_admin._apps:
                firebase_admin.initialize_app(credentials.Certificate(cred_path), {"databaseURL": db_url})
            self._ref = rtdb.reference("/" + self.root, url=db_url)
            atexit.register(self.flush)
            log.info(f"RTDB sink enabled root='/{self.root}', batch={batch_size}, flush={flush_secs}s")
        except Exception as e:
            self.enabled = False
            log.error("RTDB init failed; CSV-only. %s", e)
    def _path(self, ts_iso, sensor):
        ts_clean = ts_iso.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts_clean)
            ymd = dt.strftime("%Y%m%d"); hms = dt.strftime("%H%M%S")
        except Exception:
            digits = re.sub(r"[^0-9T]", "", ts_iso)
            ymd = digits[:8] if len(digits) >= 8 else "19700101"
            hms = digits[-6:] if len(digits) >= 6 else "000000"
        dev = DEVICE_ID or "unknown"
        return f"{dev}/{sensor}/{ymd}/{hms}"
    def put(self, row: dict):
        if not (self.enabled and self._ref): return
        self.buffer.append(row)
        # cap buffer กัน RAM โตเมื่อเน็ตล่มนาน ๆ
        if len(self.buffer) > self.max_buffer:
            drop = len(self.buffer) - self.max_buffer
            del self.buffer[:drop]
            log.warning("Buffer capped at %d (dropped %d oldest rows)", self.max_buffer, drop)
        now = time.time()
        if len(self.buffer) >= self.batch_size or (now - self.last_flush) >= self.flush_secs:
            self.flush()
    def flush(self):
        if not (self.enabled and self._ref and self.buffer): return
        try:
            updates = {}
            for r in self.buffer:
                path = self._path(r["ts"], r["sensor"])
                updates[path] = {**r, "device_id": DEVICE_ID}
            self._ref.update(updates)
            n = len(updates)
            self.buffer.clear()
            self.last_flush = time.time()
            log.info("RTDB flush OK (%d rows)", n)
        except Exception as e:
            log.warning("RTDB flush failed (buffer kept): %s", e)

# ---------- Rate limiter (avg window + change trigger) ----------
class _Agg:
    def __init__(self, push_secs, change_delta):
        self.push_secs = float(push_secs)
        self.change_delta = float(change_delta)
        self.reset()
        self.last_published_pm25 = math.nan
        self.last_pub_ts = 0.0
    def reset(self):
        self.start_ts = time.time()
        self.sum1 = self.sum25 = self.sum10 = 0.0
        self.min25 = float('inf'); self.max25 = float('-inf')
        self.n = 0
    def add(self, pm1, pm25, pm10):
        if any(math.isnan(x) for x in (pm1, pm25, pm10)): return
        self.sum1 += pm1; self.sum25 += pm25; self.sum10 += pm10; self.n += 1
        self.min25 = min(self.min25, pm25); self.max25 = max(self.max25, pm25)
    def should_emit(self):
        if self.n == 0: return False
        now = time.time()
        time_ok = (now - self.last_pub_ts) >= self.push_secs
        delta_ok = False
        if not math.isnan(self.last_published_pm25):
            avg25 = self.sum25 / self.n
            delta_ok = abs(avg25 - self.last_published_pm25) >= self.change_delta
        return time_ok or delta_ok
    def emit(self, sensor, ts_iso):
        if self.n == 0: return None
        avg1  = self.sum1 / self.n
        avg25 = self.sum25 / self.n
        avg10 = self.sum10 / self.n
        row = {"ts": ts_iso, "sensor": sensor, "pm1": avg1, "pm25": avg25, "pm10": avg10,
               "n": self.n, "min25": self.min25, "max25": self.max25}
        self.last_published_pm25 = avg25
        self.last_pub_ts = time.time()
        self.reset()
        return row

# ---------- Cleanup ----------
_closed = False
def cleanup():
    global _closed
    if _closed: return
    _closed = True
    try: rtdb_sink.flush()
    except Exception: pass
    try: reader_in.close()
    except Exception: pass
    try: reader_out.close()
    except Exception: pass
    log.info("Cleanup done.")
atexit.register(cleanup)
def _sig_exit(signum, frame):
    cleanup(); sys.exit(0)
for _sig in (signal.SIGINT, signal.SIGTERM):
    try: signal.signal(_sig, _sig_exit)
    except Exception: pass

# ---------- Init ----------
reader_in  = PMSStreamReader(INDOOR_PORT)
reader_out = PMSStreamReader(OUTDOOR_PORT)
rtdb_sink  = RTDBSink(FIREBASE_CREDS, FIREBASE_RTDB_URL, RTDB_ROOT,
                      batch_size=RTDB_BATCH_SIZE, flush_secs=RTDB_FLUSH_SECS,
                      max_buffer=SINK_MAX_BUFFER)

agg_in  = _Agg(RTDB_PUSH_SECS, RTDB_CHANGE_DELTA)
agg_out = _Agg(RTDB_PUSH_SECS, RTDB_CHANGE_DELTA)

print(f"Starting PMS backend → CSV + RTDB({RTDB_PUSH_SECS}s avg/Δ{RTDB_CHANGE_DELTA}) "
      f"as DEVICE_ID='{DEVICE_ID}'. Read every {READ_INTERVAL_SEC}s. Ctrl+C to stop.")
log.info("Program started.")

# ---------- Main loop ----------
try:
    while True:
        pm_in  = reader_in.read()
        pm_out = reader_out.read()

        ts_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')
        append_csv(CSV_FILE, [ts_iso, *pm_in, *pm_out, DEVICE_ID])

        # สะสมค่าเพื่อตัดสินใจส่งขึ้น RTDB
        agg_in.add(*pm_in); agg_out.add(*pm_out)

        emit_in  = agg_in.emit("indoor", ts_iso)  if agg_in.should_emit()  else None
        emit_out = agg_out.emit("outdoor", ts_iso) if agg_out.should_emit() else None
        for row in (emit_in, emit_out):
            if row: rtdb_sink.put(row)

        print(f"{ts_iso} | In PM2.5={pm_in[1]} | Out PM2.5={pm_out[1]}")
        time.sleep(READ_INTERVAL_SEC)
except KeyboardInterrupt:
    print("\nStopped by user.")
finally:
    cleanup()
