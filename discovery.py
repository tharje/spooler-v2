"""
UDP broadcast discovery for Elegoo printers.
"""

import json
import socket
import time

DISCOVERY_PORT = 3000


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
