Here's a complete, build-ready spec for Option C. It's written so a developer or coding agent can implement it end-to-end against the current `Surgical_Instrument` codebase.
 
---
 
# Option C — Decoupled Python inference via `appsink` + `cairooverlay`
 
## 0. Goal & success criteria
 
Replace the inline `gvadetect` element with a **decoupled** Python inference path so displayed FPS is no longer gated by inference. Keep `gencamsrc` capture and GStreamer display.
 
**Acceptance:**
- Live Basler: displayed/E2E ≥ 60 FPS (target camera rate), steady, no periodic ~1 s stalls.
- Detection boxes visible, lagging ≤ ~1–2 frames.
- Works for both `basler` and `file` sources with the same graph.
- No hardcoded CPU pinning, no GLX/vsync/ctypes.
- Runs inside the existing `surgical-pipeline` container.
 
## 1. Architecture
 
Single Python process owns a GStreamer pipeline (via `gi`/`Gst`), not `gst-launch`:
 
```
[basler] gencamsrc ! bayer2rgb? ! vapostproc ! video/x-raw(NV12)     ┐
[file]   filesrc ! qtdemux ! h264parse ! vah264dec ! vapostproc      ┘
   ! tee name=t
     t. ! queue(leaky=downstream,max-size-buffers=2)
          ! vapostproc ! video/x-raw,format=BGRx
          ! cairooverlay(draw=on_draw)          # display branch — every frame
          ! videoconvert ! autovideosink sync=<false live / true file>
     t. ! queue(leaky=downstream,max-size-buffers=1)
          ! vapostproc ! video/x-raw,format=RGB,width=640,height=640   # infer branch
          ! appsink name=infer emit-signals=true drop=true max-buffers=1 sync=false
```
 
Threads/roles:
- **GLib main loop** drives the pipeline and both callbacks.
- **`appsink` `new-sample` callback** → hand frame to OpenVINO `AsyncInferQueue` (non-blocking) → return immediately.
- **AsyncInfer completion callback** → post-process → write boxes to a lock-guarded `LatestDetections`.
- **`cairooverlay` `draw` callback** (fires per display frame) → read `LatestDetections`, draw boxes.
 
The infer branch is `leaky + drop=true + max-buffers=1 + system-memory RGB caps`, so it **never holds VA surfaces and never back-pressures the tee** → display branch runs free at camera rate.
 
## 2. New files
 
Create under pipeline:
1. `appsink_infer.py` — the GStreamer app + inference orchestration (main entrypoint).
2. `ov_detector.py` — OpenVINO IR loading, async inference queue, pre/post-processing.
3. (optional) `overlay_state.py` — thread-safe `LatestDetections` store.
 
Keep existing pipeline_string.py/launcher.py for the `gvadetect` path; add Option C as a selectable backend (see §8).
 
## 3. Dependencies (pipeline/Dockerfile)
 
Ensure present in the `surgical-pipeline` image:
- `python3-gi`, `gir1.2-gstreamer-1.0`, `gstreamer1.0-plugins-good` (provides `cairooverlay`), `gstreamer1.0-plugins-base`.
- `python3-gi-cairo`, `gir1.2-gtk-3.0` not required; `pycairo` **is** required for the `cairooverlay` draw context (`pip install pycairo`).
- `openvino` (already present), `numpy`, `opencv-python-headless` (for letterbox/NMS; or implement NMS in numpy to avoid the dep).
- `pypylon` already present (for `gencamsrc`/Basler; not used directly here).
 
Add a build check: `gst-inspect-1.0 cairooverlay` must succeed.
 
## 4. `ov_detector.py` — inference wrapper
 
### 4.1 Model load
```python
import openvino as ov
core = ov.Core()
model = core.read_model("/models/yolo11n_polyp/best_openvino_model/best.xml")
cfg = {
    "PERFORMANCE_HINT": "LATENCY",
    "GPU_DISABLE_WINOGRAD_CONVOLUTION": "YES",
    "NUM_STREAMS": "1",
    "ALLOW_AUTO_BATCHING": "NO",
}
compiled = core.compile_model(model, "GPU", cfg)   # device from env
NIREQ = int(os.environ.get("OV_NIREQ", "4"))
infer_queue = ov.AsyncInferQueue(compiled, NIREQ)
infer_queue.set_callback(on_infer_done)   # see §6
```
- Input: `[1,3,640,640]`, f32, RGB, normalized `/255`, NCHW.
- Output: `[1,5,8400]` → 8400 anchors × (cx, cy, w, h, score) in 640-space (single class = polyp).
 
### 4.2 Preprocessing (letterbox)
Input to this function is already an RGB 640×640 frame **iff** you let `vapostproc` resize (simplest). Two choices — pick **A** for least Python work:
 
**A (recommended):** infer branch caps already force `RGB,width=640,height=640` (GPU resize via `vapostproc`). Then Python only does `/255`, HWC→CHW, add batch. **Note:** `vapostproc` does a plain stretch resize (no aspect-ratio letterbox); the model was exported with letterbox, so account for this in coordinate mapping (§4.4) by using a simple scale (no padding) — acceptable for a 1280×720→640×640 stretch if detection quality is fine; verify accuracy vs `gvadetect`. If accuracy drops, use **B**.
 
**B (accurate):** infer branch caps = full-res `RGB`; do letterbox in Python (`cv2` or numpy): resize keeping aspect ratio into 640×640 with gray padding; record `scale`, `pad_x`, `pad_y` for inverse mapping.
 
### 4.3 Submit (non-blocking)
```python
def submit(frame_rgb, frame_meta):
    blob = preprocess(frame_rgb)                # (1,3,640,640) f32
    infer_queue.start_async({0: blob}, userdata=frame_meta)
```
`start_async` returns immediately if a request is free; the `appsink` callback must **not** block — if all requests are busy, skip this frame (the branch is already drop=true).
 
### 4.4 Postprocessing (in `on_infer_done`)
- Read output `[1,5,8400]` → transpose to `[8400,5]`.
- `scores = out[:,4]`; keep `scores >= THRESHOLD` (0.5).
- boxes `cx,cy,w,h` (640-space) → `x1,y1,x2,y2`.
- **NMS** (numpy or `cv2.dnn.NMSBoxes`), IoU 0.45.
- Map back to display resolution:
  - Option A: `x *= disp_w/640`, `y *= disp_h/640`.
  - Option B: `x = (x - pad_x)/scale`, `y = (y - pad_y)/scale`.
- Store list of `(x1,y1,x2,y2,score)` in `LatestDetections` with timestamp.
 
## 5. `overlay_state.py` — thread-safe store
```python
class LatestDetections:
    def __init__(self): self._lock=Lock(); self._boxes=[]; self._ts=0
    def set(self, boxes, ts): 
        with self._lock: self._boxes, self._ts = boxes, ts
    def get(self):
        with self._lock: return list(self._boxes), self._ts
```
Overlay uses boxes only if `now - ts < 200 ms` (staleness guard, matches reference).
 
## 6. `appsink_infer.py` — GStreamer app
 
### 6.1 Build pipeline
- Build the graph string from §1 based on `SOURCE_KIND` (reuse the source-segment logic from `pipeline_string.py::_build_source` — refactor it to be importable, or duplicate the source elements).
- Use `Gst.parse_launch(pipeline_str)`; then `pipeline.get_by_name("infer")` for appsink, `pipeline.get_by_name("overlay")` for cairooverlay.
 
### 6.2 appsink handler
```python
def on_new_sample(sink):
    sample = sink.emit("pull-sample")
    buf = sample.get_buffer()
    caps = sample.get_caps()
    w,h = caps_wh(caps)
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    frame = np.frombuffer(mapinfo.data, np.uint8).reshape(h, w, 3)  # RGB
    detector.submit(frame.copy(), meta={"pts": buf.pts})
    buf.unmap(mapinfo)
    return Gst.FlowReturn.OK
appsink.connect("new-sample", on_new_sample)
```
- `frame.copy()` because the buffer is released after unmap.
- If `AsyncInferQueue` has no free request, `submit` should early-return (track free count or catch and drop).
 
### 6.3 cairooverlay draw
```python
def on_draw(overlay, cr, timestamp, duration):
    boxes, ts = latest.get()
    if now_ns() - ts > 200_000_000: return
    cr.set_source_rgb(0,1,0); cr.set_line_width(2)
    for (x1,y1,x2,y2,score) in boxes:
        cr.rectangle(x1,y1,x2-x1,y2-y1); cr.stroke()
        cr.move_to(x1, y1-4); cr.show_text(f"{score:.2f}")
overlay.connect("draw", on_draw)
```
- `cairooverlay` provides display-resolution coordinates; ensure §4.4 maps boxes to **display** size (the caps of the display branch), not 640.
 
### 6.4 Infer completion callback (§4.1 `on_infer_done`)
```python
def on_infer_done(request, userdata):
    out = request.get_output_tensor(0).data   # [1,5,8400]
    boxes = postprocess(out)                   # display-space
    latest.set(boxes, now_ns())
```
 
### 6.5 FPS + latency
- Add `gvafpscounter` on the **display** branch (before sink) for displayed FPS, or count draw callbacks.
- Optional: measure inference latency (submit ts → completion ts) and expose via the existing `/latency` mechanism.
 
## 7. Source-segment reuse
Refactor `pipeline_string.py::_build_source` so `appsink_infer.py` can import and reuse the exact `basler`/`file` source element lists (keeps `gencamsrc` props, `bayer2rgb`, `vapostproc` identical to today). Do **not** duplicate/diverge camera config.
 
## 8. Integration with launcher.py
Add an env switch `PIPELINE_BACKEND=gvadetect|appsink` (default `gvadetect` for now):
- In `_spawn`, if `PIPELINE_BACKEND == "appsink"`, spawn `python3 /opt/appsink_infer.py` (with the same source/device/display env) instead of `gst-launch-1.0 <string>`.
- Pass through: `SOURCE_KIND`, `SOURCE_ARG`, `IR_XML`, `DETECTION_DEVICE`, `THRESHOLD`, `PIPELINE_VIDEO_SINK`, `PIPELINE_SINK_SYNC`, `OV_NIREQ`, display env (`DISPLAY`, .X11-unix).
- Keep the supervisor/respawn logic unchanged (it just supervises a different subprocess).
- Warmup (§ existing `PIPELINE_WARMUP`) still applies — do one throwaway async inference at startup in `ov_detector` init.
 
Copy the new files in Dockerfile (`COPY appsink_infer.py ov_detector.py overlay_state.py /opt/`).
 
## 9. Config knobs (env)
- `PIPELINE_BACKEND=appsink`
- `DETECTION_DEVICE=GPU`
- `OV_NIREQ=4`
- `INFER_THRESHOLD=0.5`, `INFER_IOU=0.45`
- `INFER_INPUT_MODE=stretch|letterbox` (A vs B in §4.2)
- `PIPELINE_VIDEO_SINK`, `PIPELINE_SINK_SYNC` (reuse existing)
 
## 10. Test plan / acceptance
1. **Sanity (no infer):** run graph with the infer branch's appsink set to `fakesink` — confirm display ≥ 80 FPS (proves decoupling doesn't cost display FPS).
2. **Full C, live Basler:** confirm displayed FPS ≥ 60 steady, no ~1 s stalls in `gvafpscounter`.
3. **Accuracy check:** compare boxes/counts against the old `gvadetect` run on the same recorded clip; if stretch-mode (A) degrades, switch to letterbox (B).
4. **Latency:** LED/high-speed-camera glass-to-glass unchanged or better vs current.
5. **File mode:** same graph with `filesrc` source plays with boxes.
6. **BU machine:** run 2–4 on the BU validation box — this is the real pass/fail.
 
## 11. Risks & mitigations
- **appsink back-pressure:** must be `drop=true max-buffers=1` + system-memory RGB caps; verify no `VAMemory` on the infer branch. If back-pressure still appears, add `queue leaky=downstream max-size-buffers=1` immediately before appsink (already in graph).
- **`cairooverlay` needs system memory:** the `vapostproc ! video/x-raw,BGRx` before it handles that (~1 ms GPU download on display branch).
- **Stretch vs letterbox accuracy:** default to letterbox (B) if unsure; it matches the exported model.
- **GIL / numpy copy cost:** only on the (dropped) infer branch; display branch is pure GStreamer.
 
## 12. Deliverables
- `pipeline/appsink_infer.py`, `pipeline/ov_detector.py`, `pipeline/overlay_state.py`
- pipeline_string.py refactor to export source-segment builder
- launcher.py `PIPELINE_BACKEND` switch
- Dockerfile deps + COPY
- README/docs note for the new backend + env knobs
- Benchmark results table (display FPS, infer FPS, glass-to-glass) `gvadetect` vs `appsink`, on local + BU machines
 
---
 
Want me to also generate the actual starter code for `ov_detector.py` and `appsink_infer.py` (skeletons with the callbacks wired), so the developer starts from working files rather than the spec alone?