
"""
pm25_single.py — One-file MVP for "Dust-Control Tent"

Features:
- Read two sensors (indoor/outdoor) via UART (PMSx003 family), OR run in SIMULATE=1 mode.
- Always write every reading into a durable SQLite queue (offline-first).
- Background uploader thread flushes queued rows to Firebase Realtime Database with idempotent paths.
- Minimal env-based config. No external config files required.

Quick start:
  pip install firebase-admin pyserial
  export FIREBASE_RTDB_URL="https://<your>.firebasedatabase.app"
  export FIREBASE_CREDENTIALS="/path/to/firebase-adminsdk.json"
  # Optional serial ports (set to your actual ports) OR use SIMULATE=1
  export SERIAL_INDOOR="/dev/ttyAMA0"
  export SERIAL_OUTDOOR="/dev/ttyAMA1"
  python pm25_single.py

Environment variables (with defaults):
  DEVICE_ID                (auto: hostname or machine-id[:8])
  FIREBASE_RTDB_URL        (no default; required for uploads)
  FIREBASE_CREDENTIALS     (/etc/pm25/firebase-adminsdk.json)
  RTDB_ROOT                (pm_readings)
  PM25_SQLITE_DB           (pm25.db)   -- file path for SQLite queue
  READ_INTERVAL_SEC        (1)         -- read loop interval
  BAUD_RATE                (9600)
  SERIAL_INDOOR            (unset)     -- if unset AND SIMULATE=0, indoor disabled
  SERIAL_OUTDOOR           (unset)     -- if unset AND SIMULATE=0, outdoor disabled
  SIMULATE                 (0)         -- set "1" to generate fake data (no hardware needed)
  LOGLEVEL                 (INFO)
"""
import os, time, socket, logging, math
from datetime import datetime, timezone
import threading
import sqlite3

# Optional imports (graceful fallback if not installed)
try:
    import serial
    _serial_ok = True
except Exception:
    _serial_ok = False

try:
    import firebase_admin
    from firebase_admin import credentials, db
    _fb_ok = True
except Exception:
    _fb_ok = False

# ---------- Logging ----------
log = logging.getLogger("pm25-single")
logging.basicConfig(level=os.getenv("LOGLEVEL","INFO"))

# ---------- Config ----------
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS", "/etc/pm25/firebase-adminsdk.json")
FIREBASE_RTDB_URL = os.getenv("FIREBASE_RTDB_URL", "").strip()
RTDB_ROOT = os.getenv("RTDB_ROOT", "pm_readings")
PM25_SQLITE_DB = os.getenv("PM25_SQLITE_DB", "pm25.db")
READ_INTERVAL_SEC = float(os.getenv("READ_INTERVAL_SEC", "1"))
BAUD_RATE = int(os.getenv("BAUD_RATE", "9600"))
SERIAL_INDOOR = os.getenv("SERIAL_INDOOR", "").strip()
SERIAL_OUTDOOR = os.getenv("SERIAL_OUTDOOR", "").strip()
SIMULATE = os.getenv("SIMULATE", "0").strip() == "1"

def _auto_device_id():
    v = os.getenv("DEVICE_ID")
    if v and v.strip(): return v.strip()
    try:
        hn = socket.gethostname().strip()
        if hn: return hn
    except Exception: pass
    try:
        mid = open("/etc/machine-id").read().strip()
        if mid: return mid[:8]
    except Exception: pass
    return "unknown"

DEVICE_ID = _auto_device_id()

# ---------- SQLite Queue ----------
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT UNIQUE,        -- device_id + ts + sensor
    device_id TEXT NOT NULL,
    sensor TEXT NOT NULL,         -- "indoor" | "outdoor"
    ts TEXT NOT NULL,             -- ISO8601 (UTC, Z)
    pm1 REAL, pm25 REAL, pm10 REAL,
    n INTEGER, min25 REAL, max25 REAL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_pending ON queue(sent_at) WHERE sent_at IS NULL;
"""

class QueueDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()
        self.conn.commit()

    def _ensure_schema(self):
        for stmt in SCHEMA.strip().split(";"):
            s = stmt.strip()
            if s:
                self.conn.execute(s)

    @staticmethod
    def _mk_record_id(device_id, ts, sensor):
        return f"{device_id}:{ts}:{sensor}"

    def put_row(self, device_id, row: dict):
        rec_id = self._mk_record_id(device_id, row['ts'], row['sensor'])
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO queue (record_id, device_id, sensor, ts, pm1, pm25, pm10, n, min25, max25) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec_id, device_id, row['sensor'], row['ts'], row.get('pm1'), row.get('pm25'),
                 row.get('pm10'), row.get('n',1), row.get('min25'), row.get('max25'))
            )
            self.conn.commit()
        return rec_id

    def get_pending(self, limit=200):
        cur = self.conn.execute(
            "SELECT id, record_id, device_id, sensor, ts, pm1, pm25, pm10, n, min25, max25 "
            "FROM queue WHERE sent_at IS NULL ORDER BY id ASC LIMIT ?", (int(limit),)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def mark_sent(self, ids):
        if not ids: return
        qmarks = ",".join("?" for _ in ids)
        self.conn.execute(f"UPDATE queue SET sent_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id IN ({qmarks})", ids)
        self.conn.commit()

# ---------- Firebase Uploader Thread ----------
class FirebaseUploader(threading.Thread):
    def __init__(self, queue_db: QueueDB, batch_size=200, flush_secs=2.0):
        super().__init__(daemon=True)
        self.qdb = queue_db
        self.batch_size = int(os.getenv("RTDB_BATCH_SIZE", str(batch_size)))
        self.flush_secs = float(os.getenv("RTDB_FLUSH_SECS", str(flush_secs)))
        self._ref = None

    def _init_firebase(self):
        if not _fb_ok or not FIREBASE_RTDB_URL:
            raise RuntimeError("firebase_admin not available or RTDB URL missing")
        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS)
            firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_RTDB_URL})
        return db.reference("/")

    @staticmethod
    def _rtdb_path(ts_iso: str, sensor: str) -> str:
        # ts_iso like "2025-09-14T12:34:56Z"
        dt = datetime.fromisoformat(ts_iso.replace("Z","+00:00"))
        ymd = dt.strftime("%Y%m%d")
        hms = dt.strftime("%H%M%S")
        return f"/{RTDB_ROOT}/{DEVICE_ID}/{sensor}/{ymd}/{hms}"

    def run(self):
        backoff = 1.0
        while True:
            try:
                if self._ref is None:
                    self._ref = self._init_firebase()
                    log.info("Uploader connected to RTDB.")
                rows = self.qdb.get_pending(limit=self.batch_size)
                if not rows:
                    time.sleep(self.flush_secs)
                    backoff = 1.0
                    continue
                updates, ids = {}, []
                for r in rows:
                    path = self._rtdb_path(r["ts"], r["sensor"])
                    updates[path] = {
                        "ts": r["ts"],
                        "pm1": r["pm1"],
                        "pm25": r["pm25"],
                        "pm10": r["pm10"],
                        "n": r["n"],
                        "min25": r["min25"],
                        "max25": r["max25"],
                        "device_id": r["device_id"],
                    }
                    ids.append(r["id"])
                self._ref.update(updates)
                self.qdb.mark_sent(ids)
                log.info("Uploaded %d rows.", len(ids))
                backoff = 1.0
            except Exception as e:
                log.warning("Uploader error: %s", e)
                self._ref = None
                time.sleep(min(backoff, 60.0))
                backoff *= 2.0

# ---------- PMSx003 Reader ----------
def _utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _open_serial(port: str, baud: int):
    if not _serial_ok:
        raise RuntimeError("pyserial not installed. pip install pyserial")
    return serial.Serial(port=port, baudrate=baud, timeout=2)

def _read_pms_once(ser):
    """
    Read a single frame from Plantower PMSx003 (e.g., PMS5003)
    Frame format: 0x42 0x4D <2B len> ... 32 bytes payload total (len=28) + checksum
    Returns dict {'pm1','pm25','pm10'} or None
    """
    if not ser or not ser.is_open:
        return None
    # sync to 0x42 0x4D
    b = ser.read(1)
    while b and b != b'\x42':
        b = ser.read(1)
    if not b:
        return None
    b2 = ser.read(1)
    if b2 != b'\x4d':
        return None
    # length
    lb = ser.read(2)
    if len(lb) != 2:
        return None
    length = lb[0] << 8 | lb[1]
    data = ser.read(length)
    if len(data) != length:
        return None
    # checksum
    cs = ser.read(2)
    if len(cs) != 2:
        return None
    # Extract standard env PM at bytes [6..11] of payload (after 2B length)
    # data layout (per PMS5003): [frame bytes not included here]
    try:
        pm1 = (data[6] << 8) | data[7]
        pm25 = (data[8] << 8) | data[9]
        pm10 = (data[10] << 8) | data[11]
        return {"pm1": float(pm1), "pm25": float(pm25), "pm10": float(pm10)}
    except Exception:
        return None

# ---------- Main loop ----------
def main():
    log.info("Starting pm25_single.py  (DEVICE_ID=%s  SIMULATE=%s)", DEVICE_ID, SIMULATE)
    qdb = QueueDB(PM25_SQLITE_DB)

    # Start uploader thread (it will retry if Firebase not configured)
    uploader = FirebaseUploader(qdb)
    uploader.start()

    indoor_ser = None
    outdoor_ser = None
    if not SIMULATE:
        if SERIAL_INDOOR:
            try:
                indoor_ser = _open_serial(SERIAL_INDOOR, BAUD_RATE)
                log.info("Indoor serial opened: %s", SERIAL_INDOOR)
            except Exception as e:
                log.warning("Indoor serial open failed: %s", e)
        if SERIAL_OUTDOOR:
            try:
                outdoor_ser = _open_serial(SERIAL_OUTDOOR, BAUD_RATE)
                log.info("Outdoor serial opened: %s", SERIAL_OUTDOOR)
            except Exception as e:
                log.warning("Outdoor serial open failed: %s", e)
        if not (indoor_ser or outdoor_ser):
            log.warning("No serial ports open and SIMULATE=0. Running idle; set SIMULATE=1 to try without hardware.")

    t0 = time.time()
    i = 0
    while True:
        ts = _utc_now_iso()

        if SIMULATE:
            # simple waves for demo (outdoor higher than indoor)
            i += 1
            pm25_out = 50 + 10*math.sin(i/20.0)
            pm25_in  = 20 + 5*math.sin(i/25.0 + 1.0)
            samples = {
                "indoor": {"pm1": pm25_in*0.6, "pm25": pm25_in, "pm10": pm25_in*1.4},
                "outdoor": {"pm1": pm25_out*0.6, "pm25": pm25_out, "pm10": pm25_out*1.4},
            }
        else:
            samples = {}
            if indoor_ser:
                r = _read_pms_once(indoor_ser)
                if r: samples["indoor"] = r
            if outdoor_ser:
                r = _read_pms_once(outdoor_ser)
                if r: samples["outdoor"] = r

        for sensor, d in samples.items():
            row = {
                "sensor": sensor,
                "ts": ts,
                "pm1": float(d["pm1"]),
                "pm25": float(d["pm25"]),
                "pm10": float(d["pm10"]),
                "n": 1,
                "min25": float(d["pm25"]),
                "max25": float(d["pm25"]),
            }
            qdb.put_row(DEVICE_ID, row)
            log.info("Queued %s: pm25=%.1f (ts=%s)", sensor, row["pm25"], ts)

        # pacing
        dt = READ_INTERVAL_SEC - (time.time() - t0)
        t0 = time.time()
        if dt < 0: dt = 0.0
        time.sleep(dt)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bye.")

