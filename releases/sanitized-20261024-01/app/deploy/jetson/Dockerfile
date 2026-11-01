# Jetson PolicyServer runtime.  The base image already contains the Jetson
# CUDA build of PyTorch; do not replace it with a generic aarch64/x86 wheel.
FROM nvcr.io/nvidia/pytorch:25.06-py3-igpu

# Only the PolicyServer/LeRobot import dependencies are added.  The LeRobot
# source itself is bind-mounted from the isolated test directory at run time.
RUN pip install --no-cache-dir \
      "grpcio>=1.73.1,<2.0.0" \
      "protobuf>=6.31.1,<8.0.0" \
      "draccus>=0.8" \
      "huggingface_hub<2" \
      "av>=14" \
      "einops" \
      "jsonlines" \
      "pyyaml" \
      "scipy" \
    && pip install --no-cache-dir "numpy==1.26.4"

WORKDIR /work
