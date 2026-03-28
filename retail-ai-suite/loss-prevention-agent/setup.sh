#!/bin/bash

# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# Color codes for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

export APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HOST_IP=$(ip route get 1 2>/dev/null | awk '{print $7}')
if [ -z "$HOST_IP" ]; then
    export HOST_IP="localhost"
fi

# Project name
PROJECT_NAME="loss-prevention-agent"

# SceneScape certs source
SCENESCAPE_CERTS="${APP_DIR}/../../scenescape/secrets/certs"

# Setting command usage and invalid arguments handling before the actual setup starts
if [ "$#" -eq 0 ] || ([ "$#" -eq 1 ] && [ "$1" = "--help" ]); then
    # If no valid argument is passed, print usage information
    echo -e "-----------------------------------------------------------------"
    echo -e "${YELLOW}USAGE: ${GREEN}source setup.sh ${BLUE}[--setenv | --setup | --run | --restart | --stop | --clean | --help]"
    echo -e "${YELLOW}"
    echo -e "  --setenv:                 Set environment variables without building image or starting any containers"
    echo -e "  --setup:                  Build and run the loss prevention agent"
    echo -e "  --run:                    Start the agent without building image (if already built)"
    echo -e "  --restart:                Restart the loss prevention agent"
    echo -e "  --stop:                   Stop the agent"
    echo -e "  --clean:                  Clean up containers, volumes, and logs"
    echo -e "                              • --keep-models - Remove all application volume data except VLM models"
    echo -e "  --help:                   Show this help message${NC}"
    echo -e "-----------------------------------------------------------------"
    echo -e "${CYAN}NOTE: This assumes SceneScape is already running in the same network.${NC}"
    echo -e "-----------------------------------------------------------------"
    return 0

elif [ "$#" -gt 2 ]; then
    echo -e "${RED}ERROR: Too many arguments provided.${NC}"
    echo -e "${YELLOW}Use --help for usage information${NC}"
    return 1

elif [ "$1" != "--help" ] && [ "$1" != "--setenv" ] && [ "$1" != "--run" ] && [ "$1" != "--setup" ] && [ "$1" != "--restart" ] && [ "$1" != "--stop" ] && [ "$1" != "--clean" ]; then
    # Default case for unrecognized option
    echo -e "${RED}Unknown option: $1 ${NC}"
    echo -e "${YELLOW}Use --help for usage information${NC}"
    return 1

elif [ "$1" = "--clean" ] && [ "$#" -eq 2 ] && [ "$2" != "--keep-models" ]; then
    echo -e "${RED}ERROR: Invalid option for --clean: $2${NC}"
    echo -e "${YELLOW}Valid options: --keep-models${NC}"
    echo -e "${YELLOW}Use --help for usage information${NC}"
    return 1

elif [ "$1" = "--stop" ] || [ "$1" = "--clean" ]; then
    echo -e "${YELLOW}Stopping Loss Prevention Agent ${RED}${PROJECT_NAME} ${YELLOW}... ${NC}"
    
    docker compose -f "${APP_DIR}/docker/docker-compose.yaml" -p ${PROJECT_NAME} down 2> /dev/null

    if [ $? -ne 0 ]; then
        echo -e "${RED}Failed to stop Loss Prevention Agent services. ${NC}"
        return 1
    fi
    echo -e "${GREEN}All containers for Loss Prevention Agent stopped and removed! ${NC}"

    if [ "$1" = "--clean" ]; then
        echo -e "${YELLOW}Removing volumes for Loss Prevention Agent ... ${NC}"
        if [ "$2" = "--keep-models" ]; then
            echo -e "${CYAN}Keeping VLM model cache volume (ov-models)...${NC}"
            docker volume ls | grep $PROJECT_NAME | grep -v "ov-models" | awk '{ print $2 }' | xargs docker volume rm 2>/dev/null || true
        else
            docker volume ls | grep $PROJECT_NAME | awk '{ print $2 }' | xargs docker volume rm 2>/dev/null || true
        fi
        echo -e "${GREEN}Cleanup completed successfully. ${NC}"
    fi

    return 0
fi

# Export environment variables required by application (HOST_IP already set above)
export LOG_LEVEL=${LOG_LEVEL:-INFO}
export USER_GROUP_ID=$(id -g)
export VIDEO_GROUP_ID=$(getent group video | awk -F: '{printf "%s\n", $3}' 2>/dev/null || echo "44")
export RENDER_GROUP_ID=$(getent group render | awk -F: '{printf "%s\n", $3}' 2>/dev/null || echo "109")

# Store Configuration (can be overridden by environment variables)
export STORE_NAME=${STORE_NAME:-retail_store_1}
export STORE_ID=${STORE_ID:-store_001}

# MQTT Configuration
export MQTT_HOST=${MQTT_HOST:-broker.scenescape.intel.com}
export MQTT_PORT=${MQTT_PORT:-1883}

# MinIO Configuration
export MINIO_API_PORT=${MINIO_API_PORT:-9000}
export MINIO_CONSOLE_PORT=${MINIO_CONSOLE_PORT:-9001}
export MINIO_ROOT_USER=${MINIO_ROOT_USER:-minioadmin}
export MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD:-minioadmin}

# VLM Service Configuration
export VLM_MODEL_NAME=${VLM_MODEL_NAME:-Qwen/Qwen2.5-VL-3B-Instruct}

# VLM OpenVINO Configuration
export VLM_DEVICE=${VLM_DEVICE:-CPU}
export VLM_COMPRESSION_WEIGHT_FORMAT=${VLM_COMPRESSION_WEIGHT_FORMAT:-int8}
export VLM_SEED=${VLM_SEED:-42}
export VLM_WORKERS=${VLM_WORKERS:-1}
export VLM_LOG_LEVEL=${VLM_LOG_LEVEL:-info}
export VLM_ACCESS_LOG_FILE=${VLM_ACCESS_LOG_FILE:-/dev/null}

# Automatically adjust VLM settings for GPU
if [[ "$VLM_DEVICE" == "GPU" ]]; then
    export VLM_COMPRESSION_WEIGHT_FORMAT=int4
    export VLM_WORKERS=1  # GPU works best with single worker
fi

# Health Check Configuration
export HEALTH_CHECK_INTERVAL=${HEALTH_CHECK_INTERVAL:-30s}
export HEALTH_CHECK_TIMEOUT=${HEALTH_CHECK_TIMEOUT:-10s}
export HEALTH_CHECK_RETRIES=${HEALTH_CHECK_RETRIES:-3}
export HEALTH_CHECK_START_PERIOD=${HEALTH_CHECK_START_PERIOD:-10s}

# Backend Port Configuration
export LP_AGENT_PORT=${LP_AGENT_PORT:-8082}

# Get and print the ports of all running services
print_service_endpoints() {
    echo -e
    echo -e "${MAGENTA}======================================================="
    echo -e "SERVICE ENDPOINTS"
    echo -e "=======================================================${NC}"
    
    for CONTAINER_NAME in $(docker ps --format '{{.Names}}' | grep $PROJECT_NAME);
    do
        case "$CONTAINER_NAME" in
            *loss-prevention-agent*)
                BACKEND_SERVICE_NAME="Loss Prevention Agent API"
                PORT=$(docker port "$CONTAINER_NAME" 8082 | cut -d: -f2)
                echo -e "${CYAN}$BACKEND_SERVICE_NAME -> http://$HOST_IP:$PORT/docs${NC}"
                echo -e "${CYAN}  Health  -> http://$HOST_IP:$PORT/health${NC}"
                echo -e "${CYAN}  Alerts  -> http://$HOST_IP:$PORT/api/v1/lp/alerts${NC}"
                echo -e "${CYAN}  Sessions -> http://$HOST_IP:$PORT/api/v1/lp/sessions${NC}"
                ;;
            *vlm*)
                SERVICE_NAME="VLM Service"
                PORT=$(docker port "$CONTAINER_NAME" 8000 | cut -d: -f2)
                echo -e "${BLUE}$SERVICE_NAME -> http://$HOST_IP:$PORT${NC}"
                ;;
            *minio*)
                SERVICE_NAME="MinIO Console"
                PORT=$(docker port "$CONTAINER_NAME" 9001 | cut -d: -f2)
                echo -e "${GREEN}$SERVICE_NAME -> http://$HOST_IP:$PORT${NC}"
                ;;
        esac
    done
    echo -e "${MAGENTA}=======================================================${NC}"
    echo -e
}

# Print environment summary
print_env_summary() {
    echo -e "${MAGENTA}======================================================="
    echo -e "ENVIRONMENT CONFIGURATION"
    echo -e "=======================================================${NC}"
    echo -e "${CYAN}Store Name:${NC} $STORE_NAME"
    echo -e "${CYAN}Store ID:${NC} $STORE_ID"
    echo -e "${CYAN}MQTT Broker:${NC} $MQTT_HOST:$MQTT_PORT"
    echo -e "${CYAN}VLM Model:${NC} $VLM_MODEL_NAME"
    echo -e "${CYAN}VLM Device:${NC} $VLM_DEVICE"
    echo -e "${CYAN}MinIO:${NC} localhost:$MINIO_API_PORT (console: $MINIO_CONSOLE_PORT)"
    echo -e "${CYAN}Agent Port:${NC} $LP_AGENT_PORT"
    echo -e "${MAGENTA}=======================================================${NC}"
    echo -e
}

# Exit after setting environment variables if --setenv is passed
if [ "$1" = "--setenv" ]; then
    print_env_summary
    echo -e "${GREEN}Environment variables set successfully${NC}"
    return 0
fi

# Build and run services based on the argument
case "$1" in
    "--setup")
        echo -e "${BLUE}Setting up Loss Prevention Agent...${NC}"
        print_env_summary

        # Copy SceneScape TLS cert if not already present
        if [ ! -f "${APP_DIR}/secrets/certs/scenescape-ca.pem" ]; then
            echo -e "${YELLOW}Copying SceneScape TLS certificate...${NC}"
            mkdir -p "${APP_DIR}/secrets/certs"
            if [ -f "${SCENESCAPE_CERTS}/scenescape-ca.pem" ]; then
                cp "${SCENESCAPE_CERTS}/scenescape-ca.pem" "${APP_DIR}/secrets/certs/"
                echo -e "${GREEN}TLS certificate copied.${NC}"
            else
                echo -e "${RED}WARNING: SceneScape CA cert not found at ${SCENESCAPE_CERTS}/scenescape-ca.pem${NC}"
                echo -e "${YELLOW}Copy it manually: mkdir -p ${APP_DIR}/secrets/certs && cp <path-to-ca.pem> ${APP_DIR}/secrets/certs/${NC}"
            fi
        fi

        docker compose -f "${APP_DIR}/docker/docker-compose.yaml" -p ${PROJECT_NAME} up --build -d
        ;;
    "--run")
        echo -e "${BLUE}Starting Loss Prevention Agent...${NC}"
        print_env_summary
        docker compose -f "${APP_DIR}/docker/docker-compose.yaml" -p ${PROJECT_NAME} up -d
        ;;
    "--restart")
        echo -e "${BLUE}Restarting Loss Prevention Agent...${NC}"
        docker compose -f "${APP_DIR}/docker/docker-compose.yaml" -p ${PROJECT_NAME} restart
        ;;
esac

# Check if the command was successful
if [ $? -ne 0 ]; then
    echo -e "${RED}Failed to execute docker compose command${NC}"
    return 1
fi

# Wait for services to be ready
echo -e "${YELLOW}Waiting for services to start...${NC}"
sleep 5

# Print service endpoints
print_service_endpoints

echo -e "${GREEN}Loss Prevention Agent is ready!${NC}"
echo -e "${CYAN}Connected to SceneScape MQTT broker at: $MQTT_HOST:$MQTT_PORT${NC}"
echo -e "${YELLOW}NOTE: Update zone_config.json with your SceneScape region UUIDs:${NC}"
echo -e "${YELLOW}  ${APP_DIR}/src/config/zone_config.json${NC}"