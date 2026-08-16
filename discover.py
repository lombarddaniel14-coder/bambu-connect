"""
discover.py - Find a Bambu Lab printer on the local network.

Two methods run together:
  1. SSDP listen  - Bambu printers periodically broadcast a NOTIFY to
                    239.255.255.250:2021. The packet contains the printer's
                    IP, serial (USN) and model. This is the authoritative
                    source (gives IP<->serial mapping) but only fires if the
                    printer is powered on and LAN discovery is enabled.
  2. Port scan    - Scan the local /24 for TCP 8883 (MQTT-TLS) and 990 (FTPS).
                    A host with both open is almost certainly the printer.

Usage:  py discover.py [--timeout 12]
"""
import socket, struct, threading, ipaddress, argparse, sys, time

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 2021          # Bambu uses 2021, NOT the standard 1900
MQTT_TLS_PORT = 8883
FTPS_PORT = 990


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def ssdp_listen(results, stop_evt, timeout):
    """Passively listen for Bambu SSDP NOTIFY broadcasts on UDP 2021."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", SSDP_PORT))
        mreq = struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as e:
        print(f"[ssdp] could not bind/join multicast on {SSDP_PORT}: {e}")
        return
    sock.settimeout(1.0)
    print(f"[ssdp] listening on udp/{SSDP_PORT} for {timeout}s ...")
    end = time.time() + timeout
    while time.time() < end and not stop_evt.is_set():
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        text = data.decode("utf-8", "ignore")
        if "bambu" in text.lower() or "USN" in text or "3dprinter" in text.lower():
            info = {"ip": addr[0], "raw": text}
            for line in text.splitlines():
                low = line.lower()
                if low.startswith("usn:"):
                    info["serial"] = line.split(":", 1)[1].strip()
                elif low.startswith("devmodel.bambu.com:") or low.startswith("model:"):
                    info["model"] = line.split(":", 1)[1].strip()
                elif low.startswith("devname.bambu.com:"):
                    info["name"] = line.split(":", 1)[1].strip()
                elif low.startswith("location:"):
                    info["location"] = line.split(":", 1)[1].strip()
            results["ssdp"].append(info)
            print(f"[ssdp] *** Bambu device at {addr[0]}  serial={info.get('serial','?')}  model={info.get('model','?')}")
    sock.close()


def check_port(ip, port, timeout=0.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def scan_host(ip, results, lock):
    if check_port(ip, MQTT_TLS_PORT):
        ftps = check_port(ip, FTPS_PORT)
        with lock:
            results["scan"].append({"ip": ip, "mqtt_8883": True, "ftps_990": ftps})
        print(f"[scan] {ip}: 8883 OPEN  990 {'OPEN' if ftps else 'closed'}  <-- likely Bambu printer")


def port_scan(results, timeout):
    lip = local_ip()
    net = ipaddress.ip_network(lip + "/24", strict=False)
    print(f"[scan] scanning {net} for tcp/{MQTT_TLS_PORT} (this host = {lip}) ...")
    lock = threading.Lock()
    threads = []
    for host in net.hosts():
        t = threading.Thread(target=scan_host, args=(str(host), results, lock))
        t.daemon = True
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=12)
    args = ap.parse_args()

    results = {"ssdp": [], "scan": []}
    stop_evt = threading.Event()

    ssdp_t = threading.Thread(target=ssdp_listen, args=(results, stop_evt, args.timeout))
    ssdp_t.start()
    port_scan(results, timeout=8)
    ssdp_t.join()

    print("\n================ RESULTS ================")
    if results["ssdp"]:
        print("SSDP found (authoritative — includes serial):")
        for d in results["ssdp"]:
            print(f"  IP={d['ip']}  serial={d.get('serial','?')}  model={d.get('model','?')}  name={d.get('name','?')}")
    else:
        print("SSDP: no Bambu broadcast heard (printer off, on another VLAN, or mcast blocked).")
    if results["scan"]:
        print("\nPort scan candidates (8883 open):")
        for d in results["scan"]:
            print(f"  IP={d['ip']}  mqtt=8883 open  ftps={'open' if d['ftps_990'] else 'closed'}")
    else:
        print("\nPort scan: no host with 8883 open on this /24.")

    if not results["ssdp"] and not results["scan"]:
        print("\n>>> Printer not found. Make sure it is powered on, on the SAME")
        print(">>> network as this PC (10.0.0.x), and not isolated by a guest/IoT VLAN.")


if __name__ == "__main__":
    main()
