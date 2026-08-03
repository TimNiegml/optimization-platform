"""Minimal MCP (Model Context Protocol) stdio client — stdlib only.

Why our own instead of a library: the platform is closed-source and must stay
permissive-only + dependency-light. MCP's stdio transport is just newline
delimited JSON-RPC 2.0 over a child process's stdin/stdout, which is ~150 lines.
No new third-party dependency is introduced by this module.

    with McpStdioClient(["OpticStudioMCPServer.exe"]) as mcp:
        mcp.call_tool("zemax_get_merit_function", {"includeValues": True})

Design notes
  * a daemon reader thread drains stdout into a queue, so `timeout` actually
    works (a bare readline() would block forever on a hung server);
  * stderr is drained into a bounded ring buffer, so a chatty server can never
    fill the pipe and deadlock us — `client.stderr_tail()` shows it on failure;
  * server-initiated notifications/requests are ignored (we expose no
    capabilities), responses are matched by JSON-RPC id.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections import deque
from typing import Any, Optional, Sequence

PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    """Transport-level or tool-level failure talking to an MCP server."""


class McpStdioClient:
    def __init__(self,
                 command: Sequence[str],
                 env: Optional[dict[str, str]] = None,
                 cwd: Optional[str] = None,
                 timeout: float = 120.0,
                 client_name: str = "optplat"):
        if not command:
            raise ValueError("command must not be empty")
        self.command = list(command)
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.client_name = client_name

        self._proc: Optional[subprocess.Popen] = None
        self._out: "queue.Queue[Optional[str]]" = queue.Queue()
        self._err: deque[str] = deque(maxlen=200)
        self._next_id = 0
        self._lock = threading.Lock()
        self._tools: Optional[list[dict]] = None

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> "McpStdioClient":
        if self._proc is not None:
            return self
        full_env = dict(os.environ)
        if self.env:
            full_env.update(self.env)
        self._proc = subprocess.Popen(
            self.command, cwd=self.cwd, env=full_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": "0.1"},
        })
        self._notify("notifications/initialized", {})
        return self

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    def __enter__(self) -> "McpStdioClient":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- MCP surface --------------------------------------------------------
    def list_tools(self, refresh: bool = False) -> list[dict]:
        if self._tools is None or refresh:
            res = self._request("tools/list", {})
            self._tools = list(res.get("tools", []))
        return self._tools

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> Any:
        """Call a tool and return its payload, JSON-decoded when possible.

        MCP tool results carry a `content` list; these servers return one text
        block holding the JSON result object. `structuredContent` is preferred
        when the server provides it.
        """
        res = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if res.get("isError"):
            raise McpError(f"tool {name} failed: {self._text(res)}")
        if "structuredContent" in res:
            return res["structuredContent"]
        text = self._text(res)
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text

    @staticmethod
    def _text(res: dict) -> str:
        parts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts)

    def stderr_tail(self, n: int = 20) -> str:
        return "\n".join(list(self._err)[-n:])

    # ---- JSON-RPC plumbing --------------------------------------------------
    def _pump_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            self._out.put(line)
        self._out.put(None)          # EOF sentinel

    def _pump_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            self._err.append(line.rstrip("\n"))

    def _send(self, msg: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise McpError(f"MCP server is not running.\n{self.stderr_tail()}")
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict) -> dict:
        with self._lock:                       # one in-flight request at a time
            self._next_id += 1
            req_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            return self._await(req_id, method)

    def _await(self, req_id: int, method: str) -> dict:
        while True:
            try:
                line = self._out.get(timeout=self.timeout)
            except queue.Empty:
                raise McpError(
                    f"timeout after {self.timeout}s waiting for '{method}'.\n{self.stderr_tail()}")
            if line is None:
                raise McpError(f"MCP server exited while waiting for '{method}'.\n"
                               f"{self.stderr_tail()}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue                       # non-protocol chatter on stdout
            if not isinstance(msg, dict) or msg.get("id") != req_id:
                continue                       # notification / other id
            if "error" in msg:
                err = msg["error"]
                raise McpError(f"{method} -> {err.get('code')}: {err.get('message')}")
            return msg.get("result", {}) or {}
