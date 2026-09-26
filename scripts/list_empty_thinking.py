"""筛选待重答的 thinking 类空答题（postcheck 同口径）。

判定（与 gen_quality_report.py 一致）：
- 硬无效：空 / none / null / nan
- 疑似：文本性拒答 / JSON 全空
输出：thinking 类的 qid 列表（空格分隔，可直接喂给 rerun_answer.py）。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\list_empty_thinking.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BAD = {"", "none", "null", "nan"}
COP_OUT = re.compile(r"数据未提供|数据缺失|无法计算|未提供|无法确定|无法判断")


def main() -> None:
    res = pd.read_excel(ROOT / "output" / "result.xlsx", keep_default_na=False)
    qdf = pd.read_excel(ROOT / "data" / "tests.xlsx")
    qdf["key"] = qdf["id"].astype(str)
    qmap = dict(zip(qdf["key"], qdf["question_type"].astype(str)))

    empty_qids: list[str] = []
    others: list[str] = []
    for r in res.itertuples():
        qid = str(r.id)
        ans = str(r.answer).strip()
        qtype = qmap.get(qid, "?")
        invalid = ans.lower() in BAD
        suspect = False
        if not invalid:
            if COP_OUT.search(ans):
                suspect = True
            elif ans[:1] in "[{":
                try:
                    parsed = json.loads(ans)
                    if parsed is None or parsed == [] or parsed == {} or (
                            isinstance(parsed, list) and all(x is None for x in parsed)):
                        suspect = True
                except (json.JSONDecodeError, ValueError):
                    pass
        if invalid or suspect:
            (empty_qids if qtype == "thinking" else others).append(qid)

    print(f"thinking 类空答题 {len(empty_qids)} 个：")
    print(" ".join(empty_qids))
    from collections import Counter
    print(f"\n其他题型空答题 {len(others)} 个（分布：{dict(Counter(qmap.get(q, '?') for q in others))}）")


if __name__ == "__main__":
    main()
