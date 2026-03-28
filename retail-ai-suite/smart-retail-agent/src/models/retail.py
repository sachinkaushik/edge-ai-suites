# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from .vlm import VLMAnalysisData


@dataclass
class CameraConfig:
    """Camera configuration with description."""
    name: str
    number: int
    description: str
    data_topic: str
    image_topic: str


@dataclass
class RegionInfo:
    """Information about a region an object is in."""
    region_id: str
    region_name: Optional[str] = None
    entered_timestamp: Optional[datetime] = None


@dataclass
class SceneObject:
    """Tracked object in the scene with rich metadata."""
    object_id: str  # Unique ID that persists across frames
    category: str  # e.g., "person", "vehicle"
    visibility: float  # 0.0 to 1.0, may be inaccurate sometimes
    camera_ids: List[str] = field(default_factory=list)  # Which cameras see this object
    regions: List[RegionInfo] = field(default_factory=list)  # Which regions the object is in
    metadata: Dict[str, Any] = field(default_factory=dict)  # Additional metadata


@dataclass
class SceneData:
    """Scene data from scenescape/data/scene topic."""
    scene_id: str
    object_type: str  # e.g., "persons"
    objects: List[SceneObject]
    timestamp: Optional[datetime] = None


@dataclass
class CameraImage:
    """Camera image data from MQTT."""
    camera_id: str
    camera_number: int
    image_base64: str
    timestamp: Optional[datetime] = None
    image_size_bytes: Optional[int] = None


@dataclass
class StoreData:
    """Core retail store data structure."""
    store_id: str
    store_name: str
    timestamp: datetime
    
    # Generic camera data - dictionary with camera_id as key
    camera_data: Dict[str, Dict[str, Any]] = None  # camera_id -> {count, detections, etc}
    
    # Scene object tracking data
    scene_data: Optional[SceneData] = None
    
    def __post_init__(self):
        if self.camera_data is None:
            self.camera_data = {}


@dataclass
class RetailAnalysisResponse:
    """Complete retail store analysis response."""
    timestamp: str
    store_id: str
    data: StoreData
    camera_images: Dict[str, Dict[str, Any]]  # Camera images by camera_id
    vlm_analysis: VLMAnalysisData
    response_age: Optional[float] = None


@dataclass
class CameraDataMessage:
    """MQTT camera data message structure."""
    camera_id: str
    camera_number: int
    store_id: str
    detections: Dict[str, Any]  # Generic detections data from scenescape
    timestamp: Optional[datetime] = None
    image_data: Optional[str] = None  # Base64 encoded image

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


@dataclass
class StoreSnapshot:
    """Snapshot of store data at a point in time."""
    timestamp: datetime
    store_id: str
    camera_data: Dict[str, Any]  # camera_id -> detection data
    camera_images: Optional[Dict[str, CameraImage]] = None  # camera_id -> CameraImage (single image per camera)
    store_data: Optional[StoreData] = None
    scene_data: Optional[SceneData] = None  # Latest scene tracking data
    camera_configs: Optional[Dict[str, CameraConfig]] = None  # Camera configs by name


# Type aliases for better code readability
CameraTopics = List[str]
CameraImagesDict = Dict[str, CameraImage]  # camera_id -> image
