# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Rule Engine — evaluates ENTERED / EXITED / PERSON_LOST events and fires alerts.

Built-in detection rules (part of LP service business logic):
  1. Restricted Zone Violation  (on ENTERED RESTRICTED)
  2. Repeated Visits            (on ENTERED HIGH_VALUE, count > threshold)
  3. Checkout state tracking    (on ENTERED CHECKOUT)
  4. Checkout Bypass            (on ENTERED EXIT without CHECKOUT)
  5. Loitering                  (on EXITED HIGH_VALUE, dwell > threshold)
  6. PERSON_LOST cleanup        (check open high-value visits for loitering)

Conditionally calls external BehavioralAnalysis Service and Rule Service
when specific patterns are detected (e.g., person in HIGH_VALUE zone).
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
        behavioral_analysis_client=None,
        rule_service_client=None,
        frame_manager=None,
    ) -> None:
        self.config = config
        self.session_mgr = session_manager
        self._alert_callback = alert_callback
        self._ba_client = behavioral_analysis_client
        self._rule_svc_client = rule_service_client
        self._frame_mgr = frame_manager

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

        # Conditionally call external Rule Service
        await self._call_rule_service(event, session)

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
        """Track monitoring state, check repeated visits, trigger behavioral analysis."""
        session.visited_high_value = True
        visit_count = session.zone_visit_counts.get(event.region_id, 0)

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

        # Trigger pose analysis via external BehavioralAnalysis Service
        await self._trigger_behavioral_analysis(event.object_id, event.region_id)

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
        """Fire loitering alert if dwell time exceeds threshold (once per zone)."""
        session = self.session_mgr.get_session(event.object_id)

        # Check if loiter alert already triggered for this zone
        if session and session.loiter_alerted.get(event.region_id):
            logger.debug(
                "Loiter alert already fired for zone",
                object_id=event.object_id,
                region_id=event.region_id,
            )
            return

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

            # Mark loiter alert as triggered for this zone
            if session:
                session.loiter_alerted[event.region_id] = True

    # ---- PERSON_LOST handler -------------------------------------------------
    async def _on_person_lost(self, event: RegionEvent) -> None:
        """Check open high-value visits for loitering on session expiry."""
        session = self.session_mgr.get_session(event.object_id)
        if not session:
            return
        logger.info("Person lost event processed", object_id=event.object_id)

    # ---- External service calls (conditional) --------------------------------
    async def _trigger_behavioral_analysis(
        self, object_id: str, region_id: str
    ) -> None:
        """
        Trigger pose analysis via external BehavioralAnalysis Service
        for persons in HIGH_VALUE zones.
        """
        if not self._ba_client or not self._frame_mgr:
            return

        session = self.session_mgr.get_session(object_id)
        if not session:
            return

        frame_keys = self._frame_mgr.get_person_frame_keys(object_id)
        if not frame_keys:
            logger.debug("No frames available for behavioral analysis", object_id=object_id)
            return

        frames_b64 = await self._frame_mgr.get_frames_base64(frame_keys)
        if not frames_b64:
            return

        zone_info = {
            "region_id": region_id,
            "zone_type": "HIGH_VALUE",
            "zone_name": self.config.get_zone_name(region_id),
        }

        # Call pose analysis
        pose_result = await self._ba_client.analyze_pose(
            object_id, frame_keys, frames_b64, zone_info
        )

        if pose_result and pose_result.get("detected"):
            logger.warning(
                "Pose analysis flagged suspicious motion",
                object_id=object_id,
                wrist=pose_result.get("wrist_side"),
            )

            # Escalate to VLM concealment check
            vlm_result = await self._ba_client.analyze_concealment(
                object_id, frame_keys, frames_b64, pose_result
            )

            if vlm_result and vlm_result.get("concealment") and \
               vlm_result.get("confidence", 0.0) >= 0.80:
                session.concealment_suspected = True
                alert = Alert(
                    alert_type=AlertType.CONCEALMENT,
                    alert_level=AlertLevel.WARNING,
                    object_id=object_id,
                    timestamp=session.last_seen,
                    details={
                        "confidence": vlm_result.get("confidence"),
                        "observation": vlm_result.get("observation", ""),
                        "frame_idx": vlm_result.get("frame_idx"),
                    },
                    evidence_keys=frame_keys,
                )
                await self._fire_alert(alert)

    async def _call_rule_service(self, event: RegionEvent, session) -> None:
        """Conditionally call external Rule Service for advanced evaluation."""
        if not self._rule_svc_client:
            return

        session_context = {
            "object_id": session.object_id,
            "visited_checkout": session.visited_checkout,
            "visited_high_value": session.visited_high_value,
            "visited_exit": session.visited_exit,
            "concealment_suspected": session.concealment_suspected,
            "zone_visit_counts": session.zone_visit_counts,
            "current_zones": session.current_zones,
        }

        result = await self._rule_svc_client.evaluate(
            event_type=event.event_type.value,
            object_id=event.object_id,
            region_id=event.region_id,
            zone_type=event.zone_type.value,
            session_context=session_context,
        )

        if result and result.get("actions"):
            for action in result["actions"]:
                if action.get("type") == "ALERT":
                    logger.info(
                        "External rule service triggered alert",
                        object_id=event.object_id,
                        action=action,
                    )

    # ---- alert dispatch ------------------------------------------------------
    async def _fire_alert(self, alert: Alert) -> None:
        if self._alert_callback:
            try:
                await self._alert_callback(alert)
            except Exception:
                logger.exception("Alert callback error", alert_id=alert.alert_id)
