#!/usr/bin/env python3
"""One-off: log in to your MyDyson account and save the fan's local MQTT
credential to .env. Standard library only (uses the `openssl` CLI for AES).

    python3 scripts/get_credentials.py [--country US] [--host <fan's LAN IP>]

Talks only to appapi.cp.dyson.com. Your account password and token are never
written to disk; only the device's local credential is.
"""
import argparse
import base64
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://appapi.cp.dyson.com"
UA = {"User-Agent": "android client"}
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")


def call(method, path, params=None, body=None, token=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = dict(UA)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        sys.exit(f"Dyson API {method} {path} failed: HTTP {e.code} {detail}")


def decrypt(blob):
    """Dyson local credentials: base64(AES-256-CBC), fixed key 1..32, zero IV."""
    key = bytes(range(1, 33)).hex()
    out = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-K", key, "-iv", "00" * 16],
        input=base64.b64decode(blob), capture_output=True, check=True,
    ).stdout
    return json.loads(out)


def devices(token):
    """Normalise devices from the v3 manifest, falling back to v2."""
    found = []
    try:
        for d in call("GET", "/v3/manifest", token=token) or []:
            mqtt = (d.get("connectedConfiguration") or {}).get("mqtt") or {}
            if mqtt.get("localBrokerCredentials"):
                found.append({
                    "serial": d.get("serialNumber"), "name": d.get("name"),
                    "type": mqtt.get("mqttRootTopicLevel") or d.get("type"),
                    "model": d.get("model") or d.get("productName"),
                    "creds": mqtt["localBrokerCredentials"],
                })
    except SystemExit as e:
        print(f"(v3 manifest unavailable: {e}; trying v2)")
    if not found:
        for d in call("GET", "/v2/provisioningservice/manifest", token=token) or []:
            if d.get("LocalCredentials"):
                found.append({
                    "serial": d.get("Serial"), "name": d.get("Name"),
                    "type": d.get("ProductType"), "model": None,
                    "creds": d["LocalCredentials"],
                })
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="US")
    ap.add_argument("--culture", default="en-US")
    ap.add_argument("--host", help="fan's LAN IP (asked for if omitted)")
    ap.add_argument("--serial", help="pick this device if the account has several")
    a = ap.parse_args()
    q = {"country": a.country, "culture": a.culture}
    host = a.host or input("Fan's LAN IP address: ").strip()

    # Dyson answers 401 to everything until the client has made this call.
    call("GET", "/v1/provisioningservice/application/Android/version")
    email = input("MyDyson email: ").strip()
    status = call("POST", "/v3/userregistration/email/userstatus",
                  {"country": a.country}, {"email": email})
    if status.get("accountStatus") != "ACTIVE":
        sys.exit(f"Account not active for country {a.country}: {status}")

    challenge = call("POST", "/v3/userregistration/email/auth", q, {"email": email})
    print("Dyson has emailed you a verification code.")
    otp = input("Code from email: ").strip()
    password = getpass.getpass("MyDyson password (not stored): ")
    auth = call("POST", "/v3/userregistration/email/verify", q, {
        "email": email, "password": password,
        "challengeId": challenge["challengeId"], "otpCode": otp,
    })

    devs = devices(auth["token"])
    if not devs:
        sys.exit("No devices with local credentials on this account.")
    for d in devs:
        print(f"  found: {d['serial']}  type={d['type']}  {d['name'] or ''} {d['model'] or ''}")
    if a.serial:
        devs = [d for d in devs if d["serial"] == a.serial]
    if len(devs) != 1:
        sys.exit("Re-run with --serial <one of the serials above>.")
    d = devs[0]
    secret = decrypt(d["creds"])["apPasswordHash"]

    new = {"DYSON_HOST": host, "DYSON_SERIAL": d["serial"],
           "DYSON_TYPE": d["type"], "DYSON_PASSWORD": secret}
    keep = []  # other settings already in .env, e.g. DYSON_CENTER
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            keep = [ln.rstrip("\n") for ln in f if ln.split("=", 1)[0].strip() not in new]
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # also tighten a pre-existing file
    with os.fdopen(fd, "w") as f:
        f.write("\n".join([f"{k}={v}" for k, v in new.items()] + keep) + "\n")
    print(f"Wrote {os.path.normpath(ENV_PATH)} (mode 600) for {d['serial']}.")


if __name__ == "__main__":
    main()
