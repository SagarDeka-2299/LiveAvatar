from __future__ import annotations

import json

from livekit import api

AGENT_NAME = "lili-avatar-agent"


def build_agent_dispatch_room_config(
    *,
    room_name: str,
    assistant_id: int,
    tenant_id: str,
) -> api.RoomConfiguration:
    """Pre-configure a room so an explicit-dispatch agent joins when the first user connects.

    The agent worker reads ctx.job.metadata to know which tenant and assistant
    to serve. Both identifiers are required: the worker must look the
    assistant up in the right tenant DB.
    """
    dispatch = api.RoomAgentDispatch(
        agent_name=AGENT_NAME,
        metadata=json.dumps(
            {"tenant_id": tenant_id, "assistant_id": assistant_id}
        ),
    )
    return api.RoomConfiguration(name=room_name, agents=[dispatch])


def create_join_token(
    api_key: str,
    api_secret: str,
    identity: str,
    room_name: str,
    participant_name: str | None = None,
    room_config: api.RoomConfiguration | None = None,
) -> str:
    grant = api.VideoGrants(room_join=True, room=room_name)
    token = (
        api.AccessToken(api_key=api_key, api_secret=api_secret)
        .with_identity(identity)
        .with_grants(grant)
    )
    if participant_name:
        token = token.with_name(participant_name)
    if room_config is not None:
        token = token.with_room_config(room_config)
    return token.to_jwt()
