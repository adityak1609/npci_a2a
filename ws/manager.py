"""Ticket-scoped WebSocket connection manager for NPCI replies.

TODO(scaling): This manager is in-memory per-process.  Running multiple
FastAPI instances behind a load balancer means a ticket connected to
instance A never receives the broadcast fired by instance B handling
the NPCI reply.  When multi-instance deployment is needed, replace this
with Redis pub/sub fan-out (e.g. broadcaster library or manual
redis.pubsub subscription per WebSocket).
"""

from collections import defaultdict
from fastapi import WebSocket


class ConnectionManager:
    """Manage WebSocket clients subscribed to individual NPCI ticket IDs."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, ticket_id: str, websocket: WebSocket) -> None:
        """Accept and register a WebSocket for a ticket ID."""
        await websocket.accept()
        self._connections[ticket_id].add(websocket)

    def disconnect(self, ticket_id: str, websocket: WebSocket) -> None:
        """Remove a WebSocket from a ticket subscription."""
        sockets = self._connections.get(ticket_id)
        if not sockets:
            return
        sockets.discard(websocket)
        if not sockets:
            self._connections.pop(ticket_id, None)

    async def broadcast_to_ticket(self, ticket_id: str, payload: dict) -> None:
        """Send a JSON payload to every client currently watching a ticket ID."""
        sockets = list(self._connections.get(ticket_id, set()))
        for websocket in sockets:
            try:
                await websocket.send_json(payload)
            except Exception:
                self.disconnect(ticket_id, websocket)


manager = ConnectionManager()
