"""临时调试：复现 Responses -> Chat Completions 翻译路径的异常。"""

from __future__ import annotations

import asyncio
import json
import os

os.environ["CODEX_AI_GATEWAY_DATA_DIR"] = os.environ.get(
    "CODEX_AI_GATEWAY_DATA_DIR",
    r"C:\Users\zhuhe\AppData\Local\Temp\gateway-visual-20260828000436",
)

from codex_ai_gateway.adapters.protocol_normal_form import normalize_request
from codex_ai_gateway.adapters.responses_chat_translation import (
    chat_request_from_normal,
)


async def main() -> None:
    body = {"model": "deepseek-chat", "input": "用一句话介绍你自己", "stream": False}
    try:
        normal = normalize_request(inbound_protocol="responses", body=body)
        print("messages:", len(normal.messages))
        chat_body = chat_request_from_normal(
            normal, target_model="deepseek-v4-flash"
        )
        print("chat_body:", json.dumps(chat_body, ensure_ascii=False)[:200])
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("TRANSLATE_ERROR", type(exc).__name__, exc)


if __name__ == "__main__":
    asyncio.run(main())
