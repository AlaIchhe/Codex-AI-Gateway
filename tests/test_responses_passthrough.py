"""Tests for responses_passthrough: orphan delta detection and injection."""

from __future__ import annotations

import json

from codex_ai_gateway.adapters.responses_passthrough import ResponsesPassthrough


def _sse(event_type: str, data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n".encode()


def _parse_frames(frames: list[bytes]) -> list[dict]:
    events = []
    for frame in frames:
        text = frame.decode()
        for line in text.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class TestOrphanReasoningDelta:
    def test_injects_reasoning_item_when_orphan(self) -> None:
        rp = ResponsesPassthrough()
        chunk = _sse("response.reasoning_summary_delta", {
            "type": "response.reasoning_summary_delta",
            "item_id": "orig-1",
            "delta": "thinking...",
        })
        frames = rp.feed(chunk)
        events = _parse_frames(frames)

        types = [e["type"] for e in events]
        assert types == [
            "response.output_item.added",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_delta",
        ]
        # The injected item should be reasoning type
        assert events[0]["item"]["type"] == "reasoning"

    def test_no_inject_when_reasoning_item_exists(self) -> None:
        rp = ResponsesPassthrough()
        # First, send a proper output_item.added for reasoning
        added = _sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "rs-1", "type": "reasoning", "status": "in_progress"},
        })
        delta = _sse("response.reasoning_summary_delta", {
            "type": "response.reasoning_summary_delta",
            "item_id": "rs-1",
            "delta": "thinking...",
        })
        frames = rp.feed(added) + rp.feed(delta)
        events = _parse_frames(frames)

        types = [e["type"] for e in events]
        assert types == [
            "response.output_item.added",
            "response.reasoning_summary_delta",
        ]

    def test_no_inject_after_item_done(self) -> None:
        rp = ResponsesPassthrough()
        added = _sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "rs-1", "type": "reasoning", "status": "in_progress"},
        })
        done = _sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"id": "rs-1", "type": "reasoning", "status": "completed"},
        })
        # After done, a new reasoning delta should trigger injection again
        delta = _sse("response.reasoning_summary_delta", {
            "type": "response.reasoning_summary_delta",
            "item_id": "rs-2",
            "delta": "more thinking...",
        })
        frames = rp.feed(added) + rp.feed(done) + rp.feed(delta)
        events = _parse_frames(frames)

        types = [e["type"] for e in events]
        assert types == [
            "response.output_item.added",
            "response.output_item.done",
            "response.output_item.added",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_delta",
        ]


class TestOrphanOutputTextDelta:
    def test_injects_message_item_when_orphan(self) -> None:
        rp = ResponsesPassthrough()
        chunk = _sse("response.output_text.delta", {
            "type": "response.output_text.delta",
            "item_id": "msg-1",
            "delta": "Hello!",
        })
        frames = rp.feed(chunk)
        events = _parse_frames(frames)

        types = [e["type"] for e in events]
        assert types == [
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
        ]
        assert events[0]["item"]["type"] == "message"
        assert events[0]["item"]["role"] == "assistant"

    def test_no_inject_when_message_item_exists(self) -> None:
        rp = ResponsesPassthrough()
        added = _sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg-1", "type": "message", "status": "in_progress", "role": "assistant", "content": []},
        })
        delta = _sse("response.output_text.delta", {
            "type": "response.output_text.delta",
            "item_id": "msg-1",
            "delta": "Hello!",
        })
        frames = rp.feed(added) + rp.feed(delta)
        events = _parse_frames(frames)

        types = [e["type"] for e in events]
        assert types == [
            "response.output_item.added",
            "response.output_text.delta",
        ]


class TestPassthrough:
    def test_created_and_completed_passthrough(self) -> None:
        rp = ResponsesPassthrough()
        created = _sse("response.created", {"type": "response.created", "response": {"id": "resp-1"}})
        completed = _sse("response.completed", {"type": "response.completed", "response": {"id": "resp-1"}})
        frames = rp.feed(created) + rp.feed(completed)
        events = _parse_frames(frames)
        assert [e["type"] for e in events] == ["response.created", "response.completed"]

    def test_output_item_added_tracked(self) -> None:
        rp = ResponsesPassthrough()
        added = _sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "fc-1", "type": "function_call", "status": "in_progress", "call_id": "c1", "name": "web_search", "arguments": ""},
        })
        frames = rp.feed(added)
        events = _parse_frames(frames)
        assert len(events) == 1
        assert events[0]["type"] == "response.output_item.added"

    def test_function_call_delta_no_inject_when_item_active(self) -> None:
        rp = ResponsesPassthrough()
        added = _sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "fc-1", "type": "function_call", "status": "in_progress", "call_id": "c1", "name": "search", "arguments": ""},
        })
        delta = _sse("response.function_call_arguments.delta", {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc-1",
            "delta": '{"query": "test"}',
        })
        frames = rp.feed(added) + rp.feed(delta)
        events = _parse_frames(frames)
        assert [e["type"] for e in events] == [
            "response.output_item.added",
            "response.function_call_arguments.delta",
        ]


class TestChunkedStreaming:
    def test_multi_chunk_orphan_delta(self) -> None:
        """孤儿 delta 跨多个 chunk 到达时仍能正确修补。"""
        rp = ResponsesPassthrough()
        full_event = _sse("response.reasoning_summary_delta", {
            "type": "response.reasoning_summary_delta",
            "item_id": "rs-1",
            "delta": "thinking...",
        })
        # Split into two chunks
        mid = len(full_event) // 2
        frames = rp.feed(full_event[:mid])
        assert frames == []  # incomplete frame, buffered

        frames = rp.feed(full_event[mid:])
        events = _parse_frames(frames)
        types = [e["type"] for e in events]
        assert types == [
            "response.output_item.added",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_delta",
        ]

    def test_flush_remaining(self) -> None:
        rp = ResponsesPassthrough()
        event = _sse("response.reasoning_summary_delta", {
            "type": "response.reasoning_summary_delta",
            "item_id": "rs-1",
            "delta": "end",
        })
        # Feed without trailing \n\n so it stays in buffer
        partial = event[:-2]
        frames = rp.feed(partial)
        assert frames == []
        frames = rp.flush()
        events = _parse_frames(frames)
        assert len(events) >= 1
