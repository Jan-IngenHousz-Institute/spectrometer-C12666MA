"""Keep the PC from going to sleep during a recording (sleep drops the USB
serial links). Windows only; elsewhere this does nothing.

The setting belongs to the calling thread and is cleared when that thread
ends, so call it from one long-lived thread."""

import sys

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on: bool) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
