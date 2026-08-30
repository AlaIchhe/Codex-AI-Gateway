"""临时脚本：读取 cc-switch 数据库中的供应商 base_url（不输出密钥）。"""

from __future__ import annotations

import json
import os
import sqlite3


def main() -> None:
    db_path = os.path.expanduser(r"~\.cc-switch\cc-switch.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    print("tables:", tables)
    for table in tables:
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        print(f"-- {table}: {cols}")
        if not any(c in cols for c in ("settings_config", "config", "json", "data")):
            continue
        cur.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        for row in rows:
            record = dict(zip(cols, row, strict=False))
            for _key, value in record.items():
                if isinstance(value, str) and value.lstrip().startswith("{"):
                    try:
                        parsed = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                    flattened = json.dumps(parsed, ensure_ascii=False)
                    if "base_url" in flattened or "baseUrl" in flattened:
                        print(f"[{table}] id={record.get('id')} name={record.get('name')}")
                        print("  keys:", list(parsed.keys()))
                        print("  snippet:", flattened[:400])
    conn.close()


if __name__ == "__main__":
    main()
