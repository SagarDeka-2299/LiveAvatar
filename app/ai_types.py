"""Typed results shared by the AI provider clients + ``ai_router``.

Lives in its own module so the per-provider clients (``openai_client``,
``gemini_client``, ``azure_openai_client``) can return strongly-typed
results without importing ``ai_router``, which imports them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Gender = Literal["male", "female"]


class PersonaAnalysis(BaseModel):
    """Output of the single combined vision call done at persona creation.

    The wire JSON schema (sent to OpenAI / Azure OpenAI as
    ``response_format.json_schema.schema`` with ``strict: true``, and to
    Gemini as ``generationConfig.responseSchema``) is generated from
    this model via :func:`persona_analysis_schema_for_wire`, so the
    field-level descriptions below flow straight to the model and there
    is no second source of truth for the schema.
    """

    model_config = ConfigDict(extra="forbid")

    gender: Gender = Field(
        description=(
            "Apparent gender presentation of the subject in the portrait. "
            "Pick the one that best matches what you observe."
        ),
    )
    voice_description: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "Detailed description of how this character would sound in the "
            "real world — use your imagination. Cover vocal register and "
            "pitch, speaking pace and rhythm, tonal quality "
            "(warm/crisp/deep/breathy etc.), and any accent or regional "
            "quality if it fits the portrait. Write 2–4 vivid sentences of "
            "plain prose so an ElevenLabs Voice Design call can consume it "
            "directly — no markdown, no bullet points, no labels."
        ),
    )


def persona_analysis_schema_for_wire() -> dict:
    """JSON Schema sent to OpenAI / Azure OpenAI / Gemini for strict
    structured output.

    Pydantic's generated schema is post-processed to match the strict
    structured-output dialect every provider accepts:

      * literal types (``Literal["male", "female"]``) are inlined back
        from Pydantic's ``$defs`` reference into the property itself,
        because OpenAI's strict mode doesn't accept ``$ref``;
      * ``title`` is dropped (not part of the strict-output contract);
      * ``additionalProperties: false`` is asserted at every object
        level (Pydantic emits it when ``extra="forbid"``, but we
        defend in depth).
    """
    schema = PersonaAnalysis.model_json_schema()
    return _flatten_for_strict_output(schema)


def _flatten_for_strict_output(schema: dict) -> dict:
    defs = schema.pop("$defs", {})
    # Drop the class docstring — it's developer-facing implementation
    # context, not field instruction the model needs.
    schema.pop("description", None)

    def resolve(node: object) -> object:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                target = defs.get(name) or {}
                # Merge in any sibling annotations (e.g. ``description``)
                # that lived alongside the $ref.
                merged = {**target}
                for key, value in node.items():
                    if key == "$ref":
                        continue
                    merged[key] = value
                return resolve(merged)
            cleaned = {k: resolve(v) for k, v in node.items() if k != "title"}
            if cleaned.get("type") == "object":
                cleaned.setdefault("additionalProperties", False)
            return cleaned
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)  # type: ignore[return-value]


# Cached at import time — the schema is static.
PERSONA_ANALYSIS_JSON_SCHEMA: dict = persona_analysis_schema_for_wire()
