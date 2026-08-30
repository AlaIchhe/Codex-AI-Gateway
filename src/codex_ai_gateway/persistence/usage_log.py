"""用量事件存储：pending 文件 + 按日 NDJSON finalized 追加。

不可变证据。启动时运行 recovery：
- stale pending 标记为 interrupted outcome
- 损坏尾部隔离为 .corrupt
- 同一 attempt id 仅保留最早的 valid finalization
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from codex_ai_gateway.models.entities import Outcome, UsageEvent
from codex_ai_gateway.persistence.atomic_writer import (
    atomic_write_json,
    cleanup_crash_residue,
)
from codex_ai_gateway.persistence.locks import FileLock


class UsageLog:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.pending_dir = self.data_dir / "usage/pending"
        self.events_dir = self.data_dir / "usage/events"
        self.lock_path = self.data_dir / ".usage-log.lock"
        self._local = threading.RLock()
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def _day_for(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d")

    def create_pending(self, event: UsageEvent) -> Path:
        pending_path = self.pending_dir / f"{event.id}.json"
        atomic_write_json(pending_path, event.model_dump(mode="json"))
        return pending_path

    def record_finalized(self, event: UsageEvent) -> None:
        """追加 finalized NDJSON 并移除 pending 文件。"""
        with FileLock(self.lock_path):
            self._record_finalized_unlocked(event)

    def _record_finalized_unlocked(self, event: UsageEvent) -> None:
        """在已持锁时写入 finalized NDJSON 并移除 pending 文件。"""
        with self._local:
            day = self._day_for(
                datetime.fromisoformat(event.started_at.replace("Z", "+00:00"))
            )
            event_path = self.events_dir / f"{day}.ndjson"
            event_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
            with event_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            pending = self.pending_dir / f"{event.id}.json"
            pending.unlink(missing_ok=True)

    def remove_pending(self, event_id: str) -> None:
        (self.pending_dir / f"{event_id}.json").unlink(missing_ok=True)

    def read_events(self, day: str | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.events_dir.glob("*.ndjson")):
            if day and path.stem != day:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return results

    def run_recovery(self) -> dict[str, int]:
        """启动修复：损坏尾部隔离、stale pending 标记、重复 finalized 去重。"""
        stats = {"corrupt_isolated": 0, "stale_interrupted": 0, "duplicates": 0}
        with self._local:
            with FileLock(self.lock_path):
                self._recover_event_files(stats)
                self._recover_stale_pending(stats)
        return stats

    def _recover_event_files(self, stats: dict[str, int]) -> None:
        for path in sorted(self.events_dir.glob("*.ndjson")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            except OSError:
                continue
            valid: list[str] = []
            seen: dict[str, int] = {}
            for line in lines:
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    stats["corrupt_isolated"] += 1
                    continue
                ev_id = parsed.get("id")
                if ev_id in seen:
                    stats["duplicates"] += 1
                    continue
                seen[ev_id] = len(valid)
                valid.append(line)
            corrupt = [raw for raw in lines if raw.strip() and not _line_parses(raw)]
            if corrupt:
                stats["corrupt_isolated"] += len(corrupt)
                corrupt_path = path.with_suffix(path.suffix + ".corrupt")
                with corrupt_path.open("a", encoding="utf-8") as fh:
                    for raw in corrupt:
                        fh.write(raw)
            cleaned = "".join(valid)
            if cleaned != "".join(lines):
                with path.open("w", encoding="utf-8") as fh:
                    fh.write(cleaned)
                    fh.flush()
                    os.fsync(fh.fileno())

    def _recover_stale_pending(self, stats: dict[str, int]) -> None:
        for pending in self.pending_dir.glob("*.json"):
            cleanup_crash_residue(pending)
            if not pending.exists():
                continue
            try:
                raw = json.loads(pending.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pending.unlink(missing_ok=True)
                continue
            raw["outcome"] = Outcome.interrupted.value
            if raw.get("duration_ms") is None:
                raw["duration_ms"] = 0
            event = UsageEvent.model_validate(raw)
            self._record_finalized_unlocked(event)
            stats["stale_interrupted"] += 1


def _line_parses(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except json.JSONDecodeError:
        return False
