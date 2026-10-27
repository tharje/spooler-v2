"""
Printer actions: one implementation per action a user can send to a printer.

Every function validates its input, calls the printer object and returns an
error message (str) or None when the action was accepted. ws_handler is a thin
router on top of this; nothing here knows about WebSockets.
"""

import asyncio

import control
import features

from printers.protocol import (
    CMD_CAMERA, CMD_DELETE_FILES, CMD_LIGHT,
    CMD_PAUSE, CMD_RESUME, CMD_STATUS, CMD_STOP,
)

ERR_NOT_CONNECTED = "Printer not connected"
ERR_NO_FILENAME = "No filename specified"
ERR_BAD_FILENAME = "Invalid filename"

MAX_DELETE_FILES = 50
SPEED_MIN, SPEED_MAX = 10, 200

# One pending light-refresh task per printer — cancelled when a new light command arrives
_light_refresh_tasks: dict = {}


async def _run_light_refresh(printer) -> None:
    try:
        if printer.printer_type == "cc2":
            await asyncio.sleep(0.5)
            await printer.send_cmd(1002)
            await asyncio.sleep(1.5)
            await printer.send_cmd(1002)
        else:
            await asyncio.sleep(0.4)
            await printer.send_cmd(CMD_STATUS, {})
    except asyncio.CancelledError:
        pass
    finally:
        _light_refresh_tasks.pop(printer.id, None)


def _schedule_light_refresh(printer) -> None:
    old = _light_refresh_tasks.pop(printer.id, None)
    if old:
        old.cancel()
    _light_refresh_tasks[printer.id] = asyncio.create_task(_run_light_refresh(printer))


def _bad_filename(filename: str) -> bool:
    return ".." in filename.replace("\\", "/").split("/")


async def _send(printer, cmd: int, data: dict) -> str | None:
    return None if await printer.send_cmd(cmd, data) else ERR_NOT_CONNECTED


async def pause(printer) -> str | None:
    err = await _send(printer, CMD_PAUSE, {})
    if err is None:
        # So state_reason attributes the resulting pause/stop to "spooler"
        # rather than "printer"/"unknown" when it's observed shortly after.
        printer.mark_spooler_command()
    return err


async def resume(printer) -> str | None:
    return await _send(printer, CMD_RESUME, {})


async def stop(printer) -> str | None:
    err = await _send(printer, CMD_STOP, {})
    if err is None:
        printer.mark_spooler_command()
    return err


async def request_status(printer) -> str | None:
    return await _send(printer, CMD_STATUS, {})


async def camera_on(printer) -> str | None:
    return await _send(printer, CMD_CAMERA, {"Enable": True})


async def set_light(printer, on: bool) -> str | None:
    err = await _send(printer, CMD_LIGHT, {"LightStatus": {"SecondLight": bool(on), "RgbLight": [0, 0, 0]}})
    if err is None:
        _schedule_light_refresh(printer)
    return err


async def set_speed(printer, speed) -> str | None:
    # Out-of-range or non-numeric values are ignored, as before.
    if not isinstance(speed, (int, float)) or not SPEED_MIN <= int(speed) <= SPEED_MAX:
        return None
    if printer.printer_type == "cc2":
        await printer.send_cmd(1031, {"speed": int(speed)})
    else:
        await printer.send_cmd(CMD_LIGHT, {"PrintSpeedPct": int(speed)})
    return None


async def list_files(printer) -> str | None:
    return None if await printer.request_file_list() else "Could not retrieve file list"


async def start_print(printer, filename, print_opts=None) -> str | None:
    filename = (filename or "").strip()
    if not filename:
        return ERR_NO_FILENAME
    if _bad_filename(filename):
        return ERR_BAD_FILENAME
    if not isinstance(print_opts, dict):
        print_opts = None
    ok = await printer.start_print_file(filename, print_opts=print_opts)
    return None if ok else "Could not start print"


async def delete_files(printer, files) -> str | None:
    # An invalid list is ignored silently, as before.
    if not (isinstance(files, list) and len(files) <= MAX_DELETE_FILES
            and all(isinstance(f, str) and f for f in files)):
        return None
    if printer.printer_type == "cc2":
        await printer.delete_files(files)
    else:
        await printer.send_cmd(CMD_DELETE_FILES, {"FileList": files})
    return None


async def delete_file(printer, filename) -> str | None:
    filename = (filename or "").strip()
    if not filename or _bad_filename(filename):
        return ERR_BAD_FILENAME
    if printer.printer_type == "cc2":
        await printer.delete_files([filename])
    elif printer.printer_type == "prusa":
        await printer.delete_file_prusa(filename)
    elif printer.printer_type == "moonraker":
        await printer.delete_file_moonraker(filename)
    else:
        await printer.send_cmd(CMD_DELETE_FILES, {"FileList": [filename]})
    return None


def get_file_info(printer, filename) -> None:
    filename = (filename or "").strip()
    if filename and hasattr(printer, "fetch_file_info"):
        asyncio.create_task(printer.fetch_file_info(filename))


# ── Advanced control (T12) ────────────────────────────────────────────────────

async def _advanced(printer, action: str, params: dict, run) -> str | None:
    err = control.validate_command(printer, action, params, features.is_enabled("advanced_control"))
    if err is None:
        ok = await run()
        err = None if ok else "Not supported by this printer yet"
    control.log_command("web", printer, action, params, err or "ok")
    return err


async def set_temperature(printer, heater, target) -> str | None:
    return await _advanced(printer, "set_temperature", {"heater": heater, "target": target},
                           lambda: printer.set_temperature(heater, float(target)))


async def set_fan(printer, fan, percent) -> str | None:
    return await _advanced(printer, "set_fan", {"fan": fan, "percent": percent},
                           lambda: printer.set_fan(fan, int(percent)))


async def move_axis(printer, axis, distance) -> str | None:
    return await _advanced(printer, "move_axis", {"axis": axis, "distance": distance},
                           lambda: printer.move_axis(axis, float(distance)))


async def home(printer, axes="XYZ") -> str | None:
    return await _advanced(printer, "home", {"axes": axes}, lambda: printer.home(axes))


async def heaters_off(printer) -> str | None:
    async def run():
        results = [await printer.set_temperature(h, 0.0)
                   for h, cap in control.HEATERS.items() if cap in printer.capabilities]
        return bool(results) and all(results)
    return await _advanced(printer, "heaters_off", {}, run)
