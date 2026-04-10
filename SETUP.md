# LiveAvatar — Fresh Machine Setup Guide

Tested on: **Ubuntu 24.04 LTS** with **NVIDIA Tesla V100 16GB** (GCP instance).

---

## Prerequisites

- Ubuntu 22.04 or 24.04 LTS
- NVIDIA GPU with CUDA 11.8+ support (≥8 GB VRAM recommended, 16 GB for comfort)
- ~30 GB free disk space (8.7 GB models + Docker image layers)
- Internet access (HuggingFace mirror is used for model downloads)

---

## Step 1 — Install Docker

```bash
curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sudo sh /tmp/get-docker.sh
sudo systemctl start docker
sudo systemctl enable docker

# Verify
sudo docker --version
sudo docker compose version
```

---

## Step 2 — Install NVIDIA Drivers

```bash
sudo apt-get update
sudo apt-get install -y ubuntu-drivers-common linux-headers-$(uname -r)

# Detect available drivers
sudo ubuntu-drivers devices

# Install the recommended driver (535 as of this writing)
sudo apt-get install -y nvidia-driver-535

# Load the kernel module (no reboot needed if the module loads cleanly)
sudo modprobe nvidia

# Verify GPU is visible
nvidia-smi
```

> If `nvidia-smi` still fails after `modprobe`, reboot the machine (`sudo reboot`) and try again.

---

## Step 3 — Install nvidia-container-toolkit (Docker GPU passthrough)

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# Configure Docker to use the NVIDIA runtime
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify GPU is accessible inside Docker
sudo docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

---

## Step 4 — Install uv (Python package manager)

Needed only to run the model download script on the host.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env   # adds uv to PATH for current shell
uv --version
```

---

## Step 5 — Clone the project

```bash
git clone <repo-url> /opt/shared/LiveAvatar
cd /opt/shared/LiveAvatar
```

---

## Step 6 — Fix directory permissions (if needed)

If the repo is owned by root/devteam and your user can't write:

```bash
sudo chown <your-user>:<your-group> /opt/shared/LiveAvatar/.venv 2>/dev/null || \
  sudo mkdir -p /opt/shared/LiveAvatar/.venv && \
  sudo chown <your-user>:<your-user> /opt/shared/LiveAvatar/.venv

sudo mkdir -p /opt/shared/LiveAvatar/uploads
sudo chown <your-user>:<your-user> /opt/shared/LiveAvatar/uploads
```

Install project dependencies into `.venv` (required so the download script can use `uv run`):

```bash
cd /opt/shared/LiveAvatar
uv sync --no-install-project
```

---

## Step 7 — Download model weights (~8.7 GB)

Run all downloads in parallel to save time:

```bash
cd /opt/shared/LiveAvatar

# Create model directories
mkdir -p models/{musetalk,musetalkV15,syncnet,dwpose,face-parse-bisent,sd-vae,whisper}

export HF_ENDPOINT=https://hf-mirror.com   # HuggingFace mirror — remove if you have direct HF access

# MuseTalk V1.0
uv run huggingface-cli download TMElyralab/MuseTalk \
  --local-dir models \
  --include "musetalk/musetalk.json" "musetalk/pytorch_model.bin" &

# MuseTalk V1.5
uv run huggingface-cli download TMElyralab/MuseTalk \
  --local-dir models \
  --include "musetalkV15/musetalk.json" "musetalkV15/unet.pth" &

# Stable Diffusion VAE
uv run huggingface-cli download stabilityai/sd-vae-ft-mse \
  --local-dir models/sd-vae \
  --include "config.json" "diffusion_pytorch_model.bin" &

# Whisper tiny
uv run huggingface-cli download openai/whisper-tiny \
  --local-dir models/whisper \
  --include "config.json" "pytorch_model.bin" "preprocessor_config.json" &

# DWPose
uv run huggingface-cli download yzd-v/DWPose \
  --local-dir models/dwpose \
  --include "dw-ll_ucoco_384.pth" &

# LatentSync SyncNet
uv run huggingface-cli download ByteDance/LatentSync \
  --local-dir models/syncnet \
  --include "latentsync_syncnet.pt" &

# Face parse BiSeNet (Google Drive + PyTorch CDN)
uv run gdown --id 154JgKpzCPW82qINcVieuPH3fZ2e0P812 \
  -O models/face-parse-bisent/79999_iter.pth &
curl -L https://download.pytorch.org/models/resnet18-5c106cde.pth \
  -o models/face-parse-bisent/resnet18-5c106cde.pth &

wait
echo "All models downloaded."
```

Verify total size should be ~8.7 GB:

```bash
du -sh models/
```

---

## Step 8 — Config changes from original (already applied in this repo)

Two files were modified from the original to expose on port **8000** instead of 7860:

**Dockerfile** — changed base image port and CMD:
```
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

**compose.yaml** — changed port mapping:
```yaml
ports:
  - "8000:8000"
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8000/"]
```

No other changes were needed — the original project files work as-is.

---

## Step 9 — Build and run

```bash
cd /opt/shared/LiveAvatar

# Build the Docker image (takes 5-10 min first time — downloads PyTorch CUDA wheels)
sudo docker compose build

# Start in background
sudo docker compose up -d

# Watch logs during startup (model loading takes ~1-2 min)
sudo docker compose logs -f
```

The container is healthy when you see:
```
[WARMUP] Done in ...s
```
or the healthcheck passes (visible in `sudo docker compose ps`).

---

## Step 10 — Verify

```bash
# Container status
sudo docker compose ps

# GPU usage (should show ~7-8 GB VRAM used)
nvidia-smi

# HTTP check
curl http://localhost:8000/
curl http://localhost:8000/avatars
```

Open **http://localhost:8000** in a browser.

---

## Management commands

```bash
# Stop
sudo docker compose down

# Stop and remove all data volumes (avatars, caches)
sudo docker compose down -v

# View live logs
sudo docker compose logs -f

# Restart
sudo docker compose restart

# Rebuild after code changes
sudo docker compose build && sudo docker compose up -d
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `nvidia-smi` fails after driver install | Run `sudo modprobe nvidia` or reboot |
| Docker can't see GPU | Re-run `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| `uv sync` fails with permission denied | `sudo mkdir -p .venv && sudo chown $USER:$USER .venv` |
| Container exits immediately | Check `sudo docker compose logs` — likely missing model files in `./models/` |
| Port 8000 not accessible | Check `sudo docker compose ps` — ensure status is `Up (healthy)` not `Up (starting)` |
| OOM / CUDA out of memory | Reduce `BATCH_SIZE` in `server.py` (default 4), rebuild |
