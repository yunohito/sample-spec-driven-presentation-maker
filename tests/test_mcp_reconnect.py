# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for mcp_reconnect.MCPReconnect — based on docs/design/mcp-reconnect.md."""

import sys
import threading
import time
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# Mock strands SDK modules before importing mcp_reconnect
_strands = ModuleType("strands")
_strands.Agent = MagicMock
_strands_hooks = ModuleType("strands.hooks")
_strands_hooks_events = ModuleType("strands.hooks.events")
_strands_hooks_events.AfterToolCallEvent = type("AfterToolCallEvent", (), {})
_strands_hooks_events.BeforeToolCallEvent = type("BeforeToolCallEvent", (), {})
_strands_tools = ModuleType("strands.tools")
_strands_tools_mcp = ModuleType("strands.tools.mcp")
_strands_tools_mcp.MCPClient = MagicMock

sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("strands.hooks", _strands_hooks)
sys.modules.setdefault("strands.hooks.events", _strands_hooks_events)
sys.modules.setdefault("strands.tools", _strands_tools)
sys.modules.setdefault("strands.tools.mcp", _strands_tools_mcp)

from mcp_reconnect import MCPReconnect


def _make_reconnect(factory_fn=None, max_retries=3):
    if factory_fn is None:
        factory_fn = MagicMock(return_value=MagicMock())
    return MCPReconnect(factory_fn=factory_fn, jwt_token="test-jwt", max_retries=max_retries)


def _make_error_event(text="Connection to the MCP server was closed", exception=None, tool_name="get_preview"):
    event = MagicMock()
    event.result = {
        "toolUseId": "test-123",
        "status": "error",
        "content": [{"text": text}],
    }
    event.exception = exception
    event.tool_use = {"name": tool_name, "input": {}}
    event.agent = MagicMock()
    event.agent.tool_registry = MagicMock()
    return event


# ---------------------------------------------------------------------------
# 1. new_client
# ---------------------------------------------------------------------------


class TestNewClient:
    def test_returns_new_client(self):
        new = MagicMock()
        r = _make_reconnect(factory_fn=MagicMock(return_value=new))
        assert r.new_client() is new

    def test_factory_exception_propagates(self):
        r = _make_reconnect(factory_fn=MagicMock(side_effect=ConnectionError("down")))
        with pytest.raises(ConnectionError):
            r.new_client()

    def test_does_not_affect_shared_client(self):
        shared = MagicMock()
        r = _make_reconnect()
        r.set_client(shared)
        r.new_client()
        assert r.client is shared


# ---------------------------------------------------------------------------
# 2. reconnect(agent)
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_success_first_attempt(self):
        new = MagicMock()
        r = _make_reconnect(factory_fn=MagicMock(return_value=new))
        old = MagicMock()
        r.set_client(old)
        agent = MagicMock()

        assert r.reconnect(agent) is True
        assert r.client is new
        agent.tool_registry.unload_tool_provider.assert_called_once_with(old)
        agent.tool_registry.process_tools.assert_called_once_with([new])

    def test_success_second_attempt(self):
        new = MagicMock()
        factory = MagicMock(side_effect=[ConnectionError("fail"), new])
        r = _make_reconnect(factory_fn=factory, max_retries=3)
        r.set_client(MagicMock())
        agent = MagicMock()

        assert r.reconnect(agent) is True
        assert r.client is new
        assert factory.call_count == 2

    def test_all_retries_fail(self):
        factory = MagicMock(side_effect=ConnectionError("down"))
        r = _make_reconnect(factory_fn=factory, max_retries=2)
        old = MagicMock()
        r.set_client(old)
        agent = MagicMock()

        assert r.reconnect(agent) is False
        assert r.client is old  # unchanged
        assert factory.call_count == 2

    def test_concurrent_reconnect_blocked(self):
        """Second thread waits for first to finish."""
        slow_factory = MagicMock(side_effect=lambda jwt: (time.sleep(0.1), MagicMock())[1])
        r = _make_reconnect(factory_fn=slow_factory, max_retries=1)
        r.set_client(MagicMock())

        results = []

        def do_reconnect():
            agent = MagicMock()
            results.append(r.reconnect(agent))

        t1 = threading.Thread(target=do_reconnect)
        t2 = threading.Thread(target=do_reconnect)
        t1.start()
        time.sleep(0.02)  # ensure t1 gets lock first
        t2.start()
        t1.join()
        t2.join()

        # Only one factory call (t2 sees the lock is held and waits)
        assert slow_factory.call_count == 1
        assert all(results)

    def test_tool_registry_updated(self):
        new = MagicMock()
        r = _make_reconnect(factory_fn=MagicMock(return_value=new))
        old = MagicMock()
        r.set_client(old)
        agent = MagicMock()

        r.reconnect(agent)
        agent.tool_registry.unload_tool_provider.assert_called_once_with(old)
        agent.tool_registry.process_tools.assert_called_once_with([new])


# ---------------------------------------------------------------------------
# 3. after_tool_hook
# ---------------------------------------------------------------------------


class TestAfterToolHook:
    def test_mcp_connection_closed_triggers_reconnect(self):
        new = MagicMock()
        r = _make_reconnect(factory_fn=MagicMock(return_value=new))
        r.set_client(MagicMock())
        event = _make_error_event(text="Tool execution failed: Connection to the MCP server was closed")

        r.after_tool_hook(event)
        assert r.client is new

    def test_mcp_client_initialization_error_triggers_reconnect(self):
        MCPClientInitError = type("MCPClientInitializationError", (Exception,), {})
        exc = MCPClientInitError("the client session is not running")
        new = MagicMock()
        r = _make_reconnect(factory_fn=MagicMock(return_value=new))
        r.set_client(MagicMock())
        event = _make_error_event(text=str(exc), exception=exc)

        r.after_tool_hook(event)
        assert r.client is new

    def test_non_mcp_error_does_not_trigger_reconnect(self):
        old = MagicMock()
        r = _make_reconnect()
        r.set_client(old)
        event = _make_error_event(text="File not found: /tmp/slides.json")

        r.after_tool_hook(event)
        assert r.client is old  # unchanged

    def test_success_result_does_not_trigger_reconnect(self):
        old = MagicMock()
        r = _make_reconnect()
        r.set_client(old)
        event = MagicMock()
        event.result = {"status": "success", "content": [{"text": "ok"}]}
        event.tool_use = {"name": "get_preview", "input": {}}

        r.after_tool_hook(event)
        assert r.client is old

    def test_non_dict_result_does_not_trigger_reconnect(self):
        old = MagicMock()
        r = _make_reconnect()
        r.set_client(old)
        event = MagicMock()
        event.result = "some string"
        event.tool_use = {"name": "get_preview", "input": {}}

        r.after_tool_hook(event)
        assert r.client is old


# ---------------------------------------------------------------------------
# 4. before_tool_hook
# ---------------------------------------------------------------------------


class TestBeforeToolHook:
    def test_no_block_when_not_reconnecting(self):
        r = _make_reconnect()
        event = MagicMock()
        start = time.time()
        r.before_tool_hook(event)
        assert time.time() - start < 0.1

    def test_blocks_during_reconnect(self):
        r = _make_reconnect()
        r._lock.acquire()  # simulate reconnection in progress

        blocked = []

        def call_hook():
            start = time.time()
            r.before_tool_hook(MagicMock())
            blocked.append(time.time() - start)

        t = threading.Thread(target=call_hook)
        t.start()
        time.sleep(0.1)
        r._lock.release()
        t.join()

        assert blocked[0] >= 0.05  # was blocked


# ---------------------------------------------------------------------------
# 5. is_mcp_error
# ---------------------------------------------------------------------------


class TestIsMcpError:
    @pytest.mark.parametrize("text", [
        "Connection to the MCP server was closed",
        "the client session is not running",
        "MCPClientInitializationError: session not running",
        "EOF on transport",
        "broken pipe",
        "failed to start mcp client",
    ])
    def test_mcp_errors_detected(self, text):
        r = _make_reconnect()
        event = _make_error_event(text=text)
        assert r._is_mcp_error(event) is True

    @pytest.mark.parametrize("text", [
        "File not found: /tmp/slides.json",
        "ValidationException: Too much media",
        "Invalid parameter: slide_index",
    ])
    def test_non_mcp_errors_not_detected(self, text):
        r = _make_reconnect()
        event = _make_error_event(text=text)
        assert r._is_mcp_error(event) is False

    def test_permanent_errors_not_detected(self):
        r = _make_reconnect()
        event = _make_error_event(text="unauthorized: invalid token")
        assert r._is_mcp_error(event) is False

    def test_non_error_status_not_detected(self):
        r = _make_reconnect()
        event = MagicMock()
        event.result = {"status": "success", "content": []}
        assert r._is_mcp_error(event) is False

    def test_non_dict_result_not_detected(self):
        r = _make_reconnect()
        event = MagicMock()
        event.result = "string"
        assert r._is_mcp_error(event) is False


# ---------------------------------------------------------------------------
# 6. drain_events / has_pending_events
# ---------------------------------------------------------------------------


class TestEvents:
    def test_initial_state_empty(self):
        r = _make_reconnect()
        assert r.has_pending_events() is False
        assert r.drain_events() == []

    def test_reconnect_emits_events(self):
        r = _make_reconnect(factory_fn=MagicMock(return_value=MagicMock()))
        r.set_client(MagicMock())
        agent = MagicMock()
        r.reconnect(agent)

        assert r.has_pending_events() is True
        events = r.drain_events()
        assert any(e["type"] == "reconnecting" for e in events)
        assert any(e["type"] == "reconnected" for e in events)

    def test_drain_clears_events(self):
        r = _make_reconnect(factory_fn=MagicMock(return_value=MagicMock()))
        r.set_client(MagicMock())
        r.reconnect(MagicMock())
        r.drain_events()
        assert r.has_pending_events() is False

    def test_failed_reconnect_emits_failed_event(self):
        r = _make_reconnect(factory_fn=MagicMock(side_effect=ConnectionError("down")), max_retries=1)
        r.set_client(MagicMock())
        r.reconnect(MagicMock())

        events = r.drain_events()
        assert any(e["type"] == "failed" for e in events)


# ---------------------------------------------------------------------------
# 7. Composer integration
# ---------------------------------------------------------------------------


class TestComposerIntegration:
    def test_new_client_returns_independent_instance(self):
        """new_client returns a different instance from the shared client."""
        shared = MagicMock()
        factory = MagicMock(side_effect=[MagicMock(), MagicMock()])
        r = _make_reconnect(factory_fn=factory)
        r.set_client(shared)

        independent = r.new_client()
        assert independent is not shared

    def test_composer_failure_does_not_affect_shared_client(self):
        """Composer's independent client dying doesn't change shared client."""
        shared = MagicMock()
        r = _make_reconnect()
        r.set_client(shared)

        # Simulate composer getting and losing a client
        group_mcp = r.new_client()
        del group_mcp  # "dies"

        assert r.client is shared
