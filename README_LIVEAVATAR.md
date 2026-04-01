# LiveAvatar

Real-time lip-sync streaming built on [MuseTalk v1.5](https://github.com/TMElyralab/MuseTalk).  
Upload a reference video once to create an avatar, then stream any audio and watch the synced talking-head video play back live in the browser — audio and video arrive already synced from the server.

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

## Step-by-step setup

### 1. Clone the repo

```bash
git clone https://github.com/SagarDeka-2299/LiveAvatar.git
cd LiveAvatar
```

### 2. Install UV

UV is the only system-level dependency. It manages Python 3.10 and all packages in an isolated virtualenv.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env   # or restart your shell
```

Verify:
```bash
uv --version
```

### 3. Install all Python dependencies

This creates `.venv/` with Python 3.10 and installs all pinned packages (torch, diffusers, mmcv, etc.).

```bash
uv sync
```

> **Note:** `mmcv` is fetched from the OpenMMLab CDN wheel for CUDA 11.8 / PyTorch 2.0.  
> If your CUDA version differs, edit the `mmcv` URL in `pyproject.toml` — see [OpenMMLab CDN](https://download.openmmlab.com/mmcv/dist/).

### 4. Make ffmpeg available

The bundled `imageio_ffmpeg` binary needs to be on PATH:

```bash
mkdir -p ~/bin
ln -sf "$(uv run python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" ~/bin/ffmpeg
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Verify:
```bash
ffmpeg -version | head -1
```

### 5. Download model weights

```bash
uv run python scripts/download_weights.py
```

This downloads all required models into `./models/`:

```
models/
├── musetalk/           # UNet + VAE weights
├── whisper/            # Whisper encoder (tiny)
├── dwpose/             # DWPose face/body detector
├── face-parse-bisenet/ # BiSeNet face parser
└── sd-vae-ft-mse/      # Stable Diffusion VAE
```

> If the script fails for any weight, you can download manually — see `scripts/download_weights.py` for the source URLs.

### 6. (Optional) Verify GPU access

```bash
uv run python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

---

## Running the server

```bash
PATH="$HOME/bin:$PATH" uv run uvicorn server:app --host 0.0.0.0 --port 7860
```

On first start the server loads all models into GPU memory (~3–4 GB VRAM). This takes about 30–60 seconds. You will see:

```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:7860
```

To run in the background and keep logs:

```bash
PATH="$HOME/bin:$PATH" nohup uv run uvicorn server:app --host 0.0.0.0 --port 7860 > server.log 2>&1 &
tail -f server.log
```

---

## Using the web UI

Open **http://\<your-server-ip\>:7860** in a browser.

### Create an avatar

1. Go to the **Avatars** tab.
2. Click **+ New Avatar**.
3. Enter a name and select a reference video (MP4/MOV/AVI, any resolution, 25fps+ recommended).
4. Click **Create** — face landmarks are extracted and VAE-encoded. This takes **1–5 minutes** depending on video length. A progress indicator shows while it runs.
5. Once complete the avatar card appears in the grid with a thumbnail.

Avatars are saved to disk (`./results/avatars/`) and indexed in `avatars.db` (SQLite). They persist across server restarts.

### Stream audio

1. Go to the **Stream** tab.
2. Click an avatar card to select it (purple border = selected).
3. Click **Choose audio file** and pick a WAV, MP3, FLAC, or OGG file.
4. Click **▶ Stream**.
5. The server processes the audio through Whisper and the UNet, buffers the first batch of frames, then streams binary packets. Playback starts automatically once buffered — audio and video are in sync from the first frame.

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
│   └── index.html              # Single-page browser UI
├── pyproject.toml              # UV project — all pinned dependencies
├── uv.lock                     # Exact lock file
├── .python-version             # Pins Python 3.10 for UV
├── avatars.db                  # SQLite — avatar metadata (created at runtime)
├── uploads/                    # Temp storage for uploaded videos/audio
├── results/avatars/            # Prepared avatar data (VAE latents, masks, frames)
├── models/                     # Downloaded model weights (not in git)
├── musetalk/                   # MuseTalk model code
└── scripts/
    ├── realtime_inference.py   # Avatar class (preparation + inference loop)
    └── download_weights.py     # Weight downloader
```

---

## Architecture

```
Browser                          Server
──────                           ──────
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

Canvas + Web Audio API
  scheduleAudioChunk(pcm, frameIdx)  → src.start(playStart + frameIdx/fps)
  renderLoop()                       → drawImage at floor(elapsed * fps)
```

---

## Troubleshooting

**`ffmpeg not found`**  
Re-run step 4. Check `which ffmpeg` returns `~/bin/ffmpeg`.

**`pkg_resources` / `mmengine` import error**  
`setuptools` was upgraded beyond 69.x. Run: `uv add setuptools==69.5.1`

**`mmcv` install fails with 404**  
The CDN URL in `pyproject.toml` uses `torch2.0.0/`. Do not change it to `torch2.0.1` — that folder does not exist on the CDN.

**Avatar preparation hangs forever**  
Check `server.log` — a missing model weight usually causes a silent failure. Re-run `python scripts/download_weights.py`.

**Black canvas / no video**  
Check the browser console for WebSocket errors. Usually means the server errored during inference — check `server.log`.

**VRAM OOM**  
Reduce `BATCH_SIZE` in `server.py` (line near top, default 4). Setting it to 2 halves peak VRAM usage at the cost of slightly higher first-frame latency.

---

## License

Model weights and original MuseTalk code are subject to the [MuseTalk license](https://github.com/TMElyralab/MuseTalk/blob/main/LICENSE).  
The streaming server (`server.py`) and UI (`static/index.html`) added in this repo are MIT licensed.
