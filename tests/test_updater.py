"""自更新核心测试：版本比较、清单解析、策略决策、缓存与退避。"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from codex_ai_gateway.services.updater import (
    MANIFEST_CACHE_SECONDS,
    UpdateError,
    UpdateService,
    is_newer,
    parse_manifest,
    parse_version,
    sha256_file,
)


def make_service(tmp_path, *, deployed: str | None = "v0.2.27") -> UpdateService:
    app_root = tmp_path / "app"
    data_dir = app_root / "data"
    (app_root / "releases").mkdir(parents=True)
    data_dir.mkdir(parents=True)
    if deployed is not None:
        (app_root / "deployed-version").write_text(deployed + "\n", encoding="utf-8")
    return UpdateService(data_dir, app_root=app_root)


def manifest_payload(version: str = "v0.2.28", sha256: str = "a" * 64) -> dict:
    return {
        "version": version,
        "notes_url": "https://example.test/notes",
        "assets": [
            {
                "name": f"codex-ai-gateway-{version}.zip",
                "url": f"https://example.test/{version}.zip",
                "sha256": sha256,
            }
        ],
    }


def test_parse_version_and_is_newer() -> None:
    assert parse_version("v0.2.27") == (0, 2, 27, 1, ())
    assert parse_version("0.2") == (0, 2, 0, 1, ())
    assert parse_version("v0.3.0-beta.2") is not None
    assert parse_version("nonsense") is None

    assert is_newer("v0.2.28", "v0.2.27") is True
    assert is_newer("v0.2.27", "v0.2.27") is False
    assert is_newer("v0.2.26", "v0.2.27") is False
    assert is_newer("v0.3.0-beta.1", "v0.2.27") is True
    assert is_newer("v0.3.0-beta.1", "v0.3.0") is False
    assert is_newer("v0.3.0", "v0.3.0-beta.1") is True
    assert is_newer("garbage", "v0.2.27") is False
    assert is_newer("v0.2.28", "local-build") is False


def test_parse_manifest_validation() -> None:
    manifest = parse_manifest(manifest_payload())
    assert manifest.version == "v0.2.28"
    assert manifest.archive() is not None
    assert manifest.archive().sha256 == "a" * 64

    with pytest.raises(UpdateError):
        parse_manifest([])
    with pytest.raises(UpdateError):
        parse_manifest({"assets": []})
    with pytest.raises(UpdateError):
        parse_manifest({"version": "v1.0.0", "assets": []})


def test_policy_roundtrip_and_pinned_validation(tmp_path) -> None:
    service = make_service(tmp_path)
    assert service.read_policy()["policy"] == "notify"

    service.write_policy(policy="pinned", pinned_version="v0.2.20")
    policy = service.read_policy()
    assert policy["policy"] == "pinned"
    assert policy["pinned_version"] == "v0.2.20"

    with pytest.raises(UpdateError):
        service.write_policy(policy="pinned", pinned_version="")
    with pytest.raises(UpdateError):
        service.write_policy(policy="bogus")


def test_plan_auto_installs_newer(tmp_path) -> None:
    service = make_service(tmp_path)
    service.write_policy(policy="auto")
    service.write_state(
        {
            "manifest": manifest_payload(),
            "manifest_tag": "latest",
            "fetched_at": 0,
        }
    )
    plan = service.plan()
    assert plan["action"] == "install"
    assert plan["reason"] == "newer_available"
    assert plan["target_version"] == "v0.2.28"
    assert plan["sha256"] == "a" * 64


def test_plan_notify_and_pinned_and_dismissed(tmp_path) -> None:
    service = make_service(tmp_path)
    service.write_state(
        {"manifest": manifest_payload(), "manifest_tag": "latest", "fetched_at": 0}
    )
    assert service.plan()["action"] == "none"
    assert service.plan()["reason"] == "policy_notify"

    service.write_policy(policy="auto", dismissed_version="v0.2.28")
    assert service.plan()["reason"] == "dismissed"

    service.write_policy(policy="pinned", pinned_version="v0.2.20")
    service.write_state(
        {
            "manifest": manifest_payload("v0.2.20"),
            "manifest_tag": "v0.2.20",
            "fetched_at": 0,
        }
    )
    plan = service.plan()
    assert plan["action"] == "install"
    assert plan["reason"] == "pinned_target"
    assert plan["target_version"] == "v0.2.20"


def test_status_notify_reports_update_available(tmp_path) -> None:
    service = make_service(tmp_path)
    service.write_state(
        {"manifest": manifest_payload(), "manifest_tag": "latest", "fetched_at": 0}
    )
    status = service.status()
    assert status["policy"] == "notify"
    assert status["action"] == "none"
    assert status["action_reason"] == "policy_notify"
    assert status["update_available"] is True
    assert status["target_version"] == "v0.2.28"

    service.write_state(
        {
            "manifest": manifest_payload("v0.2.27"),
            "manifest_tag": "latest",
            "fetched_at": 0,
        }
    )
    up_to_date = service.status()
    assert up_to_date["update_available"] is False
    assert up_to_date["action_reason"] == "up_to_date"


def test_plan_up_to_date(tmp_path) -> None:
    service = make_service(tmp_path, deployed="v0.2.28")
    service.write_policy(policy="auto")
    service.write_state(
        {"manifest": manifest_payload(), "manifest_tag": "latest", "fetched_at": 0}
    )
    plan = service.plan()
    assert plan["action"] == "none"
    assert plan["reason"] == "up_to_date"


def test_plan_not_managed(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    service = UpdateService(data_dir, app_root=tmp_path / "missing")
    service.write_policy(policy="auto")
    service.write_state(
        {"manifest": manifest_payload(), "manifest_tag": "latest", "fetched_at": 0}
    )
    assert service.plan()["action"] == "none"
    assert service.plan()["reason"] == "not_managed"


class _FakeFetch(UpdateService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str | None] = []
        self.payload: dict = manifest_payload()

    async def _fetch_manifest(self, tag, *, etag=None):  # type: ignore[override]
        self.calls.append(tag)
        return parse_manifest(self.payload), '"etag-1"'


def test_check_uses_cache_within_ttl(tmp_path) -> None:
    service = _FakeFetch(tmp_path / "app" / "data", app_root=tmp_path / "app")
    (tmp_path / "app" / "releases").mkdir(parents=True)
    (tmp_path / "app" / "deployed-version").write_text("v0.2.27\n", encoding="utf-8")
    service.write_policy(policy="auto")

    first = asyncio.run(service.check())
    assert first["latest_version"] == "v0.2.28"
    assert first["update_available"] is True
    assert service.calls == [None]

    second = asyncio.run(service.check())
    assert second["latest_version"] == "v0.2.28"
    assert service.calls == [None], "TTL 内不应重复请求清单"

    forced = asyncio.run(service.check(force=True))
    assert forced["latest_version"] == "v0.2.28"
    assert service.calls == [None, None]


class _FailingFetch(UpdateService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    async def _fetch_manifest(self, tag, *, etag=None):  # type: ignore[override]
        self.calls += 1
        raise UpdateError("boom")


def test_check_backoff_after_failure(tmp_path) -> None:
    service = _FailingFetch(tmp_path / "app" / "data", app_root=tmp_path / "app")
    (tmp_path / "app" / "releases").mkdir(parents=True)
    (tmp_path / "app" / "deployed-version").write_text("v0.2.27\n", encoding="utf-8")

    status = asyncio.run(service.check())
    assert status["last_check_error"] == "boom"
    assert status["next_check_at"] is not None
    assert service.calls == 1

    asyncio.run(service.check())
    assert service.calls == 1, "退避期内不应再次请求"

    state = service.read_state()
    assert state["fail_count"] == 1
    assert state["next_check_at"] > 0


def test_manifest_cache_ttl_constant() -> None:
    assert MANIFEST_CACHE_SECONDS >= 6 * 3600


def test_sha256_file(tmp_path) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"codex-ai-gateway")
    assert sha256_file(target) == hashlib.sha256(b"codex-ai-gateway").hexdigest()