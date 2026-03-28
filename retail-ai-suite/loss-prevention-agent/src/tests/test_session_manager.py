# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Tests for SessionManager — region diff logic and event firing."""

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from models.events import EventType, ZoneType
from services.session_manager import SessionManager


class FakeConfig:
    """Minimal ConfigService stub for testing."""

    def get_rules_config(self):
        return {"session_timeout_seconds": 5}

    def get_zones(self):
        return {
            "region-electronics": {"name": "Electronics", "type": "HIGH_VALUE"},
            "region-checkout": {"name": "Checkout", "type": "CHECKOUT"},
            "region-exit": {"name": "Exit", "type": "EXIT"},
            "region-stockroom": {"name": "Stockroom", "type": "RESTRICTED"},
        }

    def get_zone_type(self, region_id):
        z = self.get_zones().get(region_id)
        return z["type"] if z else None

    def get_zone_name(self, region_id):
        z = self.get_zones().get(region_id)
        return z["name"] if z else None


@pytest.fixture
def config():
    return FakeConfig()


@pytest.fixture
def manager(config):
    return SessionManager(config)


@pytest.mark.asyncio
async def test_create_session_fires_entered(manager):
    """A new object_id appearing in a known region fires ENTERED."""
    events = []
    manager.register_event_handler(lambda e: events.append(e))

    data = [{"id": "42", "regions": [{"id": "region-electronics"}], "cameras": ["cam1"]}]
    await manager.on_scene_data("scene1", "persons", {"objects": data})

    assert len(events) == 1
    assert events[0].event_type == EventType.ENTERED
    assert events[0].zone_type == ZoneType.HIGH_VALUE
    assert events[0].object_id == "42"


@pytest.mark.asyncio
async def test_region_exit_fires_exited(manager):
    """Removing a region from the person's current set fires EXITED."""
    events = []
    manager.register_event_handler(lambda e: events.append(e))

    # Enter
    data = [{"id": "42", "regions": [{"id": "region-electronics"}], "cameras": ["cam1"]}]
    await manager.on_scene_data("scene1", "persons", {"objects": data})

    # Exit
    data = [{"id": "42", "regions": [], "cameras": ["cam1"]}]
    await manager.on_scene_data("scene1", "persons", {"objects": data})

    assert len(events) == 2
    assert events[1].event_type == EventType.EXITED
    assert events[1].dwell_seconds is not None


@pytest.mark.asyncio
async def test_unknown_region_ignored(manager):
    """Regions not in zone_config produce no events."""
    events = []
    manager.register_event_handler(lambda e: events.append(e))

    data = [{"id": "99", "regions": [{"id": "unknown-region"}], "cameras": ["cam1"]}]
    await manager.on_scene_data("scene1", "persons", {"objects": data})

    assert len(events) == 0
