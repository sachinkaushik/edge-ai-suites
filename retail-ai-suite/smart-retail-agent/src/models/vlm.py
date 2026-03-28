# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class VLMAnalysisData:
    """VLM analysis results — stores the raw VLM response string."""
    summary: str  # Raw VLM response text
    analysis_timestamp: Optional[datetime] = None
