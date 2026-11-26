"""Local MQTT client for a Dyson purifier/heater (Hot+Cool, product type 527*)."""
import json
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

ENV_POLL_SECONDS = 30
ADJUST_TIMEOUT_SECONDS = 20  # how long to wait for the head to reach a new direction
BRIGHTNESS_LEVELS = (1, 2, 3)  # display levels this fan accepts (0 is refused)
HEAT_GUARD_SECONDS = 60  # minimum gap between the heater stopping and a restart


class HeatGuard(RuntimeError):
    """Refused: the heater was switched off too recently to restart it."""


def _heater_enabled(raw):
    return raw.get("fpwr") == "ON" and raw.get("hmod") == "HEAT"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v, scale=1.0, offset=0.0):
    """Dyson sends numbers as zero-padded strings, or OFF/INIT/FAIL when absent."""
    try:
        return round(int(v) * scale + offset, 1)
    except (TypeError, ValueError):
        return None


def _on(v):
    return None if v is None else v in ("ON", "HEAT")


def encode(changes, raw):
    """Translate friendly settings into Dyson STATE-SET fields. Raises ValueError."""
    data = {}
    for key, val in changes.items():
        if key == "power":
            data["fpwr"] = "ON" if val else "OFF"
        elif key == "speed":
            if val == "auto":
                data["auto"] = "ON"
            elif isinstance(val, int) and not isinstance(val, bool) and 1 <= val <= 10:
                data.update(fpwr="ON", auto="OFF", fnsp=f"{val:04d}")
            else:
                raise ValueError("speed must be 1-10 or 'auto'")
        elif key == "oscillation":
            if val:
                if raw.get("osal") and raw.get("osal") == raw.get("osau"):
                    raise ValueError("the head is set to a fixed direction; set a sweep first")
                data.update(oson="ON", fpwr="ON")
                # Older 527 firmware wants the sweep angles re-sent; models that
                # report a preset width in ancp keep it if we send nothing extra.
                if raw.get("ancp") in (None, "CUST") and raw.get("osal") and raw.get("osau"):
                    data.update(ancp="CUST", osal=raw["osal"], osau=raw["osau"])
            else:
                data["oson"] = "OFF"
        elif key == "aim":  # point at a fixed direction: equal lower/upper sweep angle
            if not isinstance(val, int) or isinstance(val, bool) or not 5 <= val <= 355:
                raise ValueError("aim must be 5-355 degrees")
            data.update(oson="ON", ancp="CUST", osal=f"{val:04d}", osau=f"{val:04d}")
        elif key == "sweep":  # custom oscillation window [low, high] in degrees
            ok = (isinstance(val, list) and len(val) == 2
                  and all(isinstance(v, int) and not isinstance(v, bool) and 5 <= v <= 355 for v in val))
            if not ok or val[1] - val[0] < 30:
                raise ValueError("sweep must be [low, high], 5-355 degrees, at least 30 apart")
            data.update(oson="ON", ancp="CUST", osal=f"{val[0]:04d}", osau=f"{val[1]:04d}")
        elif key == "brightness":
            if isinstance(val, bool) or val not in BRIGHTNESS_LEVELS:
                raise ValueError(f"brightness must be one of {BRIGHTNESS_LEVELS}")
            data["bril"] = f"{val:04d}"
        elif key == "night_mode":
            data["nmod"] = "ON" if val else "OFF"
        elif key == "heat":
            data["hmod"] = "HEAT" if val else "OFF"
        elif key == "target_c":
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not 1 <= val <= 37:
                raise ValueError("target_c must be 1-37")
            data["hmax"] = f"{round((val + 273.15) * 10):04d}"
        elif key == "front_airflow":
            data["fdir"] = "ON" if val else "OFF"
        elif key == "sleep_timer":
            if val in (0, None, False):
                data["sltm"] = "OFF"
            elif isinstance(val, int) and 1 <= val <= 540:
                data["sltm"] = f"{val:04d}"
            else:
                raise ValueError("sleep_timer must be 0-540 minutes")
        else:
            raise ValueError(f"unknown setting: {key}")
    if not data:
        raise ValueError("no settings given")
    return data


class Dyson:
    def __init__(self, host, serial, product_type, password):
        self.host, self.serial = host, serial
        self.root = f"{product_type}/{serial}"
        self.raw, self.env_raw, self.faults = {}, {}, {}
        self.connected = False
        self.updated = None
        self._lock = threading.Lock()
        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        self._c.username_pw_set(serial, password)
        self._c.on_connect = self._on_connect
        self._c.on_disconnect = self._on_disconnect
        self._c.on_message = self._on_message
        self._c.reconnect_delay_set(1, 30)
        self.last_error = None
        self._heat_stopped = None  # monotonic time the heater was last switched off
        self._pending = None  # (expected raw fields, deadline) while the head is moving
        self.paused = True  # True until start(); also set by the kill switch

    def start(self, paused=False):
        threading.Thread(target=self._poll, daemon=True).start()
        self.paused = True
        if not paused:
            self.resume()

    def stop(self):
        self._c.loop_stop()
        self._c.disconnect()

    def pause(self):
        """Kill switch: drop the connection and send nothing until resume()."""
        self.paused = True
        self._c.disconnect()
        self._c.loop_stop()  # the network thread ends after a requested disconnect
        self.connected = False
        self._pending = None

    def resume(self):
        if not self.paused:
            return
        self.paused = False
        self._c.connect_async(self.host, 1883, keepalive=30)
        self._c.loop_start()

    # -- mqtt callbacks -----------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason, props):
        if reason.is_failure:
            self.last_error = str(reason)
            return
        self.connected, self.last_error = True, None
        client.subscribe([(f"{self.root}/status/current", 1), (f"{self.root}/status/faults", 1)])
        self.refresh()

    def _on_disconnect(self, client, userdata, flags, reason, props):
        self.connected = False
        if reason.is_failure:
            self.last_error = str(reason)

    def _on_message(self, client, userdata, msg):
        try:
            p = json.loads(msg.payload)
        except ValueError:
            return
        kind = p.get("msg")
        with self._lock:
            was_heating = _heater_enabled(self.raw)
            if kind == "CURRENT-STATE":
                self.raw.update(p.get("product-state", {}))
            elif kind == "STATE-CHANGE":  # values arrive as [old, new]
                self.raw.update({k: v[1] if isinstance(v, list) else v
                                 for k, v in p.get("product-state", {}).items()})
            elif kind == "ENVIRONMENTAL-CURRENT-SENSOR-DATA":
                self.env_raw.update(p.get("data", {}))
            elif kind in ("CURRENT-FAULTS", "FAULTS-CHANGE"):
                for group in ("product-errors", "product-warnings", "module-errors", "module-warnings"):
                    for k, v in p.get(group, {}).items():
                        self.faults[k] = v[1] if isinstance(v, list) else v
            else:
                return
            if was_heating and not _heater_enabled(self.raw):  # by us, the remote or the app
                self._heat_stopped = time.monotonic()
            self.updated = time.time()

    # -- commands -----------------------------------------------------------
    def _send(self, payload):
        # QoS 0 and only while connected: a command must never be queued and
        # delivered later (e.g. heat-on arriving after the fan rejoins Wi-Fi).
        if self.paused or not self.connected:
            return False
        payload["time"] = _now()
        self._c.publish(f"{self.root}/command", json.dumps(payload), qos=0)
        return True

    def refresh(self):
        self._send({"msg": "REQUEST-CURRENT-STATE"})
        self._send({"msg": "REQUEST-CURRENT-FAULTS"})
        self._send({"msg": "REQUEST-PRODUCT-ENVIRONMENT-CURRENT-SENSOR-DATA"})

    def _poll(self):
        while True:
            time.sleep(ENV_POLL_SECONDS)
            if self.connected:
                self.refresh()  # also re-reads state, in case a change notice was missed

    def set(self, changes):
        if self.paused:
            raise ConnectionError("disconnected from the fan (kill switch is on)")
        if not self.connected:
            raise ConnectionError("not connected to the fan")
        data = encode(changes, self.raw)
        with self._lock:
            before, after = _heater_enabled(self.raw), _heater_enabled({**self.raw, **data})
            wait = self.heat_guard_remaining()
            if after and not before and wait:
                raise HeatGuard(f"heater was just switched off; wait {wait}s before restarting it")
            if before and not after:
                self._heat_stopped = time.monotonic()
            if "osal" in data:  # direction change: the fan reports oscillation on while it travels
                expected = {k: data[k] for k in ("osal", "osau", "oson")}
                if data["osal"] == data["osau"]:
                    expected["oson"] = "OFF"  # a fixed aim ends with the head parked
                self._pending = (expected, time.monotonic() + ADJUST_TIMEOUT_SECONDS)
        if not self._send({"msg": "STATE-SET", "mode-reason": "LAPP", "data": data}):
            raise ConnectionError("not connected to the fan")
        return data

    def adjusting(self):
        """True while a direction change is still being carried out by the fan."""
        if self._pending is None:
            return False
        expected, deadline = self._pending
        if time.monotonic() > deadline or all(self.raw.get(k) == v for k, v in expected.items()):
            self._pending = None
            return False
        return True

    def current_settings(self):
        """The user-facing settings worth remembering as a preset."""
        s = self.snapshot()["state"]
        keys = ("power", "auto", "speed", "heat", "target_c", "night_mode", "brightness",
                "sweep_low", "sweep_high", "oscillation")
        return {k: s[k] for k in keys if s.get(k) is not None}

    def apply_settings(self, p):
        """Apply a preset in one command."""
        ch = {}
        if p.get("auto"):
            ch["speed"] = "auto"
        elif p.get("speed"):
            ch["speed"] = int(p["speed"])
        if "target_c" in p:
            ch["target_c"] = p["target_c"]
        if "heat" in p:
            ch["heat"] = bool(p["heat"])
        if "night_mode" in p:
            ch["night_mode"] = p["night_mode"]
        # Skip values the fan reported but won't accept back (set from the Dyson app).
        if p.get("brightness") in BRIGHTNESS_LEVELS:
            ch["brightness"] = int(p["brightness"])
        lo, hi = p.get("sweep_low"), p.get("sweep_high")
        if lo is not None and hi is not None and (lo == hi or hi - lo >= 30):
            if lo == hi:
                ch["aim"] = int(lo)
            else:
                ch["sweep"] = [int(lo), int(hi)]
                if not p.get("oscillation"):
                    ch["oscillation"] = False  # keep the window but leave the head parked
        ch["power"] = bool(p.get("power", True))  # last, so "off" wins over speed's implied on
        return self.set(ch)

    def heat_guard_remaining(self):
        if self._heat_stopped is None:
            return 0
        return max(0, round(HEAT_GUARD_SECONDS - (time.monotonic() - self._heat_stopped)))

    # -- view ---------------------------------------------------------------
    def snapshot(self):
        with self._lock:
            r, e = dict(self.raw), dict(self.env_raw)
            faults = {k: v for k, v in self.faults.items() if v not in ("OK", "NONE")}
        fnsp = r.get("fnsp")
        return {
            "connected": self.connected,
            "paused": self.paused,
            "error": self.last_error,
            "serial": self.serial,
            "updated": self.updated,
            "heat_guard_s": self.heat_guard_remaining(),
            "adjusting": self.adjusting(),
            "state": {
                "power": _on(r.get("fpwr")),
                "auto": _on(r.get("auto")) if "auto" in r else (fnsp == "AUTO" if fnsp else None),
                "speed": _num(fnsp),
                # A fixed aim (equal angles) is never "oscillating", even mid-travel.
                "oscillation": _on(r.get("oson")) and r.get("osal") != r.get("osau"),
                "brightness": _num(r.get("bril")),
                "sweep_low": _num(r.get("osal")),
                "sweep_high": _num(r.get("osau")),
                "night_mode": _on(r.get("nmod")),
                "heat": _on(r.get("hmod")),
                "heating_now": _on(r.get("hsta")),
                "target_c": _num(r.get("hmax"), 0.1, -273.15),
                "front_airflow": _on(r.get("fdir")),
                "sleep_timer": _num(e.get("sltm")),
                "hepa_filter_pct": _num(r.get("hflr")),
                "carbon_filter_pct": _num(r.get("cflr")),
            },
            "env": {
                "temp_c": _num(e.get("tact"), 0.1, -273.15),
                "humidity": _num(e.get("hact")),
                "pm25": _num(e.get("p25r") if e.get("p25r") else e.get("pm25")),
                "pm10": _num(e.get("p10r") if e.get("p10r") else e.get("pm10")),
                "voc": _num(e.get("va10"), 0.1),
                "no2": _num(e.get("noxl"), 0.1),
                "hcho": _num(e.get("hchr") if e.get("hchr") else e.get("hcho")),
            },
            "faults": faults,
            "raw": {"state": r, "env": e},
        }
