""""全局网关 token 服务。"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any

from codex_ai_gateway.integrations.secret_store import SecretStore
from codex_ai_gateway.models.entities import GatewayToken, TokenStatus
from codex_ai_gateway.util import utc_now, uuid7

PREFIX = "gwg_"


def _hash(raw_token: str, signing_key: bytes) -> str:
    return hmac.new(signing_key, raw_token.encode(), hashlib.sha256).hexdigest()


def token_view_hash(raw_token: str, signing_key: bytes) -> str:
    return _hash(raw_token, signing_key)


def _record(raw_token: str, signing_key: bytes, *, now: str) -> tuple[GatewayToken, str]:
    digest = _hash(raw_token, signing_key)
    return (
        GatewayToken(
            id=uuid7(),
            lookup_hash=digest,
            prefix=raw_token[:8],
            last4=raw_token[-4:],
            issued_at=now,
        ),
        raw_token,
    )


def create_gateway_token(signing_key: bytes) -> tuple[GatewayToken, str]:
    return _record(PREFIX + secrets.token_urlsafe(32), signing_key, now=utc_now())


def verify_gateway_token(
    raw_token: str, tokens: list[GatewayToken], signing_key: bytes
) -> GatewayToken | None:
    digest = _hash(raw_token, signing_key)
    now_text = utc_now()
    for token in tokens:
        if token.status == TokenStatus.revoked:
            continue
        if token.status == TokenStatus.grace and token.grace_until and token.grace_until < now_text:
            continue
        if hmac.compare_digest(token.lookup_hash, digest):
            return token
    return None


def rotate_gateway_token(
    existing: GatewayToken, signing_key: bytes
) -> tuple[GatewayToken, str]:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    new_record, raw = create_gateway_token(signing_key)
    new_record.predecessor_id = existing.id
    new_record.issued_at = now.isoformat()
    existing.status = TokenStatus.grace
    existing.grace_until = (now + timedelta(seconds=30)).isoformat()
    existing.successor_id = new_record.id
    return new_record, raw


def apply_rotation(state: Any, existing: GatewayToken, new_record: GatewayToken) -> None:
    # rotate_gateway_token 已写入 grace 状态；这里保持历史记录并追加新 token。
    if not any(token.id == existing.id for token in state.gateway_tokens):
        state.gateway_tokens.append(existing)
    state.gateway_tokens.append(new_record)


def apply_revoke(state: Any, existing: GatewayToken) -> None:
    for token in state.gateway_tokens:
        if token.id == existing.id:
            token.status = TokenStatus.revoked

KEY_SIGNING_ACCOUNT = "gateway_signing_key"


def get_or_create_signing_key(secret_store: SecretStore) -> bytes:
    secret = secret_store.get_secret(KEY_SIGNING_ACCOUNT)
    if secret is None:
        secret = os.urandom(32).hex()
        secret_store.set_secret(KEY_SIGNING_ACCOUNT, secret)
    return bytes.fromhex(secret)
