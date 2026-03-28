# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Loss Prevention Agent — main application entry point.

Wires together all services:
  MQTT → SessionManager → RuleEngine → AlertPublisher
                        → PoseAnalyzer → VLMClient → AlertPublisher
                        → FrameStore (MinIO)
"""

import asyncio
import base64
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
import uvicorn
from fastapi import FastAPI

from api.routes import router
from services.config import ConfigService
from services.mqtt_service import MQTTService
from services.session_manager import SessionManager
from services.rule_engine import RuleEngine
from services.pose_analyzer import PoseAnalyzer
from services.vlm_client import VLMClient
from services.frame_store import FrameStore
from services.alert_publisher import AlertPublisher
from services.scenescape_client import SceneScapeClient

# ---- Structured logging setup -----------------------------------------------
logging.basicConfig(format="%(message)s", stream=__import__("sys").stdout, level=logging.DEBUG)
logging.getLogger().setLevel(logging.DEBUG)

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


# ---- FastAPI lifespan --------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start all services, yield, then tear down."""
    logger.info("Starting Loss Prevention Agent")

    # 1. Config
    config = ConfigService()
    app.state.config = config

    # 1b. Discover zones from SceneScape API (match by region name)
    ss_client = SceneScapeClient(config)
    app.state.scenescape_client = ss_client
    ss_user = os.environ.get("SCENESCAPE_API_USER", "")
    ss_pass = os.environ.get("SCENESCAPE_API_PASSWORD", "")
    if ss_user and ss_pass:
        # Authenticate first — needed for both scene resolution and zone discovery
        authenticated = await ss_client.authenticate(ss_user, ss_pass)
        if not authenticated:
            logger.error("SceneScape API authentication failed")
        else:
            # Resolve scene_name → scene_id via SceneScape API
            scene_name = config.get_scene_name()
            if scene_name:
                scene_id = await ss_client.resolve_scene_id(scene_name)
                if scene_id:
                    config.set_scene_id(scene_id)
                    logger.info("Scene resolved from name", scene_name=scene_name, scene_id=scene_id)
                else:
                    logger.error("Could not resolve scene_name to scene_id", scene_name=scene_name)

            # Discover and map zones (already authenticated, skip re-auth)
            regions = await ss_client.fetch_regions()
            if regions:
                discovered = ss_client.map_zones(regions)
                if discovered:
                    config.merge_zones(discovered)
                    logger.info("Zone discovery complete", zones=len(config.get_zones()))
                else:
                    logger.warning("Zone discovery found no matching regions")
            else:
                logger.warning("No regions found in SceneScape")
    else:
        logger.warning(
            "SCENESCAPE_API_USER / SCENESCAPE_API_PASSWORD not set, "
            "zone discovery skipped. Use POST /api/v1/lp/zones/discover "
            "or PUT /api/v1/lp/zones/{region_id} to add zones at runtime."
        )

    # 2. Frame store (MinIO)
    frame_store = FrameStore(config)
    await frame_store.ensure_buckets()
    app.state.frame_store = frame_store

    # 3. MQTT
    mqtt_svc = MQTTService(config)
    await mqtt_svc.initialize()
    loop = asyncio.get_running_loop()
    mqtt_svc.set_event_loop(loop)
    app.state.mqtt_service = mqtt_svc

    # 4. Alert publisher
    alert_pub = AlertPublisher(config, mqtt_svc)
    app.state.alert_publisher = alert_pub

    # 5. Session manager
    session_mgr = SessionManager(config)
    app.state.session_manager = session_mgr

    # 6. Rule engine
    rule_engine = RuleEngine(config, session_mgr, alert_callback=alert_pub.publish)
    app.state.rule_engine = rule_engine

    # 7. Pose analyzer
    pose_analyzer = PoseAnalyzer(config)
    app.state.pose_analyzer = pose_analyzer

    # 8. VLM client
    vlm_client = VLMClient(
        config, frame_store, session_mgr, alert_callback=alert_pub.publish
    )
    app.state.vlm_client = vlm_client

    # ---- Wire callbacks ----
    # Session manager fires events → rule engine + pose gating
    session_mgr.register_event_handler(rule_engine.on_event)

    # Pose analyzer escalates → VLM client
    pose_analyzer.register_flag_handler(vlm_client.on_shelf_to_waist)

    # MQTT scene data → session manager (liveness: cameras, bbox, last_seen)
    mqtt_svc.register_scene_data_handler(session_mgr.on_scene_data)

    # MQTT region events → session manager (enter/exit with dwell from SceneScape)
    mqtt_svc.register_region_event_handler(session_mgr.on_region_event)

    # MQTT camera images → frame storage + pose triggering
    async def on_camera_image(camera_name: str, data: dict) -> None:
        image_b64 = data.get("image", data.get("data", ""))
        if not image_b64:
            return
        image_bytes = base64.b64decode(image_b64)
        ts = datetime.now(timezone.utc)
        minio_key = frame_store.store_frame(camera_name, image_bytes, ts)

        # Update frame buffers for all sessions visible on this camera
        for session in session_mgr.get_all_sessions().values():
            if camera_name in session.current_cameras:
                session.add_frame_key(minio_key)
                # Feed future frames to VLM if pending
                vlm_client.collect_future_frame(session.object_id, minio_key)

    mqtt_svc.register_camera_image_handler(on_camera_image)

    # Start background tasks
    mqtt_task = asyncio.create_task(mqtt_svc.start())
    expiry_task = asyncio.create_task(session_mgr.run_expiry_loop())

    logger.info(
        "Loss Prevention Agent started",
        store_id=config.get_store_id(),
        zones=len(config.get_zones()),
        cameras=len(config.get_cameras()),
    )

    yield

    # ---- Shutdown ----
    logger.info("Shutting down Loss Prevention Agent")
    await mqtt_svc.stop()
    expiry_task.cancel()
    mqtt_task.cancel()


# ---- App ---------------------------------------------------------------------

app = FastAPI(
    title="Loss Prevention Agent",
    description="Store-wide Suspicious Activity Detection",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1/lp")


@app.get("/health")
async def health():
    return {"status": "healthy"}


# ---- Entry point -------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8082,
        log_level="info",
    )
