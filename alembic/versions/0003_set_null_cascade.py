"""switch persona_avatars + assistants FKs to ON DELETE SET NULL

Revision ID: 0003_set_null_cascade
Revises: 0002_voice_gender
Create Date: 2026-05-25

Deleting a persona, avatar, or voice should no longer wipe its descendants —
the dependents survive, just with their link nulled. Concretely:

    persona_avatars.persona_id   CASCADE  →  SET NULL  (nullable)
    assistants.persona_id        CASCADE  →  SET NULL  (nullable)
    assistants.avatar_id         CASCADE  →  SET NULL  (nullable)

(The ``voices.persona_id`` column has no FK constraint at the DB level, but
the application clears voice fields on the persona explicitly in the voice
delete handler — see app/main.py ``delete_voice``.)
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0003_set_null_cascade"
down_revision: Union[str, None] = "0002_voice_gender"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── persona_avatars.persona_id ───
    op.drop_constraint(
        "persona_avatars_persona_id_fkey", "persona_avatars", type_="foreignkey"
    )
    op.alter_column("persona_avatars", "persona_id", nullable=True)
    op.create_foreign_key(
        "persona_avatars_persona_id_fkey",
        "persona_avatars",
        "persona_entities",
        ["persona_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ─── assistants.persona_id ───
    op.drop_constraint(
        "assistants_persona_id_fkey", "assistants", type_="foreignkey"
    )
    op.alter_column("assistants", "persona_id", nullable=True)
    op.create_foreign_key(
        "assistants_persona_id_fkey",
        "assistants",
        "persona_entities",
        ["persona_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ─── assistants.avatar_id ───
    op.drop_constraint(
        "assistants_avatar_id_fkey", "assistants", type_="foreignkey"
    )
    op.alter_column("assistants", "avatar_id", nullable=True)
    op.create_foreign_key(
        "assistants_avatar_id_fkey",
        "assistants",
        "persona_avatars",
        ["avatar_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Going back to CASCADE means re-enforcing NOT NULL — fail any rows that
    # currently have NULL links so the operator can see the integrity gap
    # before forcing the cascade behaviour back on.
    op.drop_constraint(
        "assistants_avatar_id_fkey", "assistants", type_="foreignkey"
    )
    op.alter_column("assistants", "avatar_id", nullable=False)
    op.create_foreign_key(
        "assistants_avatar_id_fkey",
        "assistants",
        "persona_avatars",
        ["avatar_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        "assistants_persona_id_fkey", "assistants", type_="foreignkey"
    )
    op.alter_column("assistants", "persona_id", nullable=False)
    op.create_foreign_key(
        "assistants_persona_id_fkey",
        "assistants",
        "persona_entities",
        ["persona_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        "persona_avatars_persona_id_fkey", "persona_avatars", type_="foreignkey"
    )
    op.alter_column("persona_avatars", "persona_id", nullable=False)
    op.create_foreign_key(
        "persona_avatars_persona_id_fkey",
        "persona_avatars",
        "persona_entities",
        ["persona_id"],
        ["id"],
        ondelete="CASCADE",
    )
