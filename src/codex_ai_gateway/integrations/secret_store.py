"""Secret 后端适配层。

通过 keyring 读写凭据与虚拟 key keyed hash 签名材料。启动时验证可写；若不可写
或退化为明文文件，则拒绝启动。
"""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError

SERVICE = "codex-ai-gateway"


class SecretUnavailableError(RuntimeError):
    pass


class SecretStore:
    """Keyring 适配层。

    所有条目使用 `<service>:<account>` 作为 keyring 键。明文只在受保护后端。
    """

    def __init__(self, service: str = SERVICE) -> None:
        self.service = service

    def _validate_backend(self) -> None:
        backend = keyring.get_keyring()
        if backend is None:
            raise SecretUnavailableError("未找到可用的 Secret 后端")
        # 禁止退化为明文文件后端。
        backend_name = type(backend).__name__.lower()
        if "fail" in backend_name and "keyring" not in backend_name:
            raise SecretUnavailableError("Secret 后端不可用")

    def verify_usable(self) -> None:
        """启动时验证 Secret 后端可读写；不可用时抛出异常。"""
        self._validate_backend()
        probe = "__codex-ai-gateway-probe__"
        try:
            keyring.set_password(self.service, probe, "ok")
            got = keyring.get_password(self.service, probe)
            if got != "ok":
                raise SecretUnavailableError("Secret 后端读写校验失败")
            keyring.delete_password(self.service, probe)
        except KeyringError as exc:
            raise SecretUnavailableError(f"Secret 后端不可用: {exc}") from exc

    def set_secret(self, account: str, value: str) -> None:
        try:
            keyring.set_password(self.service, account, value)
        except KeyringError as exc:
            raise SecretUnavailableError(f"写入 Secret 失败: {exc}") from exc

    def get_secret(self, account: str) -> str | None:
        try:
            return keyring.get_password(self.service, account)
        except KeyringError:
            return None

    def delete_secret(self, account: str) -> None:
        try:
            keyring.delete_password(self.service, account)
        except KeyringError:
            pass


def _is_plaintext_file_fallback(store: SecretStore) -> bool:
    """检测是否退化为明文文件后端（仅用于启动拒绝提示）。"""
    backend = keyring.get_keyring()
    name = type(backend).__module__.lower()
    return "file" in name or "plaintext" in name


def assert_no_plaintext_fallback(store: SecretStore) -> None:
    if _is_plaintext_file_fallback(store):
        raise SecretUnavailableError("Secret 后端退化为明文文件，拒绝启动")


class InMemorySecretStore(SecretStore):
    """纯内存 Secret 后端，仅供无头测试和本地开发使用。

    不具备跨重启持久性，生产环境通过 FR-045 机制禁止使用。
    """

    def __init__(self) -> None:
        super().__init__(service="codex-ai-gateway-test")
        self._store: dict[str, str] = {}

    def verify_usable(self) -> None:
        return

    def set_secret(self, account: str, value: str) -> None:
        self._store[account] = value

    def get_secret(self, account: str) -> str | None:
        return self._store.get(account)

    def delete_secret(self, account: str) -> None:
        self._store.pop(account, None)
