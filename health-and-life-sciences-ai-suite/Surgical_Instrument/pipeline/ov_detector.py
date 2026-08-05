from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import openvino as ov

from overlay_state import DetectionBox

log = logging.getLogger("ov_detector")


@dataclass(frozen=True)
class FrameMeta:
    display_width: int
    display_height: int
    submit_ns: int


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    if boxes.size == 0:
        return []
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[i] + areas[rest] - inter
        iou = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
        order = rest[iou <= iou_threshold]
    return keep


class OpenVINODetector:
    def __init__(
        self,
        *,
        model_xml: str,
        device: str,
        threshold: float,
        iou_threshold: float,
        nireq: int,
        on_result: Callable[[list[DetectionBox], int], None],
    ) -> None:
        self.threshold = threshold
        self.iou_threshold = iou_threshold
        self._on_result = on_result
        self._free = threading.BoundedSemaphore(max(1, nireq))
        self._submitted = 0
        self._completed = 0
        self._dropped_busy = 0

        core = ov.Core()
        model = core.read_model(model_xml)
        cfg = {
            "PERFORMANCE_HINT": "LATENCY",
            "NUM_STREAMS": "1",
            "ALLOW_AUTO_BATCHING": "NO",
        }
        if device.upper() == "GPU":
            cfg["GPU_DISABLE_WINOGRAD_CONVOLUTION"] = "YES"
        compiled = core.compile_model(model, device.upper(), cfg)
        self._input = compiled.inputs[0]
        shape = list(self._input.shape)
        if len(shape) != 4:
            raise ValueError(f"expected NCHW model input, got {shape}")
        _, _, self.input_h, self.input_w = (int(v) for v in shape)
        # Letterbox bookkeeping (constant once the first real frame is seen).
        # The infer branch delivers an aspect-correct downscale (e.g. 640x360)
        # which _preprocess pads to the square model input (e.g. 640x640).
        self._content_w = self.input_w
        self._content_h = self.input_h
        self._pad_x = 0
        self._pad_y = 0
        self._queue = ov.AsyncInferQueue(compiled, max(1, nireq))
        self._queue.set_callback(self._on_complete)
        log.info("[appsink] OpenVINO compiled device=%s input=%s nireq=%s", device.upper(), shape, nireq)

        self._warmup()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "submitted": self._submitted,
            "completed": self._completed,
            "dropped_busy": self._dropped_busy,
        }

    def submit(self, frame_bgrx: np.ndarray, meta: FrameMeta) -> bool:
        if not self._free.acquire(blocking=False):
            self._dropped_busy += 1
            return False
        blob = self._preprocess(frame_bgrx)
        self._submitted += 1
        try:
            self._queue.start_async({self._input: blob}, userdata=meta)
        except Exception:
            self._free.release()
            raise
        return True

    def _warmup(self) -> None:
        blob = np.zeros((1, 3, self.input_h, self.input_w), dtype=np.float32)
        if not self._free.acquire(blocking=False):
            return
        try:
            self._queue.start_async(
                {self._input: blob},
                userdata=FrameMeta(display_width=self.input_w, display_height=self.input_h, submit_ns=time.perf_counter_ns()),
            )
            self._queue.wait_all()
        finally:
            pass

    def _preprocess(self, frame_bgrx: np.ndarray) -> np.ndarray:
        h, w = int(frame_bgrx.shape[0]), int(frame_bgrx.shape[1])
        if w > self.input_w or h > self.input_h:
            raise ValueError(
                f"appsink delivered {(h, w)} which exceeds model input "
                f"{(self.input_h, self.input_w)}; the infer branch must emit an "
                f"aspect-correct downscale that fits the square model input "
                f"(e.g. 640x360 for a 640x640 model)."
            )
        # BGRx -> RGB: drop alpha and swap B/R in one vectorized slice.
        rgb = frame_bgrx[:, :, [2, 1, 0]]
        # Letterbox: centre-pad the aspect-correct frame into the square model
        # input with the standard gray (114) fill, matching the export-time
        # preprocessing so detection accuracy is preserved.
        pad_x = (self.input_w - w) // 2
        pad_y = (self.input_h - h) // 2
        canvas = np.full((self.input_h, self.input_w, 3), 114, dtype=np.uint8)
        canvas[pad_y:pad_y + h, pad_x:pad_x + w] = rgb
        self._content_w, self._content_h = w, h
        self._pad_x, self._pad_y = pad_x, pad_y
        chw = canvas.astype(np.float32) / 255.0
        return np.ascontiguousarray(chw.transpose(2, 0, 1)[None])

    def _on_complete(self, request, userdata) -> None:  # noqa: ANN001
        try:
            meta = userdata if isinstance(userdata, FrameMeta) else FrameMeta(self.input_w, self.input_h, time.perf_counter_ns())
            output = request.get_output_tensor(0).data
            boxes = self._postprocess(output, meta.display_width, meta.display_height)
            self._completed += 1
            self._on_result(boxes, time.perf_counter_ns())
        except Exception as exc:  # noqa: BLE001
            log.warning("[appsink] inference completion failed: %s", exc)
        finally:
            self._free.release()

    def _postprocess(self, output: np.ndarray, display_width: int, display_height: int) -> list[DetectionBox]:
        pred = np.asarray(output)[0]
        if pred.shape[0] < pred.shape[1]:
            pred = pred.transpose(1, 0)
        if pred.shape[1] < 5:
            return []

        boxes_cxcywh = pred[:, :4]
        if pred.shape[1] == 5:
            scores = pred[:, 4]
        else:
            scores = pred[:, 4:].max(axis=1)
        keep = scores >= self.threshold
        if not np.any(keep):
            return []

        boxes_cxcywh = boxes_cxcywh[keep]
        scores = scores[keep]
        xyxy = np.empty_like(boxes_cxcywh)
        xyxy[:, 0] = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
        xyxy[:, 1] = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
        xyxy[:, 2] = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
        xyxy[:, 3] = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2
        # Undo the letterbox padding (square model space -> content space)...
        xyxy[:, [0, 2]] -= self._pad_x
        xyxy[:, [1, 3]] -= self._pad_y
        # ...then map content space -> display space. Both the content frame and
        # the display frame are pure downscales of the same source, so a plain
        # ratio is exact.
        xyxy[:, [0, 2]] *= display_width / float(self._content_w)
        xyxy[:, [1, 3]] *= display_height / float(self._content_h)
        xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, display_width - 1)
        xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, display_height - 1)

        idxs = _nms(xyxy, scores, self.iou_threshold)
        return [
            DetectionBox(
                x1=float(xyxy[i, 0]),
                y1=float(xyxy[i, 1]),
                x2=float(xyxy[i, 2]),
                y2=float(xyxy[i, 3]),
                score=float(scores[i]),
            )
            for i in idxs
        ]