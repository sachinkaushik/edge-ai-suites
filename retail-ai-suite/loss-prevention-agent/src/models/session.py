# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Person session data model for loss prevention tracking."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set


@dataclass
class RegionVisit:
    """Record of a person visiting a specific region."""
    region_id: str
    region_name: str
    zone_type: str
    entry_time: datetime
    exit_time: Optional[datetime] = None

    @property
    def duration_seconds(self) -> float:
        end = self.exit_time or datetime.utcnow()
        return (end - self.entry_time).total_seconds()


@dataclass
class PersonSession:
    """
    Live state of a tracked person in the store.

    Created when SceneScape first reports an object_id.
    Updated on every subsequent scene message.
    Expired when the ID is absent for longer than session_timeout.
    """
    object_id: str
    first_seen: datetime
    last_seen: datetime

    # Current position
    current_cameras: List[str] = field(default_factory=list)
    current_regions: Set[str] = field(default_factory=set)
    bbox: Optional[Dict] = None  # {x, y, w, h} on primary camera

    # History
    camera_history: List[str] = field(default_factory=list)
    region_visits: List[RegionVisit] = field(default_factory=list)

    # Behavioral flags
    visited_checkout: bool = False
    visited_exit: bool = False
    visited_high_value: bool = False
    concealment_suspected: bool = False
    visit_count_per_region: Dict[str, int] = field(default_factory=dict)

    # Frame references (MinIO keys for rolling buffer)
    frame_buffer: List[str] = field(default_factory=list)
    max_frame_buffer: int = 60  # ~30s at 2fps

    def get_open_visits(self) -> List[RegionVisit]:
        """Return region visits that have not been closed."""
        return [v for v in self.region_visits if v.exit_time is None]

    def close_visit(self, region_id: str, exit_time: datetime) -> Optional[RegionVisit]:
        """Close an open visit for a given region."""
        for visit in self.region_visits:
            if visit.region_id == region_id and visit.exit_time is None:
                visit.exit_time = exit_time
                return visit
        return None

    def add_frame_key(self, minio_key: str) -> None:
        """Append a frame key, evicting the oldest if buffer is full."""
        self.frame_buffer.append(minio_key)
        if len(self.frame_buffer) > self.max_frame_buffer:
            self.frame_buffer.pop(0)
