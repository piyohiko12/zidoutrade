"""RSI_AUTOPILOT_V1.

The package is deliberately inert on import.  Importing it never connects to
OpenD, reads an account, or creates an order.
"""

from __future__ import annotations

PROGRAM_ID = "RSI_AUTOPILOT_V1"
__version__ = "0.1.0"

__all__ = ["PROGRAM_ID", "__version__"]
