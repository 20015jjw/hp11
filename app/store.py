"""Presets and schedules, kept in one small JSON file, plus the schedule runner."""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

RUN_WINDOW_MINUTES = 5  # keep retrying a missed/refused schedule for this long
ACTIONS = ("off", "on", "preset")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {}
        self.data.setdefault("presets", {})
        self.data.setdefault("schedules", [])
        self.data.setdefault("paused", False)  # kill switch, kept across restarts

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    def view(self):
        with self._lock:
            return json.loads(json.dumps(self.data))

    def set_paused(self, paused):
        with self._lock:
            self.data["paused"] = bool(paused)
            self._save()

    # -- presets ------------------------------------------------------------
    def save_preset(self, name, settings):
        name = (name or "").strip()
        if not 1 <= len(name) <= 30:
            raise ValueError("preset name must be 1-30 characters")
        with self._lock:
            self.data["presets"][name] = settings
            self._save()

    def delete_preset(self, name):
        with self._lock:
            if self.data["presets"].pop(name, None) is None:
                raise KeyError(name)
            self.data["schedules"] = [s for s in self.data["schedules"] if s.get("preset") != name]
            self._save()

    def preset(self, name):
        with self._lock:
            return self.data["presets"][name]

    # -- schedules ----------------------------------------------------------
    def add_schedule(self, time_, days, action, preset=None):
        if not isinstance(time_, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_):
            raise ValueError("time must be HH:MM (24-hour)")
        if (not isinstance(days, list) or not days
                or any(isinstance(d, bool) or d not in range(7) for d in days)):
            raise ValueError("days must be a non-empty list of 0 (Mon) to 6 (Sun)")
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}")
        with self._lock:
            if action == "preset" and preset not in self.data["presets"]:
                raise ValueError("unknown preset")
            entry = {"id": uuid.uuid4().hex[:8], "time": time_, "days": sorted(set(days)),
                     "action": action, "preset": preset if action == "preset" else None,
                     "enabled": True, "last_run": None, "last_result": None}
            self.data["schedules"].append(entry)
            self.data["schedules"].sort(key=lambda s: s["time"])
            self._save()
            return entry

    def update_schedule(self, sid, **fields):
        with self._lock:
            for s in self.data["schedules"]:
                if s["id"] == sid:
                    s.update(fields)
                    self._save()
                    return s
        raise KeyError(sid)

    def delete_schedule(self, sid):
        with self._lock:
            n = len(self.data["schedules"])
            self.data["schedules"] = [s for s in self.data["schedules"] if s["id"] != sid]
            if len(self.data["schedules"]) == n:
                raise KeyError(sid)
            self._save()


def due(entry, now):
    """Is this schedule inside its run window right now and not yet run today?"""
    if not entry.get("enabled") or now.weekday() not in entry["days"]:
        return False
    h, m = map(int, entry["time"].split(":"))
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    late = (now - start).total_seconds()
    return 0 <= late < RUN_WINDOW_MINUTES * 60 and entry.get("last_run") != start.isoformat()


def run_entry(fan, store, entry):
    """Carry out one schedule entry; "on" resumes whatever the fan was last set to."""
    if entry["action"] == "off":
        return fan.set({"power": False})
    if entry["action"] == "on":
        return fan.set({"power": True})
    return fan.apply_settings(store.preset(entry["preset"]))


def scheduler(fan, store, stop, tick=15):
    while not stop.wait(tick):
        now = datetime.now()
        for entry in store.view()["schedules"]:
            if not due(entry, now):
                continue
            h, m = map(int, entry["time"].split(":"))
            stamp = now.replace(hour=h, minute=m, second=0, microsecond=0).isoformat()
            try:
                run_entry(fan, store, entry)
                result = "ok"
            except (ConnectionError, RuntimeError) as e:  # offline or heat guard: retry next tick
                try:
                    store.update_schedule(entry["id"], last_result=f"retrying: {e}")
                except KeyError:
                    pass
                continue
            except Exception as e:  # bad preset etc.: don't retry
                result = f"failed: {e}"
            try:
                store.update_schedule(entry["id"], last_run=stamp,
                                      last_result=f"{result} at {now:%a %H:%M}")
            except KeyError:
                pass
