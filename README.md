# Bambu Connect — local API for Daniel's Bambu Lab P1S

A small Python toolkit that talks to the printer **directly over your LAN** (no
cloud): live status over MQTT-TLS, and file transfer over FTPS. Everything you
need is already wired up except the printer's **IP address**.

## What was auto-detected from Bambu Studio

Pulled from `C:\Users\Daniel\AppData\Roaming\BambuStudio\BambuStudio.conf`:

| Field | Value | Source |
|---|---|---|
| Printer model | **Bambu Lab P1S** (0.4 nozzle) | `models` block |
| Serial (dev id) | **`YOUR_PRINTER_SERIAL`** | `access_code` / `calis` blocks |
| LAN access code | **`YOUR_ACCESS_CODE`** | `user_access_code` block |
| Region | North America | `app.region` |
| MQTT/FTP TLS | enabled | `enable_ssl_for_mqtt/ftp` |

Serial + access code are the two hard-to-get credentials, and they're already in
`config.json`. **The only thing missing is the printer's IP.**

> Note: your logs showed no local IP, which means Bambu Studio was talking to the
> printer through **Bambu Cloud**, not LAN mode. For this toolkit to control the
> printer you'll want **LAN Mode** on (see below). If the access code was ever
> reset, re-check it on the printer screen — the value above is what Studio saved.

## One-time setup on the printer (do this first)

1. On the P1S screen: **Settings (gear) → General → check the Access Code** —
   confirm it's `YOUR_ACCESS_CODE`. If different, update `ip`/`access_code` in
   `config.json`.
2. Settings → **turn on "LAN Only Mode"** (recommended) — this is what keeps the
   local MQTT + FTPS broker open on current firmware. (If you'd rather stay on
   cloud, local *status* may still work but control is unreliable — LAN mode is
   the supported path.)
3. Make sure the printer is on the **same network** as this PC. This PC is on
   `10.0.0.x`. If the printer is on a "guest" or IoT Wi-Fi, they won't see each
   other.

## Find the printer's IP

Easiest: on the P1S screen, **Settings → WLAN** shows the IP. Put it in
`config.json` under `"ip"`.

Or auto-discover it (printer must be powered on):

```
cd "C:\Users\Daniel\Claude\Projects\Bambu Connect"
py discover.py
```

This listens for the printer's SSDP broadcast **and** scans `10.0.0.0/24` for the
MQTT port. When found it prints the IP (and serial).

## Use it

```
py bambu.py status          # connect, dump temps/progress/state, exit
py bambu.py monitor         # live-updating dashboard, Ctrl-C to quit
py bambu.py light on        # chamber light on/off
py bambu.py pause           # pause / resume / stop the current print
py bambu.py resume
py bambu.py stop
py bambu.py home            # G28 home all axes
py bambu.py nozzle 210      # set nozzle target temp
py bambu.py bed 60          # set bed target temp
py bambu.py speed 2         # 1 silent / 2 standard / 3 sport / 4 ludicrous
py bambu.py gcode "G28"     # send a raw G-code line
py bambu.py files           # list files on the printer (FTPS)
py bambu.py upload model.3mf
py bambu.py raw '{"print":{"sequence_id":"0","command":"pause"}}'
```

Config can also come from env vars: `BAMBU_IP`, `BAMBU_SERIAL`, `BAMBU_ACCESS_CODE`.

## How it works (the local API)

- **MQTT over TLS, port 8883.** Username `bblp`, password = access code. The
  printer's cert is self-signed, so the client encrypts but skips verification
  (standard for the Bambu local API).
  - Subscribe to `device/<serial>/report` → a stream of JSON status.
  - Publish to `device/<serial>/request` → commands.
  - `{"pushing":{"command":"pushall"}}` asks for one full state dump (otherwise
    you only get deltas as things change).
- **FTPS, implicit TLS, port 990.** Same `bblp` / access-code login. Used to list
  and upload `.3mf` / `.gcode` files.

## Files

- `config.json` — credentials (serial + access code pre-filled; add the IP).
- `discover.py` — find the printer on the LAN (SSDP + port scan).
- `bambu.py` — the client + CLI (MQTT status/commands, FTPS files).

## Fallback: Bambu Cloud API (only if LAN mode is a no-go)

If you can't use LAN mode (e.g. printer must stay cloud-connected on a network
where this PC can't reach it), there's a cloud path — **not built here** because
it needs interactive login this session can't do:

1. `POST https://api.bambulab.com/v1/user-service/user/login` with your Bambu
   email/password → returns a bearer token. Bambu accounts have **2FA/email
   verification**, so this needs you at the keyboard.
2. With the token, connect to Bambu's cloud MQTT broker
   `us.mqtt.bambulab.com:8883`, username `u_<userid>`, password = token, and
   subscribe to the same `device/<serial>/report` topic.

Say the word and I'll build a `cloud.py` that walks you through the login and
reuses the same status/command code. **LAN mode is simpler, faster, private, and
recommended** — try that first.

## Community references

- Bambu Lab Wiki — [Third-party integration](https://wiki.bambulab.com/en/software/third-party-integration)
  and [Enable LAN mode](https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode)
- [OpenBambuAPI (bambu-research-group)](https://github.com/Doridian/OpenBambuAPI) — the reverse-engineered MQTT/FTP protocol reference
- [ha-bambulab (greghesp)](https://github.com/greghesp/ha-bambulab) — the Home Assistant integration, good real-world command examples
