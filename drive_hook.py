# drive_hook.py (optimized incremental sync)
import os, json, time, hashlib
from pathlib import Path
from datetime import datetime, timedelta

# ---------- Google Drive deps ----------
_gdrive_ok = False
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2 import service_account as gsa
    from google.oauth2.credentials import Credentials as UserCreds
    from google.auth.transport.requests import Request
    _gdrive_ok = True
except Exception as e:
    print(f"[GDRIVE] google libs not available: {e}")
    _gdrive_ok = False

# ---------- Helpers ----------
def _env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1","true","yes","y","on")

def _env_str(name, default=""):
    v = os.environ.get(name)
    return v if v is not None else default

def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default

def _file_md5(path, chunk=1<<20):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(chunk), b''):
            h.update(b)
    return h.hexdigest()

# ---------- Uploader ----------
class DriveUploader:
    SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    def __init__(self, *, enabled=False, auth="oauth",
                 sa_key="", oauth_client="", token_path="",
                 folder_id="", folder_name="pm25-logs",
                 queue_path="", manifest_path="", debug=True,
                 recent_days=7, max_files=10, skip_unchanged=True, use_md5=False):
        self.enabled = bool(enabled) and _gdrive_ok
        self.auth = (auth or "oauth").strip()
        self.sa_key = sa_key
        self.oauth_client = oauth_client
        self.token_path = token_path
        self.folder_id = (folder_id or "").strip()
        self.folder_name = (folder_name or "pm25-logs").strip()
        self.queue_path = queue_path or ""
        self.manifest_path = manifest_path or ""
        self.debug = bool(debug)
        self.recent_days = int(recent_days) if recent_days is not None else 0
        self.max_files = int(max_files) if max_files is not None else 0
        self.skip_unchanged = bool(skip_unchanged)
        self.use_md5 = bool(use_md5)

        self.service = None
        self._known_ids = {}   # fname -> fileId
        self._queue = []       # list[str] of file paths
        self._manifest = {}    # path -> {size, mtime, md5?, file_id?, updated?}

        if not self.enabled:
            print("[GDRIVE] disabled")
            return
        try:
            self._init_service()
            self._ensure_folder()
            self._load_queue()
            self._load_manifest()
            if self.debug:
                print(f"[GDRIVE] Ready → folder_id={self.folder_id}, auth={self.auth}, "
                      f"recent_days={self.recent_days}, max_files={self.max_files}, "
                      f"skip_unchanged={self.skip_unchanged}, use_md5={self.use_md5}")
        except Exception as e:
            print(f"[GDRIVE] init failed: {e}")
            self.enabled = False

    # ----- manifest -----
    def _load_manifest(self):
        self._manifest = {}
        if not self.manifest_path:
            return
        try:
            p = Path(self.manifest_path)
            if p.exists():
                self._manifest = json.loads(p.read_text(encoding='utf-8')) or {}
            if self.debug:
                print(f"[GDRIVE] manifest loaded: {len(self._manifest)} item(s) from {p}")
        except Exception as e:
            print(f"[GDRIVE] manifest read failed: {e}")
            self._manifest = {}

    def _save_manifest(self):
        if not self.manifest_path:
            return
        try:
            p = Path(self.manifest_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            if self.debug:
                print(f"[GDRIVE] manifest saved: {len(self._manifest)} item(s) → {p}")
        except Exception as e:
            print(f"[GDRIVE] manifest write failed: {e}")

    def _sig(self, path):
        st = os.stat(path)
        sig = {"size": st.st_size, "mtime": int(st.st_mtime)}
        if self.use_md5:
            sig["md5"] = _file_md5(path)
        return sig

    def _unchanged(self, path):
        if not self.skip_unchanged:
            return False
        p = os.path.abspath(path)
        old = self._manifest.get(p)
        if not old:
            return False
        new = self._sig(p)
        # Compare size+mtime and md5 (if tracked)
        same = (old.get("size")==new.get("size") and old.get("mtime")==new.get("mtime"))
        if self.use_md5:
            same = same and (old.get("md5")==new.get("md5"))
        return same

    # ----- auth/service -----
    def _init_service(self):
        if self.auth == "service_account":
            creds = gsa.Credentials.from_service_account_file(self.sa_key, scopes=self.SCOPES)
        else:
            creds = None
            if os.path.exists(self.token_path):
                creds = UserCreds.from_authorized_user_file(self.token_path, self.SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    if self.debug: print("[GDRIVE] refreshing OAuth token...")
                    creds.refresh(Request())
                else:
                    raise RuntimeError("OAuth token missing; create token.json with OAuth flow")
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

    def _find_file_id(self, name, manifest_key=None):
        # Try manifest hint first
        if manifest_key:
            rec = self._manifest.get(manifest_key) or {}
            fid = rec.get("file_id")
            if fid:
                return fid
        # Then local cache
        if name in self._known_ids:
            return self._known_ids[name]
        # Then query Drive
        q = f"name = '{name}' and '{self.folder_id}' in parents and trashed = false"
        res = self.service.files().list(q=q, spaces="drive", fields="files(id,name)", pageSize=1).execute()
        files = res.get("files", [])
        if files:
            fid = files[0]["id"]
            self._known_ids[name] = fid
            return fid
        return None

    # ----- upload -----
    def upload_now(self, path):
        if not self.enabled: return False
        if not path or not os.path.exists(path): return False
        p = os.path.abspath(path)
        fname = os.path.basename(p)
        if self.skip_unchanged and self._unchanged(p):
            if self.debug: print(f"[GDRIVE] skip unchanged: {fname}")
            return True

        if self.debug: print(f"[GDRIVE] uploading: {fname}")
        media = MediaFileUpload(p, mimetype="text/csv", resumable=False)

        fid = self._find_file_id(fname, manifest_key=p)
        if fid:
            res = self.service.files().update(fileId=fid, media_body=media, fields="id").execute()
            new_id = res.get("id") or fid
            if self.debug: print(f"[GDRIVE] updated: {fname}")
        else:
            meta = {"name": fname, "parents": [self.folder_id]}
            res = self.service.files().create(body=meta, media_body=media, fields="id").execute()
            new_id = res["id"]
            if self.debug: print(f"[GDRIVE] created: {fname}")

        # Update manifest
        sig = self._sig(p)
        sig["file_id"] = new_id
        sig["updated"] = int(time.time())
        self._manifest[p] = sig
        self._save_manifest()
        return True

    # ----- queue -----
    def enqueue(self, path):
        if not path: return
        p = os.path.abspath(path)
        if os.path.exists(p) and p not in self._queue:
            self._queue.append(p)
            if self.debug: print(f"[GDRIVE] enqueued: {os.path.basename(p)}")
            self._save_queue()

    def _load_queue(self):
        self._queue = []
        if not self.queue_path:
            return
        try:
            q = Path(self.queue_path)
            if q.exists():
                self._queue = json.loads(q.read_text(encoding='utf-8')) or []
            if self.debug:
                print(f"[GDRIVE] queue loaded: {len(self._queue)} item(s) from {q}")
        except Exception as e:
            print(f"[GDRIVE] load queue failed: {e}")
            self._queue = []

    def _save_queue(self):
        if not self.queue_path:
            return
        try:
            q = Path(self.queue_path)
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_text(json.dumps(self._queue, ensure_ascii=False, indent=2), encoding='utf-8')
            if self.debug:
                print(f"[GDRIVE] queue saved: {len(self._queue)} item(s)")
        except Exception as e:
            print(f"[GDRIVE] save queue failed: {e}")

    def process_queue(self, max_items=50):
        if not self.enabled: return
        if not self._queue:
            if self.debug: print("[GDRIVE] queue empty")
            return
        processed = 0
        newq = []
        for p in list(self._queue):
            if processed >= max_items:
                newq.append(p)  # keep remaining in queue
                continue
            ok = False
            try:
                ok = self.upload_now(p)
            except Exception as e:
                print(f"[GDRIVE] upload failed for {p}: {e}")
                ok = False
            if not ok:
                newq.append(p)
            else:
                processed += 1
        self._queue = newq
        self._save_queue()
        if self.debug: print(f"[GDRIVE] queue after process: {len(self._queue)} item(s), processed={processed}")

    # ----- discovery/sync -----
    def sync_local_csvs(self, csv_dir):
        try:
            pdir = Path(csv_dir)
            all_files = sorted(pdir.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
            total = len(all_files)

            # filter by recency
            recent_cut = None
            if self.recent_days and self.recent_days > 0:
                recent_cut = time.time() - (self.recent_days * 86400)
                all_files = [p for p in all_files if p.stat().st_mtime >= recent_cut]

            # limit max files
            if self.max_files and self.max_files > 0 and len(all_files) > self.max_files:
                all_files = all_files[:self.max_files]

            # enqueue only changed (or all if skip_unchanged=False)
            enq = 0
            skipped = 0
            for p in all_files:
                if self.skip_unchanged and self._unchanged(str(p)):
                    skipped += 1
                    if self.debug: print(f"[GDRIVE] skip unchanged in sync: {p.name}")
                    continue
                self.enqueue(str(p))
                enq += 1

            if self.debug:
                print(f"[GDRIVE] sync summary: scanned={total}, "
                      f"after_recent={len(all_files)+skipped}, enqueued={enq}, skipped={skipped}")
            self.process_queue()
        except Exception as e:
            print(f"[GDRIVE] sync_local_csvs failed: {e}")

# ---------- Public hooks ----------
def gdrive_setup(csv_dir="csv_logs"):
    enabled = _env_bool("GDRIVE_ENABLED", False)
    auth    = _env_str("GDRIVE_AUTH", "oauth")
    sa_key  = _env_str("GDRIVE_SA_KEY", "/etc/pm25/gdrive-sa.json")
    client  = _env_str("GDRIVE_OAUTH_CLIENT_SECRETS", "credentials.json")
    token   = _env_str("GDRIVE_TOKEN_PATH", "token.json")
    folder_id   = _env_str("GDRIVE_FOLDER_ID", "")
    folder_name = _env_str("GDRIVE_FOLDER_NAME", "pm25-logs")
    mode    = _env_str("GDRIVE_UPLOAD_MODE", "both")
    queue   = _env_str("GDRIVE_QUEUE_PATH", str(Path(csv_dir) / "upload_queue.json"))
    debug   = _env_bool("GDRIVE_DEBUG", True)

    # new knobs
    recent_days = _int("GDRIVE_SYNC_RECENT_DAYS", 7)   # 0 = no limit
    max_files   = _int("GDRIVE_SYNC_MAX_FILES", 10)    # 0 = no limit
    skip_unch   = _env_bool("GDRIVE_SKIP_UNCHANGED", True)
    use_md5     = _env_bool("GDRIVE_USE_MD5", False)
    manifest    = _env_str("GDRIVE_MANIFEST_PATH", str(Path(csv_dir) / ".gdrive_manifest.json"))

    print("[GDRIVE] CONFIG:", dict(enabled=enabled, auth=auth, folder_name=folder_name, mode=mode,
                                   recent_days=recent_days, max_files=max_files,
                                   skip_unchanged=skip_unch, use_md5=use_md5))

    uploader = DriveUploader(
        enabled=enabled, auth=auth,
        sa_key=sa_key, oauth_client=client, token_path=token,
        folder_id=folder_id, folder_name=folder_name,
        queue_path=queue, manifest_path=manifest, debug=debug,
        recent_days=recent_days, max_files=max_files,
        skip_unchanged=skip_unch, use_md5=use_md5,
    )
    if uploader.enabled and mode in ("at_start","both"):
        uploader.sync_local_csvs(csv_dir)
    return uploader, mode

def gdrive_finalize(uploader_mode_tuple, current_csv_file):
    if not uploader_mode_tuple: return
    uploader, mode = uploader_mode_tuple
    try:
        if uploader and uploader.enabled and mode in ("at_exit","both"):
            if current_csv_file:
                uploader.enqueue(current_csv_file)
            # process with a reasonable cap per exit to avoid hanging shutdown
            uploader.process_queue(max_items=25)
    except Exception as e:
        print(f"[GDRIVE] finalize failed: {e}")
