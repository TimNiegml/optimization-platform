"""Live workspace — the shared state an Agent edits and the canvas mirrors.

When the platform runs as one service (canvas + REST + MCP in a single process,
see api.py), an MCP agent (Hermes / GLM5.1 / Claude) and the drag-drop canvas
talk to the SAME in-memory workspace, keyed by a session id:

  agent → push_to_canvas(graph)  ──writes──▶  WorkspaceStore ──SSE/poll──▶ canvas 自动刷新
  canvas → (user tweaks) ──POST /workspace──▶ WorkspaceStore ──get_canvas──▶ agent reads back

Each write bumps a monotonically increasing `revision`; the canvas watches that
number (via GET /workspace/{sid} polling or the SSE stream) and re-renders only
when it changes. Pure stdlib + a lock — no new dependency.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional


class WorkspaceStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _blank(self, sid: str) -> dict[str, Any]:
        return {"session": sid, "graph": None, "bench": "single_peak",
                "result": None, "autotune": None, "note": "",
                "messages": [], "revision": 0, "updated_at": None}

    def snapshot(self, sid: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get(sid) or self._blank(sid))

    def revision(self, sid: str) -> int:
        with self._lock:
            return (self._data.get(sid) or {}).get("revision", 0)

    def update(self, sid: str, **fields: Any) -> dict[str, Any]:
        """Merge `fields` into the session workspace, bump revision, stamp time.

        A `user_message` field is special: it is APPENDED to the `messages` chat
        log (role=user) rather than overwriting a field, so a user's canvas chat
        reaches the agent via get_canvas / the SSE snapshot. An agent's `note`
        similarly gets mirrored into the log (role=agent)."""
        with self._lock:
            cur = self._data.get(sid) or self._blank(sid)
            msg = fields.pop("user_message", None)
            cur.setdefault("messages", [])
            if msg:
                cur["messages"].append({"role": "user", "text": str(msg),
                                        "ts": time.strftime("%H:%M:%S")})
            if fields.get("note"):
                cur["messages"].append({"role": "agent", "text": str(fields["note"]),
                                        "ts": time.strftime("%H:%M:%S")})
            cur["messages"] = cur["messages"][-100:]      # cap the log
            cur.update({k: v for k, v in fields.items() if v is not None})
            cur["revision"] = cur.get("revision", 0) + 1
            cur["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            cur["session"] = sid
            self._data[sid] = cur
            return dict(cur)

    def sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._data)


# process-wide singleton shared by api.py (canvas/REST) and mcp_server.py (agent)
WORKSPACE = WorkspaceStore()
