""" "网关数据面：全局 token 认证、规范模型路由、协议选择与备用切换。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
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
    restore_namespace_tool_name,
    translate_chat_chunk_to_response_event,
)
from codex_ai_gateway.adapters.responses_passthrough import ResponsesPassthrough
from codex_ai_gateway.adapters.sse_stream import SSEFrameBuffer
from codex_ai_gateway.api.errors import (
    GatewayError,
    gateway_error_response,
    make_auth_error,
    make_invalid_request,
    make_untranslatable,
)
from codex_ai_gateway.domain.circuit_breaker import (
    CONTENT_POLICY_REASON,
    FailureClassification,
    FailureDecision,
)
from codex_ai_gateway.domain.error_mapping import ERROR_EXCERPT_LIMIT, map_provider_error
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
from codex_ai_gateway.util import utc_now

router = APIRouter()
logger = logging.getLogger("codex_ai_gateway.gateway")

# token 最近使用时间的落盘节流窗口：内存里每次都更新，落盘按窗口合并。
TOKEN_TOUCH_INTERVAL_SECONDS = 60.0
_token_persisted_at: dict[str, float] = {}
_TOKEN_TOUCH_LOCK = threading.Lock()


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
    _record_token_use(runtime, token)
    return token


def _record_token_use(runtime: Runtime, token: GatewayToken) -> None:
    """记录 token 最近使用时间。

    内存内每次请求都更新（管理端立即可见），落盘按 token 节流：远端
    admin-state.json 已达 21MB，逐请求全量重写会拖慢数据面。记录失败只告警。
    """
    now = utc_now()
    token.last_used_at = now
    monotonic_now = time.monotonic()
    with _TOKEN_TOUCH_LOCK:
        last_persisted = _token_persisted_at.get(token.id)
        if (
            last_persisted is not None
            and monotonic_now - last_persisted < TOKEN_TOUCH_INTERVAL_SECONDS
        ):
            return
        _token_persisted_at[token.id] = monotonic_now
    try:
        runtime.state_store.mutate(lambda s: _touch_token(s, token.id, now))
    except Exception:  # noqa: BLE001 - 使用时间属于遥测，落盘失败不阻断请求
        logger.warning("记录 token 使用时间失败（不影响本次请求）", exc_info=True)


def _touch_token(state: Any, token_id: str, used_at: str) -> None:
    for token in state.gateway_tokens:
        if token.id == token_id:
            token.last_used_at = used_at


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
        candidates = route_candidates(
            state,
            canonical,
            prefer_chat=_has_custom_tools(body),
            circuit_breaker=runtime.circuit_breaker,
        )
        if not candidates:
            # 方案 A：不存在“全部目标冷却 → 拒绝请求”的状态。候选为空只可能
            # 是没有已批准 offering / 启用上游，与失败避让无关。
            raise GatewayError(
                error_type="provider_error",
                code="no_available_upstream",
                message="该模型暂无可用上游，请检查上游配置或模型列表。",
                status_code=503,
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


def _retry_after_headers(seconds: float | None) -> dict[str, str] | None:
    if seconds is None:
        return None
    return {"Retry-After": str(max(1, math.ceil(seconds)))}


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
    last_retry_after: float | None = None
    # 命中「不在套餐内」的记录：见 _register_not_in_plan 的剔除规则。
    not_in_plan_seen: dict[tuple[str, str], set[WireProtocol]] = {}
    dead_targets: set[tuple[str, str]] = set()
    # 命中内容审查的上游：一次请求里的候选都是同一个 canonical model，过滤词表
    # 只跟上游有关，所以它在该上游的另一个协议面上必然同样拒收——换协议没有意义，
    # 直接跳过它的剩余候选，省一次注定失败的上游往返。
    content_blocked_upstreams: set[str] = set()
    for ordinal, (offering, upstream, protocol) in enumerate(candidates, start=1):
        if (upstream.id, offering.provider_model_id) in dead_targets:
            continue
        if upstream.id in content_blocked_upstreams:
            continue
        event = _new_event(runtime, canonical_id, offering, upstream, protocol, ordinal)
        runtime.usage_log.create_pending(event)
        chat_body: dict[str, Any] | None = None
        try:
            custom_tool_names: set[str] = set()
            namespace_tool_aliases: dict[str, dict[str, str]] = {}
            if protocol == WireProtocol.chat_completions:
                normal = normalize_request(inbound_protocol="responses", body=body)
                validate_translatable(normal)
                chat_body = chat_request_from_normal(
                    normal, target_model=offering.provider_model_id
                )
                custom_tool_names = normal.custom_tool_names
                namespace_tool_aliases = normal.namespace_tool_aliases
            else:
                chat_body = disable_unsupported_web_search(ensure_prefill_continuation(body))
            streaming = bool(body.get("stream"))
            path = (
                "/chat/completions"
                if protocol == WireProtocol.chat_completions
                else "/responses"
            )
            if streaming:
                upstream_stream = await runtime.upstream_client.open_stream(
                    upstream,
                    path=path,
                    method="POST",
                    json_body=chat_body,
                    headers=_public_headers(request),
                )
                if upstream_stream.status_code >= 400:
                    status_code = upstream_stream.status_code
                    error_headers = dict(upstream_stream.headers)
                    error_body = await upstream_stream.read_error_body()
                    await upstream_stream.aclose()
                    mapped, classification = _record_upstream_failure(
                        runtime,
                        upstream,
                        offering.provider_model_id,
                        wire_protocol=protocol,
                        status_code=status_code,
                        headers=error_headers,
                        body=error_body,
                    )
                    if mapped.code == _NOT_IN_PLAN_CODE:
                        _register_not_in_plan(
                            runtime,
                            upstream,
                            offering,
                            protocol,
                            seen=not_in_plan_seen,
                            dead=dead_targets,
                        )
                    if classification.reason == CONTENT_POLICY_REASON:
                        content_blocked_upstreams.add(upstream.id)
                    _remember_request_digest(event, protocol, chat_body)
                    _finalize(
                        runtime, event, Outcome.failed, mapped=mapped, status_code=status_code
                    )
                    if _should_hop(offering, classification, status_code):
                        last_error = _prefer_terminal_error(last_error, mapped)
                        last_retry_after = runtime.circuit_breaker.remaining(
                            upstream.id,
                            offering.provider_model_id,
                            wire_protocol=protocol,
                        )
                        continue
                    return mapped_response(mapped)
                return await _stream_response(
                    request,
                    runtime,
                    event,
                    upstream,
                    upstream_stream,
                    protocol,
                    offering,
                    custom_tool_names=custom_tool_names,
                    namespace_tool_aliases=namespace_tool_aliases,
                )
            result = await runtime.upstream_client.request(
                upstream,
                path=path,
                method="POST",
                json_body=chat_body,
                headers=_public_headers(request),
            )
            if result.status_code >= 400:
                mapped, classification = _record_upstream_failure(
                    runtime,
                    upstream,
                    offering.provider_model_id,
                    wire_protocol=protocol,
                    status_code=result.status_code,
                    headers=result.headers,
                    body=result.body,
                )
                if mapped.code == _NOT_IN_PLAN_CODE:
                    _register_not_in_plan(
                        runtime,
                        upstream,
                        offering,
                        protocol,
                        seen=not_in_plan_seen,
                        dead=dead_targets,
                    )
                if classification.reason == CONTENT_POLICY_REASON:
                    content_blocked_upstreams.add(upstream.id)
                _remember_request_digest(event, protocol, chat_body)
                _finalize(
                    runtime, event, Outcome.failed, mapped=mapped, status_code=result.status_code
                )
                if _should_hop(offering, classification, result.status_code):
                    last_error = _prefer_terminal_error(last_error, mapped)
                    last_retry_after = runtime.circuit_breaker.remaining(
                        upstream.id,
                        offering.provider_model_id,
                        wire_protocol=protocol,
                    )
                    continue
                return mapped_response(mapped)
            runtime.circuit_breaker.record_success(
                upstream.id, offering.provider_model_id, wire_protocol=protocol
            )
            _learn_protocol(runtime, upstream, offering, protocol)
            _finalize_success(runtime, event, result.body)
            return Response(
                content=result.body,
                status_code=result.status_code,
                media_type=result.headers.get("content-type", "application/json"),
            )
        except UntranslatableCapabilityError as exc:
            # 客户端/翻译层能力错误：请求本身无法翻译，与上游健康无关。
            # 直接返回 4xx，不记录失败、不触发任何避让/冷却。
            mapped = make_untranslatable(exc.message, exc.capability)
            _finalize(
                runtime,
                event,
                Outcome.failed,
                mapped=mapped,
                status_code=mapped.status_code,
            )
            return mapped_response(mapped)
        except Exception as exc:
            logger.warning("upstream attempt failed: %s", type(exc).__name__)
            mapped, classification = _record_upstream_failure(
                runtime,
                upstream,
                offering.provider_model_id,
                wire_protocol=protocol,
                status_code=None,
                headers={},
                body=b"",
                error=exc,
            )
            _remember_request_digest(event, protocol, chat_body)
            _finalize(
                runtime,
                event,
                Outcome.failed,
                mapped=mapped,
                status_code=502,
                fallback_trigger="connection_failure",
            )
            if _should_hop(offering, classification, None):
                last_error = _prefer_terminal_error(last_error, mapped)
                last_retry_after = runtime.circuit_breaker.remaining(
                    upstream.id,
                    offering.provider_model_id,
                    wire_protocol=protocol,
                )
                continue
            return mapped_response(mapped)
    if last_error:
        return mapped_response(last_error, headers=_retry_after_headers(last_retry_after))
    return gateway_error_response(
        error_type="provider_error",
        code="no_available_upstream",
        message="所有上游尝试失败。",
        status_code=502,
        headers=_retry_after_headers(last_retry_after),
    )


# 终端错误优先级：数字越大越应该让客户端看到。上游限流/故障比「端点不支持该模型」
# 更能解释「为什么这次请求失败」。
_TERMINAL_ERROR_RANK: dict[str, int] = {
    # 内容审查拒收排最高：它是唯一「等多久都不会自愈、必须用户动手」的原因。
    # 若被限流类错误盖掉，用户会一直重试同一份带毒上下文。
    ProviderErrorType.content_policy.value: 4,
    ProviderErrorType.rate_limit.value: 3,
    ProviderErrorType.quota_budget.value: 3,
    ProviderErrorType.authentication.value: 2,
    ProviderErrorType.upstream_fault.value: 2,
    ProviderErrorType.model_permission.value: 1,
}


def _terminal_error_rank(mapped: GatewayError) -> int:
    return _TERMINAL_ERROR_RANK.get(str(mapped.details.get("provider_error_type", "")), 0)


def _prefer_terminal_error(current: GatewayError | None, candidate: GatewayError) -> GatewayError:
    """所有候选都失败时，返回最能定位问题的那个错误。

    典型场景：chat 端点被 429 限流，随后同一上游的 responses 端点回「不支持该
    模型」；若原样返回后者，用户会以为模型下线，而真实原因是限流。
    """
    if current is None:
        return candidate
    if _terminal_error_rank(candidate) >= _terminal_error_rank(current):
        return candidate
    return current


def _remember_request_digest(
    event: UsageEvent, protocol: WireProtocol, body: dict[str, Any] | None
) -> None:
    """失败时记下出站请求形态：上游说 param=messages.N 时才知道 N 是谁。"""
    if protocol != WireProtocol.chat_completions or not isinstance(body, dict):
        return
    event.outbound_request_digest = _request_digest(body)


def _request_digest(body: dict[str, Any]) -> dict[str, Any]:
    raw_messages = body.get("messages")
    messages = raw_messages if isinstance(raw_messages, list) else []
    roles = [str(m.get("role")) for m in messages if isinstance(m, dict)]
    role_counts: dict[str, int] = {}
    for role in roles:
        role_counts[role] = role_counts.get(role, 0) + 1
    empty_content_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict)
        and message.get("role") != "assistant"
        and not str(message.get("content") or "").strip()
    ]
    mid_system_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "system" and index != 0
    ]
    tool_call_count = sum(
        len(message.get("tool_calls") or [])
        for message in messages
        if isinstance(message, dict)
    )
    return {
        "message_count": len(messages),
        "role_counts": role_counts,
        "leading_roles": roles[:8],
        "empty_content_indexes": empty_content_indexes[:10],
        "mid_system_indexes": mid_system_indexes[:10],
        "tool_call_count": tool_call_count,
        "tool_count": len(body.get("tools") or []),
        "stream": bool(body.get("stream")),
    }


def _record_upstream_failure(
    runtime: Runtime,
    upstream: Upstream,
    provider_model_id: str,
    *,
    status_code: int | None,
    headers: dict[str, str],
    body: bytes,
    error: Exception | None = None,
    wire_protocol: WireProtocol | None = None,
) -> tuple[GatewayError, FailureClassification]:
    mapped = _mapped_error(status_code or 502, body, error=error, upstream=upstream)
    excerpt = mapped.details.get("upstream_error_excerpt")
    if excerpt:
        logger.warning(
            "上游返回错误 upstream=%s model=%s protocol=%s status=%s code=%s excerpt=%s",
            upstream.name,
            provider_model_id,
            getattr(wire_protocol, "value", wire_protocol),
            status_code,
            mapped.code,
            excerpt,
        )
    classification = runtime.circuit_breaker.record_failure(
        upstream.id,
        provider_model_id,
        status_code=status_code,
        error_type=mapped.details.get("provider_error_type"),
        retry_after=_header_value(headers, "retry-after"),
        code=mapped.code,
        message=mapped.message,
        wire_protocol=wire_protocol,
    )
    return mapped, classification


_NOT_IN_PLAN_CODE = "provider_model_not_in_plan"


def _register_not_in_plan(
    runtime: Runtime,
    upstream: Upstream,
    offering: Any,
    protocol: WireProtocol,
    *,
    seen: dict[tuple[str, str], set[WireProtocol]],
    dead: set[tuple[str, str]],
) -> None:
    """处理一次 ``MODEL_NOT_IN_PLAN``，必要时把已经确定不可用的目标剔除。

    剔除粒度按「有多少证据」决定，而不是一律按模型删：

    * 已确认协议的 offering：只剔除刚刚报错的那个协议面。同一个模型在
      ``/responses`` 被套餐拦住不代表 ``/chat/completions`` 也不可用。
    * ``unconfirmed`` 占位：一个对象同时代表两个协议面，只有两个协议都报
      ``MODEL_NOT_IN_PLAN`` 才能断定整个模型不在套餐内，这时才剔除占位，
      并在本次请求里跳过它的剩余候选（省掉一次注定失败的上游往返）。
    """
    key = (upstream.id, offering.provider_model_id)
    if getattr(offering, "wire_protocol", None) != WireProtocol.unconfirmed:
        _prune_not_in_plan_offering(runtime, upstream, key[1], wire_protocol=protocol)
        return
    protocols = seen.setdefault(key, set())
    protocols.add(protocol)
    if len(protocols) < 2:
        # 另一个协议还没证明不可用：留着占位让它继续试，避免把只在
        # /responses 被套餐拦住的模型整个判死。
        return
    _prune_not_in_plan_offering(runtime, upstream, key[1], wire_protocol=None)
    dead.add(key)


def _prune_not_in_plan_offering(
    runtime: Runtime,
    upstream: Upstream,
    provider_model_id: str,
    *,
    wire_protocol: WireProtocol | None,
) -> None:
    """把「不在套餐内」的目标从该上游剔除，之后不再尝试它。

    继续保留 offering 只会让每次请求都先白打一遍上游再回落备用上游，既浪费
    上游限流配额，也会把真实原因盖成"认证失败"。``wire_protocol`` 为 None
    表示整个模型在该上游都不可用。

    剔除只改当前状态：下一次模型同步（``_run_upstream_pipeline``）会按上游
    实际返回重建 offering，因此升级套餐后会自动恢复，不需要手动清理。
    """
    state_store = getattr(runtime, "state_store", None)
    if state_store is None or not hasattr(state_store, "mutate"):
        return

    def is_target(item: Any) -> bool:
        if item.upstream_id != upstream.id or item.provider_model_id != provider_model_id:
            return False
        if wire_protocol is None:
            return True
        return item.wire_protocol == wire_protocol

    def apply(state: Any) -> None:
        removed = {item.id for item in getattr(state, "offerings", []) if is_target(item)}
        if not removed:
            return
        state.offerings = [o for o in state.offerings if o.id not in removed]
        state.model_mappings = [
            mapping
            for mapping in getattr(state, "model_mappings", [])
            if mapping.offering_id not in removed
        ]

    try:
        state_store.mutate(apply, trigger="offering.not_in_plan")
    except Exception:
        logger.warning(
            "剔除不在套餐内的模型失败: upstream=%s model=%s protocol=%s",
            upstream.id,
            provider_model_id,
            wire_protocol.value if wire_protocol is not None else "all",
        )


def _should_hop(
    offering: Any,
    classification: FailureClassification,
    status_code: int | None,
) -> bool:
    """是否继续尝试下一个候选。

    协议未确认的占位 offering 只有一次猜中协议的机会：此时 4xx 更可能是
    「猜错了端点」而不是用户请求有问题，所以继续换另一个协议，而不是把
    400 直接抛回客户端。
    """
    if classification.decision is FailureDecision.hop:
        return True
    if offering.wire_protocol != WireProtocol.unconfirmed:
        return False
    return status_code is not None and 400 <= status_code < 500


def _learn_protocol(
    runtime: Runtime, upstream: Upstream, offering: Any, protocol: WireProtocol
) -> None:
    """把真实请求试出来的协议落盘，之后不再试错。

    只对 ``unconfirmed`` 占位 offering 生效：写回 ``model_protocol_probe`` 并把
    offering 提升为具体协议。落盘失败不影响本次请求。
    """
    if offering.wire_protocol != WireProtocol.unconfirmed:
        return
    if protocol not in (WireProtocol.responses, WireProtocol.chat_completions):
        return
    if not hasattr(runtime.state_store, "mutate"):
        return
    upstream_id = upstream.id
    provider_model_id = offering.provider_model_id
    now = utc_now()

    def apply(state: Any) -> None:
        for index, item in enumerate(getattr(state, "upstreams", [])):
            if item.id != upstream_id:
                continue
            probe = dict(item.model_protocol_probe or {})
            if probe.get(provider_model_id) != [protocol.value]:
                probe[provider_model_id] = [protocol.value]
                state.upstreams[index] = item.model_copy(
                    update={"model_protocol_probe": probe, "updated_at": now}
                )
        for index, item in enumerate(getattr(state, "offerings", [])):
            if (
                item.upstream_id != upstream_id
                or item.provider_model_id != provider_model_id
                or item.wire_protocol != WireProtocol.unconfirmed
            ):
                continue
            state.offerings[index] = item.model_copy(
                update={
                    "wire_protocol": protocol,
                    "identity_evidence": {
                        "source": "live_request",
                        "protocol": protocol.value,
                    },
                    "updated_at": now,
                }
            )

    try:
        runtime.state_store.mutate(apply, trigger="protocol.learned")
    except Exception:
        logger.warning(
            "协议学习落盘失败: upstream=%s model=%s protocol=%s",
            upstream_id,
            provider_model_id,
            protocol.value,
        )


def _header_value(headers: dict[str, str], name: str) -> str | None:
    if not headers:
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


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
            "provider_error_type": provider.get("provider_error_type"),
            "fingerprint": provider.get("fingerprint"),
            "upstream_error_excerpt": provider.get("excerpt"),
        },
    )


def _has_custom_tools(body: dict[str, Any]) -> bool:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return False
    return any(isinstance(tool, dict) and tool.get("type") == "custom" for tool in tools)


def mapped_response(exc: GatewayError, *, headers: dict[str, str] | None = None) -> Response:
    return gateway_error_response(
        error_type=exc.error_type,
        code=exc.code,
        message=exc.message,
        status_code=exc.status_code,
        details=exc.details,
        headers=headers or exc.headers,
    )


async def _stream_response(
    request: Request,
    runtime: Runtime,
    event: UsageEvent,
    upstream: Upstream,
    upstream_stream: Any,
    protocol: WireProtocol,
    offering: Any,
    *,
    custom_tool_names: set[str] | None = None,
    namespace_tool_aliases: dict[str, dict[str, str]] | None = None,
) -> Response:
    is_chat = protocol == WireProtocol.chat_completions
    model_label = event.canonical_model_label or event.provider_model_id

    async def iterator() -> AsyncIterator[bytes]:
        started = False
        stream_initialized = False
        message_started = False
        accumulated_text = ""
        accumulated_tool_calls: dict[int, dict[str, Any]] = {}
        last_usage: dict[str, Any] | None = None
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
                    chunk_usage = parsed.get("usage")
                    if isinstance(chunk_usage, dict):
                        # 只保留非 null 的 usage：多数上游中途 chunk 为 null，
                        # 仅最后一个 chunk（include_usage 生效时）携带真实值
                        last_usage = chunk_usage
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
                                        tc_index,
                                        call_id,
                                        fn_preview.get("name") or "",
                                        is_custom=is_custom,
                                        namespace_tool_aliases=namespace_tool_aliases,
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
                    chunk_usage = parsed.get("usage")
                    if isinstance(chunk_usage, dict):
                        last_usage = chunk_usage
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

                if last_finish_reason is None:
                    # 上游流在 finish_reason 之前结束（典型为中途断开）。
                    # 伪装成 completed 会让 Codex 误认为回答完整，按失败收尾。
                    error_msg = "上游流式响应在 finish_reason 之前中断，无法保证回答完整。"
                    runtime.circuit_breaker.record_failure(
                        upstream.id,
                        event.provider_model_id,
                        status_code=502,
                        wire_protocol=protocol,
                        error_type=ProviderErrorType.upstream_fault.value,
                        code="provider_upstream_fault",
                        message=error_msg,
                    )
                    _finalize(
                        runtime,
                        event,
                        Outcome.failed,
                        mapped=_mapped_error(
                            502, b"", error=RuntimeError(error_msg), upstream=upstream
                        ),
                        status_code=502,
                    )
                    yield response_sse(response_failed_event(model_label, error_msg))
                    return

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
                        namespace_tool_aliases=namespace_tool_aliases,
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
                        custom_restored, custom_ns = restore_namespace_tool_name(
                            fn.get("name") or "", namespace_tool_aliases
                        )
                        custom_item = {
                            "id": call_id,
                            "type": "custom_tool_call",
                            "call_id": call_id,
                            "name": custom_restored,
                            "input": inp if isinstance(inp, str) else "",
                            "status": "completed",
                        }
                        if custom_ns is not None:
                            custom_item["namespace"] = custom_ns
                        output_items.append(custom_item)
                    else:
                        restored_name, restored_ns = restore_namespace_tool_name(
                            fn.get("name") or "", namespace_tool_aliases
                        )
                        restored_item = {
                            "id": call_id,
                            "type": "function_call",
                            "call_id": call_id,
                            "name": restored_name,
                            "arguments": fn.get("arguments") or "",
                            "status": "completed",
                        }
                        if restored_ns is not None:
                            restored_item["namespace"] = restored_ns
                        output_items.append(restored_item)
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
                        usage=last_usage,
                    )
                )

            runtime.circuit_breaker.record_success(
                upstream.id, event.provider_model_id, wire_protocol=protocol
            )
            _learn_protocol(runtime, upstream, offering, protocol)
            _finalize_success(runtime, event, b"", streaming=True)
        except Exception as exc:
            error_msg = f"上游 {upstream.name} 流式传输异常: {type(exc).__name__}: {exc}"
            runtime.circuit_breaker.record_failure(
                upstream.id,
                event.provider_model_id,
                status_code=None,
                wire_protocol=protocol,
                error_type=ProviderErrorType.upstream_fault.value,
                code="provider_upstream_fault",
                message=error_msg,
            )
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
                    yield response_sse(
                        response_completed_event(model=model_label, usage=last_usage)
                    )
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
        excerpt = mapped.details.get("upstream_error_excerpt")
        if excerpt:
            event.upstream_error_excerpt = str(excerpt)[:ERROR_EXCERPT_LIMIT]
    if fallback_trigger:
        event.fallback_trigger = fallback_trigger
    runtime.usage_log.record_finalized(event)
