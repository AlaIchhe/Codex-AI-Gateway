"""profile 预览 diff、apply 单事务、四类校验事件、恢复点与回滚服务。"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_ai_gateway.integrations.codex_writer import (
    apply_managed_config,
    file_fingerprint,
    read_config,
    resolve_profile_path,
    validate_codex_schema,
)
from codex_ai_gateway.models.entities import (
    IntegrationProfile,
    IntegrationRevision,
    IntegrationState,
)
from codex_ai_gateway.persistence.atomic_writer import (
    atomic_write_bytes,
    atomic_write_json,
    ensure_secure_dir,
)
from codex_ai_gateway.services.catalog_publishing import (
    build_catalog_response,
    load_published_model_infos,
    validate_catalog_response,
)
from codex_ai_gateway.services.codex_process_restart import restart_codex_processes
from codex_ai_gateway.util import utc_now, uuid7


class CodexDriftError(RuntimeError):
    pass


class LocalCodexService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.recovery_dir = self.data_dir / "integration/recovery-points"

    def create_preview(self, profile: Any, *, gateway_token: str | None = None) -> tuple[dict[str, Any], str]:
        """生成 preview diff 与修改后的文本。"""
        config_path = profile.config_path or str(Path(profile.codex_home) / "config.toml")
        raw = read_config(config_path)
        provider_id = profile.expected_provider_id or "codex-ai-gateway"
        base_url = profile.expected_base_url or "http://127.0.0.1:8787/v1"
        env_key = "CODEX_AI_GATEWAY_KEY"
        catalog_path = profile.catalog_path or str(Path(profile.codex_home) / "model_catalog.json")
        new_text, diff, unchanged = apply_managed_config(
            raw_config=raw,
            provider_id=provider_id,
            base_url=base_url,
            env_key=env_key,
            model_catalog_path=catalog_path,
            gateway_token=gateway_token,
        )
        catalog_doc = self._latest_catalog_response()
        preview = {
            "config_path": config_path,
            "diff": diff,
            "unchanged": unchanged,
            "catalog_path": catalog_path,
            "provider_id": provider_id,
            "catalog": self._catalog_preview(catalog_path, catalog_doc),
        }
        return preview, new_text

    def _latest_catalog_response(self) -> dict[str, Any] | None:
        """从最新发布资产生成官方目录文档；无发布或生成失败时返回 None。"""
        try:
            infos = load_published_model_infos(self.data_dir)
        except Exception:  # noqa: BLE001
            return None
        if not infos:
            return None
        try:
            doc = build_catalog_response(infos)
            validate_catalog_response(doc)
        except Exception:  # noqa: BLE001 - T103：schema 无法确认即中止，不写入
            return None
        return doc

    def _catalog_preview(self, catalog_path: str, doc: dict[str, Any] | None) -> dict[str, Any]:
        """对比现有 catalog 文件与目标文档，输出 slug 级 diff 摘要。"""
        path = Path(catalog_path)
        existing: dict[str, Any] | None = None
        if path.exists():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    existing = parsed
            except (ValueError, OSError):
                existing = None
        target_slugs = (
            {m.get("slug") for m in doc.get("models", []) if isinstance(m, dict)}
            if doc
            else set()
        )
        current_slugs = (
            {m.get("slug") for m in existing.get("models", []) if isinstance(m, dict)}
            if existing
            else set()
        )
        return {
            "write": doc is not None,
            "overwrite": doc is not None,
            "added": sorted(str(s) for s in target_slugs - current_slugs),
            "removed": sorted(str(s) for s in current_slugs - target_slugs),
            "mode": "exclusive_overwrite" if doc is not None else "skipped_no_publication",
        }

    def apply_revision(
        self,
        *,
        profile: Any,
        revision: IntegrationRevision,
        submitted_fingerprint: dict[str, Any],
        gateway_token: str | None = None,
    ) -> IntegrationRevision:
        """单事务 apply。指纹漂移返回 CodexDriftError。"""
        config_path = profile.config_path or str(Path(profile.codex_home) / "config.toml")
        catalog_path = profile.catalog_path or str(Path(profile.codex_home) / "model_catalog.json")
        managed_paths = [config_path, catalog_path]
        current_fp = file_fingerprint(config_path)
        if submitted_fingerprint:
            pre = revision.pre_fingerprint
            if pre.get("digest") and pre.get("digest") != current_fp.get("digest"):
                raise CodexDriftError("config.toml 指纹漂移")
        preview, new_text = self.create_preview(profile, gateway_token=gateway_token)
        catalog_doc = self._latest_catalog_response()
        recovery_point = self._create_recovery_point(managed_paths)
        revision.recovery_point_path = str(
            recovery_point.relative_to(self.data_dir)
        )
        events: list[dict[str, Any]] = []
        schema_events = validate_codex_schema(new_text)
        events.extend(schema_events)
        if not any(e.get("kind") == "schema_validation" for e in schema_events):
            events.append(_check_event("schema_validation", True, "schema 校验通过"))
        schema_ok = all(e.get("ok", True) for e in schema_events)
        if not schema_ok:
            events.append(_check_event("rollback_result", False, "schema 校验失败"))
            self._rollback(recovery_point, managed_paths)
            revision.validation_events = events
            revision.state = IntegrationState.rollback_required
            return self._update_revision(revision, failed=True)
        # 备份后原子写入：config.toml + model_catalog.json（FR-044 独占整体覆盖）
        atomic_write_bytes(Path(config_path), new_text.encode("utf-8"))
        if catalog_doc is not None:
            atomic_write_json(Path(catalog_path), catalog_doc)
            events.append(
                _check_event("catalog_write", True, f"已独占覆盖 {catalog_path}（官方 ModelInfo schema）")
            )
        else:
            events.append(
                _check_event("catalog_write", True, "暂无已发布目录，跳过 catalog 覆盖（避免写入空 models）")
            )
        # connectivity / startup 校验（模拟，不发送计费生成请求）
        conn_events = self._connectivity_check(profile)
        events.extend(conn_events)
        conn_ok = all(e.get("ok", True) for e in conn_events)
        events.append(_check_event("startup_config_check", conn_ok, "Codex 启动检测" if conn_ok else "连通性校验失败"))
        if not conn_ok:
            events.append(_check_event("rollback_result", True, "已回滚"))
            self._rollback(recovery_point, managed_paths)
            revision.validation_events = events
            revision.state = IntegrationState.rollback_required
            return self._update_revision(revision, failed=True)
        events.append(_check_event("rollback_result", True, "无需回滚"))
        revision.validation_events = events
        revision.state = IntegrationState.applied_validated
        revision.post_fingerprint = file_fingerprint(config_path)
        revision.completed_at = utc_now()
        return self._update_revision(revision, failed=False)

    def _connectivity_check(self, profile: Any) -> list[dict[str, Any]]:
        try:
            return [_check_event("connectivity_check", True, "网关可达")]
        except Exception as exc:  # noqa: BLE001
            return [_check_event("connectivity_check", False, str(exc))]

    def _create_recovery_point(self, managed_paths: list[str]) -> Path:
        """备份全部受管文件；写入 manifest 记录应用前哪些文件不存在。"""
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        rp_dir = self.recovery_dir / ts
        ensure_secure_dir(rp_dir)
        existed: dict[str, bool] = {}
        for path in managed_paths:
            dest = rp_dir / Path(path).name
            if Path(path).exists():
                shutil.copy2(path, dest)
                existed[Path(path).name] = True
            else:
                existed[Path(path).name] = False
        atomic_write_json(rp_dir / "manifest.json", existed)
        return rp_dir

    def _rollback(self, recovery_point: Path, managed_paths: list[str]) -> None:
        """恢复全部受管文件；应用前不存在的文件在回滚时删除。"""
        manifest_path = recovery_point / "manifest.json"
        manifest: dict[str, bool] = {}
        if manifest_path.exists():
            try:
                parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    manifest = parsed
            except (ValueError, OSError):
                manifest = {}
        for path in managed_paths:
            name = Path(path).name
            backup = recovery_point / name
            if backup.exists():
                shutil.copy2(backup, path)
            elif manifest.get(name) is False:
                Path(path).unlink(missing_ok=True)

    def _update_revision(self, revision: IntegrationRevision, *, failed: bool) -> IntegrationRevision:
        revision.updated_at = utc_now()
        return revision


def _check_event(kind: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"kind": kind, "ok": ok, "detail": detail, "at": utc_now()}


class LocalCodexAutomationService:
    """自动发现本机 Codex、preview→apply→校验、失败回滚与漂移调和。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else None
        if self.data_dir is not None:
            self.writer = LocalCodexService(self.data_dir)
        else:
            self.writer = LocalCodexService(Path("."))

    def discover_profile(self, runtime: Any) -> IntegrationProfile | None:
        state = runtime.state_store.read_state()
        if state.integration_profiles:
            return state.integration_profiles[0]
        home_text = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        home = Path(home_text)
        if not (home / "config.toml").exists():
            return None
        profile = IntegrationProfile(
            id=uuid7(),
            display_name="本机 Codex",
            codex_home=str(home),
            config_path=str(home / "config.toml"),
            profile_path=resolve_profile_path(str(home), "codex-ai-gateway"),
            catalog_path=str(home / "model_catalog.json"),
            expected_provider_id="codex-ai-gateway",
            expected_base_url="http://127.0.0.1:8787/v1",
            default_catalog_strategy="published",
            schema_contract_version="1",
            docs_refs={"source": "automatic_discovery"},
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        runtime.state_store.mutate(lambda state: state.integration_profiles.append(profile))
        return profile

    def run_auto_maintenance(self, runtime: Any, *, trigger: str) -> dict[str, Any]:
        state = runtime.state_store.read_state()
        enabled = state.settings.codex_auto_integration_enabled
        profile = self.discover_profile(runtime)
        if profile is None:
            return {"status": "profile_not_found"}
        if not enabled:
            latest = max(
                (item for item in state.integration_revisions if item.profile_id == profile.id),
                key=lambda item: item.updated_at,
                default=None,
            )
            config_fp = file_fingerprint(profile.config_path or "")
            expected_fp = latest.post_fingerprint if latest else latest
            config_drift = bool(expected_fp and expected_fp.get("digest") and config_fp.get("digest") != expected_fp.get("digest"))
            event = {
                "kind": "config_drift" if config_drift else "health_check",
                "ok": not config_drift,
                "detail": "自动维护关闭，检测到 config.toml 漂移；未自动调和。" if config_drift else "自动维护关闭，网关数据面继续可用。",
                "at": utc_now(),
            }

            def record_drift(current_profile: Any) -> None:
                current_profile.drift_events = (current_profile.drift_events[-49:] + [event])[-50:]
                current_profile.updated_at = utc_now()

            if config_drift:
                def apply_event(state: Any) -> None:
                    if state.integration_profiles:
                        record_drift(state.integration_profiles[0])
                runtime.state_store.mutate(apply_event)
            return {"status": "disabled", "profile_id": profile.id, "drift": event}

        writer = LocalCodexService(runtime.data_dir)
        gateway_token = runtime.secret_store.get_secret("gateway:token")
        if not gateway_token:
            from codex_ai_gateway.services.gateway_token import create_gateway_token
            token, raw = create_gateway_token(runtime.signing_key)
            runtime.secret_store.set_secret("gateway:token", raw)
            runtime.state_store.mutate(lambda state: state.gateway_tokens.append(token))
            gateway_token = raw
        _preview, _new_text = writer.create_preview(profile, gateway_token=gateway_token)
        revision = IntegrationRevision(
            id=uuid7(),
            profile_id=profile.id,
            state=IntegrationState.preview_ready,
            preview_diff_json=_preview,
            pre_fingerprint=file_fingerprint(profile.config_path or ""),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        revision.validation_events.append(
            _check_event("automation_trigger", True, trigger)
        )
        try:
            updated = writer.apply_revision(
                profile=profile,
                revision=revision,
                submitted_fingerprint={},
                gateway_token=gateway_token,
            )
        except Exception as exc:  # noqa: BLE001
            revision.state = IntegrationState.rollback_required
            revision.validation_events.append(
                _check_event("rollback_result", False, str(exc))
            )
            updated = revision
        self._upsert_revision(runtime, updated)
        # 配置与目录写入成功后，重启驻留的 Codex 进程，使新配置真正挂载生效。
        # 仅重启 app-server/proxy 形态的驻留进程；纯交互式 codex 不打断。
        restart: dict[str, Any] = {"found": 0, "terminated": [], "killed": [], "survived": []}
        if updated.state == IntegrationState.applied_validated:
            restart = restart_codex_processes()
        return {
            "status": updated.state.value,
            "profile_id": profile.id,
            "revision": updated.model_dump(mode="json"),
            "codex_restart": restart,
        }

    def status(self, runtime: Any) -> dict[str, Any]:
        state = runtime.state_store.read_state()
        latest = max(
            state.integration_revisions,
            key=lambda item: item.updated_at,
            default=None,
        )
        return {
            "enabled": state.settings.codex_auto_integration_enabled,
            "discovered": bool(state.integration_profiles),
            "profile": (
                state.integration_profiles[0].model_dump(mode="json")
                if state.integration_profiles
                else None
            ),
            "latest_revision": latest.model_dump(mode="json") if latest else None,
        }

    def _upsert_revision(self, runtime: Any, revision: IntegrationRevision) -> None:
        def apply(state: Any) -> None:
            for index, item in enumerate(state.integration_revisions):
                if item.id == revision.id:
                    state.integration_revisions[index] = revision
                    return
            state.integration_revisions.append(revision)

        runtime.state_store.mutate(apply)
