# EchoNet-Dynamic Cardiac Imaging Requirements

## Project Overview

This feature covers the use cases of cardiac imaging and left ventricle segmentation through enablement, optimization, and demonstration of the EchoNet-Dynamic model.

### Background

**EchoNet-Dynamic** is an end-to-end deep learning approach for:
- Segmenting the left ventricle
- Estimating the ejection fraction from input echocardiogram videos
- Assessing cardiomyopathy (heart failure)

---

## Key Terminology

### What is the Left Ventricle?

The **left ventricle** is the heart's main pumping chamber. It receives oxygen-rich blood from the left atrium and pumps it out to the entire body through the aorta. The left ventricle is the most muscular chamber of the heart and is critical for maintaining healthy blood circulation.

**Clinical Significance**: Dysfunction of the left ventricle is a key indicator of heart disease and heart failure.

### What is Ejection Fraction?

**Ejection fraction (EF)** is a measurement of how much blood the left ventricle pumps out with each contraction, expressed as a percentage.

**Formula**: EF = (Amount of blood pumped out / Total amount of blood in the ventricle) × 100

**Normal Range**: 
- **Normal**: 50-70%
- **Borderline**: 41-49%
- **Reduced**: ≤ 40% (indicates heart failure)

**Example**: If the left ventricle contains 100 mL of blood and pumps out 60 mL with each beat, the ejection fraction is 60%.

**Clinical Importance**: Ejection fraction is one of the most important metrics for diagnosing and monitoring heart failure, cardiomyopathy, and overall cardiac function.

### What is Segmentation?

In medical imaging, **segmentation** is the process of identifying and isolating specific anatomical structures within an image or video.

**In this context**: Segmentation refers to using AI to automatically detect and outline the boundaries of the left ventricle in echocardiogram videos.

**How it works**:
1. AI analyzes each frame of the echocardiogram video
2. Identifies the left ventricle region
3. Creates a **mask** (colored overlay) that highlights the ventricle
4. Tracks the ventricle's movement and size changes throughout the cardiac cycle

**Benefits**:
- **Automated**: Replaces manual tracing by cardiologists (saves time)
- **Consistent**: Reduces human variability and error
- **Real-time**: Enables faster diagnosis
- **Accurate**: AI can detect subtle patterns humans might miss

**Visual Output**: The segmentation mask appears as a colored region overlaid on the original grayscale echocardiogram, making the left ventricle clearly visible.

### Model Architecture

EchoNet-Dynamic builds and runs two models sequentially:

1. **Ejection Fraction Model (R2Plus1D-18)**
   - Parameters: 31.5M
   - Purpose: Estimates ejection fraction from video input

2. **Segmentation Model (DeepLabV3-ResNet50)**
   - Parameters: 39.6M
   - Purpose: Segments the left ventricle region

### Performance Goal

If KPIs are met, Intel Core Ultra meets or exceeds performance of **NVIDIA GTX 1080 Ti GPU**.

---

## System Requirements

- **Operating System**: Windows 11
- **Target Platforms**: Intel Core Ultra (PTL, NVL)
- **Hardware Acceleration**: iGPU, NPU, CPU

---

## Functional Requirements

### 1. iGPU Enablement and Optimization (Priority: P1)

**Objective**: Enable and optimize EchoNet-Dynamic on iGPU of Intel Core Ultra devices (PTL, NVL)

**Acceptance Criteria / KPIs**:

| Scope | Metric | Target |
|-------|--------|--------|
| End-to-End | Latency | < 50 msec |
| End-to-End | FPS | > 50 FPS |
| Segmentation Only | Latency | < 20 msec |
| Segmentation Only | FPS | > 50 FPS |

---

### 2. NPU Enablement and Optimization (Priority: P1)

**Objective**: Enable and optimize EchoNet-Dynamic on NPU (PTL, NVL)

**Acceptance Criteria / KPIs**:

| Scope | Metric | Target |
|-------|--------|--------|
| End-to-End | Latency | < 50 msec |
| End-to-End | FPS | > 50 FPS |
| Segmentation Only | Latency | < 20 msec |
| Segmentation Only | FPS | > 50 FPS |

---

### 3. Heterogeneous Compute UI / HW Engine Selection (Priority: P1)

**Objective**: Demonstrate the benefit of heterogeneous compute by creating a UI that allows the end user to select the hardware engine on which to run EchoNet-Dynamic.

**Hardware Options**:
- CPU
- iGPU (Integrated GPU)
- NPU (Neural Processing Unit)

**User Stories**:
- As a user, I can select which hardware engine to use for inference
- As a user, I can compare performance across different hardware engines
- As a user, I can see real-time performance metrics for my selected hardware

---

### 4. Data Visualization - Side-by-Side Video Display (Priority: P1)

**Objective**: Implement a data visualization layout for comparing original and segmented echocardiogram videos.

**Layout Specification**:

| Left Panel | Right Panel |
|------------|-------------|
| **Heading**: "Original" | **Heading**: "Segmentation" |
| Original grayscale echocardiogram video | Video clip with segmented mask of the left ventricle |

**Features**:
- Synchronized video playback
- Real-time segmentation visualization
- Clear visual distinction between original and processed output

---

### 5. Easy Deployment (Priority: P1)

**Objective**: Sample application deployment shall be easy and doable within 5 minutes for non-developer person.

**Requirements**:
- Simple installation process (minimal steps)
- Clear documentation
- Automated dependency management
- No complex configuration required
- One-click or script-based deployment

**Success Criteria**:
- Non-technical user can deploy the application in ≤ 5 minutes
- Minimal manual intervention required
- Clear error messages and troubleshooting guidance

---

## Technical Specifications

### Model Input
- **Format**: Echocardiogram video files
- **Color Space**: Grayscale
- **Expected Frame Rate**: TBD

### Model Output
- **Ejection Fraction**: Numerical value (percentage)
- **Segmentation Mask**: Binary mask highlighting left ventricle
- **Cardiomyopathy Assessment**: Classification result

### Performance Metrics

**End-to-End Pipeline**:
- Includes both models (Ejection Fraction + Segmentation)
- Target: < 50ms latency, > 50 FPS

**Segmentation Only**:
- Segmentation model in isolation
- Target: < 20ms latency, > 50 FPS

---

## Success Criteria Summary

✅ iGPU optimization meets or exceeds KPI targets  
✅ NPU optimization meets or exceeds KPI targets  
✅ User can select hardware engine via UI  
✅ Side-by-side visualization displays correctly  
✅ Non-developer can deploy within 5 minutes  
✅ Performance matches or exceeds GTX 1080 Ti baseline  

---

## Deliverables

1. Optimized EchoNet-Dynamic implementation for iGPU
2. Optimized EchoNet-Dynamic implementation for NPU
3. User interface with hardware selection
4. Side-by-side visualization component
5. Deployment package with documentation
6. Performance benchmarking results
7. User guide for deployment

---

## Timeline

*To be defined*

---

## Assumptions and Dependencies

- Access to Intel Core Ultra hardware (PTL, NVL platforms)
- Availability of EchoNet-Dynamic pre-trained models
- Access to echocardiogram test datasets
- Windows 11 development and testing environment
- Intel optimization libraries and tools availability

---

## Risks and Mitigation

| Risk | Impact | Mitigation Strategy |
|------|--------|---------------------|
| Performance targets not met on NPU | High | Early prototyping and optimization cycles |
| Model accuracy degradation after optimization | High | Continuous validation against baseline |
| Complex deployment process | Medium | Automated scripts and thorough testing |
| Hardware availability delays | Medium | Parallel development on available platforms |

---

**Document Version**: 1.0  
**Last Updated**: 2026-07-27  
**Status**: Draft
