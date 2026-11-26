# hp11

A small LAN-only web app and JSON API for a Dyson Purifier Hot+Cool, talking to
the fan's own MQTT broker. After a one-off login to fetch the fan's local
credential, nothing goes through Dyson's cloud.

Built and tested against one unit that reports product type `527M`. Other
Hot+Cool models use the same protocol but may differ in details.

- Power, fan speed and auto, heat and target temperature, night mode, display brightness
- Top-down direction dial: point the head at a fixed angle, or set a custom sweep
- Presets ("remember this setting") and a weekly schedule
- Temperature and particulate readings
- A disconnect switch that stops the app talking to the fan

**No authentication.** Anyone who can reach the port can control the fan,
including its heater. Keep it on a trusted LAN; do not expose it to the internet.

## Setup

Give the fan a DHCP reservation first; the app is configured by IP.

### 1. Get the fan's local credential (once)

    python3 scripts/get_credentials.py

Prompts for the fan's IP, your MyDyson email, the emailed code and your password,
then writes `.env` (mode 600) with the fan's local MQTT credential. Only the
standard library and the `openssl` CLI are needed. Your account password and
token are not stored.

### 2. Run it

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8011

Then open `http://<host>:8011`.

### 3. Install as a service (Debian container or VM)

For example, in a Proxmox LXC (adjust id, storage and template):

    pct create 230 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
      --hostname hp11 --unprivileged 1 --cores 1 --memory 256 --swap 0 \
      --rootfs local-lvm:2 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
      --onboot 1 --start 1

Inside it, as root:

    apt-get update && apt-get install -y git
    git clone https://github.com/20015jjw/hp11.git && cd hp11
    python3 scripts/get_credentials.py      # or copy an existing .env here
    sh deploy/install.sh

`install.sh` installs to `/opt/hp11`, runs the app as the `hp11` user under
systemd and prints the URL. To update: `git pull && sh deploy/install.sh`.
Logs: `journalctl -u hp11 -f`.

## Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `DYSON_HOST` | The fan's LAN IP |
| `DYSON_SERIAL` | Serial number; also the MQTT username |
| `DYSON_TYPE` | Product type, the root of the MQTT topics (e.g. `527M`) |
| `DYSON_PASSWORD` | Local MQTT credential from step 1 |
| `DYSON_CENTER` | Angle at which the head points straight ahead. Nominally 180; find yours by eye. Higher angles turn the head to the machine's right. |
| `HP11_DATA` | Where presets and schedules are stored. Default `./data`; the service uses `/var/lib/hp11`. |

## Behaviour worth knowing

- **Heater restart guard.** The app refuses to switch the heater back on within
  60 seconds of it being switched off, from any source.
- **Schedules** run on the app host's local clock, shown on the page as "App
  clock". Set the container's timezone (`timedatectl set-timezone <zone>`).
  A scheduled "on" resumes the fan's last settings, heat included; a scheduled
  preset applies exactly what was saved. A schedule that can't run (fan offline,
  guard, disconnected) is retried for five minutes, then skipped for the day.
- **Disconnect** persists across restarts until you reconnect.

## API

    GET  /api/state      state, sensors, faults, raw Dyson fields
    POST /api/set        any of: {"power":bool, "speed":1-10|"auto", "heat":bool,
                         "target_c":1-37, "oscillation":bool, "aim":5-355,
                         "sweep":[low,high], "night_mode":bool, "brightness":1-3,
                         "front_airflow":bool, "sleep_timer":minutes}
    POST /api/connection {"connected":false}   disconnect / reconnect
    GET  /api/config     presets and schedules
    POST /api/presets    {"name":"Evening"}    remember the current settings
    POST /api/presets/<name>/apply             DELETE /api/presets/<name>
    POST /api/schedules  {"time":"07:00", "days":[0-6, 0=Mon],
                         "action":"off"|"on"|"preset", "preset":name}
    POST /api/schedules/<id>/enabled {"enabled":bool}
    DELETE /api/schedules/<id>
    POST /api/refresh    ask the fan to resend everything
    GET  /healthz        200 when connected (or deliberately disconnected)

`scripts/heat_test.py` checks whether your unit accepts a heat-on command over
local MQTT.
