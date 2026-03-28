# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Rule Engine — evaluates ENTERED / EXITED / PERSON_LOST events and fires alerts.

Implements the five suspicious-activity scenarios defined in requirements:
  1. Restricted Zone Violation  (on ENTERED RESTRICTED)
  2. Repeated Visits            (on ENTERED HIGH_VALUE, count > threshold)
  3. Checkout state tracking    (on ENTERED CHECKOUT)
  4. Checkout Bypass            (on ENTERED EXIT without CHECKOUT)
  5. Loitering                  (on EXITED HIGH_VALUE, dwell > threshold)
  6. PERSON_LOST cleanup        (check open high-value visits for loitering)
"""

from datetime import datetime
from typing import Optional

import structlog

from models.events import EventType, RegionEvent, ZoneType
from models.alerts import Alert, AlertType, AlertLevel
from .config import ConfigService
from .session_manager import SessionManager

logger = structlog.get_logger(__name__)


class RuleEngine:
    """Stateless rule evaluator — receives events, produces alerts."""

    def __init__(
        self,
        config: ConfigService,
        session_manager: SessionManager,
        alert_callback=None,
    ) -> None:
        self.config = config
        self.session_mgr = session_manager
        self._alert_callback = alert_callback

        rules = config.get_rules_config()
        self.loiter_threshold = rules.get("loiter_threshold_seconds", 120)
        self.repeat_threshold = rules.get("repeat_visit_threshold", 3)

        logger.info(
            "RuleEngine initialized",
            loiter_threshold=self.loiter_threshold,
            repeat_threshold=self.repeat_threshold,
        )

    def set_alert_callback(self, callback) -> None:
        self._alert_callback = callback

    # ---- main dispatcher -----------------------------------------------------
    async def on_event(self, event: RegionEvent) -> None:
        """Route an event to the appropriate handler based on type + zone."""
        if event.event_type == EventType.ENTERED:
            await self._on_entered(event)
        elif event.event_type == EventType.EXITED:
            await self._on_exited(event)
        elif event.event_type == EventType.PERSON_LOST:
            await self._on_person_lost(event)

    # ---- ENTERED handlers ----------------------------------------------------
    async def _on_entered(self, event: RegionEvent) -> None:
        session = self.session_mgr.get_session(event.object_id)
        if not session:
            return

        if event.zone_type == ZoneType.RESTRICTED:
            await self._handle_restricted_entry(event)

        elif event.zone_type == ZoneType.HIGH_VALUE:
            await self._handle_high_value_entry(event, session)

        elif event.zone_type == ZoneType.CHECKOUT:
            session.visited_checkout = True
            logger.info("Checkout visited", object_id=event.object_id)

        elif event.zone_type == ZoneType.EXIT:
            session.visited_exit = True
            await self._handle_exit_entry(event, session)

    async def _handle_restricted_entry(self, event: RegionEvent) -> None:
        """Immediate zone violation alert."""
        alert = Alert(
            alert_type=AlertType.ZONE_VIOLATION,
            alert_level=AlertLevel.CRITICAL,
            object_id=event.object_id,
            timestamp=event.timestamp,
            region_id=event.region_id,
            region_name=event.region_name,
            details={"zone_type": "RESTRICTED"},
        )
        logger.warning("RESTRICTED zone violation", object_id=event.object_id,
                       region=event.region_name)
        await self._fire_alert(alert)

    async def _handle_high_value_entry(self, event: RegionEvent, session) -> None:
        """Track monitoring state and check repeated visits."""
        session.visited_high_value = True
        visit_count = session.visit_count_per_region.get(event.region_id, 0)

        logger.info(
            "HIGH_VALUE zone entered",
            object_id=event.object_id,
            region=event.region_name,
            visit_count=visit_count,
        )

        if visit_count > self.repeat_threshold:
            alert = Alert(
                alert_type=AlertType.UNUSUAL_PATH,
                alert_level=AlertLevel.WARNING,
                object_id=event.object_id,
                timestamp=event.timestamp,
                region_id=event.region_id,
                region_name=event.region_name,
                details={
                    "visit_count": visit_count,
                    "threshold": self.repeat_threshold,
                },
            )
            logger.warning(
                "Repeated high-value visits",
                object_id=event.object_id,
                count=visit_count,
            )
            await self._fire_alert(alert)

    async def _handle_exit_entry(self, event: RegionEvent, session) -> None:
        """Evaluate checkout bypass when the person reaches an exit."""
        if session.visited_high_value and not session.visited_checkout:
            level = (
                AlertLevel.CRITICAL
                if session.concealment_suspected
                else AlertLevel.WARNING
            )
            alert = Alert(
                alert_type=AlertType.CHECKOUT_BYPASS,
                alert_level=level,
                object_id=event.object_id,
                timestamp=event.timestamp,
                region_id=event.region_id,
                region_name=event.region_name,
                details={
                    "visited_high_value": True,
                    "visited_checkout": False,
                    "concealment_suspected": session.concealment_suspected,
                },
            )
            logger.warning(
                "Checkout bypass detected",
                object_id=event.object_id,
                level=level.value,
            )
            await self._fire_alert(alert)

    # ---- EXITED handlers -----------------------------------------------------
    async def _on_exited(self, event: RegionEvent) -> None:
        if event.zone_type == ZoneType.HIGH_VALUE:
            await self._check_loitering(event)

    async def _check_loitering(self, event: RegionEvent) -> None:
        """Fire loitering alert if dwell time exceeds threshold."""
        if event.dwell_seconds and event.dwell_seconds > self.loiter_threshold:
            alert = Alert(
                alert_type=AlertType.LOITERING,
                alert_level=AlertLevel.WARNING,
                object_id=event.object_id,
                timestamp=event.timestamp,
                region_id=event.region_id,
                region_name=event.region_name,
                details={
                    "dwell_seconds": round(event.dwell_seconds, 1),
                    "threshold": self.loiter_threshold,
                },
            )
            logger.warning(
                "Loitering detected",
                object_id=event.object_id,
                dwell=event.dwell_seconds,
            )
            await self._fire_alert(alert)

    # ---- PERSON_LOST handler -------------------------------------------------
    async def _on_person_lost(self, event: RegionEvent) -> None:
        """Check open high-value visits for loitering on session expiry."""
        session = self.session_mgr.get_session(event.object_id)
        if not session:
            return
        # open visits already closed by SessionManager; loitering checked via
        # the EXITED events fired during expiry.
        logger.info("Person lost event processed", object_id=event.object_id)

    # ---- alert dispatch ------------------------------------------------------
    async def _fire_alert(self, alert: Alert) -> None:
        if self._alert_callback:
            try:
                await self._alert_callback(alert)
            except Exception:
                logger.exception("Alert callback error", alert_id=alert.alert_id)
