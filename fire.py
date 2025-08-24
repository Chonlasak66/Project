import os, datetime as dt, firebase_admin
from firebase_admin import credentials, db

cred = credentials.Certificate(os.getenv("FIREBASE_CREDENTIALS", "firebase-adminsdk.json"))
firebase_admin.initialize_app(cred, {"databaseURL": os.environ["FIREBASE_RTDB_URL"]})

ref = db.reference("/pm_readings/quicktest")
payload = {"ts": dt.datetime.utcnow().isoformat(timespec="seconds")+"Z",
           "sensor":"indoor", "pm1":7.0, "pm25":10.0, "pm10":12.0}
ref.push(payload)  # เขียน 1 แถว
print("WRITE OK")

# อ่านแถวล่าสุดกลับมาดู
print("READ ->", ref.order_by_key().limit_to_last(1).get())
