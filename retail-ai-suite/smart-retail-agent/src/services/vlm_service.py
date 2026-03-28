# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""VLM service for retail store analysis."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import aiohttp
import structlog

from models import VLMAnalysisData, CameraImage, StoreSnapshot
from .config import ConfigService


logger = structlog.get_logger(__name__)


class VLMService:
    """
    VLM service for retail store scene understanding.
    
    Features:
    - Structured prompts for scene analysis
    - Object tracking integration
    - Scene understanding generation
    """

    def __init__(self, config_service: ConfigService):
        """Initialize VLM service with configuration."""
        self.config = config_service
        self.vlm_config = config_service.get_vlm_config()
        
        # VLM service configuration
        self.base_url = self.vlm_config.get("base_url", "http://vlm-service:8080")
        self.model = self.vlm_config.get("model", "gpt-4-vision-preview")
        self.timeout = self.vlm_config.get("timeout_seconds", 300)
        self.max_tokens = self.vlm_config.get("max_completion_tokens", 2000)
        self.temperature = self.vlm_config.get("temperature", 0.1)
        self.top_p = self.vlm_config.get("top_p", 0.1)
        
        # Store config service for dynamic access
        self.config_service = config_service
        
        # Cache for recent analyses
        self._analysis_cache: Dict[str, VLMAnalysisData] = {}
        
        # Semaphore to ensure only one VLM request at a time
        self._vlm_semaphore = asyncio.Semaphore(1)
        
        # Store last successful analysis to reuse when service is busy
        self._last_analysis: Optional[VLMAnalysisData] = None
        self._last_analysis_timestamp: Optional[datetime] = None
        
        logger.info("VLM service initialized", 
                   base_url=self.base_url,
                   model=self.model)
        
    def get_vlm_semaphore(self) -> asyncio.Semaphore:
        """Get the VLM semaphore for external use."""
        return self._vlm_semaphore
        
    async def analyze_store(
            self, 
            store_snapshot: StoreSnapshot,
            camera_images: List[CameraImage]
    ) -> Optional[VLMAnalysisData]:
        """
        Perform comprehensive retail store scene understanding.
        
        Args:
            store_snapshot: Current store data with scene tracking
            camera_images: List of camera images from store
            
        Returns:
            VLMAnalysisData with scene understanding
        """ 
        try:
            # Create structured prompt
            prompt = self._create_structured_prompt(store_snapshot)

            logger.info("Generated VLM prompt", prompt=prompt)
            
            # Prepare VLM request with images
            logger.info("Preparing VLM request", 
                       camera_images_available=len(camera_images),
                       camera_ids=[img.camera_id for img in camera_images],
                       image_data_present=[bool(img.image_base64) for img in camera_images])
            vlm_request = self._build_vlm_request(prompt, camera_images)
            
            # Call VLM service
            analysis_result = await self._call_vlm_service(vlm_request)
            
            if analysis_result:
                # Parse structured response
                structured_analysis = self._parse_vlm_response(analysis_result, store_snapshot)
                
                # Cache the analysis
                self._analysis_cache[store_snapshot.store_id] = structured_analysis
                
                # Store as last successful analysis for reuse when service is busy
                self._last_analysis = structured_analysis
                self._last_analysis_timestamp = datetime.now(timezone.utc)
                
                logger.info("VLM analysis completed successfully", 
                           store_id=store_snapshot.store_id,
                           summary_length=len(structured_analysis.summary))
                
                return structured_analysis
            else:
                logger.warning("VLM service returned no result, using fallback analysis")
                fallback_analysis = self._create_fallback_analysis("VLM service returned no result", store_snapshot)
                logger.info("Using fallback analysis")
                return fallback_analysis
                
        except Exception as e:
            logger.error("VLM analysis failed", error=str(e))
            fallback_analysis = self._create_fallback_analysis(f"VLM error: {str(e)}", store_snapshot)
            logger.info("Using fallback analysis")
            return fallback_analysis
    
    async def analyze_store_non_blocking(self, store_snapshot: StoreSnapshot) -> Optional[VLMAnalysisData]:
        """
        Non-blocking store analysis that uses semaphore to prevent concurrent requests.
        Returns cached analysis if VLM service is busy.
        
        Args:
            store_snapshot: Current store data
            
        Returns:
            VLMAnalysisData - either new analysis or cached result if service is busy
        """
        try:
            # Try to acquire semaphore without blocking
            if self._vlm_semaphore.locked():
                current_time = datetime.now(timezone.utc)
                logger.info("VLM service is busy, returning cached analysis", 
                           has_cached_analysis=self._last_analysis is not None,
                           last_analysis_age_minutes=(current_time - self._last_analysis_timestamp).total_seconds() / 60 
                           if self._last_analysis_timestamp else None)
                
                # Return last successful analysis if available
                if self._last_analysis:
                    # Update timestamp to current time for the cached result
                    cached_analysis = VLMAnalysisData(
                        summary=self._last_analysis.summary,
                        analysis_timestamp=current_time  # Update to current time
                    )
                    logger.debug("Returning cached VLM analysis", 
                               original_timestamp=self._last_analysis.analysis_timestamp)
                    return cached_analysis
                else:
                    logger.warning("VLM service busy and no cached analysis available")
                    return None
            
            # Semaphore is available, acquire it and perform analysis
            async with self._vlm_semaphore:
                logger.info("VLM service is free, performing new analysis")
                
                # Convert camera_images dict to list (single image per camera)
                camera_images = []
                
                if store_snapshot.camera_images:
                    for camera_id, camera_image in store_snapshot.camera_images.items():
                        # camera_image is now a single CameraImage object, not a list
                        camera_images.append(camera_image)
                
                logger.info("Collected frames for VLM analysis",
                           total_frames=len(camera_images),
                           cameras=list(store_snapshot.camera_images.keys()) if store_snapshot.camera_images else [])
                
                analysis_result = await self.analyze_store(store_snapshot, camera_images)
                
                # Cache successful result
                if analysis_result:
                    self._last_analysis = analysis_result
                    self._last_analysis_timestamp = datetime.now(timezone.utc)
                    logger.info("New VLM analysis completed and cached", 
                               summary_length=len(analysis_result.summary))
                else:
                    logger.warning("New VLM analysis returned no result")
                
                return analysis_result
                
        except Exception as e:
            logger.error("Non-blocking VLM analysis failed", error=str(e))
            return self._last_analysis  # Return cached result on error if available
    
    
    def _create_structured_prompt(self, store_snapshot: StoreSnapshot) -> str:
        """
        Create structured prompt for scene understanding.
        
        Args:
            store_snapshot: Store snapshot with camera data, images, and scene tracking
            
        Returns:
            Structured prompt string for VLM
        """
        store_name = self.config.get_store_name()
        timestamp = store_snapshot.timestamp.strftime("%H:%M:%S")
        
        # Camera information with context
        camera_info = []
        camera_contexts = {}
        if store_snapshot.camera_configs:
            for camera_name, camera_config in store_snapshot.camera_configs.items():
                camera_contexts[camera_name] = camera_config.description
                if camera_name in store_snapshot.camera_data:
                    data = store_snapshot.camera_data[camera_name]
                    camera_info.append(
                        f"Camera: {camera_name} ({camera_config.description})\n"
                        f"  Data: {json.dumps(data, indent=2)}"
                    )
        
        camera_summary = "\n".join(camera_info) if camera_info else "No camera data available"
        
        # Scene tracking data (object metadata)
        scene_info = "No scene tracking data available"
        if store_snapshot.scene_data and store_snapshot.scene_data.objects:
            scene_objects = []
            for obj in store_snapshot.scene_data.objects:
                # Build region info
                regions_info = []
                for region in obj.regions:
                    regions_info.append(f"{region.region_name or region.region_id}")
                
                # Check for employee attribute in metadata
                is_employee = False
                employee_status = "Customer"
                if obj.metadata and 'sensors' in obj.metadata:
                    sensors = obj.metadata.get('sensors', {})
                    if 'Employee_Register' in sensors:
                        employee_register = sensors.get('Employee_Register', [])
                        if isinstance(employee_register, list) and len(employee_register) > 0:
                            is_employee = True
                            employee_status = "Employee"
                
                obj_info = (
                    f"  - Object ID: {obj.object_id}\n"
                    f"    Category: {obj.category}\n"
                    f"    Role: {employee_status}\n"
                    f"    Visibility: {obj.visibility:.2f}\n"
                    f"    Visible in cameras: {', '.join(obj.camera_ids) if obj.camera_ids else 'none'}\n"
                    f"    Regions: {', '.join(regions_info) if regions_info else 'none'}"
                )
                scene_objects.append(obj_info)
            
            scene_info = f"Tracked Objects ({len(store_snapshot.scene_data.objects)} total):\n" + "\n".join(scene_objects)
        
        # Camera context mapping
        camera_context_str = "\n".join([f"- {name}: {desc}" for name, desc in camera_contexts.items()])
        
        # Create structured prompt for scene understanding
        prompt = f"""Analyze the retail store scene and provide a comprehensive scene understanding.

===== CONTEXT =====

STORE INFORMATION:
Store: {store_name}
Timestamp: {timestamp}

CAMERA LOCATIONS AND CONTEXTS:
{camera_context_str}

CAMERA DATA:
{camera_summary}

OBJECT TRACKING METADATA:
{scene_info}

===== ANALYSIS INSTRUCTIONS =====

Analyze the camera images and object tracking metadata to understand what's happening in the store. Use the unique object IDs to track the same person across frames and cameras. Consider the camera contexts (which areas they monitor) when analyzing activities and interactions.

STEP 1 - IDENTIFY PEOPLE:
First, identify and categorize people in the scene:
- Each tracked object in the metadata includes a "Role" field that indicates "Employee" or "Customer"
- This role is determined by the presence of "sensors.Employee_Register" attribute in the tracking system
- USE THIS ROLE FIELD as the primary indicator of whether someone is an employee or customer
- You may optionally verify this with visual cues (uniforms, name badges, vests, lanyards, etc.) if visible in the images
- Reference each person by their Object ID throughout your analysis

STEP 2 - ANALYZE ACTIVITIES AND INTERACTIONS:
Provide detailed analysis covering:

A) Store Overview:
   - High-level description of what's happening in the store
   - Overall activity level and flow patterns
   - Which store sections have the most activity

B) Individual Activities (reference object IDs):
   - Employee activities: restocking, standing at checkout, monitoring, organizing displays, etc.
   - Customer activities: shopping, browsing, examining products, moving through aisles, etc.

C) Person-to-Person Interactions:
   - CUSTOMER-EMPLOYEE INTERACTIONS: Customers asking for help, employees assisting customers, product recommendations, questions being answered, checkout interactions
   - EMPLOYEE-EMPLOYEE INTERACTIONS: Employees collaborating, communicating, coordinating tasks, working together on displays or restocking
   - CUSTOMER-CUSTOMER INTERACTIONS: People shopping together, conversations, group coordination

D) Person-to-Object Interactions:
   - Products being examined, picked up, or returned to shelves
   - Interaction with store fixtures (shelves, displays, shopping carts, baskets)
   - Dwell time at specific product areas or displays
   - Products being added to or removed from carts/baskets

E) Behavioral Insights:
   - Movement patterns and navigation through the store
   - Dwell times in different sections and what they indicate
   - Customer engagement levels with products or displays
   - Employee attentiveness and customer service behaviors
   - Any unusual behaviors or areas of concern
   - Actionable insights for store operations or customer experience improvement

===== OUTPUT FORMAT =====

Respond in plain text with clear paragraphs and sub-points. Use bullet points for clarity. Reference object IDs when discussing specific individuals to maintain tracking continuity."""
        
        return prompt
    
    def _build_vlm_request(self, prompt: str, camera_images: List[CameraImage]) -> Dict[str, Any]:
        """Build VLM API request with multiple images."""
        
        logger.debug("Building VLM request", 
                   prompt_length=len(prompt),
                   camera_images_count=len(camera_images))
        
        # Prepare content with text prompt and images
        content = [
            {
                "type": "text",
                "text": prompt
            }
        ]
        
        # Add camera images
        for camera_image in camera_images:
            if camera_image.image_base64:
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{camera_image.image_base64}"
                    }
                })
        
        # Use configurable parameters
        request = {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ]
        }
        
        logger.debug("VLM request built", request=request)
        
        return request
    
    async def _call_vlm_service(self, request_data: Dict[str, Any]) -> Optional[str]:
        """
        Call VLM service API with error handling.
        
        Args:
            request_data: VLM API request payload
            
        Returns:
            VLM response text or None if failed
        """
        try:
            url = f"{self.base_url}/v1/chat/completions"
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.post(url, json=request_data) as response:
                    if response.status == 200:
                        result = await response.json()
                        
                        logger.debug("VLM service raw response", response_keys=list(result.keys()) if isinstance(result, dict) else "non-dict")
                        
                        if 'choices' in result and len(result['choices']) > 0:
                            choice = result['choices'][0]
                            if 'message' in choice and 'content' in choice['message']:
                                content = choice['message']['content']
                                logger.info("VLM service response received successfully", 
                                          content_length=len(content))
                                return content.strip()
                            else:
                                logger.error("Missing message.content in VLM response")
                                return None
                        else:
                            logger.error("Invalid VLM response format - missing choices")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error("VLM service error", 
                                   status=response.status, error=error_text)
                        return None
        except aiohttp.ClientConnectorError as e:
            logger.warning("VLM service connection failed", error=str(e))
            return None
        except Exception as e:
            logger.error("VLM service call failed", error=str(e))
            return None
    
    def _parse_vlm_response(self, 
                           response_text: str,
                           store_snapshot: StoreSnapshot) -> VLMAnalysisData:
        """
        Parse VLM response into structured VLMAnalysisData.
        Uses the raw response string directly as the scene understanding summary.
        
        Args:
            response_text: Raw VLM response
            store_snapshot: Store data used for analysis
            
        Returns:
            Structured VLMAnalysisData object
        """
        try:
            logger.debug("VLM raw response", response=response_text)
            
            # Use the raw VLM response string directly as the summary
            analysis = VLMAnalysisData(
                summary=response_text.strip(),
                analysis_timestamp=datetime.now(timezone.utc)
            )
            
            logger.info("VLM response stored as raw string",
                       summary_length=len(analysis.summary))
            
            return analysis
            
        except Exception as e:
            logger.error("Error parsing VLM response", error=str(e))
            return self._create_fallback_analysis(f"Parse error: {str(e)}", store_snapshot)
    
    def _create_fallback_analysis(self, 
                                  error_msg: str,
                                  store_snapshot: StoreSnapshot) -> VLMAnalysisData:
        """
        Create fallback analysis when VLM service fails.
        
        Args:
            error_msg: Error message or partial response
            store_snapshot: Store data
            
        Returns:
            Basic VLMAnalysisData with generic information
        """
        logger.info("Creating fallback analysis")
        
        # Create basic scene understanding
        analysis = VLMAnalysisData(
            summary=f"Store monitoring active at {store_snapshot.timestamp.strftime('%H:%M:%S')}. VLM analysis temporarily unavailable.",
            analysis_timestamp=datetime.now(timezone.utc)
        )
        
        return analysis
