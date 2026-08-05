from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class DetectionBox:
    x1: float
    y1: float
    x2: float
    y2: float
    score: float


class LatestDetections:
    def __init__(self) -> None:
        self._lock = Lock()
        self._boxes: list[DetectionBox] = []
        self._ts_ns = 0

    def set(self, boxes: list[DetectionBox], ts_ns: int) -> None:
        with self._lock:
            self._boxes = list(boxes)
            self._ts_ns = ts_ns

    def get(self) -> tuple[list[DetectionBox], int]:
        with self._lock:
            return list(self._boxes), self._ts_ns