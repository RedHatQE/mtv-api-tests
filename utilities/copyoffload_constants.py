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

# MTV-6088 / MTV-777: combined VM + populator inflight throttling
VM_INFLIGHT_LIMIT = 1
# Intentionally distinct from POPULATOR_INFLIGHT_LIMIT (2) used by MTV-696 with a single VM.
VM_POPULATOR_INFLIGHT_LIMIT = 2

SOURCE_HOST_LABEL = "sourceHost"
POPULATOR_THROTTLED_EVENT_REASON = "PopulatorThrottled"
FORKLIFT_CONTROLLER_NAME = "forklift-controller"
