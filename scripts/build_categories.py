# -*- coding: utf-8 -*-
"""一次性脚本：分类清单 xlsx → data/file_categories.json（L1 静态路由元数据）。

用法：.venv\\Scripts\\python scripts\\build_categories.py
"""
import json
import sys
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
# 调研文件已归档至 research/（兼容旧位置：项目根）
_CANDIDATES = [ROOT / "research" / "表格识别数据集分类清单.xlsx",
               ROOT / "表格识别数据集分类清单.xlsx"]
SRC = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])
DST = ROOT / "research" / "file_categories.json"

VALID_CATEGORIES = {"A", "B", "C", "D", "F"}


def main() -> int:
    wb = load_workbook(SRC, read_only=True, data_only=True)
    ws = wb["逐文件分类清单"]
    rows = ws.iter_rows(values_only=True)
    header = list(next(rows))
    idx = {name: i for i, name in enumerate(header)}

    out: dict[str, dict] = {}
    for row in rows:
        if not row or not row[idx["文件名"]]:
            continue
        file_name = str(row[idx["文件名"]]).strip()
        cat = str(row[idx["主类别"]] or "").strip()[:1]
        if cat not in VALID_CATEGORIES:
            cat = "C"
        out[file_name] = {
            "category": cat,
            "language": str(row[idx["语言"]] or "zh").strip(),
            "header_structure": str(row[idx["表头结构"]] or "").strip(),
            "quality": str(row[idx["质量与干扰"]] or "").strip(),
            "note": str(row[idx["分类原因"]] or "").strip(),
        }
    wb.close()

    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    from collections import Counter
    stats = Counter(v["category"] for v in out.values())
    print(f"OK -> {DST} ({len(out)} 文件, 分布 {dict(sorted(stats.items()))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
