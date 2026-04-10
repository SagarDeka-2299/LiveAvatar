# MuseTalk Full Architecture

## Reconciling the Two Diagrams

| Feature | `musetalk_arc.jpg` (training diagram) | `musetalk.png` (inference pipeline) |
|---|---|---|
| Shows | Internal ML architecture | End-to-end video pipeline |
| VAE inputs | Reference Image + Masked Image both encoded | Implied (hidden inside "MuseTalk" block) |
| Concatenation | VAE(masked) ⊕ VAE(ref) → 8-ch input to UNet | Not shown explicitly |
| Face parsing | Not shown | Shown as separate path for blending mask |
| Blending | Not shown | Shown: MuseTalk output + mask → blend → paste |

Both diagrams are correct — they show different levels of abstraction.

---

## Component 1: Audio Processing — Whisper Encoder

**Class:** `WhisperModel` (HuggingFace `transformers`)  
**Defined in:** `musetalk/whisper/audio2feature.py`  
**Weights:** `models/whisper/pytorch_model.bin` (145 MB, OpenAI Whisper-tiny)

**Architecture:**
- 2× Conv1d (kernel=3, stride=2) → 4× `ResidualAttentionBlock` (d_model=384, heads=6, FFN=1536) → LayerNorm
- Frozen during MuseTalk training

**Tensor flow:**

| Stage | Shape | Dtype | Notes |
|---|---|---|---|
| Raw audio | `[N]` | float32 | 16 kHz mono PCM |
| Mel-spectrogram | `[1, 80, 3000]` | float32 | 30-second chunk, 80 mel bins |
| Whisper encoder output | `[1, 1500, 384]` | float32 | 5 intermediate layers extracted |
| Stacked layers | `[1, 1500, 5, 384]` | float32 | All encoder hidden states |
| Per-frame slice | `[num_frames, 10, 5, 384]` | float32 | 10 time steps × 5 layers |
| Reshaped | `[num_frames, 50, 384]` | float32 | **Final audio chunk per frame** |

**Chunk math:** at 25 fps, `whisper_idx_multiplier = 50/25 = 2.0`. With padding_left=2, padding_right=2: `(2+2+1) × 2 = 10` time steps. 10 × 5 layers = **50** Whisper tokens per video frame.

---

## Component 2: Positional Encoding

**Class:** `PositionalEncoding`  
**Defined in:** `musetalk/models/unet.py` (lines 12–27)  
**No weights** (fixed sinusoidal)

**Architecture:** Standard sinusoidal PE, d_model=384, max_len=5000

| Input | Output | Op |
|---|---|---|
| `[B, 50, 384]` float32 | `[B, 50, 384]` float16 | Element-wise add, cast to weight_dtype |

---

## Component 3: VAE Encoder

**Class:** `AutoencoderKL` (Stable Diffusion VAE)  
**Defined in:** `musetalk/models/vae.py`  
**Weights:** `models/sd-vae/` (full Stable Diffusion VAE)  
**Frozen** during MuseTalk training.

**Architecture:**
- 4 `DownEncoderBlock2D` stages: channels `[128, 256, 512, 512]`, 2 ResNet layers each
- 8× spatial downsampling total (256→32), 4-channel latent space
- `scaling_factor = 0.18215`

**Two images are encoded per frame:**

| Path | Image | Mask | Output Shape | Purpose |
|---|---|---|---|---|
| **Masked encode** | `[1, 3, 256, 256]` | zeros on bottom half (mouth hidden) | `[1, 4, 32, 32]` | Condition: "face without mouth" |
| **Reference encode** | `[1, 3, 256, 256]` | no mask | `[1, 4, 32, 32]` | Reference: full face |
| **Concatenate** | — | — | `[1, 8, 32, 32]` | **UNet input** |

Preprocessing: BGR→RGB, resize to 256×256 (LANCZOS4), normalize to `[-1, 1]` (mean=0.5, std=0.5).

The mask tensor is: `mask[:128, :] = 1, mask[128:, :] = 0` — upper half kept, lower half zeroed.

---

## Component 4: UNet — Backbone

**Class:** `UNet2DConditionModel`  
**Defined in:** `musetalk/models/unet.py` (lines 29–48)  
**Config:** `models/musetalkV15/musetalk.json`  
**Weights:** `models/musetalkV15/unet.pth` (3.2 GB) — **trainable**, this is the trained part  
**Dtype:** float16

**Architecture config:**
```
in_channels:          8     (concatenated masked + ref latents)
out_channels:         4     (predicted mouth latents)
cross_attention_dim:  384   (Whisper feature dim)
block_out_channels:   [320, 640, 1280, 1280]
layers_per_block:     2
attention_head_dim:   8
norm_num_groups:      32
sample_size:          64    (actual latent is 32×32)

Down blocks:
  CrossAttnDownBlock2D  ×3   (spatial conv + self-attn + cross-attn to audio)
  DownBlock2D           ×1   (spatial conv only, no attention)
Up blocks:
  UpBlock2D             ×1
  CrossAttnUpBlock2D    ×3
```

**Cross-attention (audio conditioning):** In each `CrossAttnDownBlock2D` / `CrossAttnUpBlock2D`:
- Q from spatial features `[B, C, H, W]` → flattened `[B, H×W, C]`
- K, V from audio `[B, 50, 384]`
- Result: each spatial position attends over all 50 audio tokens

| Input | Shape | Dtype |
|---|---|---|
| `latent_batch` | `[B, 8, 32, 32]` | float16 |
| `timesteps` | `[1]` = `tensor([0])` | int64 |
| `encoder_hidden_states` | `[B, 50, 384]` | float16 |
| **Output** `pred_latents` | `[B, 4, 32, 32]` | float16 |

**Important:** timestep is hardcoded to `0` — there is no diffusion denoising loop. The UNet is used as a single-step regression model, not iterative diffusion.

---

## Component 5: VAE Decoder

**Same `AutoencoderKL`**, decoder half. Frozen.

**Architecture:**
- 4 `UpDecoderBlock2D` stages, channels `[512, 512, 256, 128]`
- 8× spatial upsampling (32→256)

| Input | Shape | Op | Output Shape |
|---|---|---|---|
| `pred_latents` | `[B, 4, 32, 32]` | `÷ 0.18215` (unscale) | `[B, 4, 32, 32]` |
| Unscaled latents | `[B, 4, 32, 32]` | VAE decode | `[B, 3, 256, 256]` float32 |
| Decoded | `[B, 3, 256, 256]` | `x/2 + 0.5`, clamp, ×255 | `[B, 256, 256, 3]` uint8 BGR |

---

## Component 6: Face Detection & Landmark — DWPose + S3FD

**DWPose:**  
**Defined in:** `musetalk/utils/dwpose/`  
**Weights:** `models/dwpose/dw-ll_ucoco_384.pth`
- RTMPose-L, 133-keypoint wholebody pose estimation
- Input: full frame `[H, W, 3]`
- Output: `[133, 3]` keypoints (x, y, confidence)
- Face landmarks: keypoints `[23:91]` (68 facial keypoints)

**S3FD Face Detector:**  
**Defined in:** `musetalk/utils/face_detection/`
- Input: `[H, W, 3]`
- Output: `(x1, y1, x2, y2, confidence)` per detected face

**Preprocessing result per frame:**
```
coords: (x1, y1, x2, y2)   # face bounding box
```
Face crops are resized to `[256, 256]` before VAE encode.

---

## Component 7: Face Parsing — BiSeNet

**Class:** `BiSeNet`  
**Defined in:** `musetalk/utils/face_parsing/__init__.py`  
**Weights:** `models/face-parse-bisent/79999_iter.pth` + `resnet18-5c106cde.pth`

**Architecture:**
- ResNet18 backbone → feature pyramid: `[128,64,64]`, `[256,32,32]`, `[512,16,16]`
- ContextPath (multi-scale) + SpatialPath (high-res)
- FeatureFusionModule → 19-class segmentation head

| Input | Shape | Output Shape | Classes used |
|---|---|---|---|
| RGB image | `[1, 3, 512, 512]` | `[1, 19, 512, 512]` → argmax → `[512, 512]` | 1=face, 11=nose, 12=lip, 13=lip, 14=neck |

**Mode "jaw"** (default for inference): keeps class 1 (face) then erodes/dilates to isolate mouth region.

---

## Component 8: Blending

**Defined in:** `musetalk/utils/blending.py`

**Mask construction (pre-computed at avatar prep time):**
1. BiSeNet parsing → face region mask `[512, 512]`
2. Dilate with **cone kernel** (33×33, wider toward mouth)
3. Erode with **cheek kernel** (`cv2.MORPH_ELLIPSE`, 35×3 flat — removes horizontal cheek contamination)
4. Gaussian blur (kernel ≈ `0.1 × image_width`) → smooth alpha

**Blend formula:**
```
output = original_crop × (1 − mask) + generated_face × mask
```
Then paste crop back into full frame at `(x1, y1, x2, y2)`.

---

## End-to-End Data Flow

```
AUDIO PATH
──────────
.wav (16 kHz mono)
  → Mel-spectrogram [1, 80, 3000]          # 30s chunks
  → Whisper Encoder [1, 1500, 384]         # frozen
  → Slice per frame + stack layers
  → [num_frames, 50, 384]                  # audio tokens per frame
  → Positional Encoding (+PE, cast fp16)
  → [B, 50, 384]  ──────────────────────────────────┐
                                                      │ cross-attention (K,V)
IMAGE PATH                                            │
──────────                                            ▼
video frame [H, W, 3]                         UNet2DConditionModel
  → face detect (S3FD) → bbox (x1,y1,x2,y2)   - 3× CrossAttnDownBlock2D
  → crop + resize [256, 256, 3]                - 1× DownBlock2D
  → split into:                                - MidBlock
      masked (mouth hidden) [1,3,256,256]      - 1× UpBlock2D
      full reference        [1,3,256,256]      - 3× CrossAttnUpBlock2D
  → VAE Encode (frozen)                         ↓
      [1, 4, 32, 32] × 2                   [B, 4, 32, 32] pred_latents
  → concat → [B, 8, 32, 32] ───────────►       ↓
                               (Q from spatial) VAE Decode (frozen)
                                                ↓
                                           [B, 256, 256, 3] generated faces
                                                ↓
BLENDING PATH                                   ↓
─────────────                                   │
full frame [H, W, 3]                            │
  → face parse (BiSeNet)                        │
  → cone-dilate, cheek-erode, Gaussian blur     │
  → smooth alpha mask [H, W]                    │
  ← ─ ─ ─ ─ paste generated + blend ─ ─ ─ ─ ─ ┘
  → final frame [H, W, 3] BGR
```

---

## Weight Files Summary

| Component | File | Size | Trainable? |
|---|---|---|---|
| UNet (v15) | `models/musetalkV15/unet.pth` | 3.2 GB | **Yes** — the only trained part |
| VAE | `models/sd-vae/` | ~330 MB | Frozen (Stable Diffusion VAE) |
| Whisper Tiny | `models/whisper/pytorch_model.bin` | 145 MB | Frozen |
| BiSeNet face parser | `models/face-parse-bisent/79999_iter.pth` | ~50 MB | Frozen |
| ResNet18 backbone | `models/face-parse-bisent/resnet18-5c106cde.pth` | ~45 MB | Frozen |
| DWPose | `models/dwpose/dw-ll_ucoco_384.pth` | ~280 MB | Frozen |

---

## Key Configuration Parameters

```
--version:                    "v15"
--gpu_id:                     0
--vae_type:                   "sd-vae"
--unet_config:                "./models/musetalk/musetalk.json"
--unet_model_path:            "./models/musetalk/pytorch_model.bin"
--whisper_dir:                "./models/whisper"
--bbox_shift:                 0        # adjust face bbox vertical position
--fps:                        25       # output video frame rate
--audio_padding_length_left:  2        # audio context frames before current
--audio_padding_length_right: 2        # audio context frames after current
--batch_size:                 20       # inference batch size
--parsing_mode:               "jaw"    # face parsing region
--extra_margin:               10       # crop margin for v15
--left_cheek_width:           90       # cheek protection width (px)
--right_cheek_width:          90
```

---

## Streaming Real-Time Mode

From `server.py`:

```
INPUT_SR = 16000                  # Input audio sample rate
STREAM_FPS = 20                   # Output video FPS
STREAM_CHUNK_FRAMES = 20          # 1-second window (20 frames)
STREAM_HOP_FRAMES = 5             # 250 ms hop (5 frames)
window_samples = 16000            # 1 second of audio
hop_samples = 4000                # 250 ms of audio
```

Output packet format:
```
[uint32 frame_idx][uint32 jpeg_len][uint32 pcm_samples][jpeg_data][pcm_f32_data]
```

---

## torch.compile Targets

```python
unet.model         = torch.compile(unet.model,         mode="reduce-overhead", fullgraph=False)
vae.vae.decoder    = torch.compile(vae.vae.decoder,    mode="reduce-overhead", fullgraph=False)
whisper.encoder    = torch.compile(whisper.encoder,    mode="reduce-overhead", fullgraph=False)
```

Warm-up input shapes:
- UNet: `[20, 8, 32, 32]` latents + `[20, 50, 384]` audio
- VAE decoder: `[20, 4, 32, 32]`
- Whisper: `[1, 80, 3000]`

---

## Optimization Observations

1. **Single-step UNet** — timestep=0 means no denoising loop. The UNet is a single-pass conditional regression model. This is the key reason it is real-time capable.
2. **8-channel bottleneck** — concatenating masked+ref latents doubles the UNet's effective input. Potential: replace with feature-level FiLM conditioning to halve channel count.
3. **50 audio tokens per frame** — 10 time steps × 5 Whisper layers. Reducing layers (use only last 1–2 layers) would cut audio cross-attention cost by 60–80%.
4. **VAE reference latent caching** — the upper-half reference latent is constant across all frames for a given avatar. It can be computed once and reused, saving one VAE encode per frame.
5. **Blending is CPU-bound** — BiSeNet parsing + mask morphology runs on CPU in the hot path; could be moved to GPU or pre-cached per avatar.
6. **Batch size** — default batch_size=20 processes 1 second of 20 fps video per UNet call, amortizing CUDA launch overhead effectively.
