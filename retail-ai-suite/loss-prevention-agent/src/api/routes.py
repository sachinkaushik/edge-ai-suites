# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""REST API routes for the Loss Prevention Agent."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
import structlog

from services.alert_publisher import AlertPublisher
from services.config import ConfigService
from services.session_manager import SessionManager

logger = structlog.get_logger(__name__)

router = APIRouter()


def _get_alert_publisher(request: Request) -> AlertPublisher:
    return request.app.state.alert_publisher


def _get_session_manager(request: Request) -> SessionManager:
    return request.app.state.session_manager


def _get_config(request: Request) -> ConfigService:
    return request.app.state.config


# ---- Alerts ------------------------------------------------------------------

@router.get("/alerts", response_model=List[Dict[str, Any]])
async def get_alerts(
    request: Request,
    alert_type: Optional[str] = Query(None, description="Filter by alert type"),
    object_id: Optional[str] = Query(None, description="Filter by person object_id"),
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    """Return recent alerts, optionally filtered by type or person."""
    pub = _get_alert_publisher(request)
    if object_id:
        return pub.get_by_person(object_id)
    if alert_type:
        return pub.get_by_type(alert_type, limit)
    return pub.get_recent(limit)


@router.get("/alerts/count")
async def get_alert_count(request: Request) -> Dict[str, int]:
    pub = _get_alert_publisher(request)
    return {"total": pub.total_count}


# ---- Sessions ----------------------------------------------------------------

@router.get("/sessions", response_model=List[Dict[str, Any]])
async def get_sessions(request: Request) -> List[Dict[str, Any]]:
    """Return all active person sessions."""
    sm = _get_session_manager(request)
    sessions = sm.get_all_sessions()
    return [
        {
            "object_id": s.object_id,
            "first_seen": s.first_seen.isoformat(),
            "last_seen": s.last_seen.isoformat(),
            "current_cameras": s.current_cameras,
            "current_regions": list(s.current_regions),
            "visited_checkout": s.visited_checkout,
            "visited_high_value": s.visited_high_value,
            "concealment_suspected": s.concealment_suspected,
            "visit_count_per_region": s.visit_count_per_region,
            "num_region_visits": len(s.region_visits),
            "frame_buffer_size": len(s.frame_buffer),
        }
        for s in sessions.values()
    ]


@router.get("/sessions/count")
async def get_session_count(request: Request) -> Dict[str, int]:
    sm = _get_session_manager(request)
    return {"active": sm.get_active_count()}


# ---- Health ------------------------------------------------------------------

@router.get("/status")
async def get_status(request: Request) -> Dict[str, Any]:
    """Service health and basic statistics."""
    sm = _get_session_manager(request)
    pub = _get_alert_publisher(request)
    config = _get_config(request)
    zones = config.get_zones()
    return {
        "status": "operational",
        "active_sessions": sm.get_active_count(),
        "total_alerts": pub.total_count,
        "zones_configured": len(zones),
        "zone_types": {
            zt: sum(1 for z in zones.values() if z.get("type") == zt)
            for zt in ("HIGH_VALUE", "CHECKOUT", "EXIT", "RESTRICTED")
        },
        "timestamp": datetime.utcnow().isoformat(),
    }


# ---- Zones (Option A + C) ---------------------------------------------------

class ZoneInput(BaseModel):
    name: str
    type: str  # HIGH_VALUE | CHECKOUT | EXIT | RESTRICTED

VALID_ZONE_TYPES = {"HIGH_VALUE", "CHECKOUT", "EXIT", "RESTRICTED"}


@router.get("/zones")
async def get_zones(request: Request) -> Dict[str, Any]:
    """Return all configured zone mappings."""
    config = _get_config(request)
    return config.get_zones()


@router.put("/zones/{region_id}")
async def set_zone(
    request: Request, region_id: str, body: ZoneInput
) -> Dict[str, Any]:
    """Add or update a zone mapping at runtime (no restart needed)."""
    if body.type not in VALID_ZONE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid zone type '{body.type}'. Must be one of: {sorted(VALID_ZONE_TYPES)}",
        )
    config = _get_config(request)
    config.set_zone(region_id, body.name, body.type)
    return {"region_id": region_id, "name": body.name, "type": body.type}


@router.delete("/zones/{region_id}")
async def delete_zone(request: Request, region_id: str) -> Dict[str, str]:
    """Remove a zone mapping."""
    config = _get_config(request)
    removed = config.remove_zone(region_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Zone not found")
    return {"status": "removed", "region_id": region_id}


@router.post("/zones/discover")
async def discover_zones(request: Request) -> Dict[str, Any]:
    """Trigger re-discovery of zones from SceneScape API."""
    ss_client = getattr(request.app.state, "scenescape_client", None)
    if not ss_client:
        raise HTTPException(status_code=503, detail="SceneScape client not configured")

    config = _get_config(request)
    regions = await ss_client.fetch_regions()
    if not regions:
        return {"status": "no_regions", "discovered": 0, "total": len(config.get_zones())}

    new_zones = ss_client.map_zones(regions)
    added = config.merge_zones(new_zones)
    return {
        "status": "ok",
        "discovered": len(new_zones),
        "added": added,
        "total": len(config.get_zones()),
    }


@router.get("/zones/names")
async def get_zone_name_map(request: Request) -> Dict[str, str]:
    """Return the zone name → zone type map from config."""
    config = _get_config(request)
    return config.get_zone_name_map()
