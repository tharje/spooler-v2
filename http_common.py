"""State shared between http_handler.py and its route mixins (http_*.py)."""

_ws_loop = None   # the asyncio loop; set by http_handler.set_ws_adopter()


def ws_loop():
    return _ws_loop


def set_ws_loop(loop) -> None:
    global _ws_loop
    _ws_loop = loop
