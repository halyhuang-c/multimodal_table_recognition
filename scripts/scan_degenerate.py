"""扫描向量库中的单列退化表（pdfplumber lines 策略丢列的指纹），按文件统计。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\scan_degenerate.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.indexing.vector_store import TableVectorStore  # noqa: E402


def main() -> None:
    store = TableVectorStore()
    res = store._collection.get(include=["metadatas"])
    degenerate: Counter = Counter()
    total: Counter = Counter()
    for m in res["metadatas"]:
        total[m["file_name"]] += 1
        if m.get("cols", 0) <= 1 and m.get("rows", 0) >= 4:
            degenerate[m["file_name"]] += 1
    print(f"{'文件':<12} {'退化单列表':>8} {'总chunk':>8}")
    for f in sorted(total, key=lambda x: -degenerate.get(x, 0)):
        if degenerate.get(f):
            print(f"{f:<12} {degenerate[f]:>8} {total[f]:>8}")
    n = sum(degenerate.values())
    print(f"\n共 {n} 个退化单列表 / {sum(total.values())} chunk，"
          f"涉及 {len(degenerate)} 个文件")


if __name__ == "__main__":
    main()
