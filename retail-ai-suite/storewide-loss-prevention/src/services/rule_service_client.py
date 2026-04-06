# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Rule Service Client — calls external Rule Service via HTTP.

The Rule Service (separate container) handles advanced/configurable rule
evaluation that goes beyond the built-in detection rules in the LP service.

Called conditionally when specific patterns are detected.
"""

from typing import Any, Dict, List, Optional

import aiohttp
import structlog

from .config import ConfigService

logger = structlog.get_logger(__name__)


class RuleServiceClient:
    """
    HTTP client for the external Rule Service.

    Sends event context for advanced rule evaluation.
    Returns rule evaluation results and recommended actions.
    """

    def __init__(self, config: ConfigService) -> None:
        rs_cfg = config.get_rule_service_config()
        self.base_url = rs_cfg.get("base_url", "http://rule-service:8091")
        self.timeout = rs_cfg.get("timeout_seconds", 10)
        self.enabled = rs_cfg.get("enabled", True)

        logger.info(
            "RuleServiceClient initialized",
            base_url=self.base_url,
            enabled=self.enabled,
        )

    async def evaluate(
        self,
        event_type: str,
        object_id: str,
        region_id: str,
        zone_type: str,
        session_context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Send event context for rule evaluation.

        Returns:
            {
                "actions": [{"type": "ALERT"|"ESCALATE"|"LOG", ...}],
                "risk_score": float,
                "details": {...}
            }
            or None on failure / service unavailable.
        """
        if not self.enabled:
            return None

        payload = {
            "event_type": event_type,
            "object_id": object_id,
            "region_id": region_id,
            "zone_type": zone_type,
            "session": session_context,
        }

        return await self._post("/api/v1/rules/evaluate", payload)

    async def health_check(self) -> bool:
        """Check if the Rule Service is available."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/health",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    # ---- internal ------------------------------------------------------------
    async def _post(self, path: str, payload: dict) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        body = await resp.text()
                        logger.error(
                            "RuleService request failed",
                            path=path,
                            status=resp.status,
                            body=body[:200],
                        )
                        return None
        except aiohttp.ClientError as e:
            logger.error("RuleService connection error", path=path, error=str(e))
            return None
        except Exception:
            logger.exception("RuleService call error", path=path)
            return None
