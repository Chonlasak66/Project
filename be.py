
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
import json
from glob import glob
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

# ---------- Google Drive (batch upload; OAuth-first) ----------
def _env_path(default):
    try:
        return os.path.join(CSV_DIR, default)
    except Exception:
        return default
GDRIVE_ENABLED                = env_bool("GDRIVE_ENABLED", False)
GDRIVE_AUTH                   = env_str("GDRIVE_AUTH", "oauth")  # oauth | service_account
GDRIVE_OAUTH_CLIENT_SECRETS   = env_str("GDRIVE_OAUTH_CLIENT_SECRETS", "credentials.json")
GDRIVE_TOKEN_PATH             = env_str("GDRIVE_TOKEN_PATH", "token.json")
GDRIVE_SA_KEY                 = env_str("GDRIVE_SA_KEY", "/etc/pm25/gdrive-sa.json")
GDRIVE_FOLDER_ID              = env_str("GDRIVE_FOLDER_ID", "")
GDRIVE_FOLDER_NAME            = env_str("GDRIVE_FOLDER_NAME", "pm25-logs")
GDRIVE_UPLOAD_MODE            = env_str("GDRIVE_UPLOAD_MODE", "both")  # at_start | at_exit | both
GDRIVE_QUEUE_PATH             = env_str("GDRIVE_QUEUE_PATH", _env_path("upload_queue.json"))
GDRIVE_DEBUG                  = env_bool("GDRIVE_DEBUG", True)

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

_UPLOADER = None
# ---------- Optional Firebase deps ----------
try:
    import firebase_admin
    from firebase_admin import credentials, db as rtdb
    _fb_ok = True
except Exception as e:
    log.warning(f"firebase-admin not available: {e}")
    _fb_ok = False

# ---------- Optional Google Drive deps ----------
_gdrive_ok = False
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2 import service_account as gsa
    from google.oauth2.credentials import Credentials as UserCreds
    from google.auth.transport.requests import Request
    _gdrive_ok = True
except Exception as e:
    log.warning(f"google drive libs not available: {e}")
    _gdrive_ok = False
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

# ---------- Google Drive batch uploader (OAuth/SA) ----------
class DriveUploader:
    SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    def __init__(self, enabled=False, auth="oauth",
                 sa_key="", oauth_client="", token_path="",
                 folder_id="", folder_name="pm25-logs",
                 queue_path="", debug=True):
        self.enabled = bool(enabled) and _gdrive_ok
        self.auth = (auth or "oauth").strip()
        self.sa_key = sa_key
        self.oauth_client = oauth_client
        self.token_path = token_path
        self.folder_id = (folder_id or "").strip()
        self.folder_name = (folder_name or "pm25-logs").strip()
        self.queue_path = queue_path or _env_path("upload_queue.json")
        self.debug = bool(debug)
        self.service = None
        self._known_ids = {}
        self._queue = []
        if not self.enabled:
            return
        try:
            self._init_service()
            self._ensure_folder()
            self._load_queue()
            if self.debug: log.info(f"[GDRIVE] Ready → folder_id={self.folder_id}, auth={self.auth}")
        except Exception as e:
            log.warning(f"[GDRIVE] init failed: {e}")
            self.enabled = False

    def _init_service(self):
        if self.auth == "service_account":
            creds = gsa.Credentials.from_service_account_file(self.sa_key, scopes=self.SCOPES)
        else:
            creds = None
            if os.path.exists(self.token_path):
                creds = UserCreds.from_authorized_user_file(self.token_path, self.SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    if self.debug: log.info("[GDRIVE] refreshing OAuth token...")
                    creds.refresh(Request())
                else:
                    raise RuntimeError("OAuth token missing; create token.json with oauth_token_gen.py")
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _ensure_folder(self):
        if self.folder_id:
            return
        q = f"name = '{self.folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = self.service.files().list(q=q, spaces="drive", fields="files(id,name)", pageSize=1).execute()
        files = res.get("files", [])
        if files:
            self.folder_id = files[0]["id"]
        else:
            meta = {"name": self.folder_name, "mimeType": "application/vnd.google-apps.folder"}
            created = self.service.files().create(body=meta, fields="id").execute()
            self.folder_id = created["id"]

    def _load_queue(self):
        try:
            if os.path.exists(self.queue_path):
                with open(self.queue_path, "r", encoding="utf-8") as f:
                    self._queue = json.load(f) or []
            else:
                self._queue = []
            if self.debug: log.info(f"[GDRIVE] queue loaded: {len(self._queue)} item(s) from {self.queue_path}")
        except Exception as e:
            self._queue = []
            log.warning(f"[GDRIVE] load queue failed: {e}")

    def _save_queue(self):
        try:
            Path(self.queue_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.queue_path, "w", encoding="utf-8") as f:
                json.dump(self._queue, f, ensure_ascii=False, indent=2)
            if self.debug: log.info(f"[GDRIVE] queue saved: {len(self._queue)} item(s)")
        except Exception as e:
            log.warning(f"[GDRIVE] save queue failed: {e}")

    def _find_file_id(self, name):
        if name in self._known_ids:
            return self._known_ids[name]
        q = f"name = '{name}' and '{self.folder_id}' in parents and trashed = false"
        res = self.service.files().list(q=q, spaces="drive", fields="files(id,name)", pageSize=1).execute()
        files = res.get("files", [])
        if files:
            fid = files[0]["id"]
            self._known_ids[name] = fid
            return fid
        return None

    def upload_now(self, path):
        if not self.enabled: return False
        if not path or not os.path.exists(path): return False
        fname = os.path.basename(path)
        if self.debug: log.info(f"[GDRIVE] uploading: {fname}")
        media = MediaFileUpload(path, mimetype="text/csv", resumable=False)
        fid = self._find_file_id(fname)
        if fid:
            self.service.files().update(fileId=fid, media_body=media).execute()
            if self.debug: log.info(f"[GDRIVE] updated: {fname}")
        else:
            meta = {"name": fname, "parents": [self.folder_id]}
            self.service.files().create(body=meta, media_body=media, fields="id").execute()
            if self.debug: log.info(f"[GDRIVE] created: {fname}")
        return True

    def enqueue(self, path):
        if not path: return
        p = os.path.abspath(path)
        if os.path.exists(p) and p not in self._queue:
            self._queue.append(p)
            if self.debug: log.info(f"[GDRIVE] enqueued: {os.path.basename(p)}")
            self._save_queue()

    def process_queue(self):
        if not self.enabled: return
        if not self._queue:
            if self.debug: log.info("[GDRIVE] queue empty")
            return
        newq = []
        for p in list(self._queue):
            ok = False
            try:
                ok = self.upload_now(p)
            except Exception as e:
                log.warning(f"[GDRIVE] upload failed for {p}: {e}")
                ok = False
            if not ok:
                newq.append(p)
        self._queue = newq
        self._save_queue()
        if self.debug: log.info(f"[GDRIVE] queue after process: {len(self._queue)} item(s)")

    def sync_local_csvs(self, csv_dir):
        try:
            for p in sorted(glob(os.path.join(csv_dir, "*.csv"))):
                self.enqueue(p)
            self.process_queue()
        except Exception as e:
            log.warning(f"[GDRIVE] sync_local_csvs failed: {e}")

# ---------- Cleanup ----------
def cleanup():
    gdrive_finalize(_gdrive, CURRENT_CSV_FILE)
    try:
        if _UPLOADER and getattr(_UPLOADER, "enabled", False) and GDRIVE_UPLOAD_MODE in ("at_exit","both"):
            try:
                if CURRENT_CSV_FILE:
                    _UPLOADER.enqueue(CURRENT_CSV_FILE)
                _UPLOADER.process_queue()
            except Exception as e:
                log.warning(f"[GDRIVE] finalize failed: {e}")
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

    print("  GDRIVE_ENABLED    =", GDRIVE_ENABLED)
    print("  GDRIVE_AUTH       =", GDRIVE_AUTH)
    print("  GDRIVE_FOLDER_NAME=", GDRIVE_FOLDER_NAME)
    print("  GDRIVE_UPLOAD_MODE=", GDRIVE_UPLOAD_MODE)
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
                # enqueue yesterday file on rotation
                if _UPLOADER and getattr(_UPLOADER, "enabled", False) and CURRENT_CSV_FILE:
                    try:
                        _UPLOADER.enqueue(CURRENT_CSV_FILE)
                    except Exception as e:
                        log.warning(f"[GDRIVE] enqueue old csv failed: {e}")
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
from drive_hook import gdrive_setup, gdrive_finalize

# ... โค้ด config ของคุณ ...
_gdrive = gdrive_setup(csv_dir=CSV_DIR)  # จะพิมพ์ [GDRIVE] CONFIG และ Ready ให้เห็น

if __name__ == "__main__":
    main()
