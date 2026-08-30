"""模型身份匹配服务。

身份匹配契约见 specs/001-codex-ai-gateway/spec.md FR-008/FR-052/FR-057。
前缀必须由上游显式声明；日期是版本身份；发布通道和服务变体不改变身份。

v0.2.2 起：身份匹配基于「标准目录」——把 OpenRouter 快照按家族键
（剥命名空间 / 日期后缀 / 滚动别名）分组，同家族按 created 择新取 1 条，
匹配命中即归族，未命中由上游原生元数据回退。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx

from codex_ai_gateway.models.entities import (
    CanonicalModel,
    ExternalMetadataSnapshot,
    MatchMode,
    ModelIdentityMapping,
    Offering,
    SnapshotStatus,
)
from codex_ai_gateway.util import utc_now, uuid7

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
FILTER_RULE_VERSION = "identity-lexical-v1"
IGNORED_SUFFIXES = {
    "ga", "preview", "latest", "free", "batch", "experimental",
    "online", "search", "reasoning", "thinking",
}
_DATE_ISO = re.compile(r"[-:_.@+.]?(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})$")
_DATE_LONG = re.compile(r"[-:_.@+.]?(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})$")
_DATE_SHORT = re.compile(r"[-:_.@+.]?(\d{2})(\d{2})(\d{2})$")
# OpenRouter 常见 MMDD 版本后缀（如 deepseek-v4-pro-0813、deepseek-v4-flash-0731），
# 仅识别为月份/日期范围均合法的 4 位后缀；不合法则保留原样（避免误伤版本号）。
_DATE_MD = re.compile(r"[-:_.@+.]?(\d{2})(\d{2})$")
# MM-DD 带分隔符形态（如 qwen3.6-plus-04-02），与 canonical_slug 的日期后缀形态一致。
_DATE_MD_DASHED = re.compile(r"[-:_.@+.]?(\d{2})[-:_.@+.](\d{2})$")
_SUFFIX_RE = re.compile(r"[-:_.@+.]?(?P<suffix>ga|preview|latest|free|batch|experimental|online|search|reasoning|thinking)$")
_SEPARATORS = str.maketrans({"/": "-", ":": "-", "_": "-", ".": "-", "@": "-", "+": "-"})


@dataclass(frozen=True)
class OpenRouterSnapshotData:
    models: list[dict[str, Any]]
    snapshot: ExternalMetadataSnapshot | None


@dataclass(frozen=True)
class StandardEntry:
    """标准目录条目：家族键 → 同家族（跨厂商合并后）的唯一条目。"""

    family_key: str
    entry: dict[str, Any]
    openrouter_model_id: str
    created: int | None


@dataclass(frozen=True)
class MatchResult:
    mapping: ModelIdentityMapping
    candidate: dict[str, Any] | None


def _parsed_date(value: str) -> date | None:
    for pattern in (_DATE_ISO, _DATE_LONG, _DATE_SHORT):
        match = pattern.search(value)
        if not match:
            continue
        groups = match.groupdict()
        try:
            if "y" in groups:
                year = int(groups["y"])
                month = int(groups["m"])
                day = int(groups["d"])
            else:
                short = match.groups()
                year, month, day = 2000 + int(short[0]), int(short[1]), int(short[2])
            return date(year, month, day)
        except ValueError:
            continue
    return None


def _date_text(value: date) -> str:
    return value.isoformat()


def _declared_prefixes(namespace_prefixes: set[str] | None) -> set[str]:
    return {str(item).strip().lower() for item in (namespace_prefixes or set()) if str(item).strip()}


def _strip_declared_prefix(value: str, namespace_prefixes: set[str]) -> str:
    parts = value.split("/")
    while len(parts) > 1 and parts[0] in namespace_prefixes:
        parts.pop(0)
    return "/".join(parts)


def _apply_aliases(value: str, aliases: dict[str, str] | None) -> str:
    """应用上游声明的身份别名（如 doubao-seed -> seed），必须在 normalize 之后调用。"""
    if not aliases:
        return value
    for src, dst in aliases.items():
        value = value.replace(src, dst)
    return value


def normalize_model_id(value: str, *, namespace_prefixes: set[str] | None = None) -> str:
    """去空白、统一小写，并只剥离上游显式声明的命名空间前缀。"""
    raw = re.sub(r"\s+", "", value.strip().lower())
    return _strip_declared_prefix(raw, _declared_prefixes(namespace_prefixes))


def _parse_identity(value: str) -> tuple[str, str | None, str | None]:
    """返回 (family_key_base, date_text, ignored_suffix)。

    family_key_base 保留厂商路径段（如 deepseek/deepseek-v4-flash）；
    跨厂商分组时取尾段比较。未知后缀保留在 family 中。
    """
    family = value.translate(_SEPARATORS)
    date_value: date | None = None
    suffixes: list[str] = []
    while True:
        _parsed_date(family)
        if date_value is None:
            for pattern in (_DATE_ISO, _DATE_LONG, _DATE_SHORT):
                match = pattern.search(family)
                if match:
                    date_value = _parsed_date(family)
                    family = family[: match.start()]
                    break
            if date_value is None:
                # OpenRouter 用 MMDD 短版本号（如 deepseek-v4-pro-0813），
                # 仅当月份/日期合法时视为版本日期并剥离。用固定年份 2000
                # 只表达先后（同年内比较月份/日期），不影响跨年版本选择。
                match = _DATE_MD.search(family)
                if match:
                    month, day = int(match.group(1)), int(match.group(2))
                    if 1 <= month <= 12 and 1 <= day <= 31:
                        family = family[: match.start()]
                        date_value = date(2000, month, day)
                else:
                    # MM-DD 带分隔符（如同 qwen3.6-plus-04-02），与 MMDD 同语义。
                    match = _DATE_MD_DASHED.search(family)
                    if match:
                        month, day = int(match.group(1)), int(match.group(2))
                        if 1 <= month <= 12 and 1 <= day <= 31:
                            family = family[: match.start()]
                            date_value = date(2000, month, day)
            if date_value is not None:
                continue
        suffix_match = _SUFFIX_RE.search(family)
        if suffix_match:
            suffixes.append(suffix_match.group("suffix"))
            family = family[: suffix_match.start()]
            continue
        break
    return family.strip("-"), _date_text(date_value) if date_value else None, "-".join(reversed(suffixes)) or None


def _tail_key(value: str) -> str:
    """取路径尾段作为跨厂商分组键。"""
    return value.rsplit("/", 1)[-1]


def family_key_of(
    value: str,
    *,
    namespace_prefixes: set[str] | None = None,
    aliases: dict[str, str] | None = None,
) -> str:
    """家族键（尾段、无厂商）：归一化 → 剥离厂商命名空间/滚动别名/日期后缀。

    匹配与路由共用同一归一逻辑，避免两套正则漂移。OpenRouter id 形态为
    `vendor/model-name`，尾段即模型家族名；`~vendor/x-latest` 的 `~`
    前缀视为厂商命名空间标记一并剥离。
    """
    prefixes = _declared_prefixes(namespace_prefixes)
    raw = _apply_aliases(normalize_model_id(value, namespace_prefixes=prefixes), aliases)
    raw = raw.lstrip("~")
    raw = _tail_key(raw)
    family, _, _ = _parse_identity(raw)
    return family


def _entry_created(model: dict[str, Any]) -> int | None:
    value = model.get("created")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return int(value)
    return None


def build_standard_catalog(
    models: list[dict[str, Any]],
    *,
    namespace_prefixes: set[str] | None = None,
    aliases: dict[str, str] | None = None,
) -> list[StandardEntry]:
    """从 OpenRouter 快照构建标准目录。

    - 剔除带 `:` 变体后缀的条目（:free / :batch）
    - 按尾段家族键分组（跨厂商同名合并）
    - 同家族按 created 降序取第 1 条；created 缺失视为最旧
    """
    prefixes = _declared_prefixes(namespace_prefixes)
    grouped: dict[str, list[tuple[dict[str, Any], str, int | None]]] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id", ""))
        if not model_id or ":" in model_id:
            continue
        family = family_key_of(
            model_id, namespace_prefixes=prefixes, aliases=aliases
        )
        if not family:
            continue
        grouped.setdefault(family, []).append((model, model_id, _entry_created(model)))
    catalog: list[StandardEntry] = []
    for family_key, entries in grouped.items():
        entries.sort(key=lambda item: (item[2] is not None, item[2] or 0), reverse=True)
        model, model_id, created = entries[0]
        catalog.append(
            StandardEntry(
                family_key=family_key,
                entry=model,
                openrouter_model_id=model_id,
                created=created,
            )
        )
    return catalog


def snapshot_from_response(
    *,
    url: str,
    status_headers: dict[str, str] | Any,
    body: bytes,
    data: Any,
    fetched_at: str,
) -> ExternalMetadataSnapshot | None:
    models = data.get("data") if isinstance(data, dict) else []
    if not isinstance(models, list):
        return None
    headers = status_headers if isinstance(status_headers, dict) else dict(getattr(status_headers, "items", lambda: [])())
    digest = hashlib.sha256(body).hexdigest()
    headers_blob = "\n".join(
        f"{key.lower()}:{headers.get(key, headers.get(key.title(), ''))}"
        for key in ("etag", "last-modified", "x-request-id")
    )
    return ExternalMetadataSnapshot(
        id=uuid7(),
        source_url=url,
        response_version=headers.get("x-openrouter-version") or headers.get("x-openrouter-version".title()),
        etag=headers.get("etag") or headers.get("ETag"),
        last_modified=headers.get("last-modified") or headers.get("Last-Modified"),
        body_sha256=digest,
        headers_digest=hashlib.sha256(headers_blob.encode()).hexdigest(),
        fetched_at=fetched_at,
        expires_at=(date.fromisoformat(fetched_at[:10]) + timedelta(days=1)).isoformat() + fetched_at[10:],
        filter_rule_version=FILTER_RULE_VERSION,
        status=SnapshotStatus.current,
        models_json=[m for m in models if isinstance(m, dict)] if isinstance(models, list) else [],
    )


async def fetch_openrouter_snapshot(api_key: str | None = None) -> OpenRouterSnapshotData:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    now = utc_now()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(OPENROUTER_MODELS_URL, headers=headers)
            response.raise_for_status()
            body = response.content
            data = response.json()
            snapshot = snapshot_from_response(
                url=str(response.request.url),
                status_headers=dict(response.headers),
                body=body,
                data=data,
                fetched_at=now,
            )
    except (httpx.HTTPError, ValueError):
        return OpenRouterSnapshotData(models=[], snapshot=None)
    models = data.get("data") if isinstance(data, dict) else []
    return OpenRouterSnapshotData(
        models=[m for m in models if isinstance(m, dict)] if isinstance(models, list) else [],
        snapshot=snapshot,
    )


async def fetch_openrouter_models(api_key: str | None = None) -> list[dict[str, Any]]:
    return (await fetch_openrouter_snapshot(api_key)).models


def match_offering(
    offering: Offering,
    models: list[dict[str, Any]],
    *,
    namespace_prefixes: set[str] | None = None,
    snapshot_id: str | None = None,
    aliases: dict[str, str] | None = None,
    catalog: list[StandardEntry] | None = None,
) -> MatchResult:
    """按标准目录匹配 offering；未命中时回退到上游原生元数据。

    - 命中：match_mode=normalized，openrouter_model_id=标准条目 id，
      candidate=标准条目（供能力基线提取）
    - 未命中：match_mode=upstream_fallback，openrouter_model_id=None，
      candidate=None（聚合侧用 offering.native_metadata_json 建 canonical）

    catalog 可由调用方传入（聚合时构建一次复用），缺省时即时构建。
    """
    prefixes = _declared_prefixes(namespace_prefixes)
    normalized = _apply_aliases(
        normalize_model_id(offering.provider_model_id, namespace_prefixes=prefixes), aliases
    )
    family_key = family_key_of(offering.provider_model_id, namespace_prefixes=prefixes, aliases=aliases)
    if catalog is None:
        catalog = build_standard_catalog(models, namespace_prefixes=prefixes, aliases=aliases)
    entry = next((item for item in catalog if item.family_key == family_key), None)
    if entry is None:
        mapping = ModelIdentityMapping(
            id=uuid7(),
            offering_id=offering.id,
            openrouter_model_id=None,
            match_mode=MatchMode.upstream_fallback,
            normalized_key=normalized,
            family_key=family_key,
            evidence_json={
                "reason": "not_in_standard_catalog",
                "openrouter_snapshot_id": snapshot_id,
                "matched_at": utc_now(),
            },
            matched_at=utc_now(),
        )
        return MatchResult(mapping, None)
    mapping = ModelIdentityMapping(
        id=uuid7(),
        offering_id=offering.id,
        openrouter_model_id=entry.openrouter_model_id,
        match_mode=MatchMode.normalized,
        normalized_key=normalized,
        family_key=family_key,
        evidence_json={
            "reason": "standard_catalog_hit",
            "openrouter_model_id": entry.openrouter_model_id,
            "created": entry.created,
            "openrouter_snapshot_id": snapshot_id,
            "matched_at": utc_now(),
        },
        matched_at=utc_now(),
    )
    return MatchResult(mapping, entry.entry)


def canonical_from_candidate(
    candidate: dict[str, Any], *, now: str, slug: str | None = None
) -> CanonicalModel:
    model_id = str(candidate.get("id", ""))
    slug = slug or model_id.rsplit("/", 1)[-1]
    return CanonicalModel(
        id=uuid7(),
        openrouter_model_id=model_id,
        display_name=str(candidate.get("name") or slug),
        slug=slug,
        capability_baseline=candidate,
        status="unavailable",
        first_matched_at=now,
        updated_at=now,
    )


def canonical_from_offering(
    offering: Offering,
    *,
    now: str,
    namespace_prefixes: set[str] | None = None,
    aliases: dict[str, str] | None = None,
    slug: str | None = None,
) -> CanonicalModel:
    """上游回退路径：以归一化 provider id 为 slug 建 canonical。"""
    prefixes = _declared_prefixes(namespace_prefixes)
    normalized = _apply_aliases(
        normalize_model_id(offering.provider_model_id, namespace_prefixes=prefixes), aliases
    )
    slug = slug or normalized.rsplit("/", 1)[-1].strip("-") or "fallback"
    return CanonicalModel(
        id=uuid7(),
        openrouter_model_id=None,
        display_name=offering.display_name or slug,
        slug=slug,
        capability_baseline=dict(offering.native_metadata_json or {}),
        status="unavailable",
        first_matched_at=now,
        updated_at=now,
    )
