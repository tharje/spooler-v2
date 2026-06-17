"""
Base printer connection: shared state, status tracking, history, and broadcast.

Subclasses (CC1Connection, CC2Connection, …) must implement:
  - connect()   — establish the transport and run the receive loop
  - send_cmd()  — send a CMD_* command to the printer
"""

import asyncio
import time

import state
from persistence import append_history, filament_mm_to_grams, save_printers
from printers.protocol import decode_printinfo
from spoolman import spoolman_deduct


class PrinterConnection:
    def __init__(
        self,
        printer_id: str,
        ip: str,
        name: str,
        mainboard_id: str = "",
        printer_type: str = "cc1",
        access_code: str = "",
    ):
        self.id           = printer_id
        self.ip           = ip
        self.name         = name
        self.mainboard_id = mainboard_id
        self.printer_type = printer_type
        self.access_code  = access_code
        self.connected    = False
        self.status: dict = {}
        self.attrs: dict  = {}
        self.camera_url: str | None = None
        self._task: asyncio.Task | None = None
        self._last_print_status = None
        self._print_start_time: float | None = None

    # ── Public interface ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        pi = decode_printinfo(self.status.get("PrintInfo", {}))
        filament_mm = pi.get("TotalExtrusion", 0) or 0
        # Replace raw PrintInfo (may have hex-encoded SDCP keys) with decoded version
        # so the browser can read plain field names like Filename directly.
        status = {**self.status, "PrintInfo": pi} if "PrintInfo" in self.status else self.status
        return {
            "id":              self.id,
            "ip":              self.ip,
            "name":            self.name,
            "printer_type":    self.printer_type,
            "has_access_code": bool(self.access_code),
            "mainboard_id":    self.mainboard_id,
            "connected":       self.connected,
            "status":          status,
            "attrs":           self.attrs,
            "camera_url":      self.camera_url,
            "filament_mm":     round(filament_mm, 1),
            "filament_g":      filament_mm_to_grams(filament_mm),
        }

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def start(self) -> None:
        try:
            while True:
                await self.connect()
                if not self.connected:
                    print(f"[Printer {self.name}] Retrying in 5 s …")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    # ── Subclass contract ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        raise NotImplementedError

    async def send_cmd(self, cmd: int, data: dict) -> bool:
        raise NotImplementedError

    async def request_file_list(self) -> bool:
        from printers.protocol import CMD_LIST_FILES
        return await self.send_cmd(CMD_LIST_FILES, {"Url": "/", "IsDir": True})

    async def start_print_file(self, filename: str) -> bool:
        from printers.protocol import CMD_START
        return await self.send_cmd(CMD_START, {"Filename": filename})

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _broadcast_state(self) -> None:
        await state.broadcast_to_browsers({
            "type":    "printer_update",
            "printer": self.to_dict(),
        })

    async def _check_print_transition(self) -> None:
        pi = decode_printinfo(self.status.get("PrintInfo", {}))
        cur_status = pi.get("Status")

        ACTIVE   = {1, 2, 3, 4, 7, 9, 10, 12, 13, 15, 16, 18, 19, 20, 21}
        PRINTING = {2, 3, 4, 13}

        if cur_status in ACTIVE and self._last_print_status not in ACTIVE:
            self._print_start_time = time.time()

        if cur_status in (9, 8, 14, 0) and self._last_print_status in PRINTING | {5, 6}:
            filament_mm = pi.get("TotalExtrusion", 0) or 0
            filename    = pi.get("Filename", "")
            print_time  = pi.get("PrintTime", 0) or 0
            completed   = cur_status == 9
            if filament_mm > 0 or filename:
                entry = {
                    "timestamp":    time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "printer_id":   self.id,
                    "printer_name": self.name,
                    "filename":     filename,
                    "filament_mm":  round(filament_mm, 1),
                    "filament_g":   filament_mm_to_grams(filament_mm),
                    "print_time_s": int(print_time),
                    "completed":    completed,
                }
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, append_history, entry)
                label = "Completed" if completed else "Cancelled"
                print(f"[History] {label}: {filename} – {filament_mm:.0f}mm / "
                      f"{filament_mm_to_grams(filament_mm)}g")
                await state.broadcast_to_browsers({"type": "history_entry", "entry": entry})
                if filament_mm > 0:
                    loop.run_in_executor(
                        None,
                        spoolman_deduct,
                        self.id,
                        filament_mm_to_grams(filament_mm),
                        loop,
                    )

        self._last_print_status = cur_status
