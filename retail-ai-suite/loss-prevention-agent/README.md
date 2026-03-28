# Loss Prevention Agent

Store-wide suspicious activity detection for Intel SceneScape retail deployments. The system monitors person behavior across store zones using real-time tracking from SceneScape, lightweight pose detection, and VLM-based confirmation — then fires alerts when suspicious patterns are detected.

## Prerequisites

- **Intel SceneScape** running with cameras configured and regions/zones defined
- **Docker** and **Docker Compose** installed
- SceneScape Docker network (`scenescape_scenescape`) available

## Suspicious Activities Detected

| # | Activity | Trigger | Alert Level |
|---|----------|---------|-------------|
| 1 | Merchandise Concealment | Pose (shelf-to-waist) + VLM confirmation (confidence ≥ 0.80) | WARNING |
| 2 | Checkout Bypass | Visited HIGH_VALUE zone, exited without passing CHECKOUT | WARNING / CRITICAL* |
| 3 | Loitering | > 120 s in a HIGH_VALUE zone | WARNING |
| 4 | Repeated Visits | > 3 visits to same HIGH_VALUE zone | WARNING |
| 5 | Restricted Zone Violation | Entered RESTRICTED zone (immediate) | CRITICAL |

\* Escalates to CRITICAL if concealment was also suspected for that person.

## Architecture

```
Cameras → SceneScape (DLStreamer + Controller) → MQTT Bus
    ↓                                               ↓
    ↓  scenescape/data/scene/+/+                     ↓  scenescape/event/region/+/+/+
    ↓  (position, cameras, bbox)                     ↓  (enter/exit with dwell time)
    ↓                                               ↓
Loss Prevention Agent (Behavioral Analysis Service)
  ├── MQTT Service          — subscribes to scene-data, region-event, and image topics
  ├── Session Manager       — PersonSession lifecycle, consumes SceneScape region events
  ├── Rule Engine           — 5 suspicious activity handlers → alerts
  ├── Pose Analyzer         — 10-frame sliding window, shelf-to-waist detection
  ├── VLM Client            — 20-frame assembly (T-5s to T+5s), concealment confirmation
  ├── Frame Store           — MinIO read/write (frames, crops, evidence)
  └── Alert Publisher       — MQTT + REST API + structured log
```

### Data Flow

The agent subscribes to two SceneScape MQTT topic families:

1. **Scene data** (`scenescape/data/scene/{scene_id}/person`) — continuous position snapshots used to keep sessions alive (last_seen, cameras, bbox). Only persons visible on configured cameras are tracked.
2. **Region events** (`scenescape/event/region/{scene_id}/{region_id}/count`) — native enter/exit events from SceneScape with pre-computed dwell time. These drive ENTERED/EXITED events to the Rule Engine — no local region diffing needed.

### Scene Resolution

The config uses a human-readable `scene_name` (e.g., `"Retail"`) instead of a hardcoded UUID. At startup, the agent resolves it to a scene UUID via the SceneScape REST API (`GET /api/v1/scenes`). Only data from the matching scene is processed.

### Camera Filtering

The agent only tracks persons visible on cameras listed in `app_config.json`. Persons from other scenes or cameras (e.g., a "Queuing" scene with `atag-qcam1`) are automatically filtered out.

## Quick Start

### Using setup.sh (recommended)

```bash
cd /path/to/loss-prevention-agent

# First time — builds images, copies TLS cert, starts containers
source setup.sh --setup

# Subsequent runs — start without rebuilding
source setup.sh --run

# Restart
source setup.sh --restart

# Stop
source setup.sh --stop

# Clean up (remove containers + volumes)
source setup.sh --clean

# Clean but keep VLM model cache
source setup.sh --clean --keep-models
```

### Manual setup

```bash
cd /path/to/loss-prevention-agent

# 1. Copy SceneScape TLS certificate
mkdir -p secrets/certs
cp ../../scenescape/secrets/certs/scenescape-ca.pem secrets/certs/

# 2. (Optional) Set SceneScape API credentials for zone auto-discovery
export SCENESCAPE_API_USER=scenectrl
export SCENESCAPE_API_PASSWORD=<password>

# 3. Start services
cd docker
docker compose up -d --build
```

### Environment Variables

Override defaults by exporting before running `setup.sh`:

| Variable | Default | Description |
|----------|---------|-------------|
| `VLM_DEVICE` | `CPU` | `CPU` or `GPU` for VLM inference |
| `VLM_MODEL_NAME` | `Qwen/Qwen2.5-VL-3B-Instruct` | VLM model |
| `MQTT_HOST` | `broker.scenescape.intel.com` | SceneScape MQTT broker |
| `LP_AGENT_PORT` | `8082` | Agent REST API port |
| `MINIO_API_PORT` | `9000` | MinIO API port |
| `MINIO_CONSOLE_PORT` | `9001` | MinIO web console port |
| `SCENESCAPE_API_USER` | *(empty)* | SceneScape API username (enables zone auto-discovery) |
| `SCENESCAPE_API_PASSWORD` | *(empty)* | SceneScape API password |

## Services Started

| Container | Port | Description |
|-----------|------|-------------|
| loss-prevention-agent | 8082 | FastAPI agent (alerts, sessions, health) |
| minio | 9000 / 9001 | Object storage (frames, evidence) |
| vlm-openvino-serving | 8000 | VLM inference (concealment confirmation) |

All containers join the `scenescape_scenescape` Docker network.

## API Endpoints

### Core

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/api/v1/lp/alerts` | Recent alerts (query: `?alert_type=CONCEALMENT`, `?object_id=42`) |
| GET | `/api/v1/lp/alerts/count` | Total alert count |
| GET | `/api/v1/lp/sessions` | Active person sessions |
| GET | `/api/v1/lp/sessions/count` | Active session count |
| GET | `/api/v1/lp/status` | Service status + statistics (includes zone counts) |

### Zone Management (runtime, no restart needed)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/lp/zones` | List all configured zones |
| PUT | `/api/v1/lp/zones/{region_id}` | Add or update a zone mapping |
| DELETE | `/api/v1/lp/zones/{region_id}` | Remove a zone mapping |
| POST | `/api/v1/lp/zones/discover` | Re-scan SceneScape for new regions |
| GET | `/api/v1/lp/zones/rules` | View name-pattern matching rules |

## Zone Configuration

Zones map SceneScape regions to LP zone types (`HIGH_VALUE`, `CHECKOUT`, `EXIT`, `RESTRICTED`). There are three ways to configure them — **no manual UUID copying needed**.

### Option A: Auto-discovery at startup (recommended)

Set SceneScape API credentials and the agent auto-fetches all regions and maps them by name:

```bash
export SCENESCAPE_API_USER=scenectrl
export SCENESCAPE_API_PASSWORD=<password>
source setup.sh --run
```

Name-pattern rules in `zone_config.json` control the mapping:
```json
{
  "zone_rules": [
    {"name_pattern": "checkout|register|cashier|till", "type": "CHECKOUT"},
    {"name_pattern": "exit|door|entrance|gate", "type": "EXIT"},
    {"name_pattern": "electronics|jewelry|cosmetics|high.?value", "type": "HIGH_VALUE"},
    {"name_pattern": "stock.?room|staff|restricted|office", "type": "RESTRICTED"}
  ]
}
```

Any SceneScape region whose name matches a pattern is automatically assigned that zone type.

### Option B: Runtime API (no restart)

Add, update, or remove zones at any time via the REST API:

```bash
# Add a zone
curl -X PUT http://localhost:8082/api/v1/lp/zones/<region-uuid> \
  -H "Content-Type: application/json" \
  -d '{"name": "Electronics Aisle", "type": "HIGH_VALUE"}'

# List all zones
curl http://localhost:8082/api/v1/lp/zones

# Trigger re-discovery from SceneScape
curl -X POST http://localhost:8082/api/v1/lp/zones/discover

# Remove a zone
curl -X DELETE http://localhost:8082/api/v1/lp/zones/<region-uuid>
```

### Option C: Manual config file

Directly add UUID mappings in `zone_config.json` under the `zones` key:
```json
{
  "zones": {
    "302cf49a-97ec-402d-a324-c5077b280b7b": {
      "name": "Electronics",
      "type": "HIGH_VALUE"
    }
  }
}
```
Requires a container rebuild (`docker compose up -d --build loss-prevention-agent`).

> **Note:** Runtime API changes (Option B) are in-memory and reset on container restart. Auto-discovery (Option A) re-runs on every startup.

## Project Structure

```
loss-prevention-agent/
├── setup.sh                    # Setup, run, stop, clean commands
├── README.md
├── docker/
│   ├── docker-compose.yaml     # 3 services: agent + VLM + MinIO
│   └── .env.example
├── secrets/
│   └── certs/
│       └── scenescape-ca.pem   # Copied from SceneScape (auto by setup.sh)
└── src/
    ├── main.py                 # FastAPI app, service wiring
    ├── Dockerfile
    ├── docker-entrypoint.sh
    ├── pyproject.toml
    ├── api/
    │   ├── __init__.py
    │   └── routes.py           # REST endpoints
    ├── config/
    │   ├── app_config.json     # MQTT, MinIO, VLM, pose, rules thresholds
    │   └── zone_config.json    # Region UUID → zone type mapping
    ├── models/
    │   ├── __init__.py
    │   ├── session.py          # PersonSession, RegionVisit
    │   ├── events.py           # RegionEvent, EventType, ZoneType
    │   └── alerts.py           # Alert, AlertType, AlertLevel
    ├── services/
    │   ├── __init__.py
    │   ├── config.py            # ConfigService — dynamic zone map + JSON configs
    │   ├── scenescape_client.py # SceneScape REST API — auth, fetch regions, auto-map
    │   ├── mqtt_service.py      # MQTT subscribe + publish (scene-data, region-events, images)
    │   ├── session_manager.py   # Consumes SceneScape region events → ENTER/EXIT/PERSON_LOST
    │   ├── rule_engine.py       # 5 suspicious activity handlers
    │   ├── pose_analyzer.py     # 10-frame sliding window, shelf-to-waist
    │   ├── vlm_client.py        # 20-frame assembly + VLM call
    │   ├── frame_store.py       # MinIO CRUD (frames/thumbnails/evidence)
    │   └── alert_publisher.py   # MQTT + in-memory ring buffer
    └── tests/
        ├── __init__.py
        ├── test_session_manager.py
        ├── test_rule_engine.py
        └── test_pose_analyzer.py
```
