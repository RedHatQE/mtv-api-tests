"""
Copy-offload utility constants.

This module contains constants used for copy-offload functionality validation.
"""

# Supported storage vendors for copy-offload functionality
# Immutable tuple to prevent accidental modification
SUPPORTED_VENDORS = (
    "ontap",
    "vantara",
    "primera3par",
    "pureFlashArray",
    "powerflex",
    "powermax",
    "powerstore",
    "infinibox",
    "flashsystem",
)

# MTV-696: per-ESXi-host populator throttling (ForkliftController controller_max_populator_inflight)
POPULATOR_INFLIGHT_LIMIT = 2

SOURCE_HOST_LABEL = "sourceHost"
POPULATOR_THROTTLED_EVENT_REASON = "PopulatorThrottled"
FORKLIFT_CONTROLLER_NAME = "forklift-controller"
