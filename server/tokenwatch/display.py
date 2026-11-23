import ctypes
import ctypes.util
import os


class XkbState(ctypes.Structure):
    _fields_ = [("group", ctypes.c_ubyte), ("locked_group", ctypes.c_ubyte),
                ("base_group", ctypes.c_ushort), ("latched_group", ctypes.c_ushort),
                ("mods", ctypes.c_ubyte), ("base_mods", ctypes.c_ubyte),
                ("latched_mods", ctypes.c_ubyte), ("locked_mods", ctypes.c_ubyte),
                ("compat_state", ctypes.c_ubyte), ("grab_mods", ctypes.c_ubyte),
                ("compat_grab_mods", ctypes.c_ubyte), ("lookup_mods", ctypes.c_ubyte),
                ("compat_lookup_mods", ctypes.c_ubyte), ("ptr_buttons", ctypes.c_ushort)]


def clear_caps_lock():
    if not os.environ.get("DISPLAY"):
        return
    display = None
    try:
        name = ctypes.util.find_library("X11")
        if not name:
            return
        lib = ctypes.CDLL(name)
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(XkbState)]
        lib.XkbLockModifiers.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = lib.XOpenDisplay(None)
        if display:
            state = XkbState()
            if lib.XkbGetState(display, 0x0100, ctypes.byref(state)) == 0 and state.locked_mods & 0x02:
                lib.XkbLockModifiers(display, 0x0100, 0x02, 0)
    except Exception:
        pass
    finally:
        if display:
            try:
                lib.XCloseDisplay(display)
            except Exception:
                pass
