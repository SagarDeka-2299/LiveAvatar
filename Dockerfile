# LiveAvatar — Docker image
# Base: NVIDIA CUDA 11.8 + cuDNN 8, Ubuntu 22.04
FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH="/root/.local/bin:$PATH"

# System packages: ffmpeg for audio/video processing, curl + git for UV install
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        git \
        libgl1 \
        libglib2.0-0 \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install UV (Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app

# Copy project definition first (layer-cache friendly)
COPY pyproject.toml uv.lock .python-version ./

# Install all Python dependencies into .venv (uses Python 3.10 from .python-version)
RUN uv sync --no-install-project

# Copy the rest of the source
COPY . .

# Create runtime directories
RUN mkdir -p uploads results/avatars

EXPOSE 8000

# models/ must be mounted as a volume (too large to bake in)
# docker run ... -v /host/models:/app/models
CMD ["uv", "run", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
