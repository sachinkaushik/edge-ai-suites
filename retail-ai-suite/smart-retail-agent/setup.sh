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

# Project name based on store config
PROJECT_NAME="retail-store-agent"

# Setting command usage and invalid arguments handling before the actual setup starts
if [ "$#" -eq 0 ] || ([ "$#" -eq 1 ] && [ "$1" = "--help" ]); then
    # If no valid argument is passed, print usage information
    echo -e "-----------------------------------------------------------------"
    echo -e "${YELLOW}USAGE: ${GREEN}source setup.sh ${BLUE}[--setenv | --setup | --run | --restart | --stop | --clean | --help]"
    echo -e "${YELLOW}"
    echo -e "  --setenv:                 Set environment variables without building image or starting any containers"
    echo -e "  --setup:                  Build and run the retail store agent"
    echo -e "  --run:                    Start the agent without building image (if already built)"
    echo -e "  --restart:                Restart the retail store agent"
    echo -e "  --stop:                   Stop the agent"
    echo -e "  --clean:                  Clean up containers, volumes, and logs"
    echo -e "                              • --keep-models - Remove all application volume data except VLM models"
    echo -e "  --help:                   Show this help message${NC}"
    echo -e "-----------------------------------------------------------------"
    echo -e "${CYAN}NOTE: This assumes Scenescape is already running in the same network.${NC}"
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
    echo -e "${YELLOW}Stopping Retail Store Agent ${RED}${PROJECT_NAME} ${YELLOW}... ${NC}"
    
    docker compose -f "${APP_DIR}/docker/agent-compose.yaml" -p ${PROJECT_NAME} down 2> /dev/null

    if [ $? -ne 0 ]; then
        echo -e "${RED}Failed to stop Retail Store Agent services. ${NC}"
        return 1
    fi
    echo -e "${GREEN}All containers for Retail Store Agent stopped and removed! ${NC}"

    if [ "$1" = "--clean" ]; then
        echo -e "${YELLOW}Removing volumes for Retail Store Agent ... ${NC}"
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
export NUM_CAMERAS=${NUM_CAMERAS:-4}

# MQTT Configuration
export MQTT_HOST=${MQTT_HOST:-broker.scenescape.intel.com}
export MQTT_PORT=${MQTT_PORT:-1883}

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
export AGENT_BACKEND_PORT=${AGENT_BACKEND_PORT:-8081}

# Get and print the ports of all running services
print_service_endpoints() {
    echo -e
    echo -e "${MAGENTA}======================================================="
    echo -e "SERVICE ENDPOINTS"
    echo -e "=======================================================${NC}"
    
    for CONTAINER_NAME in $(docker ps --format '{{.Names}}' | grep $PROJECT_NAME);
    do
        case "$CONTAINER_NAME" in
            *retail-agent*)
                BACKEND_SERVICE_NAME="Retail Store Agent API Docs"
                PORT=$(docker port "$CONTAINER_NAME" 8081 | cut -d: -f2)
                echo -e "${CYAN}$BACKEND_SERVICE_NAME -> http://$HOST_IP:$PORT/docs${NC}"
                ;;
            *vlm*)
                SERVICE_NAME="VLM Service"
                PORT=$(docker port "$CONTAINER_NAME" 8000 | cut -d: -f2)
                echo -e "${BLUE}$SERVICE_NAME -> http://$HOST_IP:$PORT${NC}"
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
    echo -e "${CYAN}Number of Cameras:${NC} $NUM_CAMERAS"
    echo -e "${CYAN}MQTT Broker:${NC} $MQTT_HOST:$MQTT_PORT"
    echo -e "${CYAN}VLM Model:${NC} $VLM_MODEL_NAME"
    echo -e "${CYAN}VLM Device:${NC} $VLM_DEVICE"
    echo -e "${CYAN}Backend Port:${NC} $AGENT_BACKEND_PORT"
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
        echo -e "${BLUE}Building and starting Retail Store Agent...${NC}"
        print_env_summary
        docker compose -f "${APP_DIR}/docker/agent-compose.yaml" -p ${PROJECT_NAME} up --build -d
        ;;
    "--run")
        echo -e "${BLUE}Starting Retail Store Agent...${NC}"
        print_env_summary
        docker compose -f "${APP_DIR}/docker/agent-compose.yaml" -p ${PROJECT_NAME} up -d
        ;;
    "--restart")
        echo -e "${BLUE}Restarting Retail Store Agent...${NC}"
        docker compose -f "${APP_DIR}/docker/agent-compose.yaml" -p ${PROJECT_NAME} restart
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

echo -e "${GREEN}Retail Store Agent is ready!${NC}"
echo -e "${CYAN}Connect to Scenescape MQTT broker at: $MQTT_HOST:$MQTT_PORT${NC}"
