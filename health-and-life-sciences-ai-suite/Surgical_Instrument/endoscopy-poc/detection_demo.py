import threading
import time
import os
import queue
import argparse
import cv2
import ctypes
from pathlib import Path
import glfw
from pypylon import pylon
from pypylon import genicam
from ultralytics import YOLO
import openvino as ov

# ============================ RT Scheduling ============================ #
# Pins threads to specific CPU cores and sets scheduling policies for deterministic timing.
def set_realtime(cpu, policy=os.SCHED_FIFO, priority=80):
    try:
        os.sched_setaffinity(0, {cpu})
        param = os.sched_param(priority)
        os.sched_setscheduler(0, policy, param)
        print(f"Thread pinned to CPU {cpu} | Policy {policy}, Priority {priority}")
    except Exception as e:
        print(f"Failed to set RT scheduling: {e}")

# ============================ Globals ============================ #
vsync_lock = threading.Lock()
vsync_cv = threading.Condition(vsync_lock)   # condition + same lock
shutdown_event = threading.Event()

vsync_counter = 0
vsync_trigger_time_ns = 0
vsync_trigger_perf_ns = 0

# Thread-safe queues for isolation
raw_frame_queue = queue.Queue(maxsize=2)  # Small queue to prevent backlog
inference_queue = queue.Queue(maxsize=2)
display_queue = queue.Queue(maxsize=2)

# Global detection storage
latest_detection_results = None
detection_results_lock = threading.Lock()

# Global camera resolution
CAMERA_WIDTH = 544
CAMERA_HEIGHT = 544

# ============================ Helpers ============================ #

def has_node(cam, name: str) -> bool:
    try:
        n = cam.GetNodeMap().GetNode(name)
        return n is not None
    except genicam.LogicalErrorException:
        return False

def try_set_bool_node(cam, name: str, value: bool) -> None:
    if not has_node(cam, name):
        return
    try:
        node = cam.GetNodeMap().GetNode(name)
        if genicam.IsWritable(node):
            node.SetValue(value)
    except genicam.GenericException:
        pass

def parse_resolution(resolution_str):
    """Parse resolution string like '(1920,1080)' or '1920,1080' into tuple"""
    # Remove parentheses and spaces
    clean_str = resolution_str.strip().replace('(', '').replace(')', '').replace(' ', '')
    try:
        width, height = map(int, clean_str.split(','))
        return width, height
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid resolution format: {resolution_str}. Use format like '1920,1080' or '(1920,1080)'")

# ============================ VSync Thread (CPU 2) ============================ #
def vsync_loop():
    global vsync_counter, vsync_trigger_time_ns, vsync_trigger_perf_ns
    set_realtime(cpu=2, policy=os.SCHED_FIFO, priority=90)
    print("VSync thread started (GLX_OML_sync_control)")

    if not glfw.init():
        print("Failed to initialize GLFW")
        return

    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 2)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 0)
    glfw.window_hint(glfw.DOUBLEBUFFER, glfw.TRUE)

    # --- Find the 119Hz monitor explicitly ---
    monitors = glfw.get_monitors()
    target_monitor = None
    for m in monitors:
        mode = glfw.get_video_mode(m)
        if mode.refresh_rate >= 119:
            target_monitor = m
            print(f"Found 119Hz monitor: {glfw.get_monitor_name(m)}, {mode.refresh_rate}Hz")
            break

    if target_monitor is None:
        print("WARNING: Could not find 119Hz monitor, using default")
    else:
        print(f"Available monitors:")
        for m in monitors:
            mode = glfw.get_video_mode(m)
            print(f"  {glfw.get_monitor_name(m)}: {mode.refresh_rate}Hz")

    window = glfw.create_window(100, 100, "VSync", None, None)
    if not window:
        print("Failed to create GLFW window")
        glfw.terminate()
        return

    # --- Move window onto the 119Hz monitor before making context current ---
    if target_monitor:
        mx, my = glfw.get_monitor_pos(target_monitor)
        glfw.set_window_pos(window, mx + 10, my + 10)
        print(f"VSync window positioned on 119Hz monitor at ({mx+10}, {my+10})")

    glfw.make_context_current(window)
    glfw.swap_interval(0)

    # ---- GLX_OML_sync_control bindings via libGL ----
    libGL = ctypes.CDLL("libGL.so.1")

    glXGetCurrentDisplay = libGL.glXGetCurrentDisplay
    glXGetCurrentDisplay.restype = ctypes.c_void_p

    glXGetCurrentDrawable = libGL.glXGetCurrentDrawable
    glXGetCurrentDrawable.restype = ctypes.c_ulong

    glXGetSyncValuesOML = libGL.glXGetSyncValuesOML
    glXGetSyncValuesOML.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_int64),
        ctypes.POINTER(ctypes.c_int64),
        ctypes.POINTER(ctypes.c_int64),
    ]
    glXGetSyncValuesOML.restype = ctypes.c_int

    glXWaitForMscOML = libGL.glXWaitForMscOML
    glXWaitForMscOML.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.c_int64, ctypes.c_int64, ctypes.c_int64,
        ctypes.POINTER(ctypes.c_int64),
        ctypes.POINTER(ctypes.c_int64),
        ctypes.POINTER(ctypes.c_int64),
    ]
    glXWaitForMscOML.restype = ctypes.c_int

    dpy = glXGetCurrentDisplay()
    drawable = glXGetCurrentDrawable()
    if not dpy or drawable == 0:
        print("GLX current display/drawable not available (not running on X11/GLX?)")
        glfw.destroy_window(window)
        glfw.terminate()
        return

    # Prime sync values
    ust = ctypes.c_int64(0)
    msc = ctypes.c_int64(0)
    sbc = ctypes.c_int64(0)
    ok = glXGetSyncValuesOML(dpy, drawable, ctypes.byref(ust), ctypes.byref(msc), ctypes.byref(sbc))
    if ok == 0:
        print("glXGetSyncValuesOML failed (GLX_OML_sync_control not working)")
        glfw.destroy_window(window)
        glfw.terminate()
        return

    last_msc = int(msc.value)

    # --- For interval sanity checking ---
    prev_tick_perf_ns = None
    # At 119.88Hz, one frame = ~8.34ms. Warn if interval exceeds 12ms (drifted to ~60Hz source)
    EXPECTED_INTERVAL_MAX_NS = 12_000_000

    while not shutdown_event.is_set() and not glfw.window_should_close(window):
        target_msc = last_msc + 1

        ust = ctypes.c_int64(0)
        msc = ctypes.c_int64(0)
        sbc = ctypes.c_int64(0)

        t0 = time.perf_counter_ns()
        ok = glXWaitForMscOML(
            dpy, drawable,
            ctypes.c_int64(target_msc),
            ctypes.c_int64(0), ctypes.c_int64(0),
            ctypes.byref(ust), ctypes.byref(msc), ctypes.byref(sbc)
        )
        t1 = time.perf_counter_ns()
        wait_ms = (t1 - t0) / 1e6
        if wait_ms > 30:
            print(f"[VSYNC LOOP] glXWaitForMscOML blocked {wait_ms:.1f} ms")
        if ok == 0:
            continue

        tick_perf_ns = time.perf_counter_ns()
        tick_ust_ns = int(ust.value) * 1000
        new_msc = int(msc.value)

        # MSC jump detection
        if new_msc != last_msc + 1:
            print(f"[VSYNC LOOP] msc jumped {last_msc} -> {new_msc} (missed {new_msc - last_msc - 1})")

        # --- Interval sanity check: catch wrong-monitor drift ---
        if prev_tick_perf_ns is not None:
            interval_ns = tick_perf_ns - prev_tick_perf_ns
            if interval_ns > EXPECTED_INTERVAL_MAX_NS:
                print(f"[VSYNC] WARNING: interval {interval_ns / 1e6:.2f}ms — expected ~8.34ms. "
                      f"Possible wrong monitor (60Hz source)?")
        prev_tick_perf_ns = tick_perf_ns

        with vsync_cv:
            vsync_counter = new_msc
            vsync_trigger_time_ns = tick_ust_ns
            vsync_trigger_perf_ns = tick_perf_ns
            vsync_cv.notify_all()

        last_msc = new_msc

    glfw.destroy_window(window)
    glfw.terminate()

# ============================ Camera Thread (CPU 4) - TIME CRITICAL ============================ #
# Time-critical camera operations with minimal latency
def camera_loop():
    global CAMERA_WIDTH, CAMERA_HEIGHT
    
    set_realtime(cpu=4, policy=os.SCHED_FIFO, priority=85)  # High priority
    print("Camera thread started (ISOLATED)")
    
    camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
    camera.Open()

    # ---- ROI: Use global resolution settings ----

    # Disable centering features if present (safe)
    try_set_bool_node(camera, "CenterX", False)
    try_set_bool_node(camera, "CenterY", False)

    # Reset offsets first
    camera.OffsetX.SetValue(0)
    camera.OffsetY.SetValue(0)

    # Get camera limits
    max_width = camera.Width.GetMax()
    max_height = camera.Height.GetMax()
    
    # Validate and set resolution
    requested_width = min(CAMERA_WIDTH, max_width)
    requested_height = min(CAMERA_HEIGHT, max_height)
    
    # Set ROI size
    camera.Width.SetValue(requested_width)
    camera.Height.SetValue(requested_height)

    # Compute centered offsets snapped to increments
    inc_x = camera.OffsetX.GetInc()
    inc_y = camera.OffsetY.GetInc()

    target_x = ((max_width - requested_width) // 2 // inc_x) * inc_x
    target_y = ((max_height - requested_height) // 2 // inc_y) * inc_y

    camera.OffsetX.SetValue(int(target_x))
    camera.OffsetY.SetValue(int(target_y))

    print(f"Camera configured: {camera.Width.GetValue()}x{camera.Height.GetValue()} "
          f"at offset ({camera.OffsetX.GetValue()}, {camera.OffsetY.GetValue()}) "
          f"from max {max_width}x{max_height}")

    # Create color converter in camera thread for proper color handling
    converter = pylon.ImageFormatConverter()
    converter.OutputPixelFormat = pylon.PixelType_BGR8packed
    converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned
    
    # Camera trigger config
    camera.TriggerSelector.SetValue("FrameStart")
    camera.TriggerMode.SetValue("On")
    camera.TriggerSource.SetValue("Software")
    camera.ExposureAuto.SetValue("Off")
    camera.GainAuto.SetValue("Off")
    camera.BalanceWhiteAuto.SetValue("Off")
    camera.ExposureTime.SetValue(2000.0)
    camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
    
    last_capture_ns = 0
    last_seen = -1

    while camera.IsGrabbing() and not shutdown_event.is_set():

        with vsync_cv:
            while vsync_counter == last_seen and not shutdown_event.is_set():
                vsync_cv.wait(timeout=0.5)

            current_counter = vsync_counter
            vsync_trigger_perf_ns_holder = vsync_trigger_perf_ns

        # Detect skipped vblanks
        if last_seen != -1:
            missed = current_counter - last_seen - 1
            if missed > 0:
                print(f"Missed {missed} vblanks")

        last_seen = current_counter

        if current_counter % 2 != 0:
            continue
        
        # Camera operations
        trigger_hits_camera_ns = time.perf_counter_ns()
        
        # ---- WaitForFrameTriggerReady timing ----
        t0 = time.perf_counter_ns()
        camera.WaitForFrameTriggerReady(1000, pylon.TimeoutHandling_ThrowException)
        t1 = time.perf_counter_ns()
        if (t1 - t0) > 2_000_000:  # > 2 ms
            print(f"[CAM] WaitForFrameTriggerReady took {(t1 - t0)/1e6:.3f} ms")

        camera_triggered_ns = time.perf_counter_ns()

        # ---- ExecuteSoftwareTrigger timing ----
        t0 = time.perf_counter_ns()
        camera.ExecuteSoftwareTrigger()
        t1 = time.perf_counter_ns()
        if (t1 - t0) > 2_000_000:
            print(f"[CAM] ExecuteSoftwareTrigger took {(t1 - t0)/1e6:.3f} ms")

        trigger_executed_ns = time.perf_counter_ns()

        # ---- RetrieveResult timing ----
        t0 = time.perf_counter_ns()
        grab = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
        t1 = time.perf_counter_ns()
        if (t1 - t0) > 9_000_000:  # > 9 ms
            print(f"[CAM] RetrieveResult took {(t1 - t0)/1e6:.3f} ms")

        if grab.GrabSucceeded():
            grab_done_ns = time.perf_counter_ns()
            
            # Convert to color HERE while we have the Pylon grab result
            t0 = time.perf_counter_ns()
            converted_image = converter.Convert(grab)
            color_array = converted_image.GetArray()
            t1 = time.perf_counter_ns()
            if (t1 - t0) > 5_000_000:
                print(f"[CAM] Convert+GetArray took {(t1 - t0)/1e6:.3f} ms")

            frame_packet = {
                'color_data': color_array.copy(),  # Send color data instead of raw
                'vsync_msc': current_counter,
                "vsync_trigger_perf_ns": vsync_trigger_perf_ns_holder,
                'grab_done_ns': grab_done_ns,
                'trigger_hits_camera_ns': trigger_hits_camera_ns,
                'camera_triggered_ns': camera_triggered_ns,
                'trigger_executed_ns': trigger_executed_ns
            }
            
            # Non-blocking send to processing thread
            try:
                raw_frame_queue.put_nowait(frame_packet)
            except queue.Full:
                pass  # Drop frame if processing can't keep up
        
        grab.Release()
        time.sleep(0)  # Yield
    
    camera.StopGrabbing()
    camera.Close()
    print("Camera thread terminated")

# ============================ Processing Thread (CPU 6) - NON-CRITICAL ============================ #
# Handles frame routing and basic processing
def processing_loop():
    set_realtime(cpu=6, policy=os.SCHED_OTHER, priority=0)  # Normal priority
    print("Processing thread started")
    
    frame_count = 0
    
    while not shutdown_event.is_set():
        try:
            # Get color frame from camera thread
            frame_packet = raw_frame_queue.get(timeout=0.1)
            frame_count += 1
            
            # Get the color image data (already converted by camera thread)
            color_img = frame_packet['color_data']  # Changed from 'raw_data' to 'color_data'
            
            # Send to inference thread (every Nth frame)
            if not BYPASS_INFERENCE and frame_count % FRAME_SKIP_COUNT == 0:
                try:
                    inference_packet = {
                        'image': color_img.copy(),
                        'frame_packet': frame_packet
                    }
                    inference_queue.put_nowait(inference_packet)
                except queue.Full:
                    pass
            
            # Always send to display (latest frame) - NO INFERENCE RESULTS
            try:
                display_packet = {
                    'image': color_img,
                    'frame_packet': frame_packet,
                    'has_inference': False
                }
                display_queue.put_nowait(display_packet)
            except queue.Full:
                pass
                
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Processing error: {e}")

# ============================ Inference Thread - COMPUTE INTENSIVE ============================ #
# Runs AI inference without affecting camera timing - STORES RESULTS GLOBALLY
def inference_loop():
    global latest_detection_results
    
    det_model = None
    if not BYPASS_INFERENCE:
        try:
            print(f"Loading OpenVINO model on {INFERENCE_DEVICE}...")
            det_model_path = Path("best_openvino_model/best.xml")
            core = ov.Core()
            det_ov_model = core.read_model(det_model_path)
            det_model = YOLO(det_model_path.parent, task="detect")

            ov_config = {}
            if INFERENCE_DEVICE == "CPU":
                # Configure for multi-core CPU inference
                ov_config.update({
                    "NUM_STREAMS": "1",  # Multiple inference streams
                    "INFERENCE_NUM_THREADS": "4",  # Match our 4 cores
                })
                print("Configured for 4-core CPU inference")
            elif INFERENCE_DEVICE == "GPU":
                ov_config.update({
                    "GPU_DISABLE_WINOGRAD_CONVOLUTION": "YES", 
                    "NUM_STREAMS": "1"
                })
            elif INFERENCE_DEVICE == "NPU":
                ov_config.update({"NUM_STREAMS": "1"})

            compiled_model = core.compile_model(det_ov_model, INFERENCE_DEVICE, ov_config)
            
            custom = {"conf": 0.25, "batch": 1, "save": False, "mode": "predict"}
            args = {**det_model.overrides, **custom}
            det_model.predictor = det_model._smart_load("predictor")(
                overrides=args, _callbacks=det_model.callbacks
            )
            det_model.predictor.setup_model(model=det_model.model)
            det_model.predictor.model.ov_compiled_model = compiled_model
            print("Model loaded with multi-core config")
        except Exception as e:
            print(f"Model loading failed: {e}")
            det_model = None
    
    inference_count = 0
    
    while not shutdown_event.is_set():
        try:
            inference_packet = inference_queue.get(timeout=0.1)
            
            if det_model:
                try:
                    inference_count += 1
                    infer_start = time.perf_counter_ns()
                    
                    # Run inference using all 4 cores
                    detections = det_model(inference_packet['image'], verbose=False)
                    
                    infer_end = time.perf_counter_ns()
                    inference_time_ms = (infer_end - infer_start) / 1e6
                    
                    # SOLUTION: Store detection results globally instead of sending to display queue
                    with detection_results_lock:
                        latest_detection_results = {
                            'boxes': detections[0].boxes.data.cpu().numpy() if detections[0].boxes else None,
                            'timestamp': infer_end,
                            'inference_time_ms': inference_time_ms,
                            'frame_id': inference_packet['frame_packet']
                        }
                    
                except Exception as e:
                    print(f"Inference error: {e}")
                    
        except queue.Empty:
            continue

# ============================ Display Thread (CPU 7) - LOW PRIORITY ============================ #
# Shows results with overlaid bounding boxes and calculates comprehensive timing metrics
def display_loop():
    global latest_detection_results, CAMERA_WIDTH, CAMERA_HEIGHT
    
    set_realtime(cpu=7, policy=os.SCHED_OTHER, priority=0)
    print("Display thread started")
    
    # Print CSV header
    print("clock_time,time_betw_triggers,trigger_to_shutter_cmd,trigger_to_shutter_done,trigger_to_capture_done,trigger_to_grab_done,trigger_to_display,display_cost,qsize")
    
    WIN = "Camera"

    try:
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL | cv2.WINDOW_GUI_NORMAL)
    except cv2.error:
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)

    # Calculate display window size to maintain aspect ratio
    aspect_ratio = CAMERA_WIDTH / CAMERA_HEIGHT
    
    if aspect_ratio >= 1.0:  # Width >= Height
        display_width = 900
        display_height = int(900 / aspect_ratio)
    else:  # Height > Width
        display_height = 900
        display_width = int(900 * aspect_ratio)
    
    cv2.resizeWindow(WIN, display_width, display_height)
    cv2.moveWindow(WIN, 0, 0)
    
    print(f"Display window: {display_width}x{display_height} (aspect ratio: {aspect_ratio:.2f})")
    
    while not shutdown_event.is_set():
        try:
            display_packet = display_queue.get(timeout=0.1)
            qsize = display_queue.qsize()
            
            # Work on a copy of the image to avoid modifying original
            image = display_packet['image'].copy()
            
            # SOLUTION: Get latest detections without queue overhead
            current_detections = None
            with detection_results_lock:
                if latest_detection_results is not None:
                    current_detections = latest_detection_results.copy()  # Copy to avoid holding lock
            
            # Overlay bounding boxes if detections are recent enough (within last 200ms)
            if (current_detections and 
                (time.perf_counter_ns() - current_detections['timestamp']) < 200_000_000):
                
                if current_detections['boxes'] is not None:
                    for box in current_detections['boxes']:
                        x1, y1, x2, y2, conf, cls = box
                        
                        # Draw bounding box
                        cv2.rectangle(image, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
                        
                        # Draw confidence score
                        label = f'{conf:.2f}'
                        cv2.putText(image, label, (int(x1), int(y1)-10), 
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            
            # Display the image with proper aspect ratio (no letterboxing)
            display_image = cv2.resize(image, (display_width, display_height), interpolation=cv2.INTER_LINEAR)
            cv2.imshow(WIN, display_image)
            key = cv2.waitKey(1)
            if key == 27:
               shutdown_event.set()

        except queue.Empty:
            continue
        except Exception as e:
            print(f"Display error: {e}")
    
    cv2.destroyAllWindows()
    print("Display thread terminated")

# ============================ Main ============================ #
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RT Camera + OpenVINO (Thread Isolated)")
    parser.add_argument("--device", choices=["CPU", "GPU", "NPU"], default="CPU")
    parser.add_argument("--frame-skip", type=int, default=3)
    parser.add_argument("--bypass-inference", action="store_true")
    parser.add_argument("--resolution", type=parse_resolution, default="1920,1080",
                       help="Camera resolution in format 'width,height' or '(width,height)'. Example: --resolution 1920,1080")
    args = parser.parse_args()

    BYPASS_INFERENCE = args.bypass_inference
    FRAME_SKIP_COUNT = args.frame_skip
    INFERENCE_DEVICE = args.device
    CAMERA_WIDTH, CAMERA_HEIGHT = args.resolution

    print("=" * 60)
    print("RT Camera (Thread Isolated Architecture) - Solution 4")
    print("=" * 60)
    print("Thread isolation:")
    print("   • Camera Thread (CPU 4): Time-critical capture only")
    print("   • Processing Thread (CPU 6): Color conversion")
    print("   • Inference Thread (CPUs 8-11): AI processing -> Global storage")
    print("   • Display Thread (CPU 7): OpenCV display + bbox overlay + timing")
    print("   • VSync Thread (CPU 2): GLFW timing reference")
    print("=" * 60)
    print("Detection overlay: Global variable sharing (no queue interference)")
    print(f"Camera Resolution: {CAMERA_WIDTH}x{CAMERA_HEIGHT}")
    print("=" * 60)

    if BYPASS_INFERENCE:
        print("Mode: BYPASS INFERENCE")
    else:
        print(f"Mode: INFERENCE ({INFERENCE_DEVICE}, every {FRAME_SKIP_COUNT} frames)")

    try:
        # Start all threads
        threading.Thread(target=vsync_loop, daemon=True).start()
        threading.Thread(target=camera_loop, daemon=True).start()
        threading.Thread(target=processing_loop, daemon=True).start()
        threading.Thread(target=inference_loop, daemon=True).start()
        threading.Thread(target=display_loop, daemon=False).start()  # Main thread
        
    except KeyboardInterrupt:
        print("\nShutting down...")
        shutdown_event.set()

#Improvements
"""
4) Perform some basic improvements to the image (white balance, etc.) to test the impact of these operations on latency. 
"""
