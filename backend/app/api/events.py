"""Project event WebSocket.

    WS /ws/projects/{project_id}/events

Accepts a connection, subscribes it to the in-process event hub, and forwards
every published event (simulation_started/finished, compile_started/finished,
...) as a JSON text frame until the client disconnects. The subscription is
always torn down in ``finally`` so a dropped socket never leaks a queue.

Mounted in ``app.main`` WITHOUT the ``/api`` prefix so the path is exactly
``/ws/projects/{project_id}/events`` (contract).
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.events import get_hub

router = APIRouter(tags=["events"])


@router.websocket("/ws/projects/{project_id}/events")
async def project_events(websocket: WebSocket, project_id: str) -> None:
    await websocket.accept()
    hub = get_hub()
    queue = hub.subscribe(project_id)
    # A one-shot hello lets clients confirm the stream is live before any solve
    # runs (and gives tests a deterministic first frame).
    await websocket.send_json({"type": "connected", "project_id": project_id})
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(project_id, queue)
