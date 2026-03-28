# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
from enum import Enum

class AlertLevel(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"  
    CRITICAL = "critical"


class AlertType(Enum):
    """Types of retail alerts."""
    ANOMALY = "anomaly"
    THRESHOLD_EXCEEDED = "threshold_exceeded"
    NORMAL = "normal"
    CUSTOM = "custom"
