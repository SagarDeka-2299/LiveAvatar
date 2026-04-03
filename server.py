import math
import os
import sys
import copy
import uuid
import json
import struct
import asyncio
import sqlite3
import tempfile
import builtins
import concurrent.futures
from pathlib import Path
from datetime import datetime
from argparse import Namespace
from contextlib import asynccontextmanager

import time
import cv2
import torch
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response, Form, File, UploadFile
from fastapi.responses import HTMLResponse, FileResponse

from transformers import WhisperModel

ROOT = Path(__file__).parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:
    import imageio_ffmpeg
    _ff = str(Path(imageio_ffmpeg.get_ffmpeg_exe()).parent)
    os.environ["PATH"] = _ff + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

from musetalk.utils.utils import datagen, load_all_model
from musetalk.utils.blending import get_image_blending
from musetalk.utils.audio_processor import AudioProcessor
from musetalk.utils.face_parsing import FaceParsing
import scripts.realtime_inference as rt

# ── Config ─────────────────────────────────────────────────────────────────────
VERSION       = "v15"
GPU_ID        = 0
BATCH_SIZE    = 4
FPS           = 24          # fixed output frame rate
# Set to True to apply torch.compile to UNet, VAE decoder, and Whisper encoder.
# First inference after startup will be slow (~30-60 s) while Triton compiles;
# every subsequent call is significantly faster.
# Set to False for faster cold-start during development.
TORCH_COMPILE = os.environ.get("TORCH_COMPILE", "1") == "1"
AUDIO_PAD_L  = 2
AUDIO_PAD_R  = 2
EXTRA_MARGIN = 10
PARSING_MODE = "jaw"

# One processing window = 1 second = FPS frames = 16000 samples at 16 kHz.
# Keep STREAM_CHUNK_FRAMES == FPS so the window is always exactly 1 second.
STREAM_CHUNK_FRAMES = 24   # must equal FPS

ARGS = Namespace(
    version=VERSION, extra_margin=EXTRA_MARGIN, parsing_mode=PARSING_MODE,
    audio_padding_length_left=AUDIO_PAD_L, audio_padding_length_right=AUDIO_PAD_R,
    skip_save_images=True,
)

DB_PATH = "./results/avatars.db"

# ── Model globals ──────────────────────────────────────────────────────────────
device = vae = unet = pe = timesteps = None
whisper = audio_processor = fp = weight_dtype = None


def _warmup_models():
    """
    Run one dummy forward pass through each compiled model so that Triton/TRT
    JIT compilation fires at startup rather than on the first real request.
    Typical cost: 30-60 s the first time, cached on subsequent restarts.
    """
    t0 = time.time()
    print("[WARMUP] Warming up compiled models…", flush=True)
    with torch.no_grad():
        # UNet: always [BATCH_SIZE, 8, 32, 32] latents + [BATCH_SIZE, 50, 384] audio
        dummy_lb = torch.zeros(BATCH_SIZE, 8, 32, 32, dtype=weight_dtype, device=device)
        dummy_af = torch.zeros(BATCH_SIZE, 50, 384,  dtype=weight_dtype, device=device)
        _ = unet.model(dummy_lb, timesteps, encoder_hidden_states=dummy_af).sample

        # VAE decoder: [BATCH_SIZE, 4, 32, 32]
        dummy_lat = torch.zeros(BATCH_SIZE, 4, 32, 32, dtype=weight_dtype, device=device)
        _ = vae.vae.decode(dummy_lat)

        # Whisper encoder: [1, 80, 3000] mel spectrogram
        dummy_mel = torch.zeros(1, 80, 3000, dtype=weight_dtype, device=device)
        _ = whisper.encoder(dummy_mel, output_hidden_states=True)

    if device.type == "cuda":
        torch.cuda.synchronize()
    print(f"[WARMUP] Done in {time.time() - t0:.1f}s", flush=True)


def _load_models():
    global device, vae, unet, pe, timesteps, whisper, audio_processor, fp, weight_dtype
    device = torch.device(f"cuda:{GPU_ID}" if torch.cuda.is_available() else "cpu")
    vae, unet, pe = load_all_model(device=device)
    timesteps = torch.tensor([0], device=device)
    pe         = pe.half().to(device)
    vae.vae    = vae.vae.half().to(device)
    unet.model = unet.model.half().to(device)
    weight_dtype = unet.model.dtype
    audio_processor = AudioProcessor(feature_extractor_path="./models/whisper")
    _w = WhisperModel.from_pretrained("./models/whisper")
    whisper = _w.to(device=device, dtype=weight_dtype).eval()
    whisper.requires_grad_(False)
    fp = FaceParsing(left_cheek_width=90, right_cheek_width=90)
    rt.args = ARGS; rt.vae = vae; rt.unet = unet; rt.pe = pe
    rt.timesteps = timesteps; rt.whisper = whisper
    rt.audio_processor = audio_processor; rt.fp = fp
    rt.device = device; rt.weight_dtype = weight_dtype

    # ── Acceleration ───────────────────────────────────────────────────────────
    # TF32 matmuls: free accuracy-neutral speedup on Ampere/Ada GPUs (L4 included)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32       = True

    if TORCH_COMPILE and device.type == "cuda":
        print("[COMPILE] Applying torch.compile(mode='reduce-overhead') …", flush=True)
        # UNet: called as unet.model(lb, timesteps, encoder_hidden_states=af)
        # — compile the nn.Module directly so __call__ → forward is optimized.
        unet.model = torch.compile(unet.model, mode="reduce-overhead", fullgraph=False)

        # VAE: vae.vae.decode() calls post_quant_conv → decoder internally.
        # Compile the decoder sub-module (the heavy part); post_quant_conv is trivial.
        vae.vae.decoder = torch.compile(vae.vae.decoder, mode="reduce-overhead", fullgraph=False)

        # Whisper: called as whisper.encoder(mel, output_hidden_states=True)
        whisper.encoder = torch.compile(whisper.encoder, mode="reduce-overhead", fullgraph=False)

        print("[COMPILE] Done. Triggering JIT warmup…", flush=True)
        _warmup_models()


def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS avatars (
            id           TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            video_path   TEXT,
            avatar_dir   TEXT NOT NULL,
            thumbnail_path TEXT
        )
    """)
    con.commit()
    con.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_models()
    os.makedirs("./uploads", exist_ok=True)
    _init_db()
    yield


app = FastAPI(lifespan=lifespan)

# ── Avatar cache & thread pool ─────────────────────────────────────────────────
_avatar_cache: dict[str, rt.Avatar] = {}
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)


# ── DB helpers ─────────────────────────────────────────────────────────────────
def db_list_avatars():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, name, created_at, video_path, thumbnail_path "
        "FROM avatars ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def db_save_avatar(avatar_id, name, video_path, avatar_dir, thumbnail_path):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO avatars "
        "(id, name, created_at, video_path, avatar_dir, thumbnail_path) "
        "VALUES (?,?,?,?,?,?)",
        (avatar_id, name, datetime.utcnow().isoformat(),
         video_path, avatar_dir, thumbnail_path),
    )
    con.commit()
    con.close()


def db_get_avatar(avatar_id):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM avatars WHERE id=?", (avatar_id,)
    ).fetchone()
    con.close()
    return dict(row) if row else None


def db_delete_avatar(avatar_id):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM avatars WHERE id=?", (avatar_id,))
    con.commit()
    con.close()


# ── REST endpoints ─────────────────────────────────────────────────────────────
@app.get("/avatars")
async def list_avatars():
    return db_list_avatars()


@app.get("/avatars/{avatar_id}/thumbnail")
async def get_thumbnail(avatar_id: str):
    row = db_get_avatar(avatar_id)
    if not row or not row.get("thumbnail_path"):
        return Response(status_code=404)
    p = Path(row["thumbnail_path"])
    if not p.exists():
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/jpeg")


@app.get("/avatars/{avatar_id}/video")
async def get_avatar_video(avatar_id: str):
    row = db_get_avatar(avatar_id)
    if not row or not row.get("video_path"):
        return Response(status_code=404)
    p = Path(row["video_path"])
    if not p.exists():
        return Response(status_code=404)
    return FileResponse(str(p), media_type="video/mp4")


@app.delete("/avatars/{avatar_id}")
async def delete_avatar(avatar_id: str):
    db_delete_avatar(avatar_id)
    _avatar_cache.pop(avatar_id, None)
    return {"ok": True}


@app.post("/generate")
async def generate_endpoint(
    avatar_id: str      = Form(...),
    audio:     UploadFile = File(...),
):
    """
    Full offline inference: POST multipart(avatar_id, audio file) → MP4 download.
    Blocks until the entire video is rendered, then returns it as video/mp4.
    """
    # Load (or cache-hit) the avatar
    try:
        avatar = await asyncio.get_running_loop().run_in_executor(
            _thread_pool, _load_avatar, avatar_id
        )
    except Exception as exc:
        return Response(
            content=json.dumps({"error": str(exc)}),
            status_code=400, media_type="application/json",
        )

    # Save uploaded audio to a temp file so librosa can read it
    audio_bytes = await audio.read()
    suffix      = Path(audio.filename).suffix if audio.filename else ".wav"
    fd, audio_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        video_bytes = await asyncio.get_running_loop().run_in_executor(
            _thread_pool, _generate_video_sync, avatar, audio_path
        )
        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={"Content-Disposition": 'inline; filename="output.mp4"'},
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return Response(
            content=json.dumps({"error": str(exc)}),
            status_code=500, media_type="application/json",
        )
    finally:
        try:
            os.unlink(audio_path)
        except Exception:
            pass


# ── Avatar helpers ─────────────────────────────────────────────────────────────
def _create_avatar(avatar_id, video_path, bbox_shift):
    orig = builtins.input
    builtins.input = lambda _: "n"
    try:
        return rt.Avatar(
            avatar_id=avatar_id, video_path=video_path,
            bbox_shift=bbox_shift, batch_size=BATCH_SIZE, preparation=True,
        )
    finally:
        builtins.input = orig


def _load_avatar(avatar_id) -> rt.Avatar:
    if avatar_id in _avatar_cache:
        return _avatar_cache[avatar_id]
    row = db_get_avatar(avatar_id)
    if not row:
        raise ValueError(f"Avatar '{avatar_id}' not found.")
    orig = builtins.input
    builtins.input = lambda _: "n"
    try:
        av = rt.Avatar(
            avatar_id=avatar_id, video_path=row["video_path"],
            bbox_shift=0, batch_size=BATCH_SIZE, preparation=False,
        )
        _avatar_cache[avatar_id] = av
        return av
    finally:
        builtins.input = orig


def _extract_thumbnail(video_path: str, out_path: str) -> bool:
    cap = cv2.VideoCapture(video_path)
    ok, frame = cap.read()
    cap.release()
    if ok:
        cv2.imwrite(out_path, frame)
        return True
    return False


def _blend(avatar, res_frame, idx):
    n    = len(avatar.coord_list_cycle)
    bbox = avatar.coord_list_cycle[idx % n]
    ori  = copy.deepcopy(
        avatar.frame_list_cycle[idx % len(avatar.frame_list_cycle)]
    )
    x1, y1, x2, y2 = bbox
    face = cv2.resize(res_frame.astype(np.uint8), (x2 - x1, y2 - y1))
    mask = avatar.mask_list_cycle[idx % len(avatar.mask_list_cycle)]
    cbox = avatar.mask_coords_list_cycle[
        idx % len(avatar.mask_coords_list_cycle)
    ]
    return get_image_blending(ori, face, bbox, mask, cbox)


def _pack_frame(frame_idx: int, jpeg_bytes: bytes, pcm: np.ndarray) -> bytes:
    """Binary packet: [uint32 idx][uint32 jpeg_len][uint32 pcm_samples][jpeg][pcm_f32]"""
    pcm_f32 = pcm.astype(np.float32).tobytes()
    header  = struct.pack("<III", frame_idx, len(jpeg_bytes), len(pcm_f32) // 4)
    return header + jpeg_bytes + pcm_f32


# ── Full offline inference (audio file → MP4 bytes) ───────────────────────────
def _generate_video_sync(avatar, audio_path: str) -> bytes:
    """
    Blocking helper (runs in thread pool).
    Loads audio_path (any format librosa accepts), runs the full inference
    pipeline, and returns the final MP4 as raw bytes (video + audio muxed).
    """
    # 1. Extract Whisper features (audio_processor.get_audio_feature uses
    #    librosa internally and resamples to 16 kHz automatically)
    result = audio_processor.get_audio_feature(audio_path, weight_dtype=weight_dtype)
    if result is None:
        raise ValueError(f"Could not load audio: {audio_path}")
    feats, lib_len = result

    whisper_chunks = audio_processor.get_whisper_chunk(
        feats, device, weight_dtype, whisper, lib_len,
        fps=FPS,
        audio_padding_length_left=AUDIO_PAD_L,
        audio_padding_length_right=AUDIO_PAD_R,
    )
    n_frames = len(whisper_chunks)
    if n_frames == 0:
        raise ValueError("Audio too short — no frames extracted.")

    # 2. Run UNet + VAE for every frame
    gen = datagen(whisper_chunks, avatar.input_latent_list_cycle, BATCH_SIZE)
    res_frames = []
    local = 0
    with torch.no_grad():
        for wb, lb in gen:
            if local >= n_frames:
                break
            af  = pe(wb.to(device))
            lb  = lb.to(device=device, dtype=unet.model.dtype)
            out = unet.model(lb, timesteps, encoder_hidden_states=af).sample
            out = out.to(device=device, dtype=vae.vae.dtype)
            for frame in vae.decode_latents(out):
                if local >= n_frames:
                    break
                res_frames.append(_blend(avatar, frame, local))
                local += 1

    if not res_frames:
        raise ValueError("Inference produced no frames.")

    # 3. Pipe frames to ffmpeg → silent MP4, then mux with original audio
    h, w = res_frames[0].shape[:2]
    fd_s, silent_path = tempfile.mkstemp(suffix="_silent.mp4")
    fd_o, out_path    = tempfile.mkstemp(suffix="_final.mp4")
    os.close(fd_s)
    os.close(fd_o)
    try:
        import subprocess
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "rawvideo", "-vcodec", "rawvideo",
                "-s", f"{w}x{h}", "-pix_fmt", "bgr24",
                "-r", str(FPS), "-i", "pipe:0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                silent_path,
            ],
            stdin=subprocess.PIPE,
        )
        for frame in res_frames:
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        proc.wait()

        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", silent_path,
                "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac", "-shortest",
                out_path,
            ],
            check=True,
        )

        with open(out_path, "rb") as f:
            return f.read()
    finally:
        for p in (silent_path, out_path):
            try:
                os.unlink(p)
            except Exception:
                pass


# ── Streaming inference (chunk-by-chunk) ──────────────────────────────────────
def _run_streaming_inference(
    avatar,
    audio_q: asyncio.Queue,
    out_q: asyncio.Queue,
    loop,
):
    """
    Background thread.  Receives raw float32 PCM at exactly 16 kHz (mono)
    from audio_q and emits packed video+audio frame packets into out_q.

    Fixed pipeline parameters:
      Input audio  : 16 kHz float32 mono (client must resample before sending)
      Output video : FPS=24 fps
      Window size  : STREAM_CHUNK_FRAMES=24 frames = 16000 samples = 1 second

    Packet format (binary):
      [uint32 frame_idx][uint32 jpeg_len][uint32 pcm_samples][jpeg][pcm_f32]

    Queue protocol:
      audio_q → numpy float32 arrays (16 kHz PCM chunks) | None (end sentinel)
      out_q   ← ('text', json_str) | ('bytes', bytes)    | None (end sentinel)
    """
    def _put(item):
        asyncio.run_coroutine_threadsafe(out_q.put(item), loop).result()

    def _get():
        return asyncio.run_coroutine_threadsafe(audio_q.get(), loop).result()

    INPUT_SR      = 16000
    spf           = INPUT_SR / FPS          # samples per video frame ≈ 666.67 @ 24 fps
    chunk_samples = INPUT_SR               # 1 second = 16000 samples

    # Single accumulator at 16 kHz.  Never trimmed — absolute frame_idx
    # indexing requires the full history from sample 0.
    all_pcm   = np.array([], dtype=np.float32)
    processed = 0       # 16 kHz samples consumed so far
    frame_idx = 0       # global output frame counter

    # Latency tracking
    t_stream     = time.time()
    timing       = {"first_audio": None, "first_frame": None, "chunk_n": 0}

    def _infer_window(window: np.ndarray):
        """Whisper → PE → UNet → VAE decoder → blend → pack, for one 1-second window."""
        nonlocal frame_idx
        t0 = time.time()

        feats, lib_len = audio_processor.get_audio_feature_from_array(
            window, weight_dtype=weight_dtype
        )
        whisper_chunks = audio_processor.get_whisper_chunk(
            feats, device, weight_dtype, whisper, lib_len,
            fps=FPS,
            audio_padding_length_left=AUDIO_PAD_L,
            audio_padding_length_right=AUDIO_PAD_R,
        )
        n_frames = len(whisper_chunks)
        if n_frames == 0:
            return

        gen = datagen(
            whisper_chunks, avatar.input_latent_list_cycle,
            BATCH_SIZE, delay_frame=frame_idx,
        )

        local = 0
        with torch.no_grad():
            for wb, lb in gen:
                if local >= n_frames:
                    break
                af  = pe(wb.to(device))
                lb  = lb.to(device=device, dtype=unet.model.dtype)
                out = unet.model(lb, timesteps, encoder_hidden_states=af).sample
                out = out.to(device=device, dtype=vae.vae.dtype)

                for frame in vae.decode_latents(out):
                    if local >= n_frames:
                        break
                    combined = _blend(avatar, frame, frame_idx)
                    ok, jpeg = cv2.imencode(
                        ".jpg", combined, [cv2.IMWRITE_JPEG_QUALITY, 85]
                    )
                    if not ok:
                        frame_idx += 1
                        local     += 1
                        continue

                    # Slice exactly 1/FPS seconds of 16 kHz audio for this frame
                    a0  = int(round(frame_idx * spf))
                    a1  = int(round((frame_idx + 1) * spf))
                    pcm = (
                        all_pcm[a0:a1].copy()
                        if a1 <= len(all_pcm)
                        else np.zeros(int(spf), dtype=np.float32)
                    )
                    if len(pcm) == 0:
                        pcm = np.zeros(int(spf), dtype=np.float32)

                    # Log when the very first frame is ready
                    if timing["first_frame"] is None:
                        timing["first_frame"] = time.time()
                        delay_ms = (timing["first_frame"] - timing["first_audio"]) * 1000
                        print(
                            f"[LATENCY] First frame ready — pipeline_delay={delay_ms:.0f}ms",
                            flush=True,
                        )

                    _put(("bytes", _pack_frame(frame_idx, jpeg.tobytes(), pcm)))
                    frame_idx += 1
                    local     += 1

        elapsed = time.time() - t0
        timing["chunk_n"] += 1
        print(
            f"[LATENCY] Chunk {timing['chunk_n']}: {n_frames} frames in "
            f"{elapsed * 1000:.0f}ms  ({n_frames / elapsed:.1f} fps throughput)",
            flush=True,
        )

    try:
        while True:
            chunk = _get()
            if chunk is None:
                break

            if timing["first_audio"] is None:
                timing["first_audio"] = time.time()
                print(
                    f"[LATENCY] First audio chunk received at "
                    f"t+{timing['first_audio'] - t_stream:.3f}s",
                    flush=True,
                )

            # Accumulate 16 kHz PCM (client already resampled)
            all_pcm = np.concatenate([all_pcm, chunk])

            # Process every complete 1-second window
            while len(all_pcm) - processed >= chunk_samples:
                window = all_pcm[processed: processed + chunk_samples]
                _infer_window(window)
                processed += chunk_samples

        # Flush any remaining audio — pad to full window so Whisper's
        # zero-padding geometry guarantees actual_length ≥ 50 frames.
        remaining = all_pcm[processed:]
        if len(remaining) > 0:
            if len(remaining) < chunk_samples:
                remaining = np.pad(remaining, (0, chunk_samples - len(remaining)))
            _infer_window(remaining)

    except Exception as exc:
        import traceback
        traceback.print_exc()
        _put(("text", json.dumps({"type": "error", "detail": str(exc)})))
    finally:
        _put(("text", json.dumps({"type": "done"})))
        _put(None)


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    loop      = asyncio.get_event_loop()
    state     = "idle"
    pending   = {}
    send_lock = asyncio.Lock()
    drain_task = None

    # ── Serialised send helpers ────────────────────────────────────────────────
    async def _send_json(obj):
        async with send_lock:
            await websocket.send_json(obj)

    async def _send_text(text):
        async with send_lock:
            await websocket.send_text(text)

    async def _send_bytes(data):
        async with send_lock:
            await websocket.send_bytes(data)

    # ── Drain task: reads out_q and forwards to the WebSocket ─────────────────
    async def _drain_out():
        q = pending.get("out_q")
        if q is None:
            return
        while True:
            item = await q.get()
            if item is None:
                break
            mt, d = item
            try:
                if mt == "text":
                    await _send_text(d)
                else:
                    await _send_bytes(d)
            except Exception:
                break

    try:
        while True:
            msg = await websocket.receive()

            # ── Text (JSON control) ────────────────────────────────────────────
            if "text" in msg:
                data = json.loads(msg["text"])
                kind = data.get("type")

                if kind == "list_avatars":
                    await _send_json({
                        "type": "avatars",
                        "list": db_list_avatars(),
                    })

                elif kind == "prepare":
                    state   = "expecting_video"
                    pending = {
                        "name":       data.get("name", "Avatar"),
                        "bbox_shift": data.get("bbox_shift", 0),
                    }

                # ── Pre-load avatar into cache ─────────────────────────────────
                elif kind == "preload_avatar":
                    avatar_id = data.get("avatar_id")
                    if not avatar_id:
                        await _send_json({"type": "error", "detail": "No avatar_id."})
                    else:
                        try:
                            await loop.run_in_executor(
                                _thread_pool, _load_avatar, avatar_id
                            )
                            await _send_json({
                                "type":      "avatar_loaded",
                                "avatar_id": avatar_id,
                            })
                        except Exception as exc:
                            await _send_json({"type": "error", "detail": str(exc)})

                # ── Begin streaming session ────────────────────────────────────
                elif kind == "stream_start":
                    avatar_id = data.get("avatar_id")
                    if not avatar_id:
                        await _send_json({"type": "error", "detail": "No avatar_id."})
                        continue

                    try:
                        avatar = await loop.run_in_executor(
                            _thread_pool, _load_avatar, avatar_id
                        )
                    except Exception as exc:
                        await _send_json({"type": "error", "detail": str(exc)})
                        continue

                    audio_q = asyncio.Queue(maxsize=128)
                    out_q   = asyncio.Queue(maxsize=512)
                    pending["audio_q"] = audio_q
                    pending["out_q"]   = out_q

                    # Pipeline is fixed at 16 kHz input / FPS output.
                    # Client must resample to 16 kHz before sending.
                    _thread_pool.submit(
                        _run_streaming_inference,
                        avatar, audio_q, out_q, loop,
                    )

                    await _send_json({
                        "type":         "stream_meta",
                        "fps":          FPS,
                        "sample_rate":  16000,  # fixed: pipeline always runs at 16 kHz
                        "total_frames": -1,     # unknown in streaming mode
                    })

                    state      = "streaming"
                    drain_task = asyncio.create_task(_drain_out())

                # ── End streaming session ──────────────────────────────────────
                elif kind == "stream_end":
                    if state == "streaming" and "audio_q" in pending:
                        await pending["audio_q"].put(None)
                        state = "idle"

            # ── Binary ────────────────────────────────────────────────────────
            elif "bytes" in msg:
                raw = msg["bytes"]

                # ── Create avatar ──────────────────────────────────────────────
                if state == "expecting_video":
                    state      = "idle"
                    avatar_id  = uuid.uuid4().hex[:8]
                    video_path = f"./uploads/{avatar_id}.mp4"
                    thumb_path = f"./uploads/{avatar_id}_thumb.jpg"

                    with open(video_path, "wb") as f:
                        f.write(raw)
                    _extract_thumbnail(video_path, thumb_path)

                    name   = pending["name"]
                    prog_q: asyncio.Queue = asyncio.Queue()

                    def _prepare():
                        try:
                            av = _create_avatar(
                                avatar_id, video_path, pending["bbox_shift"]
                            )
                            _avatar_cache[avatar_id] = av
                            db_save_avatar(
                                avatar_id, name, video_path,
                                f"./results/avatars/{avatar_id}",
                                thumb_path,
                            )
                            loop.call_soon_threadsafe(prog_q.put_nowait, {
                                "type": "ready",
                                "avatar": {
                                    "id":             avatar_id,
                                    "name":           name,
                                    "created_at":     datetime.utcnow().isoformat(),
                                    "video_path":     video_path,
                                    "thumbnail_path": thumb_path,
                                },
                            })
                        except Exception as exc:
                            loop.call_soon_threadsafe(prog_q.put_nowait,
                                {"type": "error", "detail": str(exc)})

                    _thread_pool.submit(_prepare)

                    while True:
                        try:
                            result = await asyncio.wait_for(prog_q.get(), timeout=4.0)
                            await _send_json(result)
                            break
                        except asyncio.TimeoutError:
                            await _send_json({"type": "preparing"})

                # ── Streaming audio chunk ──────────────────────────────────────
                elif state == "streaming":
                    pcm = np.frombuffer(raw, dtype=np.float32).copy()
                    await pending["audio_q"].put(pcm)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await _send_json({"type": "error", "detail": str(exc)})
        except Exception:
            pass
    finally:
        if drain_task:
            drain_task.cancel()
        if "audio_q" in pending:
            try:
                pending["audio_q"].put_nowait(None)
            except Exception:
                pass


# ── UI ─────────────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return HTMLResponse(Path("static/index.html").read_text())
