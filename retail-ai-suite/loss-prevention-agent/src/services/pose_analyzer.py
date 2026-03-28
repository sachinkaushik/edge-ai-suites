# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Pose Analyzer — sliding-window shelf-to-waist detection.

Maintains a per-person window of the last 10 pose frames (~5 s @ 2 fps).
Evaluates the two-step pattern described in the requirements:
  Step 1 (frames 1-5): hand raised to shelf level
  Step 2 (frames 6-10): hand arrives at waist
If both steps are confirmed for the same wrist → SHELF_TO_WAIST_SEQUENCE.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import structlog

from .config import ConfigService

logger = structlog.get_logger(__name__)


@dataclass
class PoseFrame:
    """Keypoints extracted from a single frame."""
    timestamp: float
    left_wrist: Optional[Tuple[float, float]] = None   # (x, y) normalised
    right_wrist: Optional[Tuple[float, float]] = None
    left_hip: Optional[Tuple[float, float]] = None
    right_hip: Optional[Tuple[float, float]] = None
    left_wrist_conf: float = 0.0
    right_wrist_conf: float = 0.0
    left_hip_conf: float = 0.0
    right_hip_conf: float = 0.0
    minio_key: Optional[str] = None


@dataclass
class PoseWindow:
    """Per-person sliding window of pose frames."""
    frames: List[PoseFrame] = field(default_factory=list)
    flagged: bool = False  # True once escalated to VLM


class PoseAnalyzer:
    """
    Receives pose keypoints for persons in HIGH_VALUE zones and
    evaluates the shelf-to-waist concealment gesture.
    """

    def __init__(self, config: ConfigService) -> None:
        pose_cfg = config.get_pose_config()
        self.window_size: int = pose_cfg.get("window_size", 10)
        self.conf_threshold: float = pose_cfg.get("confidence_threshold", 0.5)
        self.waist_prox: float = pose_cfg.get("waist_proximity_threshold", 0.10)

        self._windows: Dict[str, PoseWindow] = defaultdict(PoseWindow)
        self._on_flag: Optional[Callable] = None

        logger.info(
            "PoseAnalyzer initialized",
            window_size=self.window_size,
            conf_threshold=self.conf_threshold,
            waist_proximity=self.waist_prox,
        )

    def register_flag_handler(self, handler: Callable) -> None:
        """Register async callback for SHELF_TO_WAIST_SEQUENCE flag."""
        self._on_flag = handler

    # ---- public entry point --------------------------------------------------
    async def on_pose_update(self, object_id: str, pose_frame: PoseFrame) -> None:
        """
        Append a new pose frame and evaluate the sliding window.
        Only called for persons currently in a HIGH_VALUE zone.
        """
        window = self._windows[object_id]
        if window.flagged:
            return  # already escalated, waiting for VLM

        window.frames.append(pose_frame)
        if len(window.frames) > self.window_size:
            window.frames.pop(0)

        if len(window.frames) < self.window_size:
            return  # not enough frames yet

        flagged_wrist = self._evaluate(window.frames)
        if flagged_wrist:
            window.flagged = True
            logger.warning(
                "SHELF_TO_WAIST_SEQUENCE detected",
                object_id=object_id,
                wrist=flagged_wrist,
            )
            if self._on_flag:
                await self._on_flag(object_id, flagged_wrist, window.frames)

    def reset(self, object_id: str) -> None:
        """Reset the window after VLM evaluation completes."""
        if object_id in self._windows:
            self._windows[object_id] = PoseWindow()

    def remove(self, object_id: str) -> None:
        """Remove tracking when a person leaves or session expires."""
        self._windows.pop(object_id, None)

    # ---- evaluation logic ----------------------------------------------------
    def _evaluate(self, frames: List[PoseFrame]) -> Optional[str]:
        """
        Return 'left' or 'right' if the shelf-to-waist pattern is found,
        otherwise None.
        """
        half = self.window_size // 2
        first_half = frames[:half]
        second_half = frames[half:]

        for wrist_side in ("left", "right"):
            if self._check_shelf(first_half, wrist_side) and self._check_waist(
                second_half, wrist_side
            ):
                return wrist_side
        return None

    def _check_shelf(self, frames: List[PoseFrame], side: str) -> bool:
        """Step 1: at least 2 consecutive frames with wrist above hip midpoint."""
        consecutive = 0
        for f in frames:
            wrist, conf = self._get_wrist(f, side)
            hip_mid = self._hip_midpoint(f)
            if wrist is None or hip_mid is None or conf < self.conf_threshold:
                consecutive = 0
                continue
            # Y increases downward → wrist_y < hip_y means hand is raised
            if wrist[1] < hip_mid[1]:
                consecutive += 1
                if consecutive >= 2:
                    return True
            else:
                consecutive = 0
        return False

    def _check_waist(self, frames: List[PoseFrame], side: str) -> bool:
        """Step 2: at least 3 consecutive frames with wrist near waist midpoint."""
        consecutive = 0
        for f in frames:
            wrist, conf = self._get_wrist(f, side)
            hip_mid = self._hip_midpoint(f)
            if wrist is None or hip_mid is None or conf < self.conf_threshold:
                consecutive = 0
                continue
            dist = math.hypot(wrist[0] - hip_mid[0], wrist[1] - hip_mid[1])
            if dist < self.waist_prox:
                consecutive += 1
                if consecutive >= 3:
                    return True
            else:
                consecutive = 0
        return False

    # ---- helpers -------------------------------------------------------------
    @staticmethod
    def _get_wrist(
        f: PoseFrame, side: str
    ) -> Tuple[Optional[Tuple[float, float]], float]:
        if side == "left":
            return f.left_wrist, f.left_wrist_conf
        return f.right_wrist, f.right_wrist_conf

    @staticmethod
    def _hip_midpoint(f: PoseFrame) -> Optional[Tuple[float, float]]:
        if f.left_hip and f.right_hip:
            return (
                (f.left_hip[0] + f.right_hip[0]) / 2,
                (f.left_hip[1] + f.right_hip[1]) / 2,
            )
        return f.left_hip or f.right_hip
