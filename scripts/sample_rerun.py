"""随机抽样 N 题，快照旧答案（重跑前执行，重跑后用 diff_rerun.py 对比）。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\sample_rerun.py 8 [seed]
输出: postcheck/_sample.json（qid -> 旧答案/置信度/事件）
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "postcheck" / "_sample.json"


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    res_path = ROOT / "output" / "submission.xlsx"
    if not res_path.exists():
        res_path = ROOT / "output" / "result.xlsx"   # 旧文件名兼容
    res = pd.read_excel(res_path, keep_default_na=False)
    dbg = pd.read_excel(ROOT / "output" / "result_debug.xlsx", keep_default_na=False)
    dbg["key"] = dbg["id"].astype(str)
    dmap = dbg.set_index("key")

    rng = random.Random(seed)
    idx = rng.sample(range(len(res)), n)
    snap: dict[str, dict] = {}
    for i in idx:
        r = res.iloc[i]
        qid = str(int(r["id"]))
        d = dmap.loc[qid] if qid in dmap.index else None
        snap[qid] = {
            "answer": str(r["answer"]),
            "conf": str(d["confidence"]) if d is not None else "-",
            "events": str(d["events"]) if d is not None else "",
        }
    OUT.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"seed={seed} 抽样 {n} 题已快照 -> {OUT}")
    print("重跑命令: .venv\\Scripts\\python scripts\\rerun_answer.py " + " ".join(snap))
    for qid, v in snap.items():
        print(f"  Q{qid}: {v['answer'][:60]}  [{v['conf']}] {v['events'][:50]}")


if __name__ == "__main__":
    main()
