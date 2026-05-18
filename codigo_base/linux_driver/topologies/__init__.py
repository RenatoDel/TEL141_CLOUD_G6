from __future__ import annotations
from .linear import build_linear_slice, get_linear_summary, LinearSlice, LinearVM
from .ring   import build_ring_slice,   get_ring_summary,   RingSlice,   RingVM

SUPPORTED = ["linear", "ring"]
