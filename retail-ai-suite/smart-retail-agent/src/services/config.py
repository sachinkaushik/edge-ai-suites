# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Configuration service for Retail Store Agent."""

import json
import hashlib
from typing import Dict, List, Optional
from pathlib import Path
import structlog

from models import CameraConfig

logger = structlog.get_logger(__name__)


def hash_store_name(name: str, length: int = 16) -> str:
    hash_object = hashlib.sha256(name.encode('utf-8'))
    hex_digest = hash_object.hexdigest()
    return hex_digest[:length]


class ConfigService:
    """
    Configuration service for Retail Store Agent.
    
    Manages configuration for retail store monitoring,
    MQTT topics, and VLM service settings.
    """
    
    def __init__(self):
        """Initialize configuration service."""
        self._config_dir = Path(__file__).resolve().parent.parent / "config"
        self.config = self._load_config()
        self.cameras = self._load_cameras()
        logger.info("Configuration service initialized", 
                   store_id=self.get_store_id(),
                   store_name=self.get_store_name(),
                   num_cameras=len(self.cameras))
    
    def _load_config(self) -> dict:
        """Load configuration from JSON file only."""
        config = {}

        agent_config_file = self._config_dir / "retail_store_agent.json"

        logger.info("Loading configuration file",
                    agent_config_path=str(agent_config_file))

        try:
            if agent_config_file.exists():
                with open(agent_config_file, 'r') as f:
                    file_config = json.load(f)
                config.update(file_config)
                logger.info("Loaded configuration from file", path=agent_config_file)
            else:
                logger.warning("Retail store agent config file does not exist, using defaults", path=agent_config_file)
                # Use default configuration if file doesn't exist
                config = self._get_default_config()
        
        except Exception as e:
            logger.error("Error loading config file, using defaults", error=str(e))
            config = self._get_default_config()
        
        return config
    
    def _get_default_config(self) -> dict:
        """Get default configuration values."""
        return {
            "store": {
                "name": "retail_store_1",
                "id": "store_001"
            },
            "cameras": [
                {
                    "name": "camera1",
                    "number": 1,
                    "description": "Store section 1",
                    "data_topic": "scenescape/data/camera/camera1",
                    "image_topic": "scenescape/image/camera/camera1"
                }
            ],
            "mqtt": {
                "host": "localhost",
                "port": 1883,
                "use_tls": False,
                "cert_required": False,
                "verify_hostname": False,
                "ca_cert_path": "secrets/certs/scenescape-ca.pem",
                "scene_data_topic_pattern": "scenescape/data/scene/+/+"
            },
            "vlm": {
                "base_url": "http://localhost:8000",
                "model": "Qwen/Qwen2.5-VL-3B-Instruct",
                "timeout_seconds": 300,
                "max_completion_tokens": 1500,
                "temperature": 0.1,
                "top_p": 0.1
            },
            "analysis": {
                "interval_seconds": 10,
                "analysis_window_seconds": 30
            }
        }
    
    def _load_cameras(self) -> List[CameraConfig]:
        """Load camera configurations from config."""
        cameras = []
        camera_configs = self.config.get("cameras", [])
        
        for cam_config in camera_configs:
            camera = CameraConfig(
                name=cam_config.get("name"),
                number=cam_config.get("number"),
                description=cam_config.get("description", ""),
                data_topic=cam_config.get("data_topic"),
                image_topic=cam_config.get("image_topic")
            )
            cameras.append(camera)
        
        return cameras
    
    def get_store_id(self) -> str:
        """Get the store ID."""
        return self.config.get("store", {}).get("id", "store_001")
    
    def get_store_name(self) -> str:
        """Get the store name."""
        return self.config.get("store", {}).get("name", "retail_store_1")
    
    def get_cameras(self) -> List[CameraConfig]:
        """Get list of camera configurations."""
        return self.cameras
    
    def get_camera_by_name(self, camera_name: str) -> Optional[CameraConfig]:
        """Get camera config by name."""
        for camera in self.cameras:
            if camera.name == camera_name:
                return camera
        return None
    
    def get_num_cameras(self) -> int:
        """Get number of cameras configured."""
        return len(self.cameras)
        
    def get_camera_topics(self) -> List[str]:
        """Get MQTT camera data topics from camera configs."""
        return [camera.data_topic for camera in self.cameras]

    def get_image_topics(self) -> List[str]:
        """Get MQTT image topics from camera configs."""
        return [camera.image_topic for camera in self.cameras]
    
    def get_scene_data_topic(self) -> str:
        """Get MQTT scene data topic pattern."""
        return self.config.get("mqtt", {}).get("scene_data_topic_pattern", "scenescape/data/scene/+/+")
    
    def get_mqtt_config(self) -> dict:
        """Get MQTT configuration."""
        return self.config.get("mqtt", {})
    
    def get_vlm_config(self) -> dict:
        """Get VLM service configuration."""
        return self.config.get("vlm", {})
    
    def get_analysis_config(self) -> dict:
        """Get analysis configuration."""
        return self.config.get("analysis", {})
    
    def get_analysis_interval(self) -> float:
        """Get analysis interval in seconds."""
        return self.config.get("analysis", {}).get("interval_seconds", 10.0)

    def update_config(self, key: str, value: any) -> None:
        """Update configuration value."""
        keys = key.split('.')
        config_ref = self.config
        
        # Navigate to the nested key
        for k in keys[:-1]:
            if k not in config_ref:
                config_ref[k] = {}
            config_ref = config_ref[k]
        
        # Set the value
        config_ref[keys[-1]] = value
        logger.info("Configuration updated", key=key, value=value)