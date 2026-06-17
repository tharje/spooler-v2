"""
Shared runtime state — imported by every other module.

Keeping this separate from the rest of the codebase breaks all potential
circular imports: nothing here imports from within this project.
"""

import json
import os

# Set DEBUG=1 in the environment to enable verbose MQTT/protocol logging.
DEBUG: bool = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

# { printer_id: PrinterConnection }
printers: dict = {}

# Connected browser WebSocket clients
browser_clients: set = set()

# { printer_id: { str(tray_id): spoolman_spool_id } }
tray_map: dict = {}


async def broadcast_to_browsers(msg: dict) -> None:
    if not browser_clients:
        return
    data = json.dumps(msg)
    dead = set()
    for client in list(browser_clients):
        try:
            await client.send(data)
        except Exception:
            dead.add(client)
    browser_clients.difference_update(dead)
