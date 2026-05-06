# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP client auto-reconnection.

Single MCPReconnect instance manages reconnection for both the main Agent
(via hooks) and Composer agents (via new_client()).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from strands import Agent
from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent
from strands.tools.mcp import MCPClient

logger = logging.getLogger("sdpm.agent")

# Keywords indicating MCP connection errors (checked case-insensitively)
_MCP_ERROR_KEYWORDS = (
    "connection", "session is not running", "mcpclient",
    "closed", "eof", "broken pipe", "failed to start mcp",
    "timed out", "timeout", "remote protocol error", "read error",
    "502", "503", "504",
)

# Keywords indicating non-retryable errors
_PERMANENT_KEYWORDS = ("unauthorized", "forbidden", "certificate", "ssl")


class MCPReconnect:
    """MCP reconnection manager.

    Provides:
    - new_client(): generate an independent MCPClient (for Composer)
    - reconnect(agent): replace the shared client and update tool_registry (for main Agent)
    - after_tool_hook / before_tool_hook: Agent hook integration
    - drain_events(): SSE notification events for WebUI
    """

    def __init__(self, factory_fn: Callable[[str], MCPClient], jwt_token: str, max_retries: int = 8):
        self.factory_fn = factory_fn
        self.jwt_token = jwt_token
        self.max_retries = max_retries
        self._client: MCPClient | None = None
        self._lock = threading.Lock()
        self._events: list[dict] = []

    @property
    def client(self) -> MCPClient | None:
        return self._client

    def set_client(self, client: MCPClient) -> None:
        self._client = client

    def new_client(self) -> MCPClient:
        """Create and return a new independent MCPClient."""
        return self.factory_fn(self.jwt_token)

    def reconnect(self, agent: Agent) -> bool:
        """Replace the shared client. Returns True on success."""
        if not self._lock.acquire(timeout=0):
            # Another thread is already reconnecting; wait for it
            with self._lock:
                return self._client is not None
        try:
            old = self._client
            for attempt in range(self.max_retries):
                self._events.append({"type": "reconnecting", "attempt": attempt + 1, "max_retries": self.max_retries})
                try:
                    new = self.factory_fn(self.jwt_token)
                    if old is not None:
                        try:
                            agent.tool_registry.unload_tool_provider(old)
                        except Exception:
                            pass
                    agent.tool_registry.process_tools([new])
                    self._client = new
                    self._events.append({"type": "reconnected"})
                    logger.info("MCP reconnected (attempt %d)", attempt + 1)
                    return True
                except Exception as e:
                    logger.warning("MCP reconnect attempt %d failed: %s", attempt + 1, e)
                    if "401" in str(e) or "Unauthorized" in str(e):
                        self._events.append({"type": "auth_expired"})
                        logger.error("MCP reconnect failed: JWT expired (401 Unauthorized)")
                        return False
                    time.sleep(min(2 ** attempt, 10))
            self._events.append({"type": "failed"})
            logger.error("MCP reconnect failed after %d attempts", self.max_retries)
            return False
        finally:
            self._lock.release()

    # -- Hooks ----------------------------------------------------------------

    def after_tool_hook(self, event: AfterToolCallEvent) -> None:
        """Detect MCP errors and trigger reconnection."""
        if not self._is_mcp_error(event):
            return
        error_text = self._extract_error_text(event)
        tool_name = event.tool_use.get("name", "")
        logger.warning("MCP connection lost (tool=%s): %s", tool_name, error_text[:200])
        self.reconnect(event.agent)

    def before_tool_hook(self, event: BeforeToolCallEvent) -> None:
        """Block tool calls while reconnection is in progress."""
        if not self._lock.locked():
            return
        # Wait for reconnection to complete (max 60s)
        acquired = self._lock.acquire(timeout=60)
        if acquired:
            self._lock.release()

    # -- Events ---------------------------------------------------------------

    def has_pending_events(self) -> bool:
        return len(self._events) > 0

    def drain_events(self) -> list[dict]:
        events = self._events[:]
        self._events.clear()
        return events

    # -- Private --------------------------------------------------------------

    def _is_mcp_error(self, event: AfterToolCallEvent) -> bool:
        """Determine if the tool failure is an MCP connection error."""
        result = event.result
        if not isinstance(result, dict):
            return False
        if result.get("status") != "error":
            return False

        # Check exception type
        exc = getattr(event, "exception", None)
        if exc is not None:
            type_name = type(exc).__name__
            if any(kw in type_name for kw in ("MCPClient", "MCPSession", "ToolProvider")):
                return True

        # Check error text
        error_text = self._extract_error_text(event).lower()
        if any(kw in error_text for kw in _PERMANENT_KEYWORDS):
            return False
        return any(kw in error_text for kw in _MCP_ERROR_KEYWORDS)

    @staticmethod
    def _extract_error_text(event: AfterToolCallEvent) -> str:
        """Extract error message from event result."""
        result = event.result
        if not isinstance(result, dict):
            return ""
        content = result.get("content", [])
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and "text" in item:
                return item["text"]
        return str(result)
