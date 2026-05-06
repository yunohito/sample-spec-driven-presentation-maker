# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SSE streaming: event transformation, keepalive, and cancellation management."""

import asyncio
import json
import logging
import resource as _resource
from typing import AsyncGenerator

from partial_json_parser import loads as _partial_loads
from strands import Agent

logger = logging.getLogger("sdpm.agent")

KEEPALIVE_INTERVAL = 5


async def stream_agent(agent: Agent, user_query: str, session_id: str, cancel: asyncio.Event, reconnect_handler=None) -> AsyncGenerator:
    """Stream agent responses as SSE events with keepalive and safe cancellation.

    Args:
        agent: Initialized Strands Agent.
        user_query: User's input message.
        session_id: Session ID (for logging).
        cancel: Event that signals cancellation request.
        reconnect_handler: Optional MCPReconnect for reconnection events.

    Yields:
        SSE event dicts.
    """
    last_tool_use = None
    last_tool_use_id = ""
    last_yielded_input: dict = {}  # track fully-parsed input we've already sent
    tool_name_map: dict[str, str] = {}
    in_tool_execution = False
    in_tool_since: float = 0.0  # monotonic time when in_tool became True
    _TOOL_TIMEOUT = 360  # seconds — must exceed MCP Server's sessionTimeoutSeconds (300)

    def _tool_payload(tu: dict) -> dict:
        raw = tu.get("input", "")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) and raw else raw
        except (ValueError, TypeError):
            parsed = {}
        return {"toolUse": {"name": tu.get("name", ""), "toolUseId": tu.get("toolUseId", ""), "input": parsed if isinstance(parsed, dict) else {}}}

    def _parse_input(tu: dict) -> dict | None:
        """Try to parse tool input JSON, including incomplete streaming JSON."""
        raw = tu.get("input", "")
        if isinstance(raw, dict):
            return raw if raw else None
        if not isinstance(raw, str) or not raw:
            return None
        try:
            parsed = _partial_loads(raw)
            return parsed if isinstance(parsed, dict) and parsed else None
        except Exception:
            return None

    def _should_stop() -> bool:
        return cancel.is_set() and not in_tool_execution

    async def _next(aiter):
        return await aiter.__anext__()

    stream_iter = agent.stream_async(user_query).__aiter__()
    pending = None
    keepalive_count = 0

    logger.info("stream_agent started for session %s", session_id[:12])

    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(_next(stream_iter))
            done, _ = await asyncio.wait({pending}, timeout=KEEPALIVE_INTERVAL)
            if done:
                try:
                    event = pending.result()
                    if isinstance(event, dict) and "event" in event:
                        yield event
                    elif isinstance(event, dict) and "current_tool_use" in event:
                        tu = event["current_tool_use"]
                        tu_id = tu.get("toolUseId", "")
                        if tu_id and tu_id != last_tool_use_id:
                            if last_tool_use:
                                yield _tool_payload(last_tool_use)
                            last_tool_use_id = tu_id
                            last_yielded_input = {}
                            tool_name_map[tu_id] = tu.get("name", "")
                            in_tool_execution = True
                            in_tool_since = asyncio.get_event_loop().time()
                            yield {"toolStart": {"name": tu.get("name", ""), "toolUseId": tu_id}}
                        last_tool_use = dict(tu)
                        # Early-emit full input as soon as JSON is parseable,
                        # so the UI can show instructions before the streaming tool finishes.
                        parsed = _parse_input(tu)
                        if parsed and parsed != last_yielded_input:
                            last_yielded_input = parsed
                            yield {"toolUse": {"name": tu.get("name", ""), "toolUseId": tu_id, "input": parsed}}
                    elif isinstance(event, dict) and "tool_stream_event" in event:
                        tse = event["tool_stream_event"]
                        data = tse.get("data")
                        tu = tse.get("tool_use", {})
                        if isinstance(data, dict):
                            yield {"toolStream": {"toolUseId": tu.get("toolUseId", last_tool_use_id), "name": tu.get("name", ""), "data": data}}
                    elif isinstance(event, dict) and "message" in event:
                        msg = event["message"]
                        if isinstance(msg, dict) and msg.get("role") == "user":
                            for block in msg.get("content", []):
                                if isinstance(block, dict) and "toolResult" in block:
                                    tr = block["toolResult"]
                                    tu_id = tr.get("toolUseId", "")
                                    content_text = ""
                                    for c in tr.get("content", []):
                                        if isinstance(c, dict) and "text" in c:
                                            content_text = c["text"]
                                            break
                                    in_tool_execution = False
                                    yield {"toolResult": {
                                        "toolUseId": tu_id,
                                        "name": tool_name_map.get(tu_id, ""),
                                        "status": tr.get("status", "success"),
                                        "content": content_text,
                                    }}
                    pending = None

                    # Drain reconnect events after processing
                    if reconnect_handler and reconnect_handler.has_pending_events():
                        for ev in reconnect_handler.drain_events():
                            yield {"mcp_status": ev}

                    if _should_stop():
                        logger.info("Stopping stream for session %s", session_id[:12])
                        break

                except StopAsyncIteration:
                    if last_tool_use:
                        yield _tool_payload(last_tool_use)
                    logger.info("stream_agent completed for session %s (keepalives=%d)", session_id[:12], keepalive_count)
                    break
            else:
                # Drain reconnect events during idle
                if reconnect_handler and reconnect_handler.has_pending_events():
                    for ev in reconnect_handler.drain_events():
                        yield {"mcp_status": ev}
                # Tool execution timeout detection
                if in_tool_execution and in_tool_since and (asyncio.get_event_loop().time() - in_tool_since) > _TOOL_TIMEOUT:
                    logger.error("Tool execution timeout (%ds) for session %s — cancelling", _TOOL_TIMEOUT, session_id[:12])
                    in_tool_execution = False
                    cancel.set()
                yield {"keepalive": True}
                keepalive_count += 1
                if keepalive_count % 10 == 0:
                    rss_kb = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
                    logger.info("stream_agent keepalive #%d for session %s (in_tool=%s, cancel=%s, rss_kb=%d)",
                                keepalive_count, session_id[:12], in_tool_execution, cancel.is_set(), rss_kb)
                if _should_stop():
                    logger.info("Stopping stream (idle) for session %s", session_id[:12])
                    break
    except GeneratorExit:
        logger.warning("stream_agent client disconnected (GeneratorExit) for session %s", session_id[:12])
    except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
        logger.warning("stream_agent client connection lost for session %s: %s", session_id[:12], e)
    except Exception as e:
        logger.exception("stream_agent unexpected error for session %s: %s", session_id[:12], e)
        yield {"status": "error", "error": str(e)}
