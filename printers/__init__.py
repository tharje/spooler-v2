from printers.cc1 import CC1Connection
from printers.cc2 import CC2Connection
from printers.moonraker import MoonrakerConnection
from printers.prusa import PrusaConnection

PRINTER_TYPES = {
    "cc1":       CC1Connection,
    "cc2":       CC2Connection,
    "prusa":     PrusaConnection,
    "moonraker": MoonrakerConnection,
}


def make_printer(printer_type: str, *args, **kwargs):
    """Instantiate the right PrinterConnection subclass for `printer_type`."""
    cls = PRINTER_TYPES.get(printer_type)
    if cls is None:
        raise ValueError(f"Unknown printer type: {printer_type!r}")
    return cls(*args, **kwargs)
