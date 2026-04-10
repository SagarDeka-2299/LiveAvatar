# MuseTalk Full Architecture Reference

> **Note on the two diagrams:**  
> `musetalk_arc.jpg` shows the **training-time view**: three separate encoders (VAE for masked face, VAE for reference face, Whisper for audio), their latents concatenated and fed into UNet, with a VAE decoder reconstructing the face and a training loss computed against ground truth.  
> `musetalk.png` shows the **inference/application-level view**: a full pipeline frame where face parsing produces a mask, MuseTalk generates an animated face crop, and that crop is blended back onto the original full-resolution frame.  
> Both are correct — they describe the same system at different abstraction levels. This document reconciles both and covers every component in full detail.

---

## Table of Contents

1. [System Overview & Two-Phase Architecture](#1-system-overview)
2. [Component 1: Audio Preprocessor (librosa + Whisper feature extractor)](#2-audio-preprocessor)
3. [Component 2: Whisper Encoder (openai/whisper-tiny)](#3-whisper-encoder)
4. [Component 3: Positional Encoding (PE)](#4-positional-encoding)
5. [Component 4: SD-VAE Encoder](#5-sd-vae-encoder)
6. [Component 5: UNet2DConditionModel (Backbone)](#6-unet-backbone)
7. [Component 6: SD-VAE Decoder](#7-sd-vae-decoder)
8. [Component 7: DWPose Face Detector + Landmark Extractor](#8-dwpose-landmark-extractor)
9. [Component 8: FaceAlignment (face_detection)](#9-facealignment)
10. [Component 9: BiSeNet Face Parser](#10-bisenet-face-parser)
11. [Complete Inference Data Flow](#11-complete-inference-data-flow)
12. [Avatar Preparation Phase (Offline)](#12-avatar-preparation-phase)
13. [Real-Time Streaming Pipeline](#13-real-time-streaming-pipeline)
14. [Weight Files Summary](#14-weight-files-summary)
15. [Optimisation Notes](#15-optimisation-notes)

---

## 1. System Overview

MuseTalk as implemented in this codebase operates in **two phases**:

### Phase A — Avatar Preparation (offline, once per avatar video)
```
Input video
  → Frame extraction (video2imgs)
  → Face landmark detection (DWPose + FaceAlignment)
  → Crop face region (256×256)
  → VAE encode masked frame   → latents[i]  (shape [1, 4, 32, 32])
  → VAE encode full ref frame → latents[i]  (shape [1, 4, 32, 32])
  → Concatenate               → input_latent [1, 8, 32, 32]
  → Save all input_latent_list_cycle to disk (.pt)
  → BiSeNet face parsing → blending masks → saved to disk
```

### Phase B — Inference (per audio clip or real-time stream)
```
Audio input (wav/mp3/pcm)
  → librosa resample to 16 kHz
  → Whisper mel spectrogram (80-bin, 30s segments)
  → WhisperModel.encoder (tiny, 4 layers)
  → stack all 5 hidden states → per-frame whisper_chunk [B, 50, 384]
  → PositionalEncoding        → audio_feature [B, 50, 384]

Avatar latents (pre-computed)
  → input_latent_batch [B, 8, 32, 32]

UNet2DConditionModel (8-channel input)
  → cross-attention with audio_feature
  → pred_latent [B, 4, 32, 32]

VAE Decoder
  → predicted face image [B, 3, 256, 256] (BGR numpy)

Blending
  → resize face to original bbox dimensions
  → BiSeNet-mask-weighted paste onto full frame
  → Output frame (full resolution video)
```

---

## 2. Audio Preprocessor

**Name:** `AudioProcessor`  
**Purpose:** Convert any raw audio file into Whisper-ready mel spectrogram features. Acts as the adapter between the audio domain and the MuseTalk feature space.  
**Defined in:** `musetalk/utils/audio_processor.py`  
**Weights:** None (uses HuggingFace `AutoFeatureExtractor`, config-only)  
**Config file:** `models/whisper/preprocessor_config.json` (184 KB, contains filterbank coefficients and normalization constants)

### Architecture
- Wraps HuggingFace `AutoFeatureExtractor` for `openai/whisper-tiny`
- Internally: log-mel spectrogram with 80 mel bins, 25ms window, 10ms hop
- Audio is loaded at exactly **16 kHz mono** via `librosa.load`
- Audio is split into **30-second segments** to match Whisper's max receptive field

### Key Methods

#### `get_audio_feature(wav_path) → (List[Tensor], int)`
| Stage | Shape | dtype | Notes |
|-------|-------|-------|-------|
| Raw audio (librosa) | `[N_samples]` | float32 | N = duration × 16000 |
| Per 30s segment | `[480000]` | float32 | 30×16000 |
| `input_features` (per seg) | `[1, 80, 3000]` | float32/fp16 | mel bins × time frames |

Returns: `(list_of_[1,80,3000]_tensors, total_sample_count)`

#### `get_whisper_chunk(features, …, fps, pad_L=2, pad_R=2) → Tensor[T, 50, 384]`

This is the critical temporal alignment function:

1. Pass each `[1, 80, 3000]` through `whisper.encoder` → 5 hidden states (layers 0–4), each `[1, T_audio, 384]`
2. Stack the 5 hidden states on dim 2: `[1, T_audio, 5, 384]`  
   *(note: the code reshapes to `[T, 10, 5, 384]` then `[T, 50, 384]` via `rearrange`)*
3. Trim to `actual_length = floor(duration_sec × 50)` frames at 50 fps
4. Add zero-padding: `pad_L × ceil(50/fps)` at start, `3 × pad_R × ceil(50/fps)` at end
5. For each video frame `f` at output FPS: `audio_index = floor(f × (50/fps))`; slice `audio[audio_index : audio_index + audio_feature_length_per_frame]` where `audio_feature_length_per_frame = 2 × (pad_L + pad_R + 1) = 10`
6. Rearrange: `[T, 10, 5, 384] → [T, 50, 384]` via `rearrange('b c h w -> b (c h) w')`

| Stage | Shape | Notes |
|-------|-------|-------|
| Whisper hidden states (stacked) | `[1, T_50fps, 5, 384]` | 5 transformer layers |
| Per-frame audio clip | `[1, 10, 5, 384]` | 10 audio frames centred on video frame |
| After rearrange | `[1, 50, 384]` | flattened into sequence dim |
| Full batch | `[B, 50, 384]` | B frames in a batch |

**Chunk size:** Each video frame corresponds to 1 audio feature vector of shape `[50, 384]`.  
The `50` comes from `2 × (2+2+1) = 10` audio frames × `5` Whisper hidden layers = 50 time-step slots.

---

## 3. Whisper Encoder

**Name:** `WhisperModel.encoder` (openai/whisper-tiny)  
**Purpose:** Extract rich multi-layer audio representations from mel spectrogram. Only the encoder half is used — the decoder is discarded.  
**Defined in:** HuggingFace `transformers.WhisperModel`  
**Class file:** `transformers/models/whisper/modeling_whisper.py` (external library)  
**Weights file:** `models/whisper/pytorch_model.bin` (≈ 144 MB)  
**Config file:** `models/whisper/config.json`

### Architecture (Whisper-tiny encoder)

```
Input: [B, 80, 3000]  mel spectrogram (80 bins × 3000 time frames = 30s at 50fps)
  ↓
Conv1 (80→384, kernel=3, stride=1, padding=1) + GELU
  ↓
Conv2 (384→384, kernel=3, stride=2, padding=1) + GELU   ← halves time: 3000→1500
  ↓
Positional Embedding (1500, 384)  [sinusoidal, fixed]
  ↓
4× Transformer Encoder Layers:
  - LayerNorm
  - Multi-Head Self-Attention (6 heads, head_dim=64, d_model=384)
  - Residual
  - LayerNorm
  - FFN (384 → 1536 → 384, GELU)
  - Residual
  ↓
LayerNorm
Output: [B, 1500, 384]  (encoder last hidden state)
        + hidden_states tuple: 5 tensors each [B, 1500, 384]  (layers 0,1,2,3,4)
```

### Key Tensor Shapes

| I/O | Shape | dtype | Notes |
|-----|-------|-------|-------|
| Input mel | `[1, 80, 3000]` | fp16 | 30s segment at 16kHz |
| After Conv2 (downsampled) | `[1, 1500, 384]` | fp16 | token sequence |
| Each hidden state (5 layers) | `[1, 1500, 384]` | fp16 | used for multi-layer features |
| Stacked & trimmed | `[1, T_actual, 384]` | fp16 | T_actual = duration×50 |

**Key detail:** `output_hidden_states=True` is passed. All 5 states (input embedding + 4 transformer outputs) are stacked into the audio feature for the UNet's cross-attention — this gives multi-granularity temporal context.

**Frozen during training.** Runtime dtype: **fp16**.

---

## 4. Positional Encoding (PE)

**Name:** `PositionalEncoding`  
**Purpose:** Add temporal ordering information to the per-frame audio feature batch before it enters the UNet as the cross-attention condition.  
**Defined in:** `musetalk/models/unet.py` (lines 12–27)  
**Weights:** **None** — purely computed sinusoidal buffer, no learned parameters  
**d_model:** 384 (matches Whisper's hidden dimension)

### Architecture

Standard sinusoidal positional encoding:
```
pe[pos, 2i]   = sin(pos / 10000^(2i/d_model))
pe[pos, 2i+1] = cos(pos / 10000^(2i/d_model))
```

Registered as a buffer: `shape [1, 5000, 384]` pre-computed at init.

### Tensor Shapes

| I/O | Shape | dtype | Notes |
|-----|-------|-------|-------|
| Input whisper_batch | `[B, 50, 384]` | fp16 | B = batch_size frames |
| PE slice | `[1, 50, 384]` | fp32→fp16 | broadcast over batch |
| Output | `[B, 50, 384]` | fp16 | added in-place |

**Used as the `encoder_hidden_states` argument to UNet** — i.e., this is the audio "prompt" for the cross-attention, analogous to a text prompt in Stable Diffusion.

---

## 5. SD-VAE Encoder

**Name:** `AutoencoderKL` encoder half (Stable Diffusion VAE)  
**Purpose:** Compress a 256×256 RGB face image into a compact 32×32 latent space representation. Used **twice** per frame: once for the masked face (lower half zeroed), once for the reference face. The latents are concatenated to form the 8-channel UNet input.  
**Defined in:** `musetalk/models/vae.py` (class `VAE`, lines 10–108)  
**HuggingFace class:** `diffusers.AutoencoderKL`  
**Weights file:** `models/sd-vae/diffusion_pytorch_model.bin` (≈ 319 MB)  
**Config file:** `models/sd-vae/config.json`  
**Frozen during training / inference.**

### Architecture (VAE Encoder)

```
Input: [B, 3, 256, 256]  RGB image, normalized to [-1, 1]
  ↓
DownEncoderBlock2D × 4:
  Block 1: 3→128,   spatial: 256→128
  Block 2: 128→256, spatial: 128→64
  Block 3: 256→512, spatial: 64→32
  Block 4: 512→512, spatial: 32→32  (no downsample on last block)
  Each block: 2× ResNet layer + optional Downsample2D (stride-2 conv)
  ↓
MidBlock2D:
  ResNet → Self-Attention → ResNet
  channels: 512, spatial: 32×32
  ↓
conv_norm_out + conv_out  →  mean [B, 4, 32, 32] + logvar [B, 4, 32, 32]
  ↓
sample() from DiagonalGaussianDistribution
  ↓
× scaling_factor (0.18215)
Output: [B, 4, 32, 32]  latent
```

### Preprocessing (defined in `VAE.preprocess_img`)

1. Read image (BGR) → resize to `(256, 256)` with LANCZOS4
2. Convert to RGB, normalize: `x = x / 255.0`, then `Normalize(mean=0.5, std=0.5)` → range `[-1, 1]`
3. Add batch dim: `[1, 3, 256, 256]`

For the **masked image** (`half_mask=True`): the upper half (`[:, :128, :]`) is used, and the lower half is zeroed out. This corresponds to the "Masked Image" input in `musetalk_arc.jpg` where the mouth/jaw region is black.

```python
mask_tensor = zeros(256, 256)
mask_tensor[:128, :] = 1  # upper half = 1, lower half = 0
masked_img = img * mask_tensor   # zeros out the speaking half
```

### Key Tensor Shapes

| Method | Input | Output | Notes |
|--------|-------|--------|-------|
| `preprocess_img(path, half_mask=True)` | file path | `[1, 3, 256, 256]` fp32 | lower-half zeroed |
| `preprocess_img(path, half_mask=False)` | file path | `[1, 3, 256, 256]` fp32 | full reference face |
| `encode_latents(image)` | `[1, 3, 256, 256]` | `[1, 4, 32, 32]` fp16 | scaled by 0.18215 |
| `get_latents_for_unet(img)` | image path/array | `[1, 8, 32, 32]` fp16 | concat of masked+ref latents |

**Scaling factor:** `0.18215` (from `vae.config.scaling_factor`). Applied as `z = scaling_factor × z_sampled` on encode, reversed on decode.

---

## 6. UNet Backbone

**Name:** `UNet2DConditionModel` (Stable Diffusion-style U-Net, custom-conditioned)  
**Purpose:** The core generative model. Takes 8-channel latents (masked face + reference face concatenated channel-wise) and audio embeddings (cross-attention) and predicts the 4-channel latent of the synthesized talking face. This is the **only trained module** in the MuseTalk system.  
**Defined in:** `musetalk/models/unet.py` (class `UNet`, lines 29–51)  
**HuggingFace class:** `diffusers.UNet2DConditionModel`  
**Config file:** `models/musetalkV15/musetalk.json` (same as `models/musetalk/musetalk.json`)  
**Weights files:**
- `models/musetalk/pytorch_model.bin` (≈ 3.4 GB) — original v1
- `models/musetalkV15/unet.pth` (≈ 3.4 GB) — v1.5 (loaded by default in server)

### UNet Configuration (from `musetalk.json`)

```json
{
  "in_channels": 8,          // 4 masked latent + 4 ref latent
  "out_channels": 4,         // predicted face latent
  "cross_attention_dim": 384,// Whisper hidden dim
  "attention_head_dim": 8,
  "block_out_channels": [320, 640, 1280, 1280],
  "down_block_types": [
    "CrossAttnDownBlock2D",   // resolution: 32→16
    "CrossAttnDownBlock2D",   // resolution: 16→8
    "CrossAttnDownBlock2D",   // resolution: 8→4
    "DownBlock2D"             // resolution: 4→4 (no spatial attn)
  ],
  "up_block_types": [
    "UpBlock2D",              // resolution: 4→8
    "CrossAttnUpBlock2D",     // resolution: 8→16
    "CrossAttnUpBlock2D",     // resolution: 16→32
    "CrossAttnUpBlock2D"      // resolution: 32→32
  ],
  "layers_per_block": 2,
  "sample_size": 64,         // Note: actual input is 32×32 (not 64×64)
  "act_fn": "silu",
  "norm_num_groups": 32
}
```

> **Note:** `sample_size: 64` in config is the diffusion model's original training size but actual spatial input/output is `32×32` (because the VAE downsampled 256→32 by factor 8).

### Architecture Detail

```
Input: [B, 8, 32, 32]  fp16
  ↓
conv_in: 8→320  (3×3, padding=1)  [B, 320, 32, 32]
  ↓
time_proj + time_embedding: timestep=0 → [B, 1280]
  (flip_sin_to_cos=True, freq_shift=0)
  ↓
───────────── ENCODER PATH ─────────────
CrossAttnDownBlock2D (320):
  2× ResNet(320→320) + 2× SpatialTransformer(320, 8 heads, cross_attn_dim=384)
  Downsample2D (stride=2): 32→16
  Output: [B, 320, 16, 16]

CrossAttnDownBlock2D (640):
  2× ResNet(320→640) + 2× SpatialTransformer(640, 8 heads, cross_attn_dim=384)
  Downsample2D: 16→8
  Output: [B, 640, 8, 8]

CrossAttnDownBlock2D (1280):
  2× ResNet(640→1280) + 2× SpatialTransformer(1280, 8 heads, cross_attn_dim=384)
  Downsample2D: 8→4
  Output: [B, 1280, 4, 4]

DownBlock2D (1280):
  2× ResNet(1280→1280)  [no attention]
  Output: [B, 1280, 4, 4]

───────────── BOTTLENECK ─────────────
UNetMidBlock2DCrossAttn (1280):
  ResNet → SpatialTransformer(1280, 8 heads, cross_attn_dim=384) → ResNet
  Output: [B, 1280, 4, 4]

───────────── DECODER PATH ─────────────
UpBlock2D (1280):
  3× ResNet(1280+1280→1280)  [skip connection from DownBlock2D]
  Upsample2D: 4→8
  Output: [B, 1280, 8, 8]

CrossAttnUpBlock2D (1280):
  3× ResNet + 3× SpatialTransformer(1280, 8 heads, cross_attn_dim=384)
  Skip from CrossAttnDownBlock2D(1280)
  Upsample2D: 8→16
  Output: [B, 1280, 16, 16]

CrossAttnUpBlock2D (640):
  3× ResNet + 3× SpatialTransformer(640, 8 heads, cross_attn_dim=384)
  Skip from CrossAttnDownBlock2D(640)
  Upsample2D: 16→32
  Output: [B, 640, 32, 32]

CrossAttnUpBlock2D (320):
  3× ResNet + 3× SpatialTransformer(320, 8 heads, cross_attn_dim=384)
  Skip from CrossAttnDownBlock2D(320)
  Output: [B, 320, 32, 32]

───────────── OUTPUT ─────────────
GroupNorm(32) + SiLU
conv_out: 320→4  (3×3, padding=1)
Output: [B, 4, 32, 32]
```

### Cross-Attention Mechanism (Audio Conditioning)

Each `SpatialTransformer` block performs:
1. Reshape spatial features: `[B, C, H, W] → [B, H×W, C]`
2. Self-attention on spatial tokens (Query=Key=Value=spatial features)
3. **Cross-attention** with audio: Query=spatial features `[B, H×W, C]`, Key/Value=audio `[B, 50, 384]`
4. FFN
5. Reshape back to `[B, C, H, W]`

This is the `Audio attn.` (green blocks) shown in `musetalk_arc.jpg`.

### Timestep

`timesteps = torch.tensor([0])` — always set to 0. MuseTalk doesn't use diffusion denoising; it treats the UNet as a direct regression model (predicting the face latent in a single forward pass, not iterative denoising). The time embedding acts as a fixed bias.

### Key Tensor Shapes

| Stage | Shape | dtype |
|-------|-------|-------|
| Input latents | `[B, 8, 32, 32]` | fp16 |
| Audio condition | `[B, 50, 384]` | fp16 |
| After conv_in | `[B, 320, 32, 32]` | fp16 |
| Bottleneck | `[B, 1280, 4, 4]` | fp16 |
| UNet output `.sample` | `[B, 4, 32, 32]` | fp16 |

**Batch size:** 4 (server default, configurable up to 20).  
**Parameter count:** ~860M parameters (based on ~3.4 GB weights at fp16 = 2 bytes/param).  
**Training:** Only the UNet is fine-tuned. All other models (VAE, Whisper) are frozen.

---

## 7. SD-VAE Decoder

**Name:** `AutoencoderKL` decoder half  
**Purpose:** Reconstruct a 256×256 BGR face image from the UNet's predicted 4-channel latent. This is the final image generation step before blending.  
**Defined in:** `musetalk/models/vae.py` (`VAE.decode_latents`, lines 96–108)  
**Same weights as encoder:** `models/sd-vae/diffusion_pytorch_model.bin`  
**Frozen.**

### Architecture (VAE Decoder)

```
Input: [B, 4, 32, 32]  fp16
  ↓
÷ scaling_factor (0.18215): un-scale latents
  ↓
post_quant_conv: 4→4  (1×1 conv)
  ↓
MidBlock2D:
  ResNet → Self-Attention → ResNet
  channels: 512, spatial: 32×32
  ↓
UpDecoderBlock2D × 4:
  Block 1: 512→512, spatial: 32→64    (nearest-neighbour upsample)
  Block 2: 512→256, spatial: 64→128
  Block 3: 256→128, spatial: 128→256
  Block 4: 128→128, spatial: 256→256  (no upsample on last block)
  Each block: 2× ResNet layer + optional Upsample2D
  ↓
GroupNorm + SiLU
conv_out: 128→3  (3×3, padding=1)
  ↓
Output (raw): [B, 3, 256, 256]  float32 in range [-1, 1]
```

### Post-processing (in `decode_latents`)

```python
image = (image / 2 + 0.5).clamp(0, 1)          # [-1,1] → [0,1]
image = image.detach().cpu().permute(0,2,3,1)    # NCHW → NHWC
image = (image * 255).round().astype("uint8")    # [0,255] uint8 RGB
image = image[..., ::-1]                         # RGB → BGR (for OpenCV)
```

### Key Tensor Shapes

| Stage | Shape | dtype | Notes |
|-------|-------|-------|-------|
| Input pred_latent | `[B, 4, 32, 32]` | fp16 | from UNet |
| Un-scaled | `[B, 4, 32, 32]` | fp16 | ÷ 0.18215 |
| After post_quant_conv | `[B, 4, 32, 32]` | fp16 | |
| Decoder output | `[B, 3, 256, 256]` | fp32 | after `.float()` |
| Final numpy output | `[B×H, 256, 256, 3]` | uint8 | BGR, iterated frame-by-frame |

---

## 8. DWPose Landmark Extractor

**Name:** DW-LL UCoCo (RTMPose-L, whole-body)  
**Purpose:** Detect 133 whole-body keypoints including 68 face landmarks per frame. Used during avatar preparation to find the exact face bounding box and the half-face split point (nose bridge landmark).  
**Defined in:** `musetalk/utils/preprocessing.py` (via mmpose APIs)  
**Config file:** `musetalk/utils/dwpose/rtmpose-l_8xb32-270e_coco-ubody-wholebody-384x288.py`  
**Weights file:** `models/dwpose/dw-ll_ucoco_384.pth` (≈ 388 MB)

### Architecture

- **Backbone:** RTMPose-L (CSPNeXt + SimCC head)
- **Input:** raw numpy image (HWC, BGR), any resolution
- **Output:** `keypoints[0][23:91]` — extracts face keypoints 23–90 (68 face landmarks) from the 133-keypoint wholebody output

### Usage in bounding box computation

```python
face_land_mark = keypoints[0][23:91]   # shape [68, 2] (x, y in pixels)
half_face_coord = face_land_mark[29]   # nose bridge point
range_minus = (face_land_mark[30] - face_land_mark[29])[1]  # nasal tip spread
upper_bond = max(0, half_face_coord[1] - half_face_dist)
f_landmark = (x_min, upper_bond, x_max, y_max)  # final face bbox
```

### Key Tensor Shapes

| I/O | Shape | Notes |
|-----|-------|-------|
| Input frame | `[H, W, 3]` | uint8 BGR |
| All keypoints | `[1, 133, 2]` | float32, pixel coords |
| Face landmarks | `[68, 2]` | int32 after cast |
| Output bbox | `(x1, y1, x2, y2)` | int32 pixel coords |

**Chunk size (avatar prep):** Processes **1 frame at a time** (`batch_size_fa = 1`). No chunking.

---

## 9. FaceAlignment (face_detection)

**Name:** `FaceAlignment` (2D, fan/s3fd-based)  
**Purpose:** Secondary face detection via the `face_alignment` library. Provides an additional bbox as fallback/validation alongside DWPose. Both bboxes are compared; if DWPose's landmark-derived bbox is invalid (zero area, negative coords), the FaceAlignment bbox is used.  
**Defined in:** `musetalk/utils/preprocessing.py` (imported from `face_detection`)  
**Models directory:** `musetalk/utils/face_detection/`  
  - `musetalk/utils/face_detection/detection/` — contains S3FD detector weights  
**Weights:** Bundled within `face_detection` pip package (S3FD or RetinaFace)

### Tensor Shapes

| I/O | Shape | Notes |
|-----|-------|-------|
| Input batch | `[1, H, W, 3]` | uint8, batch_size=1 |
| Output bbox | `(x1, y1, x2, y2)` or `None` | float, per-frame |

---

## 10. BiSeNet Face Parser

**Name:** `BiSeNet` (Bilateral Segmentation Network)  
**Purpose:** Generate a semantic segmentation mask of the face region — specifically isolating skin, jaw and neck areas — so that the generated mouth region can be blended seamlessly onto the avatar without hard rectangular crop boundaries. This is the mask shown in `musetalk.png`.  
**Class defined in:** `musetalk/utils/face_parsing/model.py` (`BiSeNet`, line 230)  
**Backbone file:** `musetalk/utils/face_parsing/resnet.py` (`Resnet18`)  
**Init file:** `musetalk/utils/face_parsing/__init__.py` (`FaceParsing`, line 10)  
**Weights files:**
- `models/face-parse-bisent/resnet18-5c106cde.pth` (≈ 44.7 MB) — ResNet-18 backbone (ImageNet pretrained)
- `models/face-parse-bisent/79999_iter.pth` (≈ 50.9 MB) — Full BiSeNet weights

### Architecture

```
Input: [1, 3, 512, 512]  (resized + ImageNet-normalized: mean=[0.485,0.456,0.406], std=[0.229,0.224,0.225])
  ↓
ContextPath (cp):
  Resnet18 backbone → (feat8, feat16, feat32)
    feat8:  [1, 128, H/8, W/8]   = [1, 128, 64, 64]
    feat16: [1, 256, H/16, W/16] = [1, 256, 32, 32]
    feat32: [1, 512, H/32, W/32] = [1, 512, 16, 16]
  
  ARM32: AttentionRefinementModule(512→128)  [1, 128, 16, 16]
  ARM16: AttentionRefinementModule(256→128)  [1, 128, 32, 32]
  
  Global avg pool of feat32 → conv_avg(512→128) → upsample to 16×16 → add to ARM32
  ARM32_up → upsample to 32×32 → conv_head32 → add to ARM16 → feat_cp8 [1, 128, 64, 64]
  ARM16_up → upsample to 64×64 → conv_head16 → feat_cp16 [1, 128, 64, 64]

  ↓
feat_sp = feat8 (from ResNet, replacing SpatialPath): [1, 128, 64, 64]
  ↓
FeatureFusionModule (ffm): concat(feat_sp, feat_cp8) → 256→256 with SE attention [1, 256, 64, 64]
  ↓
BiSeNetOutput (conv_out): [1, 19, H, W]    (main output, upsampled to input H,W)
BiSeNetOutput (conv_out16): [1, 19, H, W]  (auxiliary at 1/16 scale)
BiSeNetOutput (conv_out32): [1, 19, H, W]  (auxiliary at 1/32 scale)
```

**19 semantic classes:**  
`0:background, 1:skin, 2:left_brow, 3:right_brow, 4:left_eye, 5:right_eye, 6:glasses, 7:left_ear, 8:right_ear, 9:earring, 10:nose, 11:mouth, 12:upper_lip, 13:lower_lip, 14:neck, 15:necklace, 16:cloth, 17:hair, 18:hat`

### ResNet-18 Backbone (Resnet18 class)

```
Input: [1, 3, 512, 512]
conv1(3→64, 7×7, stride=2) + BN + ReLU + MaxPool(3×3, stride=2)  → [1, 64, 128, 128]
layer1: 2× BasicBlock(64→64)   → [1, 64, 128, 128]   (1/8 of input)
layer2: 2× BasicBlock(64→128, stride=2)  → [1, 128, 64, 64]   feat8
layer3: 2× BasicBlock(128→256, stride=2) → [1, 256, 32, 32]  feat16
layer4: 2× BasicBlock(256→512, stride=2) → [1, 512, 16, 16]  feat32
```

### Parsing Modes (used in v1.5)

The `FaceParsing.__call__` method supports three modes:

| Mode | Classes kept (set to 255) | Purpose |
|------|--------------------------|---------|
| `"raw"` | 1 (skin), 11,12,13 (mouth/lips) | Basic face-only mask |
| `"neck"` | 1, 11, 12, 13, 14 (neck) | Extends mask to neck |
| `"jaw"` | 1 (skin with morphological dilation) + 11,12,13 | Complex chin/jaw dilation+erosion, with cheek protection regions |

**Default mode in v1.5 server:** `"jaw"` — most precise blending around the chin.

### Soft Blending (Gaussian blur on mask)

```python
blur_kernel_size = int(0.05 * crop_size // 2 * 2) + 1  # ~5% of crop size
mask_array = cv2.GaussianBlur(mask_array, (blur_kernel_size, blur_kernel_size), 0)
```
This feathers the mask edges for smooth alpha blending:
```
blended = face_generated × mask + background × (1 - mask)
```

### Key Tensor Shapes

| Stage | Shape | dtype | Notes |
|-------|-------|-------|-------|
| Input (to BiSeNet) | `[1, 3, 512, 512]` | fp32 | ImageNet-normalized |
| Output logits | `[1, 19, 512, 512]` | fp32 | |
| argmax parsing | `[512, 512]` | int (0–18) | |
| Binary mask | `[512, 512]` | uint8 | 0 or 255 |
| After gaussian blur | `[H_crop, W_crop]` | uint8 | soft alpha values 0–255 |

**Chunk size:** 1 frame per call. Called once per avatar frame during preparation, result cached to `mask_out_path/*.png`.

---

## 11. Complete Inference Data Flow

This section traces a **single video frame** through the complete pipeline, reconciling both architecture diagrams.

```
─────────────── AUDIO BRANCH ───────────────────────────────────────────

1. Raw audio file (any format, any duration)
        ↓ librosa.load (resample to 16 kHz mono)
   audio_array: [N_samples]  float32

2. Split into 30s segments → list of mel spectrograms
   feature_extractor(segment) → [1, 80, 3000]  per segment

3. whisper.encoder([1, 80, 3000], output_hidden_states=True)
   → hidden_states: 5 × [1, 1500, 384]  fp16
   → stacked: [1, T_audio, 5, 384]  (T_audio = duration × 50)

4. For each video frame f:
   audio_index = floor(f × 50/fps)
   audio_clip = stacked_feats[:, audio_index : audio_index+10]  → [1, 10, 5, 384]
   rearrange('b c h w → b (c h) w') → [1, 50, 384]

5. Batch B frames: whisper_batch [B, 50, 384]

6. PositionalEncoding(whisper_batch) → audio_feature [B, 50, 384]

─────────────── VISUAL BRANCH ───────────────────────────────────────────

(Pre-computed during avatar preparation and cached)

7. Full resolution frame: [H_orig, W_orig, 3]  uint8 BGR
   Face bbox: (x1, y1, x2, y2)

8. crop_frame = frame[y1:y2, x1:x2]
   resize to 256×256 (LANCZOS4)

9. VAE encode(masked_frame, half_mask=True):
   → [1, 4, 32, 32]  "masked_latent"   ← lower half is black (see musetalk_arc.jpg)

10. VAE encode(ref_frame, half_mask=False):
    → [1, 4, 32, 32]  "ref_latent"     ← full reference face

11. torch.cat([masked_latent, ref_latent], dim=1)
    → input_latent: [1, 8, 32, 32]  (stored in input_latent_list_cycle)

12. Batch B frames: latent_batch [B, 8, 32, 32]

─────────────── UNET ───────────────────────────────────────────────────

13. unet.model(
       sample = latent_batch,          [B, 8, 32, 32]
       timestep = tensor([0]),         scalar
       encoder_hidden_states = audio_feature  [B, 50, 384]
    )
    → pred_latent: [B, 4, 32, 32]

─────────────── VAE DECODE ──────────────────────────────────────────────

14. vae.decode_latents(pred_latent)
    → reconstructed faces: list of B arrays, each [256, 256, 3]  uint8 BGR

─────────────── BLENDING (per frame) ───────────────────────────────────

(Using pre-computed masks from BiSeNet)

15. res_frame: [256, 256, 3]  uint8  (from VAE decoder)
    bbox = coord_list_cycle[idx]  → (x1, y1, x2, y2)
    target_w, target_h = x2-x1, y2-y1

16. cv2.resize(res_frame, (target_w, target_h))  → [target_h, target_w, 3]

17. mask = mask_list_cycle[idx]  [H_crop, W_crop]  uint8 0–255
    crop_box = mask_coords_list_cycle[idx]  (x_s, y_s, x_e, y_e)

18. Soft alpha blend:
    ori_frame[y1:y2, x1:x2] = face_resized × (mask/255) + orig_crop × (1 - mask/255)
    → output_frame: [H_orig, W_orig, 3]  uint8 BGR  (full resolution output frame)

─────────────── VIDEO OUTPUT ────────────────────────────────────────────

19. Pipe frames → ffmpeg → H.264 MP4 (CRF=18)
20. Mux with original audio → final output.mp4
```

### Reconciling the Two Diagrams

| Feature | `musetalk_arc.jpg` (Training view) | `musetalk.png` (Application view) |
|---------|-------------------------------------|-------------------------------------|
| Shows | Individual encoders + UNet internals | Full frame pipeline with blending |
| VAE Encoder | Two separate: masked + ref face | Implicit inside "MuseTalk" box |
| Skip connection | No (misread as concat of latents, not skip) | Shows mask-based blending |
| Face parsing | Shown as ⊗ merge with UNet output | Explicitly shown as "Face Parsing" → Mask |
| Blending | Labeled as loss computation context | Shown as "Blended Image" output stage |
| Audio | Whisper encoder feeding cross-attention | Implicit inside "MuseTalk" box |

The **key difference**: `musetalk_arc.jpg` shows how the half-mask and full-face latents are **separately encoded and concatenated channel-wise** (`[1,4]+[1,4]→[1,8]`) before going into the UNet. `musetalk.png`'s "MuseTalk Input" represents the already-cropped face region used for this VAE encoding.

---

## 12. Avatar Preparation Phase

**File:** `scripts/realtime_inference.py` → `Avatar.prepare_material()`  
**Triggered:** Once per avatar, results cached to disk  
**Output directory:** `./results/v15/avatars/{avatar_id}/`

```
Step 1: video2imgs()
  Input: any MP4 (any resolution, any fps)
  Output: ./full_imgs/{00000000...N}.png
  Downsampling: if source_fps > target_fps, temporal downsample
  Chunk: 1 frame at a time

Step 2: get_landmark_and_bbox() [preprocessing.py]
  DWPose inference on each frame → face landmark keypoints [68, 2]
  FaceAlignment bbox as fallback
  Extra margin (+10 px at bottom of bbox for v1.5)
  Output: coord_list, frame_list

Step 3: VAE pre-encode all frames
  For each (bbox, frame):
    crop_frame = frame[y1:y2+margin, x1:x2]
    resize to 256×256
    vae.get_latents_for_unet(crop_frame) → [1, 8, 32, 32]
  Saved: input_latent_list_cycle (forward + reversed) → latents.pt

Step 4: BiSeNet mask preparation
  For each frame in frame_list_cycle (forward + reversed):
    get_image_prepare_material(frame, bbox, fp=fp, mode="jaw")
      → crop expanded region (crop_box = face_bbox expanded by 1.5×)
      → BiSeNet([1,3,512,512]) → 19-class logits → binary mask
      → keep only lower portion (below upper_boundary_ratio=0.5)
      → GaussianBlur for soft edges
    Saved: mask_list_cycle (images), mask_coords_list_cycle (pkl)

Disk cache structure:
  latents.pt               # List[[1,8,32,32]] pre-encoded latents
  coords.pkl               # List[(x1,y1,x2,y2)] face bboxes
  mask/*.png               # Pre-computed blending masks [H_crop×W_crop] uint8
  mask_coords.pkl          # List[(xs,ys,xe,ye)] crop box coords for each mask
  full_imgs/*.png          # Full-resolution frames (looping forward+backward)
  avator_info.json         # Metadata: bbox_shift, version, fps
```

---

## 13. Real-Time Streaming Pipeline

**File:** `server.py` → `_run_streaming_inference()`  
**Client connection:** WebSocket at `/ws`  
**Protocol:** Binary packets: `[uint32 frame_idx][uint32 jpeg_len][uint32 pcm_samples][JPEG bytes][PCM float32]`

### Temporal Parameters

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `STREAM_FPS` | 20 | Output video FPS |
| `STREAM_CHUNK_FRAMES` | 20 | Window of 1 second processed per inference |
| `STREAM_HOP_FRAMES` | 5 | New frames per hop (250ms advance) |
| Audio rate | 16000 Hz | Input PCM (client must pre-resample) |
| `window_samples` | 16000 | 1-second PCM buffer |
| `hop_samples` | 4000 | 250ms hop |

### Streaming Loop

```
Client sends PCM chunks → all_pcm buffer grows
  ↓
Every 4000 samples (250ms):
  Extract 16000-sample window (1s)
    ↓ AudioProcessor.get_audio_feature_from_array()
    ↓ AudioProcessor.get_whisper_chunk()     → [24, 50, 384]
    ↓ datagen(whisper_chunks, latent_list_cycle, batch_size=4)
    ↓ per batch:
       pe(wb) → audio_feature [4, 50, 384]
       unet.model(lb, t, audio_feature) → pred [4, 4, 32, 32]
       vae.decode_latents(pred) → 4× [256,256,3] frames
       _blend(avatar, frame, idx) → full-res frame
       cv2.imencode('.jpg', combined, quality=72) → jpeg bytes
       _pack_frame(idx, jpeg, pcm_slice) → binary packet
  
  First window: emit all 20 frames
  Subsequent windows: emit only last 5 frames (new hop)
  Rolling buffer trimmed to save memory
```

---

## 14. Weight Files Summary

| Component | Weight File | Size | Format |
|-----------|------------|------|--------|
| VAE (encoder + decoder) | `models/sd-vae/diffusion_pytorch_model.bin` | 319 MB | HuggingFace `safetensors`/`bin` |
| VAE config | `models/sd-vae/config.json` | 547 B | JSON |
| UNet v1 | `models/musetalk/pytorch_model.bin` | 3.4 GB | PyTorch state_dict |
| UNet v1.5 | `models/musetalkV15/unet.pth` | 3.4 GB | PyTorch state_dict |
| UNet config | `models/musetalkV15/musetalk.json` | 748 B | JSON |
| Whisper-tiny | `models/whisper/pytorch_model.bin` | 144 MB | HuggingFace |
| Whisper config | `models/whisper/config.json` | 1.9 KB | JSON |
| Whisper feature extractor | `models/whisper/preprocessor_config.json` | 180 KB | JSON (mel filterbank) |
| DWPose | `models/dwpose/dw-ll_ucoco_384.pth` | 388 MB | PyTorch checkpoint |
| BiSeNet | `models/face-parse-bisent/79999_iter.pth` | 50.9 MB | PyTorch state_dict |
| ResNet-18 backbone | `models/face-parse-bisent/resnet18-5c106cde.pth` | 44.7 MB | PyTorch (ImageNet pretrained) |

**Total weights on disk:** ≈ 4.7 GB

---

## 15. Optimisation Notes

### What is pre-computed (avatar preparation cache)

| Operation | Pre-computed? | Why Important |
|-----------|--------------|---------------|
| VAE encoding of face crops | ✅ Yes (`latents.pt`) | Saves 2× VAE encoder calls per frame at inference |
| Face landmark detection (DWPose) | ✅ Yes (`coords.pkl`) | DWPose is slow (~50ms/frame) |
| BiSeNet face masks | ✅ Yes (`mask/*.png`) | BiSeNet is slow; masks are static |
| Whisper audio features | ❌ No | Audio-dependent, computed per request |
| UNet inference | ❌ No | Core generation, audio-dependent |
| VAE decoding | ❌ No | Output of UNet, audio-dependent |

### Runtime Acceleration (server.py)

```python
# TF32 matmuls (free on Ampere/Ada GPUs, e.g. L4)
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

# torch.compile with Triton kernel fusion
unet.model    = torch.compile(unet.model, mode="reduce-overhead")  # biggest win
vae.vae.decoder = torch.compile(vae.vae.decoder, mode="reduce-overhead")
whisper.encoder = torch.compile(whisper.encoder, mode="reduce-overhead")
```

- **`reduce-overhead` mode**: captures CUDA graphs to eliminate Python overhead on repeated fixed-shape calls
- **Warmup**: dummy forward passes at startup to trigger Triton JIT compilation

### GPU Blending Optimization

```python
# Avatar frames/masks pre-uploaded to VRAM (_prepare_avatar_on_gpu)
# Blending done with PyTorch GPU ops instead of OpenCV CPU
face_t = torch.from_numpy(res_frame).to(device, non_blocking=True)
face_resized = F.interpolate(face_t, size=(h, w), mode='bilinear')
ori_t[y1:y2, x1:x2] = face_resized * mask_crop + ori_crop * (1 - mask_crop)
```

### Identified Bottlenecks & Potential Optimisations

| Bottleneck | Cost | Potential Fix |
|-----------|------|---------------|
| UNet forward | ~80% of inference time | Quantize to INT8 (TensorRT), distill to smaller UNet |
| VAE decode | ~15% of inference time | Already compiled; further: decode at lower resolution |
| Audio preprocessing | ~3-5ms/window | Profile per-window; precompute if audio known upfront |
| BiSeNet (avatar prep) | ~100ms/frame | Run at lower resolution (256 vs 512) |
| DWPose (avatar prep) | ~50ms/frame | Batch multiple frames or use lighter model |
| JPEG encode per frame | ~2ms/frame | Use NVJPEG (GPU JPEG) or WebP |
| Latency (streaming) | 250ms hop | Reduce `STREAM_HOP_FRAMES` but increases per-frame cost |
| `input_latent_list_cycle` CPU→GPU | ~0.5ms/batch | Pre-pin to GPU memory at load time |

### Streaming Architecture Notes

- **Overlapping windows** with 250ms hop allow smoother audio→video sync at cost of 4× redundant UNet computation per second
- **Forward + reversed frame loop** (`frame_list_cycle = frames + frames[::-1]`) creates a smooth infinite loop of the avatar without jump cuts

---

*Generated from codebase analysis of `/opt/shared/LiveAvatar` on 2026-04-09.*  
*Architecture diagrams referenced: `assets/figs/musetalk_arc.jpg` (training view), `assets/figs/musetalk.png` (application view).*
