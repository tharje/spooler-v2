"""
Shared runtime state — imported by every other module.

Keeping this separate from the rest of the codebase breaks all potential
circular imports: nothing here imports from within this project.
"""

import json
import os
import time

# Set DEBUG=1 in the environment to enable verbose MQTT/protocol logging.
DEBUG: bool = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

# Set once, the first time this module is imported — which happens very early
# in process startup — so (time.time() - START_TIME) is the server's uptime.
START_TIME: float = time.time()

# { printer_id: PrinterConnection }
printers: dict = {}

# Connected browser WebSocket clients
browser_clients: set = set()

# { printer_id: { str(tray_id): spoolman_spool_id } }
tray_map: dict = {}


def _client_can_see(client, printer_id: str) -> bool:
    """A browser WS client may only receive updates for printers its user can see.
    The user (or '' for login-off / single-admin) is stashed on the socket at
    connect time; '' sees everything."""
    user = getattr(client, "_spooler_user", "")
    if not user:
        return True
    try:
        import users
        return users.can_see_printer(user, printer_id)
    except Exception:
        return True


async def broadcast_to_browsers(msg: dict) -> None:
    if not browser_clients:
        return
    # Printer-specific messages are filtered per client by visibility.
    pid = msg.get("printer_id")
    if pid is None and isinstance(msg.get("printer"), dict):
        pid = msg["printer"].get("id")
    data = json.dumps(msg)
    dead = set()
    for client in list(browser_clients):
        try:
            if pid is not None and not _client_can_see(client, pid):
                continue
            await client.send(data)
        except Exception:
            dead.add(client)
    browser_clients.difference_update(dead)
