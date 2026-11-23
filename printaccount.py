"""
Filament bookkeeping for ONE running print.

The printer reports a running total of extruded filament for the print. This
object turns that into "how much came off which spool" and survives three
things that used to lose or double-count filament:

* a printer counter that resets or goes backwards mid-print (the segments are
  added up instead of being read as negative use),
* a spool/slot change mid-print (usage is attributed to the spool that was
  feeding at the time),
* Spooler restarting mid-print (the state is saved to disk and picked up again).

Pure logic plus a tiny state file; nothing here talks to a printer or Spoolman.
"""

import json
import threading
import uuid

import persistence

# A counter reset starts again from (near) zero: the figure falls below half of
# what it was, and by more than NOISE_MM. A smaller drop is an estimate being
# corrected (CC2's figure is computed from the time left, which the printer
# revises), not a new segment, so the higher figure is kept (#61, #62).
NOISE_MM = 5.0
RESET_FRACTION = 0.5

_lock = threading.Lock()


class PrintAccounting:
    def __init__(self, print_id: str | None = None, current_spool=None, start_spool=None,
                 filename: str = "", started_at: float | None = None):
        self.print_id = print_id or uuid.uuid4().hex
        self.current_spool = current_spool    # spool feeding the extruder now (None = none linked)
        self.start_spool = start_spool        # printer-location spool seen at the start (single-material)
        self.filename = filename
        self.started_at = started_at          # epoch seconds, None if unknown (adopted mid-print)
        self.carry_mm = 0.0                   # extrusion before the last counter reset(s)
        self.last_raw_mm = 0.0                # the printer's most recent figure
        self.snapshot_mm = 0.0                # total at the last spool switch
        self.per_spool: dict = {}             # spool id (or None) -> mm
        self.resets = 0

    # ── numbers ──────────────────────────────────────────────────────────────

    def total_mm(self) -> float:
        return self.carry_mm + self.last_raw_mm

    def observe(self, raw_mm) -> bool:
        """Feed the printer's latest figure. Returns True if it was read as a
        counter reset (so the caller can log it)."""
        try:
            raw = float(raw_mm or 0)
        except (TypeError, ValueError):
            return False
        reset = False
        if raw >= self.last_raw_mm:
            self.last_raw_mm = raw
        elif raw < RESET_FRACTION * self.last_raw_mm and self.last_raw_mm - raw > NOISE_MM:
            self.carry_mm += self.last_raw_mm       # new segment; what came before still counts
            self.last_raw_mm = raw
            self.resets += 1
            reset = True
        # else: a dip, not a reset -- keep the higher value
        return reset

    def switch_spool(self, new_spool, from_idle: bool = False) -> None:
        """The feeding spool changed: everything extruded since the last switch
        belongs to the outgoing spool.

        from_idle: the printer reported no active slot until now (e.g. -1 right
        after a print starts). What was counted in that gap has no spool of its
        own, so it goes to the first spool that takes over instead of being
        thrown away as "unknown"."""
        if new_spool == self.current_spool:
            return
        if from_idle and new_spool is not None and self.current_spool is None and not self.per_spool:
            self.current_spool = new_spool
            return
        self._attribute_to_current()
        self.current_spool = new_spool

    def _attribute_to_current(self) -> None:
        delta = self.total_mm() - self.snapshot_mm
        if delta > 0:
            self.per_spool[self.current_spool] = self.per_spool.get(self.current_spool, 0.0) + delta
        self.snapshot_mm = self.total_mm()

    def finish(self) -> dict:
        """The print is over: attribute the rest and return {spool: mm}."""
        self._attribute_to_current()
        return dict(self.per_spool)

    # ── persistence ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "print_id": self.print_id, "current_spool": self.current_spool, "start_spool": self.start_spool,
            "filename": self.filename, "started_at": self.started_at, "carry_mm": self.carry_mm,
            "last_raw_mm": self.last_raw_mm, "snapshot_mm": self.snapshot_mm, "resets": self.resets,
            "per_spool": [[k, v] for k, v in self.per_spool.items()],   # JSON keys can't be None/int
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PrintAccounting":
        a = cls(d.get("print_id"), d.get("current_spool"), d.get("start_spool"),
                d.get("filename", ""), d.get("started_at"))
        a.carry_mm = float(d.get("carry_mm") or 0)
        a.last_raw_mm = float(d.get("last_raw_mm") or 0)
        a.snapshot_mm = float(d.get("snapshot_mm") or 0)
        a.resets = int(d.get("resets") or 0)
        a.per_spool = {k: float(v) for k, v in (d.get("per_spool") or [])}
        return a


# ── the state file: one entry per printer with a print in progress ──────────

def _file():
    return persistence.DATA_DIR / "active_prints.json"


def _load_all() -> dict:
    try:
        data = json.loads(_file().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_active(printer_id: str, acct: PrintAccounting) -> None:
    with _lock:
        data = _load_all()
        data[printer_id] = acct.to_dict()
        persistence._atomic_write(_file(), json.dumps(data))


def load_active(printer_id: str) -> PrintAccounting | None:
    d = _load_all().get(printer_id)
    if not isinstance(d, dict):
        return None
    try:
        return PrintAccounting.from_dict(d)
    except Exception:
        return None


def clear_active(printer_id: str) -> None:
    with _lock:
        data = _load_all()
        if data.pop(printer_id, None) is not None:
            persistence._atomic_write(_file(), json.dumps(data))
