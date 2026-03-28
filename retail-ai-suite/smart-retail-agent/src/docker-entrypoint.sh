#!/bin/bash
# Docker entrypoint script for Retail Store Agent API

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Starting Retail Store Agent${NC}"
echo "========================================"

# Set default environment variables if not provided
export LOG_LEVEL=${LOG_LEVEL:-INFO}
export VLM_MODEL=${VLM_MODEL:-Qwen/Qwen2.5-VL-3B-Instruct}
export STORE_NAME=${STORE_NAME:-retail_store_1}
export STORE_ID=${STORE_ID:-store_001}
export NUM_CAMERAS=${NUM_CAMERAS:-4}

echo "Configuration:"
echo "LOG_LEVEL: ${LOG_LEVEL}"
echo "STORE_NAME: ${STORE_NAME}"
echo "STORE_ID: ${STORE_ID}"
echo "NUM_CAMERAS: ${NUM_CAMERAS}"
echo "MQTT_HOST: ${MQTT_HOST}"
echo "MQTT_PORT: ${MQTT_PORT}"
echo "VLM_MODEL: ${VLM_MODEL}"
echo "========================================"


# Function to cleanup on exit
cleanup() {
    echo -e "${YELLOW}Shutting down services...${NC}"
    kill $BACKEND_PID 2>/dev/null || true
    exit 0
}

# Set up signal handling
trap cleanup SIGTERM SIGINT

# Start the backend API service
echo -e "${GREEN}Starting Backend API ...${NC}"
python run.py &
BACKEND_PID=$!

echo -e "${GREEN}Retail Store Agent API started successfully!${NC}"
echo -e "${GREEN}Access API documentation at: http://localhost:8081/docs${NC}"

# Wait for the process
wait $BACKEND_PID
