# senser.py (test relays on BCM 17,18,27,22 with Active LOW)
import time
from gpiozero import DigitalOutputDevice, Device
from gpiozero.pins.lgpio import LGPIOFactory
Device.pin_factory = LGPIOFactory()  # force lgpio backend

ACTIVE_LOW = True
PINS = [17, 18, 27, 22]

devs = [DigitalOutputDevice(p, active_high=(not ACTIVE_LOW), initial_value=False) for p in PINS]

def set_pin(i, on): devs[i].on() if on else devs[i].off()

print("Toggling relays...")
for i, p in enumerate(PINS):
    print(f"Pin {p} ON");  set_pin(i, True);  time.sleep(1.0)
    print(f"Pin {p} OFF"); set_pin(i, False); time.sleep(0.3)

print("All ON 2s");  [d.on() for d in devs]; time.sleep(2)
print("All OFF");    [d.off() for d in devs]
print("Done.")
