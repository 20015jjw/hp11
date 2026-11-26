"""Presets and schedules, kept in one small JSON file, plus the schedule runner."""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

RUN_WINDOW_MINUTES = 5  # keep retrying a missed/refused schedule for this long
ACTIONS = ("off", "on", "preset")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as e:
            # Unreadable store: keep the bad file, and come up disconnected, since
            # we no longer know whether the kill switch was on.
            bad = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(self.path, bad)
            except OSError:
                bad = None
            print(f"hp11: {self.path} unreadable ({e}); kept as {bad}; starting disconnected", flush=True)
            self.data = {"paused": True}
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
        if not re.fullmatch(r"[\w][\w .,'&()+-]*", name):  # it becomes a URL path segment
            raise ValueError("preset name may use letters, digits, spaces and . , ' & ( ) + -")
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
                     "enabled": True, "last_run": None, "last_result": None,
                     "since": datetime.now().isoformat()}
            self.data["schedules"].append(entry)
            self.data["schedules"].sort(key=lambda s: s["time"])
            self._save()
            return entry

    def update_schedule(self, sid, **fields):
        with self._lock:
            for s in self.data["schedules"]:
                if s["id"] == sid:
                    if fields.get("enabled") and not s.get("enabled"):
                        fields["since"] = datetime.now().isoformat()  # don't fire for a time already past
                    if all(s.get(k) == v for k, v in fields.items()):
                        return s  # nothing to write
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
    """The scheduled start this entry should run for right now, or None.

    An entry is due from its time until RUN_WINDOW_MINUTES later (also across
    midnight), once per start, and never for a start before it was created or
    last re-enabled.
    """
    if not entry.get("enabled"):
        return None
    h, m = map(int, entry["time"].split(":"))
    for back in (0, 1):
        start = (now - timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
        if start.weekday() not in entry["days"]:
            continue
        if not 0 <= (now - start).total_seconds() < RUN_WINDOW_MINUTES * 60:
            continue
        if entry.get("last_run") == start.isoformat():
            continue
        if entry.get("since") and start < datetime.fromisoformat(entry["since"]):
            continue
        return start
    return None


def run_entry(fan, store, entry):
    """Carry out one schedule entry; "on" resumes whatever the fan was last set to."""
    if entry["action"] == "off":
        return fan.set({"power": False})
    if entry["action"] == "on":
        return fan.set({"power": True})
    return fan.apply_settings(store.preset(entry["preset"]))


def _tick(fan, store, now):
    for entry in store.view()["schedules"]:
        start = due(entry, now)
        if start is None:
            if (entry.get("last_result") or "").startswith("retrying"):  # window closed unrun
                store.update_schedule(entry["id"], last_result="skipped: " + entry["last_result"][10:])
            continue
        try:
            run_entry(fan, store, entry)
            result = "ok"
        except (ConnectionError, RuntimeError) as e:  # offline or heat guard: retry next tick
            store.update_schedule(entry["id"], last_result=f"retrying: {e}")
            continue
        except Exception as e:  # bad preset etc.: don't retry
            result = f"failed: {e}"
        store.update_schedule(entry["id"], last_run=start.isoformat(),
                              last_result=f"{result} at {now:%a %H:%M}")


def scheduler(fan, store, stop, tick=15):
    while not stop.wait(tick):
        try:
            _tick(fan, store, datetime.now())
        except Exception as e:  # never let one bad tick (full disk, deleted entry) end the thread
            print(f"hp11: scheduler tick failed: {e!r}", flush=True)
