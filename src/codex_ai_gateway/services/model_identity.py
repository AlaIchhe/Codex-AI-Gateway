"""模型身份匹配服务。

身份匹配契约见 specs/001-codex-ai-gateway/spec.md FR-008/FR-052/FR-057。
前缀必须由上游显式声明；日期是版本身份；发布通道和服务变体不改变身份。
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
_SUFFIX_RE = re.compile(r"[-:_.@+.]?(?P<suffix>ga|preview|latest|free|batch|experimental|online|search|reasoning|thinking)$")
_SEPARATORS = str.maketrans({"/": "-", ":": "-", "_": "-", ".": "-", "@": "-", "+": "-"})


@dataclass(frozen=True)
class OpenRouterSnapshotData:
    models: list[dict[str, Any]]
    snapshot: ExternalMetadataSnapshot | None


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
    """返回 (family_key, date_text, ignored_suffix)。未知后缀保留在 family 中。"""
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
            if date_value is not None:
                continue
        suffix_match = _SUFFIX_RE.search(family)
        if suffix_match:
            suffixes.append(suffix_match.group("suffix"))
            family = family[: suffix_match.start()]
            continue
        break
    return family.strip("-"), _date_text(date_value) if date_value else None, "-".join(reversed(suffixes)) or None


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


def _ambiguous(
    offering: Offering,
    *,
    normalized: str,
    family: str,
    reason: str,
    candidates: list[dict[str, Any]],
    provider_date: str | None = None,
) -> MatchResult:
    mapping = ModelIdentityMapping(
        id=uuid7(),
        offering_id=offering.id,
        openrouter_model_id=None,
        match_mode=MatchMode.ambiguous,
        normalized_key=normalized,
        family_key=family,
        provider_date=provider_date,
        ignored_suffix=None,
        evidence_json={"reason": reason, "candidates": [m.get("id") for m in candidates], "matched_at": utc_now()},
        matched_at=utc_now(),
    )
    return MatchResult(mapping, None)


def match_offering(
    offering: Offering,
    models: list[dict[str, Any]],
    *,
    namespace_prefixes: set[str] | None = None,
    snapshot_id: str | None = None,
    aliases: dict[str, str] | None = None,
) -> MatchResult:
    prefixes = _declared_prefixes(namespace_prefixes)
    normalized = _apply_aliases(normalize_model_id(offering.provider_model_id, namespace_prefixes=prefixes), aliases)
    provider_family, provider_date, provider_suffix = _parse_identity(normalized)
    entries: list[tuple[dict[str, Any], str, str, str | None, str | None, str | None]] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = _apply_aliases(normalize_model_id(str(model.get("id", "")), namespace_prefixes=prefixes), aliases)
        if "/" in model_id:
            model_id = model_id.split("/", 1)[-1]
        if not model_id:
            continue
        # OpenRouter 的 id 常保留 0731 这类短版本号，canonical_slug 才携带
        # 完整日期。日期匹配必须优先使用 canonical_slug。
        identity_id = _apply_aliases(normalize_model_id(
            str(model.get("canonical_slug") or model.get("id", "")),
            namespace_prefixes=prefixes,
        ), aliases)
        if "/" in identity_id:
            identity_id = identity_id.split("/", 1)[-1]
        family, or_date, or_suffix = _parse_identity(identity_id)
        entries.append((model, model_id, family, or_date, or_suffix, identity_id))

    direct = [(m, mid, fam, od, osuf, mid) for m, mid, fam, od, osuf, mid in entries if mid == normalized]
    if len(direct) == 1:
        model, mid, family, or_date, or_suffix, _ = direct[0]
        evidence = {
            "reason": "normalized_exact",
            "input": offering.provider_model_id,
            "normalized": normalized,
            "openrouter_snapshot_id": snapshot_id,
            "provider_family": provider_family,
            "openrouter_family": family,
            "provider_date": provider_date,
            "openrouter_date": or_date,
            "provider_suffix": provider_suffix,
            "openrouter_suffix": or_suffix,
            "matched_at": utc_now(),
        }
        mapping = ModelIdentityMapping(
            id=uuid7(), offering_id=offering.id, openrouter_model_id=str(model.get("id")),
            match_mode=MatchMode.exact, normalized_key=normalized, family_key=family,
            provider_date=provider_date, openrouter_date=or_date, ignored_suffix=provider_suffix or or_suffix,
            evidence_json=evidence, matched_at=utc_now(),
        )
        return MatchResult(mapping, model)
    if len(direct) > 1:
        return _ambiguous(offering, normalized=normalized, family=provider_family, reason="exact_ambiguous", candidates=[m for m, *_ in direct], provider_date=provider_date)

    provider_family_key = provider_family.rsplit('/', 1)[-1]
    family_candidates = [entry for entry in entries if _parse_identity(entry[1].rsplit('/', 1)[-1])[0] == provider_family_key]
    def _is_variant_entry(entry: tuple[dict[str, Any], str, str, str | None, str | None, str | None]) -> bool:
        value = entry[1].lower()
        return any(tag in value for tag in (":batch", "-batch", ":free", "-free", "-latest", "latest"))

    if provider_date is not None:
        exact_date = [
            entry for entry in family_candidates
            if entry[3] == provider_date and not _is_variant_entry(entry)
        ]
        if len(exact_date) == 1:
            model, model_id, family, or_date, or_suffix, _ = exact_date[0]
            evidence = {
                "reason": "exact_provider_date", "openrouter_snapshot_id": snapshot_id,
                "provider_family": provider_family, "openrouter_family": family,
                "provider_date": provider_date, "openrouter_date": or_date,
                "provider_suffix": provider_suffix, "openrouter_suffix": or_suffix,
                "matched_at": utc_now(),
            }
            mapping = ModelIdentityMapping(
                id=uuid7(), offering_id=offering.id, openrouter_model_id=str(model.get("id")),
                match_mode=MatchMode.date_version, normalized_key=normalized, family_key=family,
                provider_date=provider_date, openrouter_date=or_date, ignored_suffix=provider_suffix or or_suffix,
                evidence_json=evidence, matched_at=utc_now(),
            )
            return MatchResult(mapping, model)
        if len(exact_date) > 1:
            return _ambiguous(offering, normalized=normalized, family=provider_family, reason="provider_date_ambiguous", candidates=[m for m, *_ in exact_date], provider_date=provider_date)

        earlier = [
            entry for entry in family_candidates
            if entry[3] is not None and entry[3] <= provider_date and not _is_variant_entry(entry)
        ]
        if earlier:
            latest_date = max(entry[3] for entry in earlier)
            nearest = [entry for entry in earlier if entry[3] == latest_date]
            if len(nearest) == 1:
                model, model_id, family, or_date, or_suffix, _ = nearest[0]
                evidence = {
                    "reason": "nearest_earlier_provider_date", "openrouter_snapshot_id": snapshot_id,
                    "provider_family": provider_family, "openrouter_family": family,
                    "provider_date": provider_date, "openrouter_date": or_date,
                    "provider_suffix": provider_suffix, "openrouter_suffix": or_suffix,
                    "matched_at": utc_now(),
                }
                mapping = ModelIdentityMapping(
                    id=uuid7(), offering_id=offering.id, openrouter_model_id=str(model.get("id")),
                    match_mode=MatchMode.date_version, normalized_key=normalized, family_key=family,
                    provider_date=provider_date, openrouter_date=or_date, ignored_suffix=provider_suffix or or_suffix,
                    evidence_json=evidence, matched_at=utc_now(),
                )
                return MatchResult(mapping, model)

        # OpenRouter 常以基础条目承载滚动版本；provider 的日期变体在无精确
        # 或更早日期候选时可回退到唯一基础条目，但这仍低于精确日期优先级。
        base_entries = [entry for entry in family_candidates if entry[3] is None and not _is_variant_entry(entry)]
        if len(base_entries) == 1:
            model, model_id, family, or_date, or_suffix, _ = base_entries[0]
            evidence = {
                "reason": "provider_date_to_base_entry", "openrouter_snapshot_id": snapshot_id,
                "provider_family": provider_family, "openrouter_family": family,
                "provider_date": provider_date, "openrouter_date": or_date,
                "provider_suffix": provider_suffix, "openrouter_suffix": or_suffix,
                "matched_at": utc_now(),
            }
            mapping = ModelIdentityMapping(
                id=uuid7(), offering_id=offering.id, openrouter_model_id=str(model.get("id")),
                match_mode=MatchMode.date_version, normalized_key=normalized, family_key=family,
                provider_date=provider_date, openrouter_date=or_date, ignored_suffix=provider_suffix or or_suffix,
                evidence_json=evidence, matched_at=utc_now(),
            )
            return MatchResult(mapping, model)
        return MatchResult(
            ModelIdentityMapping(
                id=uuid7(), offering_id=offering.id, openrouter_model_id=None,
                match_mode=MatchMode.missing, normalized_key=normalized, family_key=provider_family,
                provider_date=provider_date, evidence_json={"reason": "provider_date_not_found", "openrouter_snapshot_id": snapshot_id, "matched_at": utc_now()},
                matched_at=utc_now(),
            ),
            None,
        )

    dated = [(entry, _parsed_date(entry[3] or "")) for entry in family_candidates if entry[3] and not _is_variant_entry(entry)]
    dated = [(entry, value) for entry, value in dated if value is not None]
    today = date.fromisoformat(utc_now()[:10])
    earlier = [(entry, value) for entry, value in dated if value <= today]
    if earlier:
        latest = max(value for _, value in earlier)
        selected = [entry for entry, value in earlier if value == latest]
    else:
        base = [entry for entry in family_candidates if entry[3] is None and not _is_variant_entry(entry)]
        selected = base
    if len(selected) == 1:
        model, model_id, family, or_date, or_suffix, _ = selected[0]
        mode = MatchMode.suffix_ignored if provider_suffix or or_suffix else MatchMode.date_version if or_date else MatchMode.normalized
        evidence = {
            "reason": "latest_earlier_version" if dated else "base_entry",
            "openrouter_snapshot_id": snapshot_id,
            "provider_family": provider_family, "openrouter_family": family,
            "provider_date": provider_date, "openrouter_date": or_date,
            "provider_suffix": provider_suffix, "openrouter_suffix": or_suffix,
            "matched_at": utc_now(),
        }
        mapping = ModelIdentityMapping(
            id=uuid7(), offering_id=offering.id, openrouter_model_id=str(model.get("id")),
            match_mode=mode, normalized_key=normalized, family_key=family,
            provider_date=provider_date, openrouter_date=or_date, ignored_suffix=provider_suffix or or_suffix,
            evidence_json=evidence, matched_at=utc_now(),
        )
        return MatchResult(mapping, model)
    if len(selected) > 1:
        return _ambiguous(offering, normalized=normalized, family=provider_family, reason="family_ambiguous", candidates=[m for m, *_ in selected])
    return MatchResult(
        ModelIdentityMapping(
            id=uuid7(), offering_id=offering.id, openrouter_model_id=None,
            match_mode=MatchMode.missing, normalized_key=normalized, family_key=provider_family,
            provider_date=provider_date, evidence_json={"reason": "not_in_openrouter_catalog", "openrouter_snapshot_id": snapshot_id, "matched_at": utc_now()},
            matched_at=utc_now(),
        ),
        None,
    )


def canonical_from_candidate(candidate: dict[str, Any], *, now: str) -> CanonicalModel:
    model_id = str(candidate.get("id", ""))
    slug = model_id.rsplit("/", 1)[-1]
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
