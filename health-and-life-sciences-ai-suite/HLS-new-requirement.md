This feature covers the usecases of cardiac imaging and left ventricle segmentation through enablement, optimization, and demonstration of EchoNet-Dynamic model.  



Note 1: EchoNet-Dynamic is an end-to-end deep learning approach for segmenting the left ventricle, estimating the ejection fraction from input echocardiogram videos, and assessing cardiomyopathy (heart failure)

Note 2: EchoNet-Dynamic builds and runs two models sequentially: (1) 31.5M parameter Ejection Fraction model (R2Plus1D-18)  and (2) 39.6M parameter Segmentation model (DeepLabV3-ResNet50)

Note 3: If KPIs are met, Intel Core Ultra meets or exceeds performance of GTX 1080 Ti GPU

Requirements

Operating System: Windows 11

Enable and optimize EchoNet-Dynamic on iGPU of Intel Core Ultra devices (PTL, NVL) - P1
Acceptance Criteria / KPIs:
Scope	Metric	Target
End-to-End	Latency	< 50 msec
End-to-End	FPS	> 50 FPS
Segmentation Only	Latency	< 20 msec
Segmentation Only	FPS	> 50 FPS
 

2. Enable and optimize of EchoNet-Dynamic on NPU (PTL, NVL) - P1

Acceptance Criteria / KPIs:
Scope	Metric	Target
End-to-End	Latency	< 50 msec
End-to-End	FPS	> 50 FPS
Segmentation Only	Latency	< 20 msec
Segmentation Only	FPS	> 50 FPS
 

3.  Heterogeneous Compute UI / HW Engine Selection - P1

Show the benefit of heterogeneous compute by creating a UI that allows the end user to select the hardware engine (CPU/iGPU/NPU) ** on which to run EchoNet-Dynamic.

 

4.  Data Visualization -  Side-by-Side Video Display - P1
Implement the following data visualization layout:

Left Panel	Right Panel
Heading: "Original"	Heading: "Segmentation"
Original grayscale echocardiogram video	Video clip with segmented mask of the left ventricle
 

5. Sample application deployment shall be easy and doable within 5 minutes for non-developer person. 

 