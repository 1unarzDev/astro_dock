#!/usr/bin/env bash
echo "=== NVIDIA GPU / CUDA / ZED Check ==="

echo ""
echo "1. Check if NVIDIA kernel module is loaded"
lsmod | grep nvidia || echo "NVIDIA module not loaded"

echo ""
echo "2. Check if nvidia-smi works (desktop GPUs)"
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi
else
    echo "nvidia-smi not found"
fi

echo ""
echo "3. Check CUDA toolkit availability"
if command -v nvcc &>/dev/null; then
    nvcc --version
else
    echo "nvcc not found"
fi

echo ""
echo "4. Check CUDA devices using Python (if PyCUDA or torch available)"
python3 - <<'PY'
import ctypes

try:
    cuda = ctypes.CDLL("libcuda.so.1")
except OSError as error:
    print(f"Cannot load the CUDA driver: {error}")
else:
    count = ctypes.c_int()
    init_status = cuda.cuInit(0)
    count_status = cuda.cuDeviceGetCount(ctypes.byref(count)) if init_status == 0 else -1
    print(
        "CUDA driver initialization:",
        f"cuInit={init_status}",
        f"cuDeviceGetCount={count_status}",
        f"devices={count.value}",
    )
PY

echo ""
echo "5. List GPU devices via /dev (Jetson / embedded)"
ls -l /dev/nv* /dev/video* /dev/dri* 2>/dev/null || echo "No GPU / camera devices found under /dev"

echo ""
echo "6. Check EGL availability"
if command -v glxinfo &>/dev/null; then
    glxinfo | grep -i 'OpenGL'
else
    echo "glxinfo not installed"
fi

echo ""
echo "7. Test ZED SDK GPU allocation (if ZED SDK installed)"
if command -v zed_camera_info &>/dev/null; then
    zed_camera_info
else
    echo "zed_camera_info not installed or ZED SDK not available"
fi

echo ""
echo "8. Check environment variables for CUDA / GPU"
echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
echo "DISPLAY=$DISPLAY"
echo "EGL_PLATFORM=$EGL_PLATFORM"
echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"

echo ""
echo "=== GPU / CUDA / ZED Check Complete ==="
