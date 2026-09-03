""" "网关数据面：全局 token 认证、规范模型路由、协议选择与备用切换。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from codex_ai_gateway.adapters.protocol_normal_form import (
    UntranslatableCapabilityError,
    disable_unsupported_web_search,
    ensure_prefill_continuation,
    normalize_request,
    validate_translatable,
)
from codex_ai_gateway.adapters.responses_chat_translation import (
    chat_request_from_normal,
    response_completed_event,
    response_content_part_done_events,
    response_created_event,
    response_failed_event,
    response_function_call_done_events,
    response_message_done_event,
    response_message_started_events,
    response_sse,
    response_tool_call_started_event,
    translate_chat_chunk_to_response_event,
)
from codex_ai_gateway.adapters.responses_passthrough import ResponsesPassthrough
from codex_ai_gateway.adapters.sse_stream import SSEFrameBuffer
from codex_ai_gateway.api.errors import (
    GatewayError,
    gateway_error_response,
    make_auth_error,
    make_invalid_request,
)
from codex_ai_gateway.domain.error_mapping import map_provider_error
from codex_ai_gateway.domain.routing import (
    RoutingError,
    resolve_canonical_model,
    route_candidates,
)
from codex_ai_gateway.domain.usage import (
    estimate_usage_from_text,
    merge_usage_categories,
    parse_provider_usage,
    reporting_basis_for,
)
from codex_ai_gateway.models.entities import (
    GatewayToken,
    Outcome,
    ProviderErrorType,
    Upstream,
    UsageEvent,
    WireProtocol,
)
from codex_ai_gateway.runtime import Runtime
from codex_ai_gateway.services.gateway_token import verify_gateway_token
from codex_ai_gateway.services.upstreams import request_failure_fields
from codex_ai_gateway.util import utc_now

router = APIRouter()
logger = logging.getLogger("codex_ai_gateway.gateway")
FALLBACK_STATUSES = {429, 502, 503, 529}


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth:
        if not auth.lower().startswith("bearer "):
            return None
        return auth[7:].strip()
    return request.headers.get("x-api-key")


def _authenticate(request: Request, runtime: Runtime) -> GatewayToken:
    raw = _extract_bearer(request)
    if not raw:
        raise make_auth_error("missing_gateway_token", "请提供全局网关 token。")
    state = runtime.state_store.read_state()
    token = verify_gateway_token(raw, state.gateway_tokens, runtime.signing_key)
    if token is None:
        raise make_auth_error("unauthorized_gateway_token", "全局网关 token 无效或已吊销。")
    runtime.state_store.mutate(lambda s: _touch_token(s, token.id))
    return token


def _touch_token(state: Any, token_id: str) -> None:
    for token in state.gateway_tokens:
        if token.id == token_id:
            token.last_used_at = utc_now()


def _read_body(request: Request) -> dict[str, Any]:
    body = getattr(request.state, "gateway_body", None)
    if body is not None:
        return body
    return {}


async def _gateway(request: Request) -> Response:
    runtime = _runtime(request)
    candidates: list[tuple[Any, Upstream, WireProtocol]] = []
    canonical = None
    body: dict[str, Any] = {}
    try:
        _authenticate(request, runtime)
        raw_body = await request.body()
        try:
            body = json.loads(raw_body or b"{}")
        except json.JSONDecodeError as exc:
            raise make_invalid_request("invalid_json", "请求体不是有效 JSON。") from exc
        request.state.gateway_body = body
        model = body.get("model")
        if not model:
            raise make_invalid_request("missing_model", "请求缺少 model 字段。")
        state = runtime.state_store.read_state()
        canonical = resolve_canonical_model(state, str(model))
        candidates = route_candidates(state, canonical, prefer_chat=_has_custom_tools(body))
        if not candidates:
            raise RoutingError(
                code="no_available_upstream", message="该模型没有可用的健康上游。", status_code=503
            )
        return await _attempt_with_fallback(request, runtime, canonical.id, candidates, body)
    except GatewayError as exc:
        return gateway_error_response(
            error_type=exc.error_type,
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            details=exc.details,
            headers=exc.headers,
        )
    except RoutingError as exc:
        return gateway_error_response(
            error_type="invalid_request",
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
        )
    except UntranslatableCapabilityError as exc:
        return gateway_error_response(
            error_type="untranslatable_capability",
            code="untranslatable_capability",
            message=exc.message,
            status_code=422,
            details={"capability": exc.capability},
        )
    except Exception as exc:
        logger.exception("gateway request failed: %s", type(exc).__name__)
        return gateway_error_response(
            error_type="gateway_error",
            code="gateway_internal_error",
            message="网关内部错误。",
            status_code=500,
        )


@router.post("/v1/responses")
async def responses_v1(request: Request) -> Response:
    return await _gateway(request)


@router.post("/responses")
async def responses_root(request: Request) -> Response:
    return await _gateway(request)


async def _attempt_with_fallback(
    request: Request,
    runtime: Runtime,
    canonical_id: str,
    candidates: list[tuple[Any, Upstream, WireProtocol]],
    body: dict[str, Any],
) -> Response:
    last_error: GatewayError | None = None
    for ordinal, (offering, upstream, protocol) in enumerate(candidates, start=1):
        event = _new_event(runtime, canonical_id, offering, upstream, protocol, ordinal)
        runtime.usage_log.create_pending(event)
        try:
            custom_tool_names: set[str] = set()
            if protocol == WireProtocol.chat_completions:
                normal = normalize_request(inbound_protocol="responses", body=body)
                validate_translatable(normal)
                chat_body = chat_request_from_normal(
                    normal, target_model=offering.provider_model_id
                )
                custom_tool_names = normal.custom_tool_names
            else:
                chat_body = disable_unsupported_web_search(ensure_prefill_continuation(body))
            streaming = bool(body.get("stream"))
            if streaming:
                return await _stream_response(
                    request,
                    runtime,
                    event,
                    upstream,
                    chat_body,
                    protocol,
                    offering,
                    custom_tool_names=custom_tool_names,
                )
            result = await runtime.upstream_client.request(
                upstream,
                path="/chat/completions"
                if protocol == WireProtocol.chat_completions
                else "/responses",
                method="POST",
                json_body=chat_body,
                headers=_public_headers(request),
            )
            if result.status_code in FALLBACK_STATUSES:
                mapped = _mapped_error(result.status_code, result.body, upstream=upstream)
                runtime.state_store.mutate(
                    lambda state, upstream=upstream, result=result: _mark_upstream_failure(
                        state, upstream, result.status_code
                    )
                )
                _finalize(
                    runtime, event, Outcome.failed, mapped=mapped, status_code=result.status_code
                )
                last_error = mapped
                continue
            if result.status_code >= 400:
                mapped = _mapped_error(result.status_code, result.body, upstream=upstream)
                _finalize(
                    runtime, event, Outcome.failed, mapped=mapped, status_code=result.status_code
                )
                return mapped_response(mapped)
            _finalize_success(runtime, event, result.body)
            return Response(
                content=result.body,
                status_code=result.status_code,
                media_type=result.headers.get("content-type", "application/json"),
            )
        except Exception as exc:
            logger.warning("upstream attempt failed: %s", type(exc).__name__)
            mapped = _mapped_error(502, b"", error=exc, upstream=upstream)
            runtime.state_store.mutate(
                lambda state, upstream=upstream: _mark_upstream_failure(state, upstream, None)
            )
            _finalize(
                runtime,
                event,
                Outcome.failed,
                mapped=mapped,
                status_code=502,
                fallback_trigger="connection_failure",
            )
            last_error = mapped
            continue
    if last_error:
        return mapped_response(last_error)
    return gateway_error_response(
        error_type="provider_error",
        code="no_available_upstream",
        message="所有上游尝试失败。",
        status_code=502,
    )


def _mark_upstream_failure(state: Any, upstream: Upstream, status_code: int | None) -> None:
    fields = request_failure_fields(status_code)
    for index, item in enumerate(state.upstreams):
        if item.id == upstream.id:
            state.upstreams[index] = item.model_copy(update=fields)
            return


def _public_headers(request: Request) -> dict[str, str]:
    allowed = {"accept", "content-type", "user-agent"}
    return {k: v for k, v in request.headers.items() if k.lower() in allowed}


def _new_event(
    runtime: Runtime,
    canonical_id: str,
    offering: Any,
    upstream: Upstream,
    protocol: WireProtocol,
    ordinal: int,
) -> UsageEvent:
    state = runtime.state_store.read_state()
    canonical = next((m for m in state.canonical_models if m.id == canonical_id), None)
    return UsageEvent(
        id=str(__import__("codex_ai_gateway.util", fromlist=["uuid7"]).uuid7()),
        client_request_id=str(__import__("codex_ai_gateway.util", fromlist=["uuid7"]).uuid7()),
        started_at=utc_now(),
        upstream_id=upstream.id,
        upstream_label=upstream.name,
        offering_id=offering.id,
        canonical_model_id=canonical.id if canonical else canonical_id,
        canonical_model_label=canonical.slug if canonical else None,
        provider_model_id=offering.provider_model_id,
        inbound_protocol=WireProtocol.responses,
        outbound_protocol=protocol,
        outcome=Outcome.failed,
        attempt_ordinal=ordinal,
    )


def _mapped_error(
    status_code: int,
    body: bytes,
    *,
    error: Exception | None = None,
    upstream: Any = None,
) -> GatewayError:
    provider = map_provider_error(
        status_code,
        body=body,
        error_text=str(error) if error else None,
        upstream_name=getattr(upstream, "name", None),
    )
    return GatewayError(
        error_type="provider_error",
        code=provider.get("error_mapping_code", "provider_upstream_fault"),
        message=provider.get("message", "上游请求失败。"),
        status_code=status_code,
        details={
            "upstream_status": status_code,
            "upstream_error_type": provider.get("upstream_error_type"),
            "fingerprint": provider.get("fingerprint"),
        },
    )


def _has_custom_tools(body: dict[str, Any]) -> bool:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return False
    return any(isinstance(tool, dict) and tool.get("type") == "custom" for tool in tools)


def mapped_response(exc: GatewayError) -> Response:
    return gateway_error_response(
        error_type=exc.error_type,
        code=exc.code,
        message=exc.message,
        status_code=exc.status_code,
        details=exc.details,
    )


async def _stream_response(
    request: Request,
    runtime: Runtime,
    event: UsageEvent,
    upstream: Upstream,
    chat_body: dict[str, Any] | None,
    protocol: WireProtocol,
    offering: Any,
    *,
    custom_tool_names: set[str] | None = None,
) -> Response:
    path = "/chat/completions" if protocol == WireProtocol.chat_completions else "/responses"
    upstream_stream = await runtime.upstream_client.open_stream(
        upstream,
        path=path,
        method="POST",
        json_body=chat_body,
        headers=_public_headers(request),
    )
    if (
        upstream_stream.status_code in FALLBACK_STATUSES
        and upstream_stream.first_byte_ms is not None
    ):
        error_body = await upstream_stream.read_error_body()
        await upstream_stream.aclose()
        raise RoutingError(
            code="fallback_upstream_error",
            message=f"上游返回 {upstream_stream.status_code}，尝试备用上游。",
            status_code=upstream_stream.status_code,
        )
    if upstream_stream.status_code >= 400:
        error_body = await upstream_stream.read_error_body()
        await upstream_stream.aclose()
        mapped = _mapped_error(upstream_stream.status_code, error_body, upstream=upstream)
        _finalize(
            runtime, event, Outcome.failed, mapped=mapped, status_code=upstream_stream.status_code
        )
        return mapped_response(mapped)

    is_chat = protocol == WireProtocol.chat_completions
    model_label = event.canonical_model_label or event.provider_model_id

    async def iterator() -> AsyncIterator[bytes]:
        started = False
        stream_initialized = False
        message_started = False
        accumulated_text = ""
        accumulated_tool_calls: dict[int, dict[str, Any]] = {}
        last_chat_chunk: dict[str, Any] | None = None
        last_finish_reason: str | None = None
        emitted_tool_starts: set[int] = set()
        sse_buffer = SSEFrameBuffer()
        responses_passthrough = ResponsesPassthrough()
        READ_TIMEOUT = 15  # seconds per tick → keepalive or error
        MAX_IDLE_TICKS = 8  # 8 × 15s = 120s without data → error
        keepalive_frame = b": keepalive\n\n"

        stream_iter = upstream_stream.__aiter__()

        async def _next_chunk() -> bytes | None:
            """读一个 chunk，超时抛出 asyncio.TimeoutError。"""
            try:
                return await asyncio.wait_for(stream_iter.__anext__(), timeout=READ_TIMEOUT)
            except StopAsyncIteration:
                return None

        idle_ticks = 0
        try:
            while True:
                try:
                    chunk = await _next_chunk()
                except TimeoutError:
                    idle_ticks += 1
                    if idle_ticks >= MAX_IDLE_TICKS:
                        raise TimeoutError(
                            f"上游 {READ_TIMEOUT * MAX_IDLE_TICKS}s 无数据，中止流式传输。"
                        ) from None
                    # Send keepalive to prevent proxy timeout
                    yield keepalive_frame
                    continue

                if chunk is None:
                    break  # upstream stream ended

                started = True
                idle_ticks = 0

                if not is_chat:
                    # Responses 协议透传 + 孤儿 delta 修补
                    frames = responses_passthrough.feed(chunk)
                    for frame in frames:
                        yield frame
                    continue

                # Chat 协议：SSE 解析 + 翻译
                events = sse_buffer.feed(chunk)
                for parsed in events:
                    last_chat_chunk = parsed
                    choice_zero = (parsed.get("choices") or [{}])[0]
                    if choice_zero.get("finish_reason"):
                        last_finish_reason = choice_zero["finish_reason"]
                    if not stream_initialized:
                        yield response_sse(response_created_event(model_label))
                        stream_initialized = True

                    translated = translate_chat_chunk_to_response_event(parsed)
                    if translated is not None:
                        if translated.get("type") == "response.output_text.delta":
                            if not message_started:
                                for lifecycle_event in response_message_started_events():
                                    yield response_sse(lifecycle_event)
                                message_started = True
                            accumulated_text += str(translated.get("delta", ""))
                        yield response_sse(translated)

                    # 累积 tool_calls 信息（供 finalize 使用）
                    tc_list = (parsed.get("choices") or [{}])[0].get("delta", {}).get(
                        "tool_calls"
                    ) or []
                    for tc in tc_list:
                        tc_index = tc.get("index", 0)
                        if tc_index not in emitted_tool_starts:
                            fn_preview = tc.get("function") or {}
                            if tc.get("id") or fn_preview.get("name"):
                                call_id = tc.get("id") or f"call_{tc_index}"
                                is_custom = (fn_preview.get("name") or "") in (custom_tool_names or set())
                                yield response_sse(
                                    response_tool_call_started_event(
                                        tc_index, call_id, fn_preview.get("name") or "", is_custom=is_custom
                                    )
                                )
                                emitted_tool_starts.add(tc_index)
                        existing = accumulated_tool_calls.setdefault(
                            tc_index,
                            {
                                "index": tc_index,
                                "id": None,
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if tc.get("id"):
                            existing["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            existing["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            existing["function"]["arguments"] += fn["arguments"]

            # flush remaining buffer
            if not is_chat:
                for frame in responses_passthrough.flush():
                    yield frame

            if is_chat:
                for parsed in sse_buffer.flush():
                    translated = translate_chat_chunk_to_response_event(parsed)
                    if translated is not None:
                        if translated.get("type") == "response.output_text.delta":
                            if not message_started:
                                for lifecycle_event in response_message_started_events():
                                    yield response_sse(lifecycle_event)
                                message_started = True
                            accumulated_text += str(translated.get("delta", ""))
                        yield response_sse(translated)

            if is_chat:
                if not stream_initialized:
                    yield response_sse(response_created_event(model_label))
                    stream_initialized = True

                # 纯 tool call 回合不产生空 assistant message，
                # 否则 Codex 回放历史时会在 tool_calls 与 tool 响应之间插入空消息
                if message_started:
                    for done_ev in response_content_part_done_events(accumulated_text):
                        yield response_sse(done_ev)

                # Send tool_call done events
                if accumulated_tool_calls:
                    for tc_done in response_function_call_done_events(
                        [accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls)],
                        custom_tool_names=custom_tool_names,
                    ):
                        yield response_sse(tc_done)

                if message_started:
                    yield response_sse(response_message_done_event(accumulated_text))

                output_items: list[dict[str, Any]] = []
                for i in sorted(accumulated_tool_calls):
                    tc = accumulated_tool_calls[i]
                    fn = tc.get("function") or {}
                    call_id = tc.get("id") or f"call_{i}"
                    if fn.get("name") in (custom_tool_names or set()):
                        try:
                            parsed_args = json.loads(fn.get("arguments") or "{}")
                        except (TypeError, json.JSONDecodeError):
                            parsed_args = {}
                        inp = parsed_args.get("input") if isinstance(parsed_args, dict) else ""
                        output_items.append({
                            "id": call_id,
                            "type": "custom_tool_call",
                            "call_id": call_id,
                            "name": fn.get("name") or "",
                            "input": inp if isinstance(inp, str) else "",
                            "status": "completed",
                        })
                    else:
                        output_items.append({
                            "id": call_id,
                            "type": "function_call",
                            "call_id": call_id,
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "",
                            "status": "completed",
                        })
                if accumulated_text:
                    output_items.append({
                        "id": "msg_placeholder",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": accumulated_text, "annotations": []}],
                    })

                yield response_sse(
                    response_completed_event(
                        model=model_label,
                        finish_reason=last_finish_reason,
                        output=output_items,
                        usage=(last_chat_chunk or {}).get("usage"),
                    )
                )

            _finalize_success(runtime, event, b"", streaming=True)
        except Exception as exc:
            error_msg = f"上游 {upstream.name} 流式传输异常: {type(exc).__name__}: {exc}"
            _finalize(
                runtime,
                event,
                Outcome.failed if started else Outcome.interrupted,
                mapped=_mapped_error(502, b"", error=exc, upstream=upstream),
                status_code=502,
            )
            # Notify client with response.failed event instead of raising
            if started and stream_initialized:
                try:
                    yield response_sse(response_failed_event(model_label, error_msg))
                    yield response_sse(response_completed_event(model=model_label, usage=None))
                except Exception:
                    pass
            raise
        finally:
            await upstream_stream.aclose()

    return StreamingResponse(iterator(), media_type="text/event-stream")


def _finalize_success(
    runtime: Runtime, event: UsageEvent, body: bytes, *, streaming: bool = False
) -> None:
    usage = parse_provider_usage(body)
    estimated = estimate_usage_from_text(body)
    merged = merge_usage_categories(usage, estimated)
    event.token_usage_by_category = merged
    event.reporting_basis = reporting_basis_for(usage)
    event.outcome = Outcome.completed
    event.duration_ms = 0
    runtime.usage_log.record_finalized(event)


def _finalize(
    runtime: Runtime,
    event: UsageEvent,
    outcome: Outcome,
    *,
    mapped: GatewayError | None = None,
    status_code: int | None = None,
    fallback_trigger: str | None = None,
) -> None:
    event.outcome = outcome
    event.duration_ms = 0
    if status_code:
        event.http_upstream_status = status_code
    if mapped:
        event.error_mapping_code = mapped.code
        event.provider_error_type = ProviderErrorType(
            mapped.details.get("provider_error_type", "upstream_fault")
        )
    if fallback_trigger:
        event.fallback_trigger = fallback_trigger
    runtime.usage_log.record_finalized(event)
