"""健壮的 SSE 流解析器：增量 UTF-8 解码 + 正确帧分隔 + 多行 data 拼接。

解决的问题：
1. UTF-8 多字节字符跨 chunk 边界时不再丢失（使用增量解码器而非 errors="ignore"）
2. 同时支持 \\n\\n 和 \\r\\n\\r\\n 帧分隔符
3. 多行 `data:` 行正确拼接
4. 提供 SSEFrameBuffer 供异步流式消费使用
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass, field
from typing import Any

_FRAME_SPLIT = re.compile(r"\r?\n\r?\n")


def _flush_complete_frames(buffer: str) -> tuple[list[str], str]:
    """从 buffer 中提取所有完整 SSE 帧，返回 (frames, remainder)。

    使用 re.split 保留分隔符信息，兼容 \\n\\n 和 \\r\\n\\r\\n。
    """
    parts = _FRAME_SPLIT.split(buffer)
    if len(parts) <= 1:
        return [], buffer
    frames = parts[:-1]
    remainder = parts[-1]
    return frames, remainder


@dataclass
class SSEFrameBuffer:
    """增量 SSE 帧解码器：接收 bytes chunk，产出完整 SSE data payload dict。

    用法：
        fb = SSEFrameBuffer()
        for chunk in upstream_stream:
            for event in fb.feed(chunk):
                process(event)
    """

    _decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"),
        repr=False,
    )
    _text_buffer: str = field(default="", repr=False)

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        """喂入一个 bytes chunk，返回完整帧解析出的 JSON 事件列表。"""
        text = self._decoder.decode(chunk)
        self._text_buffer += text
        frames, self._text_buffer = _flush_complete_frames(self._text_buffer)
        events = []
        for frame in frames:
            event = parse_chat_sse_frame(frame)
            if event is not None:
                events.append(event)
        return events

    def flush(self) -> list[dict[str, Any]]:
        """流结束时调用：处理残留在 buffer 中的最后一帧（如果有的话）。"""
        text = self._decoder.decode(b"", final=True)
        self._text_buffer += text
        if not self._text_buffer.strip():
            return []
        frame = self._text_buffer
        self._text_buffer = ""
        event = parse_chat_sse_frame(frame)
        return [event] if event is not None else []


def parse_chat_sse_frame(frame: str) -> dict[str, Any] | None:
    """解析一个 SSE 帧（可能含 \\r）；注释、空帧和 DONE 返回 None。

    正确处理多行 data: 行拼接（SSE 规范：多个 data 行以 \\n 连接）。
    """
    data_lines: list[str] = []
    for line in frame.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        elif line.startswith("data: "):
            data_lines.append(line[6:])
    if not data_lines:
        return None
    payload = "\n".join(data_lines)
    if not payload or payload.strip() == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
