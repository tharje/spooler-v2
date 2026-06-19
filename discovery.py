"""
UDP broadcast discovery for Elegoo printers.
"""

import concurrent.futures
import json
import socket
import time

DISCOVERY_PORT = 3000
MQTT_PORT      = 1883


def _get_local_ip() -> str | None:
    """Return the LAN IP of this machine by routing a dummy UDP packet."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _probe_mqtt(ip: str, timeout: float) -> bool:
    """Return True if port 1883 accepts a TCP connection."""
    try:
        with socket.create_connection((ip, MQTT_PORT), timeout=timeout):
            return True
    except Exception:
        return False


def discover_cc2_printers(known_ips: set, timeout: float = 3.0) -> list:
    """Scan the local /24 subnet for devices with port 1883 open.

    Returns a list of IP strings for potential CC2 printers not already known.
    Runs in a thread pool executor — safe to call from asyncio via run_in_executor.
    """
    local_ip = _get_local_ip()
    if not local_ip or local_ip.startswith("127."):
        return []
    prefix = ".".join(local_ip.split(".")[:3])
    candidates = [
        f"{prefix}.{i}" for i in range(1, 255)
        if f"{prefix}.{i}" not in known_ips and f"{prefix}.{i}" != local_ip
    ]
    probe_timeout = max(0.2, min(0.4, timeout / 4))
    found = []
    deadline = time.time() + timeout
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        futures = {ex.submit(_probe_mqtt, ip, probe_timeout): ip for ip in candidates}
        for fut in concurrent.futures.as_completed(futures):
            ip = futures[fut]
            try:
                if fut.result():
                    found.append(ip)
            except Exception:
                pass
            if time.time() > deadline:
                break
    return found


def discover_printers(timeout: float = 3.0) -> list:
    """Send UDP broadcast M99999 and collect printer responses."""
    found = []
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        sock.bind(("", 0))
        sock.sendto(b"M99999", ("<broadcast>", DISCOVERY_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                try:
                    info = json.loads(data.decode("utf-8"))
                    info["_ip"] = addr[0]
                    found.append(info)
                except Exception:
                    pass
            except socket.timeout:
                break
    except Exception as e:
        print(f"[Discovery] Error: {e}")
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
    return found
