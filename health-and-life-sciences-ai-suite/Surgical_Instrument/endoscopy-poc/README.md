# Endoscopy Demo

## Ubuntu 24.04 Setup
1. Disable Wayland to use X11
```
sudo nano /etc/gdm3/custom.conf
``` 
and uncomment line: `WaylandEnable=false`

2. Navigate to the Display settings and select 119.88 Hz as the Refresh Rate.

3. Update grub file to isolate CPU cores:

3a. `sudo nano /etc/default/grub`

3b. Add line:
```
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash isolcpus=2,3,4,5,6,7,8,9,10,11 nohz_full=2,3,4,5,6,7,8,9,10,11 rcu_nocbs=2,3,4,5,6,7,8,9,10,11 irqaffinity=0"
```

3c. `sudo update-grub`

### Driver Installation
3.
    * If you wish to run the polyp detection model on the GPU, install GPU drivers located [here](https://github.com/intel/compute-runtime) by navigating to the latest Release and follow intructions under 'Installation procedure on Ubuntu 24.04' Section.
    * If you wish to run the polyp detection model on the NPU, install NPU drivers located [here](https://github.com/intel/linux-npu-driver) by navigating to the latest Release and follow instructions under 'Installation procedure'.

4. Place model weights in `best_openvino_model` directory with `best.bin` and `best.xml` being the file names for model weights and topology respectively.

5. Install [Pylon Viewer](https://www.baslerweb.com/en-us/downloads/software/?downloadCategory.values.label.data=pylon) and open the application once installed to ensure camera is accessible and can communicate over USB.

6. Create and activate a virtual environment for the camera based application and install dependencies:

```
pip install -r cam_requirements.txt
```

7. Validate devices recognized by OpenVINO before running the demo:

```
python3 -c "from openvino import Core; print(Core().available_devices)"
```

## Usage

1. Run camera application to being viewing object detection overlay:

```
python detection_demo.py --device GPU --frame-skip 2 --resolution 1920,1080
```

`--device` defines the inference device which can be CPU, GPU or NPU
`--frame-skip` defines every nth frame that gets inferenced upon
`--resolution` defines the resolution of the camera and opencv window display (up to 1080p)

2. Create and activate a virtual environment for the the latency visualization and install dependencies:

```
pip install -r graph_requirements.txt
```

3. Begin viewing latency chart over time:

```
python graph.py
```
