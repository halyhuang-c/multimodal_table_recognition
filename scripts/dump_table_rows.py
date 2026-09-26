"""Dump 识别缓存中某文件表格的首列全行（分析行结构用）。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\dump_table_rows.py 003.pdf [关键词]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.cache import TableStore  # noqa: E402
from table_qa.config import get_settings  # noqa: E402
from table_qa.ingest.categorize import load_profiles  # noqa: E402


def main() -> None:
    name = sys.argv[1]
    kw = sys.argv[2] if len(sys.argv) > 2 else ""
    s = get_settings()
    profiles = load_profiles(s)
    profile = profiles.get(name)
    if profile is None:
        print(f"档案缺失: {name}")
        return
    store = TableStore(s)
    tables = store.load(profile.file_id)
    for t in tables:
        print(f"===== {t.table_name or '-'} p{t.page} {t.n_rows}x{t.n_cols} =====")
        for i, row in enumerate(t.grid):
            cells = [c.text.replace("\n", "⏎") for c in row]
            line = " | ".join(cells)
            if kw:
                if kw in line:
                    print(f"  [{i}] {line}")
            else:
                print(f"  [{i}] {line[:120]}")


if __name__ == "__main__":
    main()
