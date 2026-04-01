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

import cv2
import torch
import numpy as np
import librosa
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
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
VERSION      = "v15"
GPU_ID       = 0
BATCH_SIZE   = 4
FPS          = 25
AUDIO_PAD_L  = 2
AUDIO_PAD_R  = 2
EXTRA_MARGIN = 10
PARSING_MODE = "jaw"

ARGS = Namespace(
    version=VERSION, extra_margin=EXTRA_MARGIN, parsing_mode=PARSING_MODE,
    audio_padding_length_left=AUDIO_PAD_L, audio_padding_length_right=AUDIO_PAD_R,
    skip_save_images=True,
)

DB_PATH = "./avatars.db"

# ── Model globals ──────────────────────────────────────────────────────────────
device = vae = unet = pe = timesteps = None
whisper = audio_processor = fp = weight_dtype = None


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
        "SELECT id, name, created_at, thumbnail_path "
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


@app.delete("/avatars/{avatar_id}")
async def delete_avatar(avatar_id: str):
    db_delete_avatar(avatar_id)
    _avatar_cache.pop(avatar_id, None)
    return {"ok": True}


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


def _run_inference_ws(
    avatar, audio_path: str,
    q: asyncio.Queue, loop,
    start_q: asyncio.Queue,
):
    """
    Background thread: runs GPU inference and pushes tuples into q:
      ('text', json_str)  — control messages (stream_meta, done)
      ('bytes', bytes)    — binary frame packets (jpeg + pcm)
    Signals start_q after the first batch of frames is queued.
    """
    first_batch_signaled = False

    def _signal():
        nonlocal first_batch_signaled
        if not first_batch_signaled:
            first_batch_signaled = True
            asyncio.run_coroutine_threadsafe(start_q.put(True), loop).result()

    def _put(item):
        asyncio.run_coroutine_threadsafe(q.put(item), loop).result()

    try:
        # Load audio PCM (original sample rate, mono float32)
        y, sr = librosa.load(audio_path, sr=None, mono=True)
        spf   = sr / FPS  # samples per frame

        # Whisper features
        feats, lib_len = audio_processor.get_audio_feature(
            audio_path, weight_dtype=weight_dtype
        )
        chunks = audio_processor.get_whisper_chunk(
            feats, device, weight_dtype, whisper, lib_len,
            fps=FPS, audio_padding_length_left=AUDIO_PAD_L,
            audio_padding_length_right=AUDIO_PAD_R,
        )
        total_frames = len(chunks)

        _put(("text", json.dumps({
            "type": "stream_meta",
            "fps": FPS,
            "sample_rate": int(sr),
            "total_frames": total_frames,
        })))

        frame_idx = 0
        gen = datagen(chunks, avatar.input_latent_list_cycle, BATCH_SIZE)

        with torch.no_grad():
            for wb, lb in gen:
                if frame_idx >= total_frames:
                    break

                af  = pe(wb.to(device))
                lb  = lb.to(device=device, dtype=unet.model.dtype)
                out = unet.model(lb, timesteps, encoder_hidden_states=af).sample
                out = out.to(device=device, dtype=vae.vae.dtype)

                for frame in vae.decode_latents(out):
                    if frame_idx >= total_frames:
                        break

                    combined = _blend(avatar, frame, frame_idx)
                    ok, jpeg = cv2.imencode(
                        ".jpg", combined, [cv2.IMWRITE_JPEG_QUALITY, 85]
                    )
                    if not ok:
                        frame_idx += 1
                        continue

                    a0  = int(round(frame_idx * spf))
                    a1  = int(round((frame_idx + 1) * spf))
                    pcm = y[a0:a1] if a1 <= len(y) else np.zeros(int(spf), np.float32)
                    if len(pcm) == 0:
                        pcm = np.zeros(int(spf), np.float32)

                    _put(("bytes", _pack_frame(frame_idx, jpeg.tobytes(), pcm)))
                    frame_idx += 1

                # Signal after first batch is fully queued
                _signal()

    except Exception:
        _signal()
        raise
    finally:
        _signal()
        _put(("text", json.dumps({"type": "done"})))
        _put(None)
        try:
            os.unlink(audio_path)
        except OSError:
            pass


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    loop    = asyncio.get_event_loop()
    state   = "idle"
    pending = {}

    try:
        while True:
            msg = await websocket.receive()

            # ── Text (JSON control) ────────────────────────────────────────────
            if "text" in msg:
                data = json.loads(msg["text"])
                kind = data.get("type")

                if kind == "list_avatars":
                    await websocket.send_json({
                        "type": "avatars",
                        "list": db_list_avatars(),
                    })

                elif kind == "prepare":
                    state   = "expecting_video"
                    pending = {
                        "name":       data.get("name", "Avatar"),
                        "bbox_shift": data.get("bbox_shift", 0),
                    }

                elif kind == "stream":
                    state   = "expecting_audio"
                    pending = {
                        "avatar_id": data.get("avatar_id"),
                        "filename":  data.get("filename", "audio.wav"),
                    }

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
                                    "id":         avatar_id,
                                    "name":       name,
                                    "created_at": datetime.utcnow().isoformat(),
                                },
                            })
                        except Exception as exc:
                            loop.call_soon_threadsafe(prog_q.put_nowait,
                                {"type": "error", "detail": str(exc)})

                    _thread_pool.submit(_prepare)

                    while True:
                        try:
                            result = await asyncio.wait_for(prog_q.get(), timeout=4.0)
                            await websocket.send_json(result)
                            break
                        except asyncio.TimeoutError:
                            await websocket.send_json({"type": "preparing"})

                # ── Run inference ──────────────────────────────────────────────
                elif state == "expecting_audio":
                    state     = "idle"
                    avatar_id = pending["avatar_id"]
                    if not avatar_id:
                        await websocket.send_json(
                            {"type": "error", "detail": "No avatar_id."})
                        continue

                    try:
                        avatar = await loop.run_in_executor(
                            _thread_pool, _load_avatar, avatar_id
                        )
                    except Exception as exc:
                        await websocket.send_json(
                            {"type": "error", "detail": str(exc)})
                        continue

                    suffix = Path(pending["filename"]).suffix or ".wav"
                    tmp    = tempfile.NamedTemporaryFile(
                        delete=False, suffix=suffix, dir="./uploads"
                    )
                    tmp.write(raw)
                    tmp.close()

                    pkt_q:   asyncio.Queue = asyncio.Queue(maxsize=256)
                    start_q: asyncio.Queue = asyncio.Queue()

                    _thread_pool.submit(
                        _run_inference_ws, avatar, tmp.name,
                        pkt_q, loop, start_q,
                    )

                    # Wait until first batch is buffered
                    while True:
                        try:
                            await asyncio.wait_for(start_q.get(), timeout=3.0)
                            break
                        except asyncio.TimeoutError:
                            await websocket.send_json({"type": "buffering"})

                    # Drain queue → WebSocket
                    while True:
                        item = await pkt_q.get()
                        if item is None:
                            break
                        msg_type, data = item
                        if msg_type == "text":
                            await websocket.send_text(data)
                        else:
                            await websocket.send_bytes(data)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "detail": str(exc)})
        except Exception:
            pass


# ── UI ─────────────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return HTMLResponse(Path("static/index.html").read_text())
