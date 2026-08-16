"""
bambu.py - Local API client for a Bambu Lab P1S (works for P1P/X1/A1 too).

Talks to the printer entirely over your LAN, no cloud:
  * MQTT over TLS (port 8883) for live status + commands
  * FTPS (implicit TLS, port 990) for the file system / uploads

Credentials come from config.json (or BAMBU_* env vars):
  serial       device id, used in the MQTT topic  device/<serial>/{report,request}
  access_code  the LAN access code (MQTT/FTP password, username is always 'bblp')
  ip           the printer's LAN IP address

The printer's TLS cert is self-signed, so we intentionally skip verification
(standard practice for the Bambu local API — the connection is still encrypted).

Subcommands:
  status                 connect, request a full state push, print a summary, exit
  monitor                stream live status until Ctrl-C
  pause | resume | stop  print job control
  light on | light off   chamber light
  speed <1-4>            1=silent 2=standard 3=sport 4=ludicrous
  gcode "<G-CODE>"       send a raw G-code line (e.g. "G28")
  nozzle <temp>          set nozzle target temp (M104)
  bed <temp>             set bed target temp (M140)
  home                   home all axes (G28)
  files                  list files on the printer over FTPS
  upload <path>          upload a .3mf/.gcode file to the printer over FTPS
  raw '<json>'           publish a raw JSON command object to the request topic

Examples:
  py bambu.py status
  py bambu.py monitor
  py bambu.py light on
  py bambu.py gcode "G28"
"""
import json, os, sys, ssl, time, argparse, threading
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")


# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #
def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["serial"] = os.environ.get("BAMBU_SERIAL", cfg.get("serial", ""))
    cfg["access_code"] = os.environ.get("BAMBU_ACCESS_CODE", cfg.get("access_code", ""))
    cfg["ip"] = os.environ.get("BAMBU_IP", cfg.get("ip", ""))
    missing = [k for k in ("serial", "access_code", "ip") if not cfg.get(k)]
    if missing:
        sys.exit(
            f"[config] missing required field(s): {', '.join(missing)}\n"
            f"          Edit {CONFIG_PATH} (or set BAMBU_IP / BAMBU_SERIAL / "
            f"BAMBU_ACCESS_CODE).\n"
            f"          serial + access_code are pre-filled; you almost certainly\n"
            f"          just need to add the printer's LAN 'ip'."
        )
    return cfg


# --------------------------------------------------------------------------- #
#  MQTT client
# --------------------------------------------------------------------------- #
import paho.mqtt.client as mqtt


class BambuMQTT:
    def __init__(self, cfg, on_report=None, verbose=False):
        self.cfg = cfg
        self.on_report = on_report
        self.verbose = verbose
        self.report_topic = f"device/{cfg['serial']}/report"
        self.request_topic = f"device/{cfg['serial']}/request"
        self.connected = threading.Event()
        self.latest = {}

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-connect-{int(time.time())}",
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set("bblp", cfg["access_code"])
        # Self-signed cert on the printer -> encrypt but don't verify.
        self.client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.tls_insecure_set(True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    # -- callbacks ---------------------------------------------------------- #
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(self.report_topic)
            self.connected.set()
            if self.verbose:
                print(f"[mqtt] connected, subscribed to {self.report_topic}")
        else:
            print(f"[mqtt] connect failed: {reason_code} "
                  f"(bad access code? wrong IP? LAN mode off?)")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if self.verbose:
            print(f"[mqtt] disconnected ({reason_code})")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", "ignore"))
        except json.JSONDecodeError:
            return
        # merge the 'print' block so a partial update doesn't wipe prior state
        if "print" in payload:
            self.latest.setdefault("print", {}).update(payload["print"])
        else:
            self.latest.update(payload)
        if self.on_report:
            self.on_report(payload, self.latest)

    # -- lifecycle ---------------------------------------------------------- #
    def connect(self, timeout=10):
        self.client.connect(self.cfg["ip"], 8883, keepalive=60)
        self.client.loop_start()
        if not self.connected.wait(timeout):
            raise TimeoutError(
                f"could not connect to {self.cfg['ip']}:8883 within {timeout}s "
                f"(printer off? wrong IP? not on this LAN?)"
            )

    def disconnect(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def publish(self, obj):
        self.client.publish(self.request_topic, json.dumps(obj))

    # -- commands ----------------------------------------------------------- #
    def request_full_status(self):
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})

    def print_cmd(self, command):
        self.publish({"print": {"sequence_id": "0", "command": command}})

    def gcode_line(self, line):
        if not line.endswith("\n"):
            line += "\n"
        self.publish({"print": {"sequence_id": "0", "command": "gcode_line", "param": line}})

    def set_light(self, on):
        self.publish({"system": {"sequence_id": "0", "command": "ledctrl",
                                 "led_node": "chamber_light",
                                 "led_mode": "on" if on else "off",
                                 "led_on_time": 500, "led_off_time": 500,
                                 "loop_times": 0, "interval_time": 0}})

    def set_speed(self, level):
        # 1 silent, 2 standard, 3 sport, 4 ludicrous
        self.publish({"print": {"sequence_id": "0", "command": "print_speed",
                                "param": str(level)}})


# --------------------------------------------------------------------------- #
#  Status formatting
# --------------------------------------------------------------------------- #
def summarize(latest):
    p = latest.get("print", {})
    if not p:
        return "(no status received yet)"
    state = p.get("gcode_state", "?")
    lines = [
        f"  state         : {state}",
        f"  job           : {p.get('subtask_name', '-')}",
        f"  progress      : {p.get('mc_percent', '?')}%   "
        f"(layer {p.get('layer_num', '?')}/{p.get('total_layer_num', '?')})",
        f"  time left     : {p.get('mc_remaining_time', '?')} min",
        f"  nozzle temp   : {p.get('nozzle_temper', '?')} / "
        f"{p.get('nozzle_target_temper', '?')} C",
        f"  bed temp      : {p.get('bed_temper', '?')} / "
        f"{p.get('bed_target_temper', '?')} C",
        f"  chamber temp  : {p.get('chamber_temper', '?')} C",
        f"  fan (part)    : {p.get('cooling_fan_speed', '?')}",
        f"  speed level   : {p.get('spd_lvl', '?')}  ({p.get('spd_mag', '?')}%)",
        f"  wifi signal   : {p.get('wifi_signal', '?')}",
        f"  nozzle/plate  : {p.get('nozzle_diameter', '?')}mm / bed {p.get('bed_type', '?')}",
    ]
    lights = p.get("lights_report", [])
    if lights:
        chamber = next((l.get("mode") for l in lights if l.get("node") == "chamber_light"), "?")
        lines.append(f"  chamber light : {chamber}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  FTPS (implicit TLS on 990)
# --------------------------------------------------------------------------- #
from ftplib import FTP_TLS


class ImplicitFTP_TLS(FTP_TLS):
    """FTP_TLS variant that wraps the control socket in TLS immediately
    (Bambu uses *implicit* FTPS on port 990; stdlib defaults to explicit)."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value


def ftps_connect(cfg):
    ctx = ssl._create_unverified_context()
    ftp = ImplicitFTP_TLS(context=ctx)
    ftp.connect(host=cfg["ip"], port=990, timeout=10)
    ftp.login(user="bblp", passwd=cfg["access_code"])
    ftp.prot_p()
    return ftp


def ftps_list(cfg, path="/"):
    ftp = ftps_connect(cfg)
    try:
        print(f"[ftps] listing {path} on {cfg['ip']}")
        entries = []
        ftp.retrlines("LIST " + path, entries.append)
        for e in entries:
            print("  " + e)
        if not entries:
            print("  (empty)")
    finally:
        ftp.quit()


def ftps_upload(cfg, local_path):
    local = Path(local_path)
    if not local.exists():
        sys.exit(f"[ftps] file not found: {local}")
    ftp = ftps_connect(cfg)
    try:
        remote = "/" + local.name
        with local.open("rb") as fh:
            print(f"[ftps] uploading {local} -> {remote}")
            ftp.storbinary(f"STOR {remote}", fh)
        print("[ftps] upload complete")
    finally:
        ftp.quit()


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def cmd_status(cfg):
    got = threading.Event()

    def on_report(payload, latest):
        if latest.get("print", {}).get("gcode_state") is not None:
            got.set()

    m = BambuMQTT(cfg, on_report=on_report, verbose=True)
    m.connect()
    m.request_full_status()
    got.wait(6)
    print("\n===== PRINTER STATUS =====")
    print(f"  model         : {cfg.get('printer_model', '?')}")
    print(f"  ip / serial   : {cfg['ip']} / {cfg['serial']}")
    print(summarize(m.latest))
    m.disconnect()


def cmd_monitor(cfg):
    last = [0.0]

    def on_report(payload, latest):
        now = time.time()
        if now - last[0] < 1.0:
            return
        last[0] = now
        os_clear = "\033[2J\033[H"
        print(os_clear + "===== BAMBU LIVE MONITOR (Ctrl-C to quit) =====")
        print(f"  {time.strftime('%H:%M:%S')}  {cfg.get('printer_model')}  {cfg['ip']}")
        print(summarize(latest))

    m = BambuMQTT(cfg, on_report=on_report, verbose=True)
    m.connect()
    m.request_full_status()
    print("[monitor] streaming... Ctrl-C to stop")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[monitor] stopping")
    finally:
        m.disconnect()


def _simple(cfg, fn, msg):
    m = BambuMQTT(cfg, verbose=True)
    m.connect()
    fn(m)
    time.sleep(1.0)  # let the publish flush
    print(msg)
    m.disconnect()


def main():
    ap = argparse.ArgumentParser(description="Bambu Lab P1S local API client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("monitor")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("stop")
    sub.add_parser("home")
    sub.add_parser("files")
    p_light = sub.add_parser("light"); p_light.add_argument("state", choices=["on", "off"])
    p_speed = sub.add_parser("speed"); p_speed.add_argument("level", type=int, choices=[1, 2, 3, 4])
    p_gcode = sub.add_parser("gcode"); p_gcode.add_argument("line")
    p_noz = sub.add_parser("nozzle"); p_noz.add_argument("temp", type=int)
    p_bed = sub.add_parser("bed"); p_bed.add_argument("temp", type=int)
    p_up = sub.add_parser("upload"); p_up.add_argument("path")
    p_raw = sub.add_parser("raw"); p_raw.add_argument("json")
    args = ap.parse_args()

    cfg = load_config()

    try:
        dispatch(args, cfg)
    except (OSError, TimeoutError, ssl.SSLError) as e:
        print(f"\n[error] could not reach the printer at {cfg['ip']}:")
        print(f"        {type(e).__name__}: {e}")
        print( "        checklist:")
        print( "          - printer powered on and on the SAME LAN as this PC (10.0.0.x)?")
        print( "          - correct IP?  run:  py discover.py")
        print( "          - LAN Mode / 'LAN Only' enabled on the printer screen?")
        print( "          - access code matches the one on the printer screen?")
        sys.exit(1)


def dispatch(args, cfg):
    if args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "monitor":
        cmd_monitor(cfg)
    elif args.cmd == "pause":
        _simple(cfg, lambda m: m.print_cmd("pause"), "[cmd] pause sent")
    elif args.cmd == "resume":
        _simple(cfg, lambda m: m.print_cmd("resume"), "[cmd] resume sent")
    elif args.cmd == "stop":
        _simple(cfg, lambda m: m.print_cmd("stop"), "[cmd] stop sent")
    elif args.cmd == "home":
        _simple(cfg, lambda m: m.gcode_line("G28"), "[cmd] home (G28) sent")
    elif args.cmd == "light":
        _simple(cfg, lambda m: m.set_light(args.state == "on"), f"[cmd] light {args.state} sent")
    elif args.cmd == "speed":
        _simple(cfg, lambda m: m.set_speed(args.level), f"[cmd] speed {args.level} sent")
    elif args.cmd == "gcode":
        _simple(cfg, lambda m: m.gcode_line(args.line), f"[cmd] gcode '{args.line}' sent")
    elif args.cmd == "nozzle":
        _simple(cfg, lambda m: m.gcode_line(f"M104 S{args.temp}"), f"[cmd] nozzle -> {args.temp}C")
    elif args.cmd == "bed":
        _simple(cfg, lambda m: m.gcode_line(f"M140 S{args.temp}"), f"[cmd] bed -> {args.temp}C")
    elif args.cmd == "raw":
        _simple(cfg, lambda m: m.publish(json.loads(args.json)), "[cmd] raw json sent")
    elif args.cmd == "files":
        ftps_list(cfg)
    elif args.cmd == "upload":
        ftps_upload(cfg, args.path)


if __name__ == "__main__":
    main()
