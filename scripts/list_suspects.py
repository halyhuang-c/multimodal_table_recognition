"""列出当前疑似空答题（postcheck 同口径，不分题型）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BAD = {"", "none", "null", "nan"}
COP_OUT = re.compile(r"数据未提供|数据缺失|无法计算|未提供|无法确定|无法判断|无法读取")

res = pd.read_excel(ROOT / "output" / "result.xlsx", keep_default_na=False)
qdf = pd.read_excel(ROOT / "data" / "tests.xlsx")
qdf["key"] = qdf["id"].astype(str)
qmap = dict(zip(qdf["key"], qdf["question_type"].astype(str)))

suspects = []
for r in res.itertuples():
    qid = str(r.id)
    ans = str(r.answer).strip()
    if ans.lower() in BAD:
        continue
    hit = False
    if COP_OUT.search(ans):
        hit = True
    elif ans[:1] in "[{":
        try:
            p = json.loads(ans)
            if p is None or p == [] or p == {} or (
                    isinstance(p, list) and all(x is None for x in p)):
                hit = True
        except (json.JSONDecodeError, ValueError):
            pass
    if hit:
        suspects.append((qid, qmap.get(qid, "?"), ans[:50]))

print(f"疑似空答 {len(suspects)} 题：")
for qid, t, a in suspects:
    print(f"  Q{qid} [{t}] {a}")
print("\n重跑: " + " ".join(q for q, _, _ in suspects))
