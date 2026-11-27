"""에뮬레이터에서 잠금 감지 / PIN / 패턴 잠금 해제 동작 확인 (CI용)."""
import os, subprocess, sys, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shutil
import phone_launcher as pl

adb = shutil.which("adb")
ph = pl.Phone(None, adb)
serial = ph.devices()[0].serial
sh = lambda *a: subprocess.run([adb, "-s", serial, "shell", *a], capture_output=True, text=True).stdout

def lock():
    ph.key(serial, 26); time.sleep(2); ph.key(serial, 224); time.sleep(2)

ok = True
# PIN
sh("locksettings", "set-pin", "1234")
lock()
print("locked(pin):", ph.is_locked(serial))
ph.unlock(serial, "1234"); time.sleep(3)
r = ph.is_locked(serial); print("after pin unlock locked:", r); ok &= not r
# 패턴 (Z 모양: 1-2-3-5-7-8-9)
sh("locksettings", "clear", "--old", "1234")
print(sh("locksettings", "set-pattern", "1235789"))
lock()
print("locked(pattern):", ph.is_locked(serial))
ph.key(serial, 82); time.sleep(1)
print("points:", ph.pattern_points(serial))
print("dump:", ph.pattern_debug)
import re as _re
print("ids:", sorted(set(_re.findall(r'resource-id="([^"]+)"', ph.pattern_xml)))[:60])
print("wm:", sh("wm", "size"))
t0 = time.time(); ph.unlock_pattern(serial, "1235789"); print("pattern took", round(time.time() - t0, 1), "s"); time.sleep(3)
r = ph.is_locked(serial); print("after pattern unlock locked:", r); ok &= not r
print("UNLOCK TEST", "OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
