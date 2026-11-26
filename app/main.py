"""hp11: LAN web UI + JSON API for a Dyson Hot+Cool purifier."""
import os
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from .dyson import BRIGHTNESS_LEVELS, Dyson, HeatGuard
from .store import Store, scheduler

ROOT = Path(__file__).resolve().parent


def _load_env():
    """Read ../.env if present (systemd also passes it via EnvironmentFile)."""
    f = ROOT.parent / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
try:
    fan = Dyson(os.environ["DYSON_HOST"], os.environ["DYSON_SERIAL"],
                os.environ.get("DYSON_TYPE", "527"), os.environ["DYSON_PASSWORD"])
except KeyError as e:
    raise SystemExit(f"Missing {e.args[0]}; run scripts/get_credentials.py to create .env")


CENTER = int(os.environ.get("DYSON_CENTER", "180"))  # angle that points straight ahead


store = Store(Path(os.environ.get("HP11_DATA", ROOT.parent / "data")) / "store.json")
_stop = threading.Event()


@asynccontextmanager
async def lifespan(app):
    fan.start(paused=store.view()["paused"])
    threading.Thread(target=scheduler, args=(fan, store, _stop), daemon=True).start()
    yield
    _stop.set()
    fan.stop()


app = FastAPI(title="hp11", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def state():
    return {**fan.snapshot(), "center": CENTER, "brightness_levels": BRIGHTNESS_LEVELS,
            "server_time": datetime.now().astimezone().strftime("%a %H:%M %Z")}


@app.post("/api/set")
def set_state(changes: dict = Body(...)):
    """Body: any of power, speed (1-10|"auto"), oscillation, night_mode, heat,
    target_c (1-37), aim (5-355 degrees), sweep ([low, high]), brightness (1-3), front_airflow, sleep_timer (minutes, 0 = off)."""
    try:
        return {"sent": fan.set(changes)}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HeatGuard as e:
        raise HTTPException(429, str(e))
    except ConnectionError as e:
        raise HTTPException(503, str(e))


def _guarded(fn):
    try:
        return {"sent": fn()}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HeatGuard as e:
        raise HTTPException(429, str(e))
    except ConnectionError as e:
        raise HTTPException(503, str(e))
    except KeyError:
        raise HTTPException(404, "not found")


@app.post("/api/connection")
def connection(body: dict = Body(...)):
    """Kill switch. {"connected": false} drops the link to the fan and blocks every
    command, schedules included, until {"connected": true}. Survives restarts."""
    want = bool(body.get("connected"))
    store.set_paused(not want)
    fan.resume() if want else fan.pause()
    return {"paused": fan.paused}


@app.get("/api/config")
def config():
    return store.view()


@app.post("/api/presets")
def save_preset(body: dict = Body(...)):
    """Remember the fan's current settings under body["name"]."""
    if not fan.raw:
        raise HTTPException(503, "no state from the fan yet")
    try:
        store.save_preset(body.get("name"), fan.current_settings())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return store.view()


@app.post("/api/presets/{name}/apply")
def apply_preset(name: str):
    return _guarded(lambda: fan.apply_settings(store.preset(name)))


@app.delete("/api/presets/{name}")
def delete_preset(name: str):
    try:
        store.delete_preset(name)
    except KeyError:
        raise HTTPException(404, "not found")
    return store.view()


@app.post("/api/schedules")
def add_schedule(body: dict = Body(...)):
    try:
        store.add_schedule(body.get("time"), body.get("days"), body.get("action"),
                           body.get("preset"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return store.view()


@app.post("/api/schedules/{sid}/enabled")
def enable_schedule(sid: str, body: dict = Body(...)):
    try:
        store.update_schedule(sid, enabled=bool(body.get("enabled")))
    except KeyError:
        raise HTTPException(404, "not found")
    return store.view()


@app.delete("/api/schedules/{sid}")
def delete_schedule(sid: str):
    try:
        store.delete_schedule(sid)
    except KeyError:
        raise HTTPException(404, "not found")
    return store.view()


@app.post("/api/refresh")
def refresh():
    if fan.paused or not fan.connected:
        raise HTTPException(503, "not connected to the fan")
    fan.refresh()
    return {"ok": True}


@app.get("/healthz")
def healthz():
    if fan.paused:
        return {"ok": True, "paused": True}
    if not fan.connected:
        raise HTTPException(503, "fan disconnected")
    return {"ok": True}
