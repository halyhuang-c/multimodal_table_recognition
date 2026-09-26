"""筛 mask_in_table/no_table/rag_miss 事件的题与涉及文件（重识别候选）。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\list_mask_files.py
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PAT = re.compile(r"mask_in_table|no_table|rag_miss")

res = pd.read_excel(ROOT / "output" / "result_debug.xlsx", keep_default_na=False)
qdf = pd.read_excel(ROOT / "data" / "tests.xlsx")
qdf["key"] = qdf["id"].astype(str)
fmap = dict(zip(qdf["key"], qdf["file_name"].astype(str)))

by_file: dict[str, list[str]] = defaultdict(list)
for r in res.itertuples():
    qid = str(r.id)
    ev = str(r.events)
    if PAT.search(ev):
        by_file[fmap.get(qid, "?")].append(qid)

print(f"涉及 {len(by_file)} 个文件 / {sum(len(v) for v in by_file.values())} 题：")
for f in sorted(by_file):
    print(f"  {f}: {' '.join(by_file[f])}")
print("\n重识别命令: .venv\\Scripts\\python scripts\\reingest.py " +
      " ".join(sorted(by_file)))
