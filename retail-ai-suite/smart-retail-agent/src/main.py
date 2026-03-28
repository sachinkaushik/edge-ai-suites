# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

import structlog
import uvicorn
from fastapi import FastAPI

from api.routes import router
from services.config import ConfigService
from services.mqtt_service import MQTTService
from services.vlm_service import VLMService
from services.data_aggregator import DataAggregatorService


# Configure structured logging
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
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for Retail Store Agent."""
    logger.info("Starting Retail Store Agent")
    
    try:
        # Load configuration
        config_service = ConfigService()
        app.state.config = config_service
        
        # Initialize VLM service for retail analysis
        vlm_service = VLMService(config_service)
        app.state.vlm_service = vlm_service
        
        # Initialize data aggregator service
        data_aggregator = DataAggregatorService(config_service, vlm_service)
        app.state.data_aggregator = data_aggregator
        
        # Initialize and start MQTT service for camera data
        mqtt_service = MQTTService(config_service, data_aggregator, vlm_service)
        await mqtt_service.initialize()
        
        # Set the current event loop for async task scheduling
        current_loop = asyncio.get_running_loop()
        mqtt_service.set_event_loop(current_loop)
        
        app.state.mqtt = mqtt_service
        
        # Start MQTT service in background
        mqtt_task = asyncio.create_task(mqtt_service.start())
        app.state.mqtt_task = mqtt_task
        
        logger.info("Retail Store Agent started successfully", 
                   store_id=config_service.get_store_id(),
                   store_name=config_service.get_store_name(),
                   mqtt_topics=config_service.get_camera_topics())
        
        yield
        
    except Exception as e:
        logger.error("Failed to start Retail Store Agent", error=str(e))
        raise
    
    finally:
        # Cleanup
        logger.info("Shutting down Retail Store Agent")
        
        # Stop MQTT service
        if hasattr(app.state, 'mqtt_task'):
            app.state.mqtt_task.cancel()
            try:
                await app.state.mqtt_task
            except asyncio.CancelledError:
                pass
        
        if hasattr(app.state, 'mqtt'):
            await app.state.mqtt.stop()
        
        
        logger.info("Retail Store Agent stopped")


def create_app() -> FastAPI:
    """Create and configure FastAPI application for Retail Store Agent."""
    api_name = os.getenv("API_NAME", "Retail Store Agent")
    
    app = FastAPI(
        title=api_name,
        description="Retail Store Monitoring Agent API for scene understanding",
        version="1.0.0",
        lifespan=lifespan
    )
    
    # Include API routes
    app.include_router(router, prefix="/api/v1")
    
    # Health check endpoint
    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {
            "status": "healthy", 
            "service": "retail-store-agent",
            "timestamp": datetime.utcnow().isoformat()
        }
    
    return app


def main():
    """Main entry point for Retail Store Agent."""
    # Set up logging level
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level))
    
    # Create FastAPI app
    app = create_app()
    
    # When running application on host we can override host and port via env variables
    port = int(os.getenv("AGENT_BACKEND_HOSTPORT", "8081"))
    host = os.getenv("AGENT_BACKEND_HOST", "0.0.0.0")
    
    logger.info("Starting Retail Store Agent", 
               host=host, port=port, log_level=log_level)
    
    # Run the application
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=True
    )


if __name__ == "__main__":
    main()