"""Host software for the Hamamatsu C12666MA spectrometer board (JII)."""

from .device import DeviceError, Spectrometer, find_ports
from .protocol import Frame

__version__ = "2.0.0"
__all__ = ["DeviceError", "Frame", "Spectrometer", "find_ports"]
