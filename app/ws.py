from __future__ import annotations

from collections import defaultdict

from fastapi import WebSocket


class UpdateHub:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, client_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[client_id].add(websocket)

    def disconnect(self, client_id: str, websocket: WebSocket) -> None:
        sockets = self._connections.get(client_id)
        if not sockets:
            return
        sockets.discard(websocket)
        if not sockets:
            self._connections.pop(client_id, None)

    async def send(self, client_id: str, payload: dict) -> None:
        sockets = list(self._connections.get(client_id, set()))
        stale: list[WebSocket] = []
        for websocket in sockets:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(client_id, websocket)

    async def broadcast(self, payload: dict) -> None:
        """Send payload to every connected client."""
        for client_id in list(self._connections.keys()):
            await self.send(client_id, payload)

