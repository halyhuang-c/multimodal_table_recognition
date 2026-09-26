"""检查批量重答目标题的状态：答好 / 无效 / 未重跑。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\check_rerun_status.py 19 22 31 ...
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    qids = [str(a) for a in sys.argv[1:]]
    cutoff = None      # 本轮批量重答开始时间（mtime >= 此值的算已重答）
    bad, ok, stale = [], [], []
    for qid in qids:
        p = ROOT / "runs" / "work" / "answers" / f"{qid}.json"
        if not p.exists():
            stale.append(qid)
            continue
        import datetime
        mt = datetime.datetime.fromtimestamp(p.stat().st_mtime)
        try:
            v = json.loads(p.read_text(encoding="utf-8")).get("value", "")
        except (json.JSONDecodeError, OSError):
            bad.append((qid, "文件损坏"))
            continue
        s = str(v).strip()
        if s.lower() in ("", "none", "null", "nan", "[null]", "[none]", "[]"):
            bad.append((qid, f"{s[:30]} @{mt:%H:%M}"))
        else:
            ok.append(qid)
    print(f"有效 {len(ok)}：{' '.join(ok)}")
    print(f"\n无效 {len(bad)}（需重答）：")
    for qid, why in bad:
        print(f"  {qid}: {why}")
    print(f"\n缺失 {len(stale)}：{' '.join(stale)}")


if __name__ == "__main__":
    main()
