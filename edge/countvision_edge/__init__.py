"""CountVision edge agent.

Reads camera streams, detects and tracks people and vehicles, counts line
crossings, measures occupancy / dwell time / queue length in zones, and stores
only numbers in a local SQLite buffer. No video leaves the device.
"""

__version__ = "0.3.0"
