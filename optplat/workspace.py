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
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

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
                                        "ts": time.strftime("%H:%M:%S"), "read": False})
            if fields.get("note"):
                cur["messages"].append({"role": "agent", "text": str(fields["note"]),
                                        "ts": time.strftime("%H:%M:%S")})
            cur["messages"] = cur["messages"][-100:]      # cap the log
            cur.update({k: v for k, v in fields.items() if v is not None})
            cur["revision"] = cur.get("revision", 0) + 1
            cur["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            cur["session"] = sid
            self._data[sid] = cur
            self._changed.notify_all()
            return dict(cur)

    def poll_user_messages(self, sid: str, mark_read: bool = True) -> list[dict]:
        """The agent's inbox: user chat messages it has not consumed yet. Since MCP
        is client-initiated (the platform can't wake the agent), the agent drains
        this by polling; `mark_read` flips them read so each is delivered once."""
        with self._lock:
            cur = self._data.get(sid)
            if not cur:
                return []
            unread = [m for m in cur.get("messages", [])
                      if m.get("role") == "user" and not m.get("read")]
            if mark_read:
                for m in unread:
                    m["read"] = True
            return [dict(m) for m in unread]

    def wait_user_messages(self, sid: str, timeout: float = 25.0,
                           mark_read: bool = True) -> list[dict]:
        """Long-poll an agent inbox, returning immediately when a user writes.

        This is the bridge primitive for a continuously-running Agent host. MCP
        itself is client initiated and cannot wake a dormant Hermes conversation;
        a host/worker keeps this call outstanding and invokes its model when it
        returns. The bounded timeout lets workers reconnect and shut down cleanly.
        """
        deadline = time.monotonic() + max(0.0, min(float(timeout), 60.0))
        with self._changed:
            while True:
                cur = self._data.get(sid)
                unread = [m for m in (cur or {}).get("messages", [])
                          if m.get("role") == "user" and not m.get("read")]
                if unread:
                    if mark_read:
                        for message in unread:
                            message["read"] = True
                    return [dict(message) for message in unread]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._changed.wait(remaining)

    def sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._data)


# process-wide singleton shared by api.py (canvas/REST) and mcp_server.py (agent)
WORKSPACE = WorkspaceStore()
