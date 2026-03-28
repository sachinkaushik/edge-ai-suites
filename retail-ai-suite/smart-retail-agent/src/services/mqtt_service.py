# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""MQTT service for retail store camera data subscription."""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from queue import Queue
import threading

import paho.mqtt.client as mqtt
import structlog

from models import CameraDataMessage, CameraImage
from .config import ConfigService
from .data_aggregator import DataAggregatorService
from .vlm_service import VLMService


logger = structlog.get_logger(__name__)


class MQTTService:
    """
    MQTT service for subscribing to camera data topics.
    
    Subscribes to scenescape/data/camera/camera<N> topics and processes
    incoming camera data for retail store analysis.
    """

    def __init__(self, config_service: ConfigService, data_aggregator: DataAggregatorService, vlm_service: VLMService):
        """
        Initialize MQTT service.
        
        Args:
            config_service: Configuration service instance
            data_aggregator: Data aggregator service for processing messages
            vlm_service: VLM service for analysis
        """
        self.config = config_service
        self.data_aggregator = data_aggregator
        self.mqtt_config = config_service.get_mqtt_config()
        self.vlm_service = vlm_service
        
        # MQTT connection settings - use config or fallback to localhost
        self.host = self.mqtt_config.get("host", "localhost")
        self.port = self.mqtt_config.get("port", 1883)
        self.use_tls = self.mqtt_config.get("use_tls", False)
        self.ca_cert_path = self.mqtt_config.get("ca_cert_path", "../secrets/certs/scenescape-ca.pem")
        self.cert_required = self.mqtt_config.get("cert_required", True)
        self.verify_hostname = self.mqtt_config.get("verify_hostname", False)
        self.username = self.mqtt_config.get("username")
        self.password = self.mqtt_config.get("password")
        
        # Camera topics from config
        self.camera_topics = config_service.get_camera_topics()
        self.image_topics = config_service.get_image_topics()
        self.scene_data_topic = config_service.get_scene_data_topic()
        
        # Camera configurations for matching topics to descriptions
        self.cameras = config_service.get_cameras()
        self.camera_by_data_topic = {cam.data_topic: cam for cam in self.cameras}
        self.camera_by_image_topic = {cam.image_topic: cam for cam in self.cameras}
        
        # MQTT client and connection state
        self.client = None
        self.connected = False
        self.loop = None
        
        # Message processing
        self.shutdown_event = asyncio.Event()
        
        # Rate limiting - process data every N seconds per camera
        self.last_processed_time = {}  # camera_id -> timestamp
        self.analysis_interval = config_service.get_analysis_interval()
        
        # Topic patterns for scene data: scenescape/data/scene/{scene_id}/{object_type}
        self.scene_data_pattern = re.compile(r'scenescape/data/scene/([^/]+)/([^/]+)')
        
        logger.info("MQTT service initialized", 
                   host=self.host, 
                   port=self.port, 
                   use_tls=self.use_tls,
                   has_auth=bool(self.username),
                   cert_required=self.cert_required,
                   verify_hostname=self.verify_hostname,
                   ca_cert_path=self.ca_cert_path,
                   camera_topics=self.camera_topics,
                   image_topics=self.image_topics,
                   scene_data_topic=self.scene_data_topic,
                   num_cameras=self.config.get_num_cameras(),
                   analysis_interval=self.analysis_interval)
    
    async def initialize(self) -> None:
        """Initialize MQTT client."""
        try:
            self.client = mqtt.Client()
            
            # Configure authentication
            if self.username and self.password:
                self.client.username_pw_set(self.username, self.password)
                logger.info("MQTT username/password authentication configured", username=self.username)
            
            # Configure TLS
            if self.use_tls:
                logger.info("Configuring MQTT TLS", 
                           ca_cert=self.ca_cert_path,
                           cert_required=self.cert_required,
                           verify_hostname=self.verify_hostname)
                
                try:
                    import ssl
                    
                    # Resolve certificate paths relative to the app root
                    # Get the app root directory (parent of services directory)
                    script_dir = os.path.dirname(os.path.abspath(__file__))
                    app_root = os.path.dirname(script_dir)  # Go up one level from services/
                    
                    def resolve_cert_path(cert_path):
                        if os.path.isabs(cert_path):
                            return cert_path
                        return os.path.join(app_root, cert_path)
                    
                    ca_cert_full_path = resolve_cert_path(self.ca_cert_path)
                    
                    logger.info("Certificate path resolved",
                               ca_cert=ca_cert_full_path)
                    
                    if self.cert_required:
                        # Check if CA certificate file exists
                        if not os.path.exists(ca_cert_full_path):
                            logger.error("Missing CA certificate file", ca_cert=ca_cert_full_path)
                            raise FileNotFoundError(f"Missing CA certificate file: {ca_cert_full_path}")
                        
                        # Use CA certificate only for server verification
                        self.client.tls_set(ca_certs=ca_cert_full_path, 
                                          certfile=None, 
                                          keyfile=None,
                                          cert_reqs=ssl.CERT_REQUIRED,
                                          tls_version=ssl.PROTOCOL_TLS)
                        
                        # Set hostname verification based on config
                        if not self.verify_hostname:
                            self.client.tls_insecure_set(True)
                            logger.info("TLS configured with CA certificate (hostname verification disabled)")
                        else:
                            logger.info("TLS configured with CA certificate (full verification)")
                            
                    else:
                        # TLS without certificate verification (insecure but matches some UI settings)
                        try:
                            # First try with CA cert only
                            if os.path.exists(ca_cert_full_path):
                                self.client.tls_set(ca_certs=ca_cert_full_path, 
                                                  certfile=None, 
                                                  keyfile=None,
                                                  cert_reqs=ssl.CERT_NONE,
                                                  tls_version=ssl.PROTOCOL_TLS)
                                logger.info("TLS with CA cert but no verification")
                            else:
                                # No certificates, just TLS
                                self.client.tls_set(ca_certs=None, 
                                                  certfile=None, 
                                                  keyfile=None,
                                                  cert_reqs=ssl.CERT_NONE,
                                                  tls_version=ssl.PROTOCOL_TLS)
                                logger.info("TLS without certificates")
                            
                            self.client.tls_insecure_set(True)
                            
                        except Exception as e:
                            logger.warning("Advanced TLS failed, trying basic TLS", error=str(e))
                            # Fallback to most basic TLS
                            self.client.tls_set()
                            self.client.tls_insecure_set(True)
                            logger.info("Basic TLS enabled")
                            
                except Exception as e:
                    logger.error("Failed to configure TLS", error=str(e))
                    raise
            
            # Set callbacks
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            
            logger.info("MQTT client initialized")
            
        except Exception as e:
            logger.error("Failed to initialize MQTT client", error=str(e))
            raise
    
    def _on_connect(self, client, userdata, flags, rc):
        """MQTT connection callback."""
        if rc == 0:
            self.connected = True
            logger.info(f"MQTT connected successfully to {self.host}:{self.port}")
            
            # Subscribe to camera data topics
            for topic in self.camera_topics:
                client.subscribe(topic, qos=1)
                logger.info("Subscribed to camera data topic", topic=topic)
            
            # Subscribe to camera image topics
            for topic in self.image_topics:
                client.subscribe(topic, qos=1)
                logger.info("Subscribed to camera image topic", topic=topic)
            
            # Subscribe to scene data topic
            client.subscribe(self.scene_data_topic, qos=1)
            logger.info("Subscribed to scene data topic", topic=self.scene_data_topic)
                
        else:
            self.connected = False
            logger.error("MQTT connection failed", return_code=rc)
    
    def _on_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback."""
        self.connected = False
        if rc != 0:
            logger.warning("MQTT disconnected unexpectedly", return_code=rc)
        else:
            logger.info("MQTT disconnected")
    
    def _on_message(self, client, userdata, msg):
        """MQTT message callback - process messages with rate limiting."""
        logger.debug("Received MQTT message", topic=msg.topic, payload_preview=msg.payload.decode()[:100])
        try:
            current_ts = datetime.now(timezone.utc).timestamp()
            if self.vlm_service.get_vlm_semaphore().locked():
                # logger.info("VLM analysis currently running, skipping message receipt")
                return
            
            try:
                payload = json.loads(msg.payload.decode())
                logger.debug("MQTT payload parsed", 
                           topic=msg.topic,
                           payload_type=type(payload).__name__,
                           payload_keys=list(payload.keys()) if isinstance(payload, dict) else "not_dict")
            except json.JSONDecodeError:
                logger.error("Failed to parse MQTT message payload", 
                           topic=msg.topic, payload=msg.payload.decode()[:100])
                return

            # Check if this is a camera data, camera image, or scene data topic
            if msg.topic in self.camera_by_data_topic:
                # Handle camera data message
                camera = self.camera_by_data_topic[msg.topic]
                camera_id = camera.name
                
                # Rate limiting per camera
                last_time = self.last_processed_time.get(f"data_{camera_id}", 0)
                if current_ts - last_time < self.analysis_interval:
                    return
                
                self.last_processed_time[f"data_{camera_id}"] = current_ts
                
                # Schedule async processing for camera data
                if self.loop:
                    asyncio.run_coroutine_threadsafe(
                        self._process_camera_data_message(
                            camera=camera,
                            payload=payload,
                            topic=msg.topic
                        ),
                        self.loop
                    )
                else:
                    logger.warning("No event loop set, cannot process camera data message")
                    
            elif msg.topic in self.camera_by_image_topic:
                # Handle camera image message
                camera = self.camera_by_image_topic[msg.topic]

                # Schedule async processing for camera image
                if self.loop:
                    asyncio.run_coroutine_threadsafe(
                        self._process_camera_image_message(
                            camera=camera,
                            payload=payload,
                            topic=msg.topic
                        ),
                        self.loop
                    )
                else:
                    logger.warning("No event loop set, cannot process camera image message")
            
            else:
                # Check if this is a scene data topic
                scene_match = self.scene_data_pattern.match(msg.topic)
                if scene_match:
                    # Handle scene data message
                    scene_id = scene_match.group(1)
                    object_type = scene_match.group(2)
                    
                    logger.debug("Scene data payload before processing",
                              scene_id=scene_id,
                              object_type=object_type,
                              payload_type=type(payload).__name__,
                              payload_repr=repr(payload)[:500],
                              topic=msg.topic)
                    
                    # Schedule async processing for scene data
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(
                            self._process_scene_data_message(
                                scene_id=scene_id,
                                object_type=object_type,
                                payload=payload,
                                topic=msg.topic
                            ),
                            self.loop
                        )
                    else:
                        logger.warning("No event loop set, cannot process scene data message")
                else:
                    logger.debug("Ignoring unrecognized topic", topic=msg.topic)
        
        except Exception as e:
            logger.error("Error processing MQTT message", error=str(e), topic=msg.topic)
    
    async def start(self) -> None:
        """Start MQTT service."""
        try:
            if not self.client:
                await self.initialize()
            
            logger.info("Starting MQTT connection", host=self.host, port=self.port, use_tls=self.use_tls)
            
            try:
                self.client.connect(self.host, self.port, 60)
            except Exception as e:
                error_str = str(e).lower()
                if "ssl" in error_str or "tls" in error_str:
                    logger.error("SSL/TLS connection failed", error=str(e), host=self.host, port=self.port)
                    
                    # Provide specific suggestions based on the error
                    if "wrong version number" in error_str:
                        logger.error("SSL version mismatch detected.")
                    elif "certificate" in error_str:
                        logger.error("Certificate validation failed. Try: MQTT_CERT_REQUIRED=false")
                    
                    # If TLS is enabled and we get SSL errors, suggest trying without TLS
                    if self.use_tls:
                        logger.error("Consider trying connection without TLS first to test basic connectivity")
                        
                else:
                    logger.error("MQTT connection failed", error=str(e), host=self.host, port=self.port)
                raise
            
            # Start the network loop in a separate thread
            self.client.loop_start()
            
            # Wait for connection to establish
            await asyncio.sleep(2)
            
            logger.info("MQTT service started successfully")
            
            # Keep the service running
            while not self.shutdown_event.is_set():
                await asyncio.sleep(1)
        
        except Exception as e:
            logger.error("Failed to start MQTT service", error=str(e))
            raise
    
    async def stop(self) -> None:
        """Stop MQTT service."""
        try:
            # Signal the shutdown event
            self.shutdown_event.set()
            
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()
                logger.info("MQTT service stopped")
        
        except Exception as e:
            logger.error("Error stopping MQTT service", error=str(e))
    
    def is_connected(self) -> bool:
        """Check if MQTT client is connected."""
        return self.connected
    
    def set_event_loop(self, loop):
        """Set the event loop reference for async task scheduling."""
        self.loop = loop
        logger.info("Event loop reference set for MQTT service")

    async def send_getimage_commands(self, camera_name: Optional[str] = None) -> bool:
        """Send 'getimage' command to cameras using actual camera names from config."""
        if not self.connected:
            logger.warning("MQTT Publisher not connected, cannot send commands")
            return False
        
        try:
            # Get actual camera names from config
            cameras = self.config.get_cameras()
            if camera_name:
                # Send to specific camera
                camera_names = [camera_name]
            else:
                # Send to all cameras
                camera_names = [cam.name for cam in cameras]

            if not camera_names:
                logger.warning("No camera names found in config")
                return False
            
            success_count = 0
            for cam_name in camera_names:
                try:
                    topic = f"scenescape/cmd/camera/{cam_name}"
                    message = "getimage"

                    result = self.client.publish(topic, message)
                    if result.rc == mqtt.MQTT_ERR_SUCCESS:
                        success_count += 1
                        logger.info("Getimage command sent", camera_name=cam_name, topic=topic)
                    else:
                        logger.warning("Failed to send getimage command", 
                                     camera_name=cam_name, 
                                     result_code=result.rc)
                    
                except Exception as e:
                    logger.error("Error sending getimage command", 
                               camera_name=cam_name, 
                               error=str(e))
            
            logger.info("Sent getimage commands", 
                       total_cameras=len(camera_names), 
                       successful=success_count)
            
            return success_count > 0
            
        except Exception as e:
            logger.error("Failed to send getimage commands", error=str(e))
            return False

    async def _trigger_initial_getimage_commands(self) -> None:
        """
        Trigger initial getimage commands after MQTT connection is established.
        
        This method is called automatically when the MQTT connection is established
        to request initial images from all cameras.
        """
        try:
            # Small delay to ensure subscriptions are fully established
            await asyncio.sleep(1.0)
            
            logger.info("Triggering initial getimage commands for all cameras")
            success = await self.send_getimage_commands()
            
            if success:
                logger.info("Initial getimage commands sent successfully")
            else:
                logger.warning("Failed to send initial getimage commands")
                
        except Exception as e:
            logger.error("Error triggering initial getimage commands", error=str(e))
        
    async def _process_camera_image_message(self,
                                           camera: 'CameraConfig',
                                           payload: Dict[str, Any],
                                           topic: str) -> None:
        """
        Process camera image message from MQTT.
        
        Args:
            camera: Camera configuration
            payload: Message payload with image data
            topic: MQTT topic
        """
        try:
            logger.info("Processing camera image message", camera_name=camera.name, topic=topic)
            
            # Extract data from payload
            image_data = payload.get('image')  # Base64 encoded image
            if not image_data:
                logger.warning("No image data in image message", camera_id=camera.name, topic=topic)
                return

            image_timestamp = payload.get('timestamp', None)
            if image_timestamp:
                try:
                    image_timestamp = datetime.fromisoformat(image_timestamp)
                except:
                    pass
            
            # Create camera image
            camera_image = CameraImage(
                camera_id=camera.name,
                camera_number=camera.number,
                image_base64=image_data,
                timestamp=image_timestamp,
                image_size_bytes=len(image_data) * 3 // 4 if image_data else None  # Approximate base64 decode size
            )
            
            # Send to data aggregator for processing (image only)
            await self.data_aggregator.process_camera_image(camera_image)
            
            logger.debug("Camera image processed successfully", 
                       camera_id=camera.name,
                       camera_number=camera.number,
                       image_size=len(image_data))
        except Exception as e:
            logger.error("Failed to process camera image message", 
                        error=str(e), 
                        camera_name=camera.name, 
                        topic=topic)

    async def _process_camera_data_message(self, 
                                         camera: 'CameraConfig',
                                         payload: Dict[str, Any],
                                         topic: str) -> None:
        """
        Process camera data message from MQTT.
        
        Args:
            camera: Camera configuration
            payload: Message payload
            topic: MQTT topic
        """
        try:
            logger.info("Processing camera data message", camera_name=camera.name, topic=topic)
            # Request image corresponding to current camera data
            success = await self.send_getimage_commands(camera_name=camera.name)
            if success:
                logger.info(f"Getimage command for camera {camera.name} sent successfully")
            else:
                logger.warning(f"Failed to send getimage command for camera {camera.name}")
            
            await asyncio.sleep(1)  # Small wait to allow image to arrive
                
            # Extract data from payload
            store_id = self.config.get_store_id()
            
            # Extract detection data from objects (generic)
            detections = payload.get('objects', {})
            
            data_timestamp = payload.get('timestamp', None)
            if data_timestamp:
                try:
                    data_timestamp = datetime.fromisoformat(data_timestamp)
                except:
                    pass
            
            # Create camera data message
            camera_message = CameraDataMessage(
                camera_id=camera.name,
                camera_number=camera.number,
                store_id=store_id,
                detections=detections,
                timestamp=data_timestamp,
            )
            
            # Send to data aggregator for processing
            await self.data_aggregator.process_camera_data(camera_message)
            
            logger.debug("Camera data processed successfully", 
                       camera_id=camera.name,
                       camera_number=camera.number,
                       detections=detections)
                    
        except Exception as e:
            logger.error("Failed to process camera data message", 
                        error=str(e), 
                        camera_name=camera.name, 
                        topic=topic)
    
    async def _process_scene_data_message(self,
                                         scene_id: str,
                                         object_type: str,
                                         payload: Dict[str, Any],
                                         topic: str) -> None:
        """
        Process scene data message from MQTT.
        
        Args:
            scene_id: Scene UUID
            object_type: Object type (e.g., 'persons')
            payload: Message payload with tracking data
            topic: MQTT topic
        """
        try:
            from models import SceneData, SceneObject, RegionInfo
            
            logger.info("Processing scene data message", 
                       scene_id=scene_id, 
                       object_type=object_type, 
                       topic=topic)
            
            # Validate payload is a dictionary
            if not isinstance(payload, dict):
                logger.error("Scene data payload is not a dictionary", 
                           payload_type=type(payload).__name__,
                           payload_preview=str(payload)[:200],
                           scene_id=scene_id,
                           object_type=object_type)
                return
            
            # Log payload structure for debugging
            logger.debug("Scene data payload structure",
                        payload_keys=list(payload.keys()) if isinstance(payload, dict) else "not_dict",
                        scene_id=scene_id)
            
            # Parse objects from payload
            objects_data = payload.get('objects', [])
            
            # Handle case where objects might be in a different structure
            if not objects_data and 'data' in payload:
                objects_data = payload.get('data', {}).get('objects', [])
            
            scene_objects = []
            
            for obj_data in objects_data:
                # Parse regions - can be dict {region_id: {entered: timestamp}} or array
                regions = []
                regions_data = obj_data.get('regions', {})
                
                if isinstance(regions_data, dict):
                    # Dict format: {"region_id": {"entered": "timestamp"}}
                    for region_id, region_info_data in regions_data.items():
                        if isinstance(region_info_data, dict):
                            region_info = RegionInfo(
                                region_id=region_id,
                                region_name=None,  # Not available in this format
                                entered_timestamp=region_info_data.get('entered')
                            )
                            regions.append(region_info)
                elif isinstance(regions_data, list):
                    # Array format
                    for region_data in regions_data:
                        if isinstance(region_data, dict):
                            region_info = RegionInfo(
                                region_id=region_data.get('region_id', ''),
                                region_name=region_data.get('region_name'),
                                entered_timestamp=region_data.get('entered_timestamp', region_data.get('entered'))
                            )
                            regions.append(region_info)
                
                # Object ID is in 'id' field
                object_id = obj_data.get('id', '')
                
                # Camera IDs from 'visibility' array (camera names)
                camera_ids = obj_data.get('visibility', [])
                if not isinstance(camera_ids, list):
                    camera_ids = []
                
                # Confidence for visibility score
                confidence = obj_data.get('confidence', 1.0)
                
                scene_obj = SceneObject(
                    object_id=object_id,
                    category=obj_data.get('category', obj_data.get('type', object_type)),
                    visibility=confidence,
                    camera_ids=camera_ids,
                    regions=regions,
                    metadata={k: v for k, v in obj_data.items() if k not in ['id', 'category', 'type', 'confidence', 'visibility', 'regions']}
                )
                scene_objects.append(scene_obj)
            
            # Create scene data
            scene_data = SceneData(
                scene_id=scene_id,
                object_type=object_type,
                objects=scene_objects,
                timestamp=datetime.now(timezone.utc)
            )
            
            # Send to data aggregator for processing
            await self.data_aggregator.process_scene_data(scene_data)
            
            logger.debug("Scene data processed successfully", 
                       scene_id=scene_id,
                       object_type=object_type,
                       num_objects=len(scene_objects))
                    
        except Exception as e:
            logger.error("Failed to process scene data message", 
                        error=str(e), 
                        scene_id=scene_id,
                        object_type=object_type,
                        topic=topic)
    
    def get_connection_status(self) -> Dict[str, Any]:
        """Get MQTT connection status information."""
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "use_tls": self.use_tls,
            "has_authentication": bool(self.username),
            "cert_required": self.cert_required,
            "subscribed_camera_topics": self.camera_topics,
            "subscribed_image_topics": self.image_topics,
            "subscribed_scene_topic": self.scene_data_topic,
            "analysis_interval_seconds": self.analysis_interval,
            "cameras_being_tracked": list(self.last_processed_time.keys())
        }