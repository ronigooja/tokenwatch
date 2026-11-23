import ctypes

from tokenwatch import display


class Function:
    def __init__(self, func):
        self.func = func
    def __call__(self, *args):
        return self.func(*args)


def test_caps_lock(monkeypatch):
    calls = []
    class Library:
        XOpenDisplay = Function(lambda name: 42)
        XkbGetState = Function(lambda d, k, p: (setattr(ctypes.cast(p, ctypes.POINTER(display.XkbState)).contents, "locked_mods", 2), 0)[1])
        XkbLockModifiers = Function(lambda *args: calls.append(args))
        XCloseDisplay = Function(lambda d: calls.append(("close", d)))
    monkeypatch.setenv("DISPLAY", ":77")
    monkeypatch.setattr(display.ctypes.util, "find_library", lambda name: "X11")
    monkeypatch.setattr(display.ctypes, "CDLL", lambda name: Library())
    display.clear_caps_lock()
    assert calls == [(42, 0x100, 2, 0), ("close", 42)]
    monkeypatch.delenv("DISPLAY")
    display.clear_caps_lock()
    assert len(calls) == 2
