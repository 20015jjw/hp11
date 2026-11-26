#!/usr/bin/env python3
"""Does the fan accept a heat-on command over local MQTT?

    .venv/bin/python scripts/heat_test.py

Reads the current state, sends hmod=HEAT, reports what the fan says back, then
restores the previous heat setting. Stay near the fan while this runs.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.main import fan  # noqa: E402  (loads .env)

KEYS = ("fpwr", "hmod", "hsta", "hmax", "fnsp")


def show(label):
    print(f"{label:>10}: " + "  ".join(f"{k}={fan.raw.get(k)}" for k in KEYS))


def wait(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        time.sleep(0.2)
    return cond()


fan.start()
if not wait(lambda: fan.connected and "hmod" in fan.raw, 10):
    sys.exit(f"No state from fan (connected={fan.connected}, error={fan.last_error}, "
             f"keys={sorted(fan.raw)})")
before = fan.raw.get("hmod")
show("before")
if before == "HEAT":
    print("Heat is already on; turn it off at the fan/remote and re-run for a real test.")
    sys.exit(0)

fan.set({"heat": True})
accepted = wait(lambda: fan.raw.get("hmod") == "HEAT", 8)
time.sleep(2)
show("after")
print("RESULT: heat-on over local MQTT was",
      "ACCEPTED" if accepted else "IGNORED (hmod did not change within 8s)")

fan.set({"heat": False})
wait(lambda: fan.raw.get("hmod") == before, 8)
show("restored")
fan.stop()
