# LiveAvatar

Real-time lip-sync streaming built on [MuseTalk v1.5](https://github.com/TMElyralab/MuseTalk).

Upload a reference video once to create an avatar, then stream any audio and watch the synced talking-head video play back live in the browser — audio and video arrive already synced from the server.

**Original MuseTalk paper:** [MuseTalk: Real-Time High-Fidelity Video Dubbing via Spatio-Temporal Sampling](https://arxiv.org/abs/2410.10122)  
**Original repo:** [TMElyralab/MuseTalk](https://github.com/TMElyralab/MuseTalk) — Lyra Lab, Tencent Music Entertainment

---

## How it works

1. **Avatar preparation** — face detection, VAE encoding of all frames, and mask generation are pre-computed once and saved to disk.
2. **Streaming inference** — for each audio file the server runs Whisper + UNet + VAE decode, packs each frame's JPEG with its matching PCM audio chunk, and streams binary WebSocket packets to the browser.
3. **Browser playback** — the Web Audio API schedules each audio chunk at `t = frame_index / fps` and a canvas render loop draws the matching JPEG at the same timestamp, giving sample-accurate sync.

---

## Requirements

| Requirement | Version |
|---|---|
| OS | Linux (Ubuntu 20.04+ recommended) |
| GPU | NVIDIA, CUDA 11.8 |
| VRAM | ≥ 8 GB recommended |
| Python | 3.10 (managed by UV) |
| UV | latest |

---

## Setup (without Docker)

### 1. Clone the repo

```bash
git clone https://github.com/SagarDeka-2299/LiveAvatar.git
cd LiveAvatar
```

### 2. Install UV

UV is the only system-level dependency. It manages Python 3.10 and all packages in an isolated virtualenv.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env   # or restart your shell
```

Verify:
```bash
uv --version
```

### 3. Install all Python dependencies

```bash
uv sync
```

This creates `.venv/` with Python 3.10 and installs all pinned packages (torch, diffusers, mmcv, etc.).

> **Note:** `mmcv` is fetched from the OpenMMLab CDN wheel for CUDA 11.8 / PyTorch 2.0.
> If your CUDA version differs, edit the `mmcv` URL in `pyproject.toml` — see [OpenMMLab CDN](https://download.openmmlab.com/mmcv/dist/).

### 4. Download model weights

```bash
bash download_weights.sh
```

This downloads all required models into `./models/` (~8.7 GB total):

```
models/
├── musetalk/           # MuseTalk v1.0 UNet (kept for compatibility)
├── musetalkV15/        # MuseTalk v1.5 UNet (used by server)
├── sd-vae/             # Stable Diffusion VAE
├── whisper/            # Whisper-tiny encoder
├── dwpose/             # DWPose face/body detector
├── face-parse-bisent/  # BiSeNet face parser
└── syncnet/            # LatentSync SyncNet (optional)
```

### 5. Verify GPU access (optional)

```bash
uv run python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### 6. Run the server

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 7860
```

On first start the server loads all models into GPU memory (~3–4 GB VRAM). This takes about 30–60 seconds.

To run in the background:

```bash
nohup uv run uvicorn server:app --host 0.0.0.0 --port 7860 > server.log 2>&1 &
tail -f server.log
```

---

## Setup (Docker)

### Prerequisites

- [Docker](https://docs.docker.com/engine/install/) with [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
- Model weights already downloaded on the host (run `bash download_weights.sh` first)

### Build and run

```bash
# Download model weights to host first
bash download_weights.sh

# Build image (~5–10 min on first build)
docker compose build

# Start the service
docker compose up -d

# Follow logs
docker compose logs -f
```

Open **http://localhost:7860** in your browser.

### Stopping

```bash
docker compose down
```

Avatars, results, and uploads are persisted via volume mounts and survive restarts.

---

## Using the web UI

Open **http://\<your-server-ip\>:7860** in a browser.

### Create an avatar

1. Go to the **Avatars** tab.
2. Click **+ New Avatar**.
3. Enter a name and select a reference video (MP4/MOV/AVI, any resolution, 25 fps+ recommended).
4. Click **Create** — face landmarks are extracted and VAE-encoded. This takes **1–5 minutes** depending on video length.
5. Once complete the avatar card appears in the grid with a thumbnail.

Avatars are saved to `./results/avatars/` and indexed in `avatars.db` (SQLite). They persist across server restarts.

### Stream audio

1. Go to the **Stream** tab.
2. Click an avatar card to select it.
3. Click **Choose audio file** and pick a WAV, MP3, FLAC, or OGG file.
4. Click **▶ Stream**.
5. Playback starts automatically once buffered — audio and video are in sync from the first frame.

---

## Input formats

| Type | Supported formats | Notes |
|---|---|---|
| Reference video | MP4, MOV, AVI, MKV | Any resolution; face must be clearly visible |
| Audio | WAV, MP3, FLAC, OGG | Any sample rate; mono or stereo |

---

## Project structure

```
LiveAvatar/
├── server.py                   # FastAPI server — model loading, avatar CRUD, WS streaming
├── static/
│   └── index.html              # Single-page browser UI (pure HTML/CSS/JS, no framework)
├── scripts/
│   └── realtime_inference.py   # Avatar class — preparation and streaming inference loop
├── musetalk/                   # MuseTalk model code (inference-only; training code removed)
│   ├── models/                 # UNet, VAE model definitions
│   ├── utils/                  # Face detection, parsing, blending, audio processing
│   └── whisper/                # Whisper encoder (feature extraction only)
├── configs/inference/          # Inference YAML configs
├── models/                     # Downloaded model weights (not in git, ~8.7 GB)
├── pyproject.toml              # UV project — all pinned dependencies
├── uv.lock                     # Exact lock file
├── .python-version             # Pins Python 3.10 for UV
├── download_weights.sh         # Downloads all model weights
├── Dockerfile                  # Docker build definition
├── compose.yaml                # Docker Compose service definition
├── avatars.db                  # SQLite avatar metadata (created at runtime)
├── uploads/                    # Temp storage for uploaded videos/audio
└── results/avatars/            # Prepared avatar data (VAE latents, masks, frames)
```

---

## Architecture

### System overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MUSETALK LIVEAVATAR SYSTEM                          │
│                                                                             │
│   PREP PHASE (one-time per avatar)                                          │
│   ┌──────────┐    ┌───────────────────┐    ┌──────────────────────────┐    │
│   │Reference │───▶│  Face Detection   │───▶│  VAE Encoder             │    │
│   │  Video   │    │  + DWPose (133kp) │    │  → 8ch Latents [N,8,32,32│    │
│   └──────────┘    └───────────────────┘    └──────────────────────────┘    │
│                            │                         │                      │
│                   ┌────────▼────────┐       ┌────────▼────────┐            │
│                   │  Face Parsing   │       │  Saved to disk  │            │
│                   │  (BiSeNet 19cl) │       │  latents.pt     │            │
│                   │  → Blend Masks  │       │  coords.pkl     │            │
│                   └─────────────────┘       └─────────────────┘            │
│                                                                             │
│   STREAMING INFERENCE PHASE                                                 │
│   ┌──────────┐    ┌────────────────┐    ┌──────────┐    ┌──────────────┐  │
│   │  Input   │───▶│ Whisper Encoder│───▶│   PE +   │───▶│  UNet 2D     │  │
│   │  Audio   │    │ (4L,6H,d=384)  │    │ Pos.Enc. │    │  Condition   │  │
│   │  chunks  │    │ →[T,50,384]    │    │ [T,50,384│    │  [B,8,32,32] │  │
│   └──────────┘    └────────────────┘    └──────────┘    └──────┬───────┘  │
│                                                                  │          │
│   ┌──────────┐    ┌──────────────┐    ┌──────────────┐          │          │
│   │  Output  │◀───│   Blending   │◀───│ VAE Decoder  │◀─────────┘          │
│   │ Frames + │    │  (BiSeNet +  │    │ [B,4,32,32]  │                     │
│   │   PCM    │    │  Gauss.Blur) │    │ →[B,256,256] │                     │
│   └──────────┘    └──────────────┘    └──────────────┘                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### WebSocket protocol

```
Browser                          Server
───────                          ──────
WS: {type:"prepare", name}  ──►  save video → extract frames → VAE encode
         + video bytes           → save to avatars.db
◄── {type:"preparing"} (heartbeat every 4s)
◄── {type:"ready", avatar:{id, name}}

WS: {type:"stream", avatar_id} ──► load avatar from cache/disk
         + audio bytes            → librosa decode PCM
                                  → Whisper encoder
                                  → UNet + VAE decode (batch_size=4)
◄── {type:"stream_meta", fps, sample_rate, total_frames}
◄── binary[frameIdx|jpegLen|pcmCount|JPEG|PCM float32]  × N frames
◄── {type:"done"}
```

Binary packet layout per frame:
```
[uint32 frame_idx][uint32 jpeg_len][uint32 pcm_samples][JPEG bytes][PCM f32 LE]
```

### Component breakdown

| Component | Model | Role |
|---|---|---|
| VAE | Stable Diffusion VAE (`sd-vae`) | Encodes 256×256 face crops → [8,32,32] latents; decodes UNet output back to RGB |
| UNet | MuseTalk v1.5 (`musetalkV15`) | Single-step inpainting in latent space conditioned on Whisper audio embeddings |
| Whisper | `whisper-tiny` encoder only | Extracts [50, 384] audio features per 1-second window (no decoding/transcription) |
| DWPose | `dw-ll_ucoco_384` | 133-keypoint face/body detector used during avatar preparation |
| BiSeNet | `face-parse-bisent` | 19-class face parsing for blending masks |
| Face blending | Gaussian blur composite | Seamlessly composites generated face back into original frame |

### VAE latent layout

The UNet receives an 8-channel input formed by concatenating two 4-channel latents:
- **Channels 0–3:** masked face (upper half zeroed) — tells the UNet *what region to fill*
- **Channels 4–7:** full reference latent — encodes the person's identity (skin tone, jaw shape)

This is **not** a diffusion model. The UNet runs a single forward pass at `timestep=0`.

### Client playback

```
Web Audio API:
  scheduleAudioChunk(pcm, frameIdx) → audioNode.start(playStart + frameIdx/fps)

Canvas render loop:
  elapsed = audioContext.currentTime - playStart
  frameIdx = floor(elapsed * fps)
  ctx.drawImage(frames[frameIdx])
```

This gives sample-accurate audio/video sync without any media container.

---

## Troubleshooting

**`mmcv` install fails with 404**
The CDN URL in `pyproject.toml` uses `torch2.0.0/`. Do not change it to `torch2.0.1` — that folder does not exist on the CDN.

**`pkg_resources` / `mmengine` import error**
`setuptools` was upgraded beyond 69.x. The lock file pins it to 69.5.1; if you diverged, run `uv sync` again.

**Avatar preparation hangs forever**
Check `server.log` — a missing model weight usually causes a silent failure. Re-run `bash download_weights.sh`.

**Black canvas / no video**
Check the browser console for WebSocket errors. Usually means the server errored during inference — check `server.log`.

**VRAM OOM**
Reduce `BATCH_SIZE` at the top of `server.py` (default 4). Setting it to 2 halves peak VRAM usage at the cost of slightly higher first-frame latency.

**`ffmpeg not found` (bare metal setup)**
`imageio[ffmpeg]` bundles ffmpeg and `server.py` adds it to PATH automatically. If something still fails, verify with:
```bash
uv run python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
```

---

## Model notes

MuseTalk was trained on HDTF and proprietary datasets. Key facts:
- Supports Chinese, English, Japanese, and other languages (audio-driven, language-agnostic)
- Face region size: 256×256
- v1.5 adds GAN loss, perceptual loss, and sync loss — significantly better quality than v1.0
- Known limitations: slight jitter (single-frame generation), partial identity drift on mustaches/lip color

---

## License

- **Original MuseTalk code and weights:** [MIT License](LICENSE) — Lyra Lab, Tencent Music Entertainment
- **LiveAvatar additions** (`server.py`, `static/index.html`): MIT licensed
- Third-party components (whisper, dwpose, face-parsing, S3FD) are subject to their respective licenses

---

## Citation

```bibtex
@article{musetalk,
  title={MuseTalk: Real-Time High-Fidelity Video Dubbing via Spatio-Temporal Sampling},
  author={Zhang, Yue and Zhong, Zhizhou and Liu, Minhao and Chen, Zhaokang and Wu, Bin
          and Zeng, Yubin and Zhan, Chao and He, Yingjie and Huang, Junxin and Zhou, Wenjiang},
  journal={arxiv},
  year={2025}
}
```
