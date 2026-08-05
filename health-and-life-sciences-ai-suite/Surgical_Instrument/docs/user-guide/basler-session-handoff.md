# Basler DL Streamer FPS Fix — Full Session Handoff

> Handoff notes for continuing in a new chat window. Repo root:
> `/home/intel/sakshi/edge-ai-suites/health-and-life-sciences-ai-suite/Surgical_Instrument/`

---

## 1. The problem

Client sees **~11–20 FPS** with the optimized Basler DL Streamer pipeline
(`gencamsrc ! ... ! gvadetect device=GPU`). Expected ~50+ FPS.

## 2. Root cause (PROVEN)

Single-process `gencamsrc` (pylon) + `gvadetect device=GPU` (OpenVINO) run in
**one process**. On **Arrow Lake-P** clients the OpenVINO GPU host threads
**starve the pylon USB acquisition thread** → the camera only delivers
~11–20 FPS while the GPU sits mostly idle.

Evidence:
- Latency tracer showed per-frame processing ≈ **12 ms (~90 FPS capacity)** but
  throughput ≈ **11 FPS** → frames are not being *produced* (camera starved),
  not slow to process.
- `device=CPU` → ~37 FPS (no GPU contention).
- **Process isolation** (split camera and inference into 2 processes) → ~50 FPS.

## 3. Machines

| Role | CPU | GPU | Camera | FPS |
| --- | --- | --- | --- | --- |
| Client 1 | Arrow Lake-P | 933 MHz | daA1920-160uc dart (SN 40715749) | ~11 |
| Client 2 | Core Ultra 5 225H (ARL-P) | 2200 MHz | dart 40715749 | ~20 |
| Working host | Core Ultra 7 265K (ARL-S) | — | acA1920-150uc (SN 40067928) | 140 |

All same kernel 7.0.0-28, i915, Level Zero .so 1.14.37020, GuC 70.53.0.

Camera facts (via `basler_probe.py`):
- **dart**: uses `AcquisitionFrameRate` + `AcquisitionFrameRateEnable` (NOT
  `AcquisitionFrameRateAbs`), 160 MB/s, ResultingFrameRate 82.5.
- **ace**: 300 MB/s, 162 FPS. Both YCbCr422_8 = UYVY.

## 4. Fixes

Three ways to break the in-process contention:

- **Fix B — two-process gencamsrc split** (proven ~53 FPS):
  `gencamsrc ! fdsink | fdsrc ! gvadetect`. Already-proven, DL Streamer-based.
- **Fix C — basler_reader.py | fdsrc** (~46 FPS).
- **Direct engine (this session's work)** — DL-Streamer-free pypylon + OpenVINO
  with camera/inference on **separate dedicated cores** → ~150 FPS on host.

### Client POC (reference) — `endoscopy-poc/`
pypylon 26.2.1 + OpenVINO 2026.0.0 + OpenCV + ultralytics. Multi-threaded, each
thread pinned to its **own isolated core** at SCHED_FIFO. GRUB
`isolcpus=2-11 nohz_full=2-11 rcu_nocbs=2-11 irqaffinity=0`. Frame-skip. GPU
config `GPU_DISABLE_WINOGRAD_CONVOLUTION=YES, NUM_STREAMS=1`.

---

## 4a. What is WRONG on the client vs. what WORKS on our host

### Client machines (Arrow Lake-P) — BROKEN
- Symptom: `gencamsrc ! ... ! gvadetect device=GPU` runs at **~9–20 FPS**
  (Client 1 ≈ 11 FPS @ GPU 933 MHz; Client 2 = Core Ultra 5 225H, GPU at full
  2200 MHz, ≈ 20 FPS). Also saw dips to **~8.16 / 9 / 11.68 FPS**.
- Camera: **daA1920-160uc dart** (SN 40715749), 160 MB/s, ResultingFrameRate
  **82.5**. So the camera itself *can* do 82 FPS — but the pipeline delivers
  ~11.
- **The GPU is NOT the bottleneck.** Latency tracer showed per-frame processing
  ≈ **12 ms (~90 FPS of headroom)** while throughput sat at ~11 FPS →
  **frames are not being produced**. The pylon acquisition thread is starved.
- Diagnosis: on Arrow Lake-P the in-process OpenVINO GPU host threads starve
  pylon's USB grab thread (in-process **contention**). Same kernel/driver stack
  as the working host, so it is a **scheduling/topology** effect, not a driver
  version bug.

### Our host (Arrow Lake-S, Core Ultra 7 265K) — WORKS
- Same `gencamsrc ! gvadetect device=GPU` pipeline runs **~140 FPS**.
- Camera: **acA1920-150uc ace** (SN 40067928), 300 MB/s, up to 162 FPS.
- The stronger/differently-scheduled cores keep the pylon thread fed, so no
  starvation → full frame rate. **This is why it reproduces on the client but
  not on our bench.**

### The tell-tale proof
| Test | Result | Meaning |
| --- | --- | --- |
| `device=GPU`, single process (client) | ~11 FPS | starved |
| `device=CPU`, single process (client) | ~37 FPS | no GPU→no contention |
| per-frame latency (client) | ~12 ms | GPU has ~90 FPS headroom |
| 2-process split (client) | ~53 FPS | isolation removes starvation |

---

## 4b. Everything we tried (experiments log)

**DL Streamer in-process tuning — did NOT fix it:**
- `queue ... leaky=downstream max-size-buffers=N` on various links — reordered
  the stall, never removed it.
- `gvadetect` `nireq` / `batch-size` sweeps — no meaningful gain while camera
  stayed starved.
- `preprocess-backend` / `va-surface-sharing` / DMABuf zero-copy attempts — hit
  a **DMABuf caps error** (`format=NV12` invalid; needs no-format or
  `drm-format`). NV12 vs UYVY negotiation was fiddly (camera is YCbCr422_8 =
  **UYVY**).
- `device=CPU` — jumps to **~37 FPS**, confirming the GPU path is what triggers
  the camera starvation (useful diagnostic, not the desired GPU solution).

**Host / driver matching — inconclusive:**
- Compared client vs host: **same kernel 7.0.0-28, i915, Level Zero .so
  1.14.37020, GuC 70.53.0**. Ruled out a driver-version mismatch. GPU clocks
  differ (933 MHz vs 2200 MHz vs host) but Client 2 at full 2200 MHz still only
  hit ~20 FPS → clock alone is not the fix.

**Process / core isolation — THIS is what fixed it:**
- **2-process split** `gencamsrc ! fdsink | fdsrc ! gvadetect device=GPU` →
  **~53 FPS** on the client. Proven. (Fix B.)
- **`basler_reader.py | fdsrc`** variant → **~46 FPS**. (Fix C.)
- Client's own **POC** (pypylon + OpenVINO, each thread on an isolated
  `isolcpus` core at SCHED_FIFO, frame-skip, `GPU_DISABLE_WINOGRAD_CONVOLUTION`,
  `NUM_STREAMS=1`) runs smoothly → same lesson: **give the camera its own
  core/process.**

**Our productized answer — the direct engine (`basler_engine.py`):**
- DL-Streamer-free pypylon capture + direct OpenVINO GPU inference, camera and
  inference on **separate pinnable cores** (SCHED_FIFO).
- Standalone on host: **~153 FPS inference, ~17.5 ms latency**;
  `bench_headless.py` = **237 cam / 148 inf**.
- Wired behind `PIPELINE_BASLER_INGEST=engine`, keeping the app's HTTP contract.
- **Still to prove on the Arrow Lake-P client** (the decisive test).

**Camera / environment gotchas hit along the way:**
- Camera-busy `Feature not writable: Width` = launcher/container still holding
  the camera → `curl -X POST :8000/stop; pkill -9 -f launcher.py`.
- `GENICAM_GENTL64_PATH` conflict between pypylon 26.2.1 and system pylon 7.5 →
  engine branch does `env.pop`.
- dart uses `AcquisitionFrameRate` + `AcquisitionFrameRateEnable` (NOT
  `AcquisitionFrameRateAbs` like older ace) — matters for `basler_reader.py`.
- POC froze over RDP (vsync needs a physical 119 Hz monitor) → use headless
  bench/engine over SSH.

---

## 5. What was built this session — the Direct Engine

**Decision:** keep the `surgical-pipeline` service and its HTTP contract
(`/start /stop /health /latency`); swap the DL Streamer internals for a direct
engine behind the knob **`PIPELINE_BASLER_INGEST=engine`** (default
`gencamsrc`).

### Files changed

| File | Change |
| --- | --- |
| `pipeline/basler_engine.py` | **NEW.** Threaded pypylon capture + direct OpenVINO GPU inference (YOLO11 decode + `cv2.dnn.NMSBoxes`), camera/inference on separate pinnable cores (SCHED_FIFO). Emits `FpsCounter(...)` (true inference throughput) + gst-format latency lines via `--emit-tracer`. `--duration 0` = run-forever, SIGTERM/SIGINT handled. |
| `pipeline/launcher.py` | Added `BASLER_INGEST` env read, `_engine_cores()` helper (parses "0-3"/"2,4,6" → cam_cpu, inf_cpu), and a `_spawn` branch: when `source_kind=="basler"` and `BASLER_INGEST=="engine"`, spawns `basler_engine.py` and does `env.pop("GENICAM_GENTL64_PATH")` so pypylon 26.2.1 uses its bundled GTL. `elif basler:` = existing gst-launch path. Contract unchanged. |
| `pipeline/latency_tracer_sink.py` | Added `RollingLatency.add(latency_ms)` for direct feed. Existing regex parses the engine's emitted lines. |
| `pipeline/Dockerfile` | pypylon `4.0.0 → 26.2.1`, added `numpy` + `opencv-python`; `COPY basler_engine.py /opt/basler_engine.py`. Still builds system pylon 7.5 + gencamsrc for the fallback. |
| `docker-compose.yaml` | Added `PIPELINE_BASLER_INGEST` env passthrough (+ earlier REGISTRY `image:` support). |
| `Makefile` | Added `PIPELINE_BASLER_INGEST ?=` var + passthrough in `up`/`run`. (Earlier: REGISTRY/TAG/IMAGE vars, `_BUILD_FLAG`, removed `PIPELINE_CAMERA_CORES/RT_PRIORITY`.) |
| `.gitignore` | Ignore `endoscopy-poc/.venv/` and `endoscopy-poc/best_openvino_model`. |
| `docs/user-guide/basler-direct-engine.md` | **NEW.** Usage + rationale for the engine. |
| `endoscopy-poc/` | **NEW** dir: POC sources + `bench_headless.py`. venv untracked. |

### Engine internals (`basler_engine.py`)
- `class BaslerEngine`: `start()/stop()/is_running()`, threads
  `_camera_loop / _infer_loop / _output_loop / _fps_loop`.
- `_letterbox_decode()`: YOLO11 `[1, 4+nc, N]` decode + NMS.
- `_set_rt(cpu, prio)`: affinity + SCHED_FIFO.
- `_inf_frames` = true inference throughput.
- `main()` flags: `--device --resolution --model --threshold --duration
  --cam-cpu --inf-cpu --display --emit-tracer`.

### Validated (host, ARL-S + acA)
- Standalone: `FpsCounter total≈153 fps`, `latency mean≈17.5 ms p95≈19.4 ms`.
- `bench_headless.py`: 237 cam / 148 inf.
- **NOT yet rebuilt/tested in-container** — see pending tasks.

---

## 6. How to run the engine

### Via the app
```bash
cd .../Surgical_Instrument
docker compose build surgical-pipeline          # pypylon 26.2.1 + opencv
make up SOURCE_KIND=basler SOURCE_ARG=<SERIAL> PIPELINE_GST_CORES=0-3 \
        PIPELINE_BASLER_INGEST=engine REGISTRY=false
# host acA = 40067928 ; client dart = 40715749

curl -s -X POST http://localhost:8000/start -H 'Content-Type: application/json' \
  -d '{"device":"GPU","source":{"kind":"basler","arg":"<SERIAL>"}}'
docker logs surgical-pipeline --since 30s | grep -E "FpsCounter|\[cam\]|\[inf\]" | tail
curl -s http://localhost:8000/latency | python3 -m json.tool
curl -s -X POST http://localhost:8000/stop
docker exec surgical-pipeline sh -lc 'ps -o args= -C python3' | grep basler_engine
```
Flip back to DL Streamer: leave `PIPELINE_BASLER_INGEST` unset.

### Standalone (headless, over SSH)
```bash
cd pipeline
sudo ../endoscopy-poc/.venv/bin/python basler_engine.py \
  --device GPU --resolution 1280,720 --duration 15 \
  --model ../models/yolo11n_polyp/best_openvino_model/best.xml
# add --cam-cpu N --inf-cpu M for pinning, --display for a window
```

---

## 7. Gotchas learned
- **Camera busy** (`Feature not writable: Width`): container/launcher still
  holds camera → `curl -X POST :8000/stop; pkill -9 -f launcher.py`.
- **GENICAM_GENTL64_PATH conflict** between pypylon 26.2.1 and system pylon 7.5
  → engine branch does `env.pop`. The launcher's own best-effort pypylon
  enumeration at spawn still runs with it set (may log a harmless failure).
- **DMABuf caps error**: `format=NV12` invalid; needs no-format or drm-format.
- **POC froze over RDP**: vsync path needs a physical 119 Hz monitor. Use
  `bench_headless.py` / engine (metrics-only) over SSH.
- **POC loaded inference on CPU despite --device GPU** (ultralytics wrapper
  quirk) — the direct engine avoids ultralytics.

---

## 8. Git state
- Repo `origin` (fetch orig) was `sakshijha11`; `upstream` = open-edge-platform.
- Branch **`basler`**, commit **`c2ae32a2`** = all the above changes
  (venv/symlink excluded).
- **Pushed to `sachinkaushik/edge-ai-suites` branch `basler`.**
  (Token was used inline, then removed from `.git/config`. **Revoke that PAT.**)
- PR link: https://github.com/sachinkaushik/edge-ai-suites/pull/new/basler
- `origin` currently points to `sachinkaushik`. To restore your fork:
  `git remote set-url origin https://github.com/sakshijha11/edge-ai-suites.git`

---

## 9. Pending / next steps
1. **Rebuild + test the engine in-container** (`docker compose build
   surgical-pipeline`, then `make up ... PIPELINE_BASLER_INGEST=engine`, verify
   `FpsCounter` + `/latency`). Standalone is proven; in-container is not.
2. Verify pypylon 26.2.1 enumerates the camera inside the container (GTL path).
3. **Decisive test on the Arrow Lake-P client** (dart SN 40715749): if the
   engine holds FPS there → ship it; if not → ship the proven DL Streamer
   two-process split (~50 FPS).
4. Optional: drop gencamsrc/gst-plugin-pylon build from Dockerfile if engine
   becomes default; pixel-perfect display path; dart frame-rate node fallback
   (`AcquisitionFrameRateEnable`+`AcquisitionFrameRate` → `...Abs`).

---

## 10. Key file paths
- Engine: `pipeline/basler_engine.py`
- Launcher: `pipeline/launcher.py`
- Latency: `pipeline/latency_tracer_sink.py`
- Doc: `docs/user-guide/basler-direct-engine.md`
- POC + bench: `endoscopy-poc/`
- Camera probe: `/home/intel/sakshi/basler_probe.py`
- Full transcript (pre-compaction): `/home/intel/.vscode-server/data/User/workspaceStorage/1950f00a31db26bada74e6ded6a505fb/GitHub.copilot-chat/transcripts/2df953d3-eeec-4276-b464-30e9c2c335d0.jsonl`
