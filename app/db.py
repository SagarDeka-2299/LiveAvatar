from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("AVATAR_DB_PATH", str(Path(__file__).resolve().parent.parent / "app.db")))


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                image_path TEXT NOT NULL,
                gender TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL DEFAULT 'processing',
                progress INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL DEFAULT 'queued',
                last_error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_avatars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                decoration TEXT NOT NULL DEFAULT '',
                theme_prompt TEXT NOT NULL DEFAULT '',
                preset_ids TEXT NOT NULL DEFAULT '[]',
                face_id TEXT NOT NULL DEFAULT '',
                image_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                progress INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL DEFAULT 'queued',
                last_error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (persona_id) REFERENCES persona_entities(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assistants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                first_message TEXT NOT NULL,
                persona_id INTEGER NOT NULL,
                avatar_id INTEGER NOT NULL,
                face_id TEXT NOT NULL DEFAULT '',
                simli_agent_id TEXT NOT NULL DEFAULT '',
                voice_provider TEXT NOT NULL DEFAULT '',
                voice_id TEXT,
                voice_model TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'processing',
                progress INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL DEFAULT 'queued',
                last_error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (persona_id) REFERENCES persona_entities(id) ON DELETE CASCADE,
                FOREIGN KEY (avatar_id) REFERENCES persona_avatars(id) ON DELETE CASCADE
            )
            """
        )

        _ensure_column(conn, "persona_entities", "gender", "gender TEXT NOT NULL DEFAULT 'unknown'")
        _ensure_column(conn, "persona_entities", "status", "status TEXT NOT NULL DEFAULT 'processing'")
        _ensure_column(conn, "persona_entities", "progress", "progress INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "persona_entities", "stage", "stage TEXT NOT NULL DEFAULT 'queued'")
        _ensure_column(conn, "persona_entities", "last_error", "last_error TEXT")
        _ensure_column(conn, "persona_entities", "voice_provider", "voice_provider TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "persona_entities", "voice_id", "voice_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "persona_entities", "voice_source", "voice_source TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "persona_entities", "voice_description", "voice_description TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "persona_entities", "voice_sample_path", "voice_sample_path TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "persona_entities", "voice_preview_path", "voice_preview_path TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "persona_entities", "voice_status", "voice_status TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "persona_entities", "voice_last_error", "voice_last_error TEXT")

        _ensure_column(conn, "persona_avatars", "theme_prompt", "theme_prompt TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "persona_avatars", "preset_ids", "preset_ids TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "persona_avatars", "progress", "progress INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "persona_avatars", "stage", "stage TEXT NOT NULL DEFAULT 'queued'")
        _ensure_column(conn, "persona_avatars", "last_error", "last_error TEXT")

        _ensure_column(conn, "assistants", "status", "status TEXT NOT NULL DEFAULT 'processing'")
        _ensure_column(conn, "assistants", "progress", "progress INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "assistants", "stage", "stage TEXT NOT NULL DEFAULT 'queued'")
        _ensure_column(conn, "assistants", "last_error", "last_error TEXT")
        _ensure_column(conn, "assistants", "simli_agent_id", "simli_agent_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "assistants", "face_id", "face_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "assistants", "voice_provider", "voice_provider TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "assistants", "voice_model", "voice_model TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "assistants", "language", "language TEXT NOT NULL DEFAULT 'en'")
        _ensure_column(conn, "assistants", "llm_provider", "llm_provider TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "assistants", "llm_model", "llm_model TEXT NOT NULL DEFAULT ''")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'elevenlabs',
                voice_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                sample_path TEXT NOT NULL DEFAULT '',
                preview_path TEXT NOT NULL DEFAULT '',
                persona_id INTEGER,
                status TEXT NOT NULL DEFAULT 'ready',
                last_error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_column(conn, "persona_entities", "voice_ref_id", "voice_ref_id INTEGER")


def _rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _row(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def _insert(query: str, params: tuple[Any, ...]) -> int:
    with get_conn() as conn:
        cur = conn.execute(query, params)
        return int(cur.lastrowid)


def _update(table: str, entity_id: int, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [entity_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE {table} SET {columns} WHERE id = ?", values)


def insert_persona_entity(payload: dict[str, Any]) -> int:
    return _insert(
        """
        INSERT INTO persona_entities (name, image_path, gender, status, progress, stage, last_error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["name"],
            payload["image_path"],
            payload.get("gender", "unknown"),
            payload.get("status", "processing"),
            payload.get("progress", 0),
            payload.get("stage", "queued"),
            payload.get("last_error"),
        ),
    )


_PERSONA_COLUMNS = (
    "id, name, image_path, gender, status, progress, stage, last_error, created_at, "
    "voice_provider, voice_id, voice_source, voice_description, "
    "voice_sample_path, voice_preview_path, voice_status, voice_last_error"
)


def list_persona_entities() -> list[dict[str, Any]]:
    return _rows(
        f"SELECT {_PERSONA_COLUMNS} FROM persona_entities ORDER BY id DESC"
    )


def get_persona_entity(persona_id: int) -> dict[str, Any] | None:
    return _row(
        f"SELECT {_PERSONA_COLUMNS} FROM persona_entities WHERE id = ?",
        (persona_id,),
    )


def update_persona_entity(persona_id: int, **fields: Any) -> None:
    _update("persona_entities", persona_id, **fields)


def insert_persona_avatar(payload: dict[str, Any]) -> int:
    return _insert(
        """
        INSERT INTO persona_avatars (
            persona_id, name, decoration, theme_prompt, preset_ids, face_id,
            image_path, status, progress, stage, last_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["persona_id"],
            payload["name"],
            payload.get("decoration", ""),
            payload.get("theme_prompt", ""),
            payload.get("preset_ids", "[]"),
            payload.get("face_id", ""),
            payload["image_path"],
            payload.get("status", "processing"),
            payload.get("progress", 0),
            payload.get("stage", "queued"),
            payload.get("last_error"),
        ),
    )


def list_persona_avatars() -> list[dict[str, Any]]:
    return _rows(
        """
        SELECT id, persona_id, name, decoration, theme_prompt, preset_ids, face_id,
               image_path, status, progress, stage, last_error, created_at
        FROM persona_avatars
        ORDER BY id DESC
        """
    )


def get_persona_avatar(avatar_id: int) -> dict[str, Any] | None:
    return _row(
        """
        SELECT id, persona_id, name, decoration, theme_prompt, preset_ids, face_id,
               image_path, status, progress, stage, last_error, created_at
        FROM persona_avatars
        WHERE id = ?
        """,
        (avatar_id,),
    )


def update_persona_avatar(avatar_id: int, **fields: Any) -> None:
    _update("persona_avatars", avatar_id, **fields)


def insert_assistant(payload: dict[str, Any]) -> int:
    return _insert(
        """
        INSERT INTO assistants (
            name, prompt, first_message, persona_id, avatar_id, face_id,
            simli_agent_id, voice_provider, voice_id, voice_model, language,
            llm_provider, llm_model,
            status, progress, stage, last_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["name"],
            payload["prompt"],
            payload["first_message"],
            payload["persona_id"],
            payload["avatar_id"],
            payload.get("face_id", ""),
            payload.get("simli_agent_id", ""),
            payload.get("voice_provider", ""),
            payload.get("voice_id"),
            payload.get("voice_model", ""),
            payload.get("language", "en"),
            payload.get("llm_provider", ""),
            payload.get("llm_model", ""),
            payload.get("status", "processing"),
            payload.get("progress", 0),
            payload.get("stage", "queued"),
            payload.get("last_error"),
        ),
    )


def list_assistants() -> list[dict[str, Any]]:
    return _rows(
        """
        SELECT id, name, prompt, first_message, persona_id, avatar_id, face_id,
               simli_agent_id, voice_provider, voice_id, voice_model, language,
               llm_provider, llm_model,
               status, progress, stage, last_error, created_at
        FROM assistants
        ORDER BY id DESC
        """
    )


def get_assistant(assistant_id: int) -> dict[str, Any] | None:
    return _row(
        """
        SELECT id, name, prompt, first_message, persona_id, avatar_id, face_id,
               simli_agent_id, voice_provider, voice_id, voice_model, language,
               llm_provider, llm_model,
               status, progress, stage, last_error, created_at
        FROM assistants
        WHERE id = ?
        """,
        (assistant_id,),
    )


def update_assistant(assistant_id: int, **fields: Any) -> None:
    _update("assistants", assistant_id, **fields)


def delete_all_assistants() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM assistants")
        return int(cur.rowcount)


def delete_all_persona_avatars() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM persona_avatars")
        return int(cur.rowcount)


def delete_all_persona_entities() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM persona_entities")
        return int(cur.rowcount)


# ── Per-entity delete helpers ──────────────────────────────────────────────────

_VOICE_COLUMNS = (
    "id, name, provider, voice_id, source, description, "
    "sample_path, preview_path, persona_id, status, last_error, created_at"
)


def list_voices() -> list[dict[str, Any]]:
    return _rows(f"SELECT {_VOICE_COLUMNS} FROM voices ORDER BY id DESC")


def get_voice(row_id: int) -> dict[str, Any] | None:
    return _row(f"SELECT {_VOICE_COLUMNS} FROM voices WHERE id = ?", (row_id,))


def insert_voice(payload: dict[str, Any]) -> int:
    return _insert(
        """
        INSERT INTO voices (name, provider, voice_id, source, description, sample_path, preview_path, persona_id, status, last_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["name"],
            payload.get("provider", "elevenlabs"),
            payload.get("voice_id", ""),
            payload.get("source", ""),
            payload.get("description", ""),
            payload.get("sample_path", ""),
            payload.get("preview_path", ""),
            payload.get("persona_id"),
            payload.get("status", "ready"),
            payload.get("last_error"),
        ),
    )


def update_voice(row_id: int, **fields: Any) -> None:
    _update("voices", row_id, **fields)


def delete_voice_entity(voice_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM voices WHERE id = ?", (voice_id,))
        return int(cur.rowcount)


def delete_persona_entity(persona_id: int) -> int:
    """Delete one persona (cascades to its avatars and their assistants)."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM persona_entities WHERE id = ?", (persona_id,))
        return int(cur.rowcount)


def delete_persona_avatar(avatar_id: int) -> int:
    """Delete one avatar (cascades to its assistants)."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM persona_avatars WHERE id = ?", (avatar_id,))
        return int(cur.rowcount)


def delete_assistant_entity(assistant_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM assistants WHERE id = ?", (assistant_id,))
        return int(cur.rowcount)


# ── Cascade count helpers (for the delete warning modal) ──────────────────────

def count_avatars_for_persona(persona_id: int) -> int:
    row = _row("SELECT COUNT(*) AS n FROM persona_avatars WHERE persona_id = ?", (persona_id,))
    return int(row["n"]) if row else 0


def count_assistants_for_persona(persona_id: int) -> int:
    row = _row(
        "SELECT COUNT(*) AS n FROM assistants WHERE persona_id = ?", (persona_id,)
    )
    return int(row["n"]) if row else 0


def count_assistants_for_avatar(avatar_id: int) -> int:
    row = _row("SELECT COUNT(*) AS n FROM assistants WHERE avatar_id = ?", (avatar_id,))
    return int(row["n"]) if row else 0


init_db()
