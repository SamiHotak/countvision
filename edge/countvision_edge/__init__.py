"""CountVision edge agent.

Reads camera streams, detects and tracks people and vehicles, counts line
crossings, measures occupancy / dwell time / queue length in zones, and stores
only numbers in a local SQLite buffer. No video leaves the device.
"""

import os as _os

__version__ = "0.6.0"

# "Nothing leaves the device" includes the libraries we use. ONNX Runtime 1.2x+ sends usage
# telemetry to Microsoft when it is imported (seen in phase 2 B: mobile.events.data.microsoft.com)
# and Ultralytics sends anonymous usage events. Both are switched off here, before either is
# imported. Set the variables yourself to override.
PRIVACY_ENV = {"ORT_DISABLE_TELEMETRY": "1", "YOLO_OFFLINE": "1"}
PRIVACY_ENV_SET_BY_US = {key for key in PRIVACY_ENV if key not in _os.environ}
for _key in PRIVACY_ENV_SET_BY_US:
    _os.environ[_key] = PRIVACY_ENV[_key]
