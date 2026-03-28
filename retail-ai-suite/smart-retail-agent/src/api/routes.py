# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""API routes for Retail Store Agent."""

from datetime import datetime, timedelta
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from fastapi.responses import JSONResponse
import structlog

from services.data_aggregator import DataAggregatorService

logger = structlog.get_logger(__name__)

router = APIRouter()


def get_data_aggregator(request):
    """Dependency to get data aggregator service from app state."""
    return request.app.state.data_aggregator


@router.get("/retail/current", response_model=Dict[str, Any])
async def get_current_retail_analysis(
    request: Request
) -> Dict[str, Any]:
    """
    Get current retail store analysis data.
    
    Returns complete retail analysis response with VLM insights (without images).
    """
    try:
        data_aggregator: DataAggregatorService = get_data_aggregator(request)
        
        # Get current retail analysis
        retail_response = await data_aggregator.get_current_retail_analysis()
        
        if not retail_response:
            raise HTTPException(status_code=404, detail="No retail data available")

        # Convert to dict for JSON response
        response_dict = {
            "timestamp": retail_response.timestamp,
            "response_age": retail_response.response_age if retail_response.response_age else None,
            "store_id": retail_response.store_id,
            "data": {
                "store_id": retail_response.data.store_id,
                "store_name": retail_response.data.store_name,
                "timestamp": retail_response.data.timestamp.isoformat(),
                "camera_data": retail_response.data.camera_data
            },
            "vlm_analysis": {
                "summary": retail_response.vlm_analysis.summary,
                "analysis_timestamp": retail_response.vlm_analysis.analysis_timestamp.isoformat() if retail_response.vlm_analysis.analysis_timestamp else None
            }
        }
        
        logger.info("Current retail analysis served",
                   store_id=retail_response.store_id)
        
        return response_dict
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get current retail analysis", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/status")
async def get_service_status(request: Request) -> Dict[str, Any]:
    """
    Get current service status and statistics.
    """
    try:
        data_aggregator: DataAggregatorService = get_data_aggregator(request)
        status = data_aggregator.get_service_status()
        
        logger.info("Service status requested", status=status)
        
        return {
            "status": "operational",
            "details": status,
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error("Failed to get service status", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
