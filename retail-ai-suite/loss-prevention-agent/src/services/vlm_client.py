# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
VLM Client — assembles a 20-frame window and calls the VLM for
concealment confirmation after a SHELF_TO_WAIST_SEQUENCE flag.
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
import structlog

from models.alerts import Alert, AlertLevel, AlertType
from .config import ConfigService
from .frame_store import FrameStore
from .session_manager import SessionManager

logger = structlog.get_logger(__name__)

CONCEALMENT_PROMPT = (
    "You are a loss-prevention analyst reviewing store camera footage.\n"
    "Pose detection flagged that this person's {wrist} hand moved from shelf "
    "level down to their waist area — a motion consistent with merchandise "
    "concealment.\n\n"
    "The following {num_frames} frames are in chronological order, covering "
    "approximately 10 seconds around the moment of the pose flag.\n\n"
    "Evaluate whether the person appears to be concealing merchandise.\n"
    "Respond ONLY with valid JSON:\n"
    '{{"concealment": true/false, "confidence": 0.0-1.0, '
    '"frame_idx": <most telling frame index>, '
    '"observation": "<one-sentence description>"}}'
)


class VLMClient:
    """
    Coordinates frame assembly from MinIO and sends multi-image
    prompts to the VLM service for concealment verification.
    """

    def __init__(
        self,
        config: ConfigService,
        frame_store: FrameStore,
        session_manager: SessionManager,
        alert_callback=None,
    ) -> None:
        vlm_cfg = config.get_vlm_config()
        rules_cfg = config.get_rules_config()

        self.base_url = vlm_cfg.get("base_url", "http://vlm-openvino-serving:8000")
        self.model = vlm_cfg.get("model", "Qwen/Qwen2.5-VL-3B-Instruct")
        self.timeout = vlm_cfg.get("timeout_seconds", 300)
        self.max_tokens = vlm_cfg.get("max_completion_tokens", 500)
        self.temperature = vlm_cfg.get("temperature", 0.1)
        self.top_p = vlm_cfg.get("top_p", 0.1)

        self.confidence_threshold = rules_cfg.get("vlm_confidence_threshold", 0.80)
        self.log_threshold = rules_cfg.get("vlm_log_threshold", 0.50)
        self.analysis_fps = rules_cfg.get("analysis_fps", 2)

        self.frame_store = frame_store
        self.session_mgr = session_manager
        self._alert_callback = alert_callback

        # Semaphore — only one VLM call at a time
        self._semaphore = asyncio.Semaphore(1)
        # Pending future-frame collectors: object_id → list[str]
        self._pending_futures: Dict[str, List[str]] = {}

        logger.info(
            "VLMClient initialized",
            base_url=self.base_url,
            model=self.model,
            confidence_threshold=self.confidence_threshold,
        )

    def set_alert_callback(self, callback) -> None:
        self._alert_callback = callback

    # ---- trigger (called by PoseAnalyzer flag handler) -----------------------
    async def on_shelf_to_waist(
        self, object_id: str, wrist_side: str, pose_frames: list
    ) -> None:
        """
        Assemble 20 frames (10 past + 10 future) and call VLM.
        Past frames come from MinIO; future frames are collected asynchronously.
        """
        session = self.session_mgr.get_session(object_id)
        if not session:
            return

        # --- past 10 frames from rolling buffer ---
        past_keys = session.frame_buffer[-10:] if len(session.frame_buffer) >= 10 else list(session.frame_buffer)

        # --- future 10 frames: register collector ---
        self._pending_futures[object_id] = []
        future_target = 10
        # Wait up to 6 seconds for future frames to arrive
        for _ in range(60):  # 60 × 100ms = 6s
            await asyncio.sleep(0.1)
            if len(self._pending_futures.get(object_id, [])) >= future_target:
                break

        future_keys = self._pending_futures.pop(object_id, [])[:future_target]

        all_keys = past_keys + future_keys
        if len(all_keys) < 10:
            logger.warning("Not enough frames for VLM", object_id=object_id, count=len(all_keys))
            return

        # --- fetch frame bytes from MinIO ---
        frames_b64 = await self.frame_store.get_frames_base64(all_keys)

        # --- call VLM ---
        result = await self._call_vlm(wrist_side, frames_b64)
        if result is None:
            return

        await self._process_result(object_id, result, all_keys)

    def collect_future_frame(self, object_id: str, minio_key: str) -> None:
        """Called by the frame-write path to feed future frames."""
        if object_id in self._pending_futures:
            self._pending_futures[object_id].append(minio_key)

    # ---- VLM call ------------------------------------------------------------
    async def _call_vlm(
        self, wrist_side: str, frames_b64: List[str]
    ) -> Optional[dict]:
        prompt = CONCEALMENT_PROMPT.format(
            wrist=wrist_side, num_frames=len(frames_b64)
        )

        # Build multi-image content
        content = [{"type": "text", "text": prompt}]
        for img in frames_b64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img}"},
                }
            )

        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }

        async with self._semaphore:
            try:
                async with aiohttp.ClientSession() as http:
                    url = f"{self.base_url}/v1/chat/completions"
                    async with http.post(
                        url,
                        json=body,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        if resp.status != 200:
                            logger.error("VLM request failed", status=resp.status)
                            return None
                        data = await resp.json()
                        text = data["choices"][0]["message"]["content"]
                        return self._parse_json_response(text)
            except Exception:
                logger.exception("VLM call error")
                return None

    # ---- result handling -----------------------------------------------------
    async def _process_result(
        self, object_id: str, result: dict, evidence_keys: List[str]
    ) -> None:
        confidence = result.get("confidence", 0.0)
        concealment = result.get("concealment", False)
        observation = result.get("observation", "")

        logger.info(
            "VLM result",
            object_id=object_id,
            concealment=concealment,
            confidence=confidence,
            observation=observation,
        )

        if confidence >= self.confidence_threshold and concealment:
            session = self.session_mgr.get_session(object_id)
            if session:
                session.concealment_suspected = True

            alert = Alert(
                alert_type=AlertType.CONCEALMENT,
                alert_level=AlertLevel.WARNING,
                object_id=object_id,
                timestamp=datetime.now(timezone.utc),
                details={
                    "confidence": confidence,
                    "observation": observation,
                    "frame_idx": result.get("frame_idx"),
                },
                evidence_keys=evidence_keys,
            )
            if self._alert_callback:
                await self._alert_callback(alert)

        elif confidence >= self.log_threshold:
            logger.info(
                "VLM borderline — logging only",
                object_id=object_id,
                confidence=confidence,
            )

    # ---- helpers -------------------------------------------------------------
    @staticmethod
    def _parse_json_response(text: str) -> Optional[dict]:
        import json

        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error("VLM response not valid JSON", text=text[:200])
            return None
