# -*- coding: utf-8 -*-
import os, sys, signal, atexit, time, csv, logging, re
from pathlib import Path
from datetime import datetime, timezone
import serial

# ============== Logging ==============
logging.basicConfig(
    filename='pms_backend.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("pms-backend")

# ============== Serial (PMS) ==============
INDOOR_PORT = "/dev/ttyAMA0"
OUTDOOR_PORT = "/dev/ttyAMA2"
BAUDRATE = 9600
TIMEOUT = 1

def open_serial(port):
    try:
        return serial.Serial(port, baudrate=BAUDRATE, timeout=TIMEOUT)
    except Exception as e:
        log.warning(f"Cannot open serial {port}: {e}")
        return None

ser_indoor = open_serial(INDOOR_PORT)
ser_outdoor = open_serial(OUTDOOR_PORT)

def read_pms(ser):
    """อ่านเฟรม 32B PMSx003: header 0x42 0x4D, PM1/2.5/10 = bytes 4..9"""
    if ser is None:
        return float('nan'), float('nan'), float('nan')
    try:
        data = ser.read(32)
        if len(data) == 32 and data[0] == 0x42 and data[1] == 0x4D:
            pm1  = int.from_bytes(data[4:6],  'big')
            pm25 = int.from_bytes(data[6:8],  'big')
            pm10 = int.from_bytes(data[8:10], 'big')
            return float(pm1), float(pm25), float(pm10)
    except Exception as e:
        log.warning(f"Serial read error: {e}")
    return float('nan'), float('nan'), float('nan')

# ============== CSV (local backup) ==============
CSV_FILE = "pms3005_dual.csv"
def ensure_csv_header(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode='a', newline='') as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow([
                "timestamp_iso_utc",
                "sensor_indoor_PM1.0","sensor_indoor_PM2.5","sensor_indoor_PM10",
                "sensor_outdoor_PM1.0","sensor_outdoor_PM2.5","sensor_outdoor_PM10"
            ])
ensure_csv_header(CSV_FILE)

def append_csv(path, row):
    with open(path, mode='a', newline='') as f:
        csv.writer(f).writerow(row)

# ============== Firebase Admin / Sinks ==============
FIREBASE_CREDS = os.getenv("FIREBASE_CREDENTIALS") or str(Path(__file__).with_name("firebase-adminsdk.json"))

# --- Firestore toggle (ปิดเป็นค่าเริ่มต้นเมื่อย้ายไป RTDB) ---
USE_FIRESTORE = os.getenv("USE_FIRESTORE", "0") == "1"
FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "pm_readings")
FS_BATCH_SIZE = int(os.getenv("FS_BATCH_SIZE", "50"))
FS_FLUSH_SECS = int(os.getenv("FS_FLUSH_SECS", "30"))

# --- RTDB toggle (เปิดเป็นค่าเริ่มต้น) ---
USE_RTDB = os.getenv("USE_RTDB", "1") == "1"
FIREBASE_RTDB_URL = os.getenv(
    "FIREBASE_RTDB_URL",
    # ค่าเริ่มต้นตามที่คุณให้มา (แก้เป็นของโปรเจกต์คุณได้เสมอผ่าน ENV)
    "https://pm25-dashborad-default-rtdb.asia-southeast1.firebasedatabase.app"
)
RTDB_ROOT = os.getenv("RTDB_ROOT", "pm_readings")
RTDB_BATCH_SIZE = int(os.getenv("RTDB_BATCH_SIZE", "50"))
RTDB_FLUSH_SECS = int(os.getenv("RTDB_FLUSH_SECS", "30"))

# ---- Optional deps ----
try:
    import firebase_admin
    from firebase_admin import credentials
    _fb_ok = True
except Exception as e:
    log.warning(f"firebase-admin not available: {e}")
    _fb_ok = False

# ---------- Firestore sink ----------
try:
    from firebase_admin import firestore
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from google.api_core.exceptions import PermissionDenied
    _fs_ok = True
except Exception:
    _fs_ok = False

class FirestoreSink:
    def __init__(self, cred_path, collection, batch_size=50, flush_secs=30):
        self.enabled = USE_FIRESTORE and _fb_ok and _fs_ok
        self.buffer, self.last_flush = [], time.time()
        self.collection = collection
        self.batch_size, self.flush_secs = batch_size, flush_secs
        self._auth_broken = False
        if not self.enabled:
            return
        try:
            # ตรวจคีย์ล่วงหน้า (กัน invalid_grant)
            creds = service_account.Credentials.from_service_account_file(
                cred_path, scopes=["https://www.googleapis.com/auth/datastore"]
            )
            creds.refresh(Request())
            log.info("Firebase credentials OK; token exp=%s", creds.expiry)
            # init app ถ้ายังไม่เคย
            if not firebase_admin._apps:
                firebase_admin.initialize_app(credentials.Certificate(cred_path))
            self.db = firestore.client()
            log.info(f"Firestore sink enabled (collection='{collection}', batch={batch_size}, flush={flush_secs}s)")
        except Exception as e:
            self.enabled = False
            self._auth_broken = True
            log.error("Firestore init failed; CSV-only. %s", e)
        atexit.register(self.flush)

    def put(self, row: dict):
        if not self.enabled or self._auth_broken: return
        self.buffer.append(row)
        now = time.time()
        if len(self.buffer) >= self.batch_size or (now - self.last_flush) >= self.flush_secs:
            self.flush()

    def flush(self):
        if not self.enabled or self._auth_broken or not self.buffer: return
        try:
            while self.buffer:
                chunk = self.buffer[:500]  # limit per commit
                batch = self.db.batch()
                for r in chunk:
                    doc_id = f"{r.get('sensor','unknown')}-{r.get('ts','')}"
                    ref = self.db.collection(self.collection).document(doc_id)
                    r2 = dict(r); r2["created_at"] = firestore.SERVER_TIMESTAMP
                    batch.set(ref, r2, merge=True)
                batch.commit()
                del self.buffer[:len(chunk)]
            self.last_flush = time.time()
            log.info("Firestore flush OK")
        except PermissionDenied as e:
            self._auth_broken = True
            log.error("Firestore permission denied; CSV-only. %s", e)
        except RefreshError as e:
            self._auth_broken = True
            log.error("Firestore auth error; CSV-only. %s", e)
        except Exception as e:
            log.warning("Firestore flush failed (buffer kept): %s", e)

# ---------- Realtime Database sink ----------
try:
    from firebase_admin import db as rtdb
    _rtdb_ok = True
except Exception:
    _rtdb_ok = False

class RTDBSink:
    """
    เขียน RTDB แบบบัฟเฟอร์: update หลายพาธทีเดียว
    โครงสร้าง: /<root>/<sensor>/<YYYYMMDD>/<HHMMSS> => { ts, pm1, pm25, pm10 }
    """
    def __init__(self, cred_path, db_url, root="pm_readings", batch_size=50, flush_secs=30):
        self.enabled = USE_RTDB and _fb_ok and _rtdb_ok and bool(db_url)
        self.root = root.strip("/")
        self.buffer, self.last_flush = [], time.time()
        self.batch_size, self.flush_secs = batch_size, flush_secs
        if not self.enabled:
            if USE_RTDB:
                log.warning("RTDB sink disabled: check firebase-admin / RTDB URL")
            return
        try:
            if not firebase_admin._apps:
                firebase_admin.initialize_app(credentials.Certificate(cred_path), {"databaseURL": db_url})
                self._ref = rtdb.reference("/" + self.root)
            else:
                # app ถูก init แล้ว (อาจไม่มี databaseURL) → ใช้ reference แบบระบุ url
                self._ref = rtdb.reference("/" + self.root, url=db_url)
            log.info(f"RTDB sink enabled (root='/{self.root}', batch={batch_size}, flush={flush_secs}s)")
            atexit.register(self.flush)
        except Exception as e:
            self.enabled = False
            log.error("RTDB init failed; CSV-only. %s", e)

    def _path_parts_from_iso(self, ts_iso, sensor):
        ts_clean = ts_iso.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts_clean)
            return f"{sensor}/{dt:%Y%m%d}/{dt:%H%M%S}"
        except Exception:
            digits = re.sub(r"[^0-9T]", "", ts_iso)
            ymd = digits[:8] if len(digits) >= 8 else "19700101"
            hms = digits[-6:] if len(digits) >= 6 else "000000"
            return f"{sensor}/{ymd}/{hms}"

    def put(self, row: dict):
        if not self.enabled: return
        self.buffer.append(row)
        now = time.time()
        if len(self.buffer) >= self.batch_size or (now - self.last_flush) >= self.flush_secs:
            self.flush()

    def flush(self):
        if not self.enabled or not self.buffer: return
        try:
            updates = {}
            for r in self.buffer:
                sensor = r.get("sensor", "unknown")
                ts = r.get("ts", "")
                path = self._path_parts_from_iso(ts, sensor)
                updates[path] = {"ts": ts, "sensor": sensor,
                                 "pm1": r.get("pm1"), "pm25": r.get("pm25"), "pm10": r.get("pm10")}
            self._ref.update(updates)
            n = len(updates)
            self.buffer.clear()
            self.last_flush = time.time()
            log.info("RTDB flush OK (%d rows)", n)
        except Exception as e:
            log.warning("RTDB flush failed (buffer kept): %s", e)

# ============== Cleanup ==============
_closed = False
def cleanup():
    global _closed
    if _closed: return
    _closed = True
    try:
        if fs_sink: fs_sink.flush()
    except Exception: pass
    try:
        if rtdb_sink: rtdb_sink.flush()
    except Exception: pass
    try:
        if ser_indoor: ser_indoor.close()
    except Exception: pass
    try:
        if ser_outdoor: ser_outdoor.close()
    except Exception: pass
    log.info("Cleanup done.")

atexit.register(cleanup)
def _sig_exit(signum, frame):
    cleanup(); sys.exit(0)
for _sig in (signal.SIGINT, signal.SIGTERM):
    try: signal.signal(_sig, _sig_exit)
    except Exception: pass

# ============== Init sinks ==============
fs_sink = FirestoreSink(FIREBASE_CREDS, FIRESTORE_COLLECTION, FS_BATCH_SIZE, FS_FLUSH_SECS) if USE_FIRESTORE else None
rtdb_sink = RTDBSink(FIREBASE_CREDS, FIREBASE_RTDB_URL, RTDB_ROOT, RTDB_BATCH_SIZE, RTDB_FLUSH_SECS) if USE_RTDB else None

# ============== Main loop ==============
print("Starting PMS logging → CSV", end="")
if rtdb_sink and rtdb_sink.enabled: print(" + RTDB", end="")
if fs_sink and fs_sink.enabled: print(" + Firestore", end="")
print(". Press Ctrl+C to stop.")
log.info("Program started.")

try:
    while True:
        vals_in = read_pms(ser_indoor)
        vals_out = read_pms(ser_outdoor)

        ts_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')
        append_csv(CSV_FILE, [ts_iso, *vals_in, *vals_out])

        # ใส่บัฟเฟอร์ส่งขึ้นคลาวด์
        row_in  = {"ts": ts_iso, "sensor":"indoor",  "pm1": vals_in[0],  "pm25": vals_in[1],  "pm10": vals_in[2]}
        row_out = {"ts": ts_iso, "sensor":"outdoor", "pm1": vals_out[0], "pm25": vals_out[1], "pm10": vals_out[2]}
        if rtdb_sink and rtdb_sink.enabled:
            rtdb_sink.put(row_in); rtdb_sink.put(row_out)
        if fs_sink and fs_sink.enabled:
            fs_sink.put(row_in); fs_sink.put(row_out)

        print(f"{ts_iso} | In PM2.5={vals_in[1]} | Out PM2.5={vals_out[1]}")
        time.sleep(10)
except KeyboardInterrupt:
    print("\nStopped by user.")
finally:
    cleanup()
