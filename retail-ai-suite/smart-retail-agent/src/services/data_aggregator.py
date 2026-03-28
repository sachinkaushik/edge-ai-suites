# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Data aggregator service for Retail Store Agent."""

import asyncio
import base64
import io
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import structlog

try:
    from PIL import Image
except ImportError:
    Image = None

from models import (
    CameraDataMessage, CameraImage, StoreSnapshot, StoreData,
    RetailAnalysisResponse, VLMAnalysisData, SceneData
)
from .config import ConfigService
from .vlm_service import VLMService


logger = structlog.get_logger(__name__)


class DataAggregatorService:
    """
    Data aggregator service for Retail Store Agent.
    
    Aggregates camera data, coordinates with VLM service,
    and maintains current store state for API responses.
    """

    def __init__(self, config_service: ConfigService, vlm_service: VLMService):
        """
        Initialize data aggregator service.
        
        Args:
            config_service: Configuration service
            vlm_service: VLM service for retail analysis
        """
        self.config = config_service
        self.vlm_service = vlm_service
        
        # Data storage - temporary data (latest from each camera)
        self.temp_camera_data: Dict[str, CameraDataMessage] = {}     # camera_id -> latest temp data
        self.temp_camera_images: Dict[str, CameraImage] = {}         # camera_id -> latest temp image
        self.temp_store_data: Optional[StoreData] = None
        self.temp_scene_data: Optional[SceneData] = None             # Latest scene tracking data
        
        # VLM-analyzed data storage (only data that was part of VLM analysis)
        self.vlm_analyzed_camera_images: Dict[str, CameraImage] = {}  # camera_id -> analyzed image
        self.vlm_analyzed_store_data: Optional[StoreData] = None
        self.vlm_analyzed_scene_data: Optional[SceneData] = None      # Scene data used in VLM analysis
        
        # Current state
        self.current_vlm_analysis: Optional[VLMAnalysisData] = None
        self.last_analysis_time: float = 0.0
        
        logger.info("Data aggregator service initialized")

    def _resize_image_if_needed(self, camera_image: CameraImage, max_size: int = 1024) -> CameraImage:
        """
        Resize image if it's too large for VLM processing.
        
        Args:
            camera_image: Original camera image
            max_size: Maximum dimension (will resize proportionally if larger)
            
        Returns:
            Camera image with potentially resized base64 data
        """
        if not camera_image.image_base64 or Image is None:
            return camera_image
        
        try:
            # Decode base64 to PIL Image
            image_data = base64.b64decode(camera_image.image_base64)
            img = Image.open(io.BytesIO(image_data))
            
            original_size = img.size  # (width, height)
            logger.debug("Image dimensions",
                        camera_id=camera_image.camera_id,
                        width=original_size[0],
                        height=original_size[1],
                        format=img.format)
            
            # Check if resizing is needed
            if original_size[0] > max_size or original_size[1] > max_size:
                # Calculate new size preserving aspect ratio
                ratio = min(max_size / original_size[0], max_size / original_size[1])
                new_size = (int(original_size[0] * ratio), int(original_size[1] * ratio))
                
                # Resize image
                img_resized = img.resize(new_size, Image.LANCZOS)
                logger.info("Image resized for VLM",
                           camera_id=camera_image.camera_id,
                           original_size=original_size,
                           resized_size=new_size)
                
                # Convert back to base64
                buffer = io.BytesIO()
                img_resized.save(buffer, format='JPEG', quality=85)
                resized_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
                
                # Return new camera image with resized data
                return CameraImage(
                    camera_id=camera_image.camera_id,
                    camera_number=camera_image.camera_number,
                    image_base64=resized_base64,
                    timestamp=camera_image.timestamp,
                    image_size_bytes=len(buffer.getvalue())
                )
            
            return camera_image
            
        except Exception as e:
            logger.warning("Failed to resize image",
                          camera_id=camera_image.camera_id,
                          error=str(e))
            return camera_image
    
    async def process_camera_image(self, camera_image: CameraImage) -> None:
        """
        Process incoming camera image (stores latest only).
        
        Args:
            camera_image: Camera image data from MQTT
        """
        try:
            camera_id = camera_image.camera_id
            
            # Update temporary camera image (latest only)
            self.temp_camera_images[camera_id] = camera_image
            
            logger.info("Camera image updated (temporary)", 
                       camera_id=camera_id,
                       camera_number=camera_image.camera_number,
                       image_size=camera_image.image_size_bytes,
                       has_image_data=bool(camera_image.image_base64),
                       total_temp_images_stored=len(self.temp_camera_images))
                    
        except Exception as e:
            logger.error("Failed to process camera image", error=str(e))
    
    async def process_scene_data(self, scene_data: SceneData) -> None:
        """
        Process incoming scene data with object tracking.
        
        Args:
            scene_data: Scene data with tracked objects
        """
        try:
            # Update latest scene data
            self.temp_scene_data = scene_data
            
            logger.info("Scene data updated", 
                       scene_id=scene_data.scene_id,
                       object_type=scene_data.object_type,
                       num_objects=len(scene_data.objects))
                    
        except Exception as e:
            logger.error("Failed to process scene data", error=str(e))

    async def process_camera_data(self, camera_message: CameraDataMessage) -> None:
        """
        Process incoming camera data and check if VLM analysis should be triggered.
        
        Args:
            camera_message: Camera data message from MQTT
        """
        try:
            camera_id = camera_message.camera_id
            
            # Update latest camera data
            self.temp_camera_data[camera_id] = camera_message
            
            logger.info("Camera data updated (temporary)",
                       camera_id=camera_id,
                       camera_number=camera_message.camera_number,
                       detections=camera_message.detections)
            
            # Update temporary store data
            await self._update_temp_store_data()
            
            # Check if we have data from all cameras
            num_cameras = self.config.get_num_cameras()
            if len(self.temp_camera_data) == num_cameras:
                # All cameras have reported, check if we should trigger VLM analysis
                await self._check_analysis_trigger()
                    
        except Exception as e:
            logger.error("Failed to process camera data", error=str(e))
    
    async def _update_temp_store_data(self) -> None:
        """Update temporary store data from camera inputs."""
        store_id = self.config.get_store_id()
        store_name = self.config.get_store_name()
        
        # Aggregate camera data
        camera_data_dict = {}
        for camera_id, cam_msg in self.temp_camera_data.items():
            camera_data_dict[camera_id] = {
                "camera_number": cam_msg.camera_number,
                "detections": cam_msg.detections,
                "timestamp": cam_msg.timestamp.isoformat() if cam_msg.timestamp else None
            }
        
        self.temp_store_data = StoreData(
            store_id=store_id,
            store_name=store_name,
            timestamp=datetime.now(timezone.utc),
            camera_data=camera_data_dict,
            scene_data=self.temp_scene_data  # Include scene tracking data
        )
        
        logger.info("Temporary store data updated", 
                   store_id=store_id,
                   num_cameras=len(camera_data_dict),
                   has_scene_data=self.temp_scene_data is not None)

    def _create_temp_store_snapshot(self) -> Optional[StoreSnapshot]:
        """Create a store snapshot from current temporary data for VLM analysis."""
        if not self.temp_store_data:
            return None
        
        # Get camera configs from config service
        camera_configs = {cam.name: cam for cam in self.config.get_cameras()}
        
        logger.info("Creating store snapshot with current data",
                   camera_images={cam: bool(img) for cam, img in self.temp_camera_images.items()})
        
        return StoreSnapshot(
            timestamp=datetime.now(timezone.utc),
            store_id=self.temp_store_data.store_id,
            camera_data=self.temp_store_data.camera_data.copy(),
            camera_images=self.temp_camera_images.copy(),  # Single image per camera
            store_data=self.temp_store_data,
            scene_data=self.temp_scene_data,  # Latest scene data
            camera_configs=camera_configs
        )
    
    def _save_vlm_analyzed_data(self, vlm_analysis: VLMAnalysisData, store_snapshot: StoreSnapshot) -> None:
        """Save data that was used in VLM analysis as the current analyzed data."""

        self.current_vlm_analysis = vlm_analysis

        # Copy camera images to VLM-analyzed storage
        self.vlm_analyzed_camera_images = store_snapshot.camera_images
        self.vlm_analyzed_store_data = store_snapshot.store_data
        self.vlm_analyzed_scene_data = store_snapshot.scene_data
        
        logger.info("VLM-analyzed data saved",
                   analyzed_cameras=list(self.vlm_analyzed_camera_images.keys()),
                   store_id=store_snapshot.store_id,
                   has_scene_data=store_snapshot.scene_data is not None)

    async def _check_analysis_trigger(self) -> None:
        """Check if VLM analysis should be triggered based on conditions."""
        
        if not self.temp_store_data:
            logger.debug("No store data available for analysis trigger check")
            return
        
        # For retail: trigger VLM analysis periodically
        # You can add conditions based on activity level, time since last analysis, etc.
        
        logger.info("Checking if VLM analysis should be triggered",
                   last_analysis_time=self.last_analysis_time)
        
        # Simple trigger: analyze if enough time has passed (e.g., every 30 seconds)
        analysis_window_seconds = 30
        current_time = datetime.now(timezone.utc).timestamp()
        
        if self.last_analysis_time == 0.0:
            # First analysis
            logger.info("No previous analysis, triggering VLM analysis")
            await self._trigger_vlm_analysis()
            return
        
        time_since_last_analysis = current_time - self.last_analysis_time
        
        if time_since_last_analysis >= analysis_window_seconds:
            logger.info("Analysis window expired, triggering VLM analysis",
                       time_since_last=time_since_last_analysis,
                       window_seconds=analysis_window_seconds)
            await self._trigger_vlm_analysis()
        else:
            logger.info("Skipping VLM analysis - within analysis window",
                       time_since_last=time_since_last_analysis,
                       window_seconds=analysis_window_seconds)

    async def _trigger_vlm_analysis(self) -> None:
        """Trigger VLM analysis with current store data."""
        try:
            logger.info("Starting VLM analysis trigger")
            store_snapshot = self._create_temp_store_snapshot()

            if not store_snapshot:
                logger.warning("Cannot trigger VLM analysis: no store snapshot available")
                return
        
            # Trigger VLM analysis
            try:
                vlm_analysis: VLMAnalysisData = await self.vlm_service.analyze_store_non_blocking(
                    store_snapshot=store_snapshot
                )
            
                if vlm_analysis:
                    self._save_vlm_analyzed_data(vlm_analysis, store_snapshot)
                    self.last_analysis_time = datetime.now(timezone.utc).timestamp()

                    logger.info("VLM analysis completed successfully and data saved")
                else:
                    logger.warning("VLM analysis returned no result - temporary data not saved")
                

            except Exception as vlm_error:
                logger.error("VLM analysis failed - temporary data not saved", error=str(vlm_error))
            
        except Exception as e:
            logger.error("Failed to trigger VLM analysis", error=str(e))
    
    async def get_current_retail_analysis(self) -> Optional[RetailAnalysisResponse]:
        """
        Get current retail analysis response.
        
        Returns:
            Complete retail analysis response or None if no VLM-analyzed data available
        """
        # Only return data that was part of VLM analysis
        if not self.vlm_analyzed_store_data or not self.current_vlm_analysis:
            logger.info("No VLM-analyzed data available for API response",
                       has_vlm_store_data=self.vlm_analyzed_store_data is not None,
                       has_vlm_analysis=self.current_vlm_analysis is not None)
            return None
        
        try:
            # Prepare camera images for response (only VLM-analyzed images)
            camera_images_dict = {}
            for camera_id, camera_image in self.vlm_analyzed_camera_images.items():
                if camera_image:
                    camera_images_dict[camera_id] = {
                        'camera_id': camera_image.camera_id,
                        'camera_number': camera_image.camera_number,
                        'timestamp': camera_image.timestamp.isoformat() if camera_image.timestamp else None,
                        'image_base64': camera_image.image_base64,
                        'image_size_bytes': camera_image.image_size_bytes
                    }
            
            # Create response with VLM-analyzed data only
            response = RetailAnalysisResponse(
                timestamp=datetime.now(timezone.utc).isoformat(),
                store_id=self.vlm_analyzed_store_data.store_id,
                data=self.vlm_analyzed_store_data,
                camera_images=camera_images_dict,
                vlm_analysis=self.current_vlm_analysis,
                response_age=(datetime.now(timezone.utc).timestamp() - self.last_analysis_time) if self.last_analysis_time else None,
            )
            
            return response
            
        except Exception as e:
            logger.error("Failed to create retail analysis response", error=str(e))
            return None
    
    def get_service_status(self) -> Dict[str, Any]:
        """Get current service status and statistics."""
        return {
            "store_id": self.config.get_store_id(),
            "store_name": self.config.get_store_name(),
            "analyzed_cameras": list(self.vlm_analyzed_camera_images.keys()),
            "active_analyzed_cameras": len(self.vlm_analyzed_camera_images),
            "has_vlm_analysis": self.current_vlm_analysis is not None,
            "last_analysis_time": datetime.fromtimestamp(self.last_analysis_time, tz=timezone.utc).isoformat() if self.last_analysis_time else None,
        }
