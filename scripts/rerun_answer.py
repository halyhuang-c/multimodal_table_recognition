"""定向重答指定 qid（答题层修复验证），并发执行，回填 output/*.xlsx。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\rerun_answer.py 14 [更多qid...]     # 删答案重答
    .venv\\Scripts\\python scripts\\rerun_answer.py --from-disk 14 ...  # 仅从磁盘答案回填（零LLM调用）
    .venv\\Scripts\\python scripts\\rerun_answer.py --from-disk all     # 全量重建（908题，秒级）

答题层修复（thinking.py/prompts/config）无需重新入库；
识别层修复请用 Web 调试台「重新入库」按钮。
注意：改 prompts 会使识别缓存指纹失效触发全量重识别（烧额度），
重识别需求用 Web 调试台单文件触发，勿跑全量 run。
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.config import get_settings  # noqa: E402
from table_qa.ingest.categorize import load_profiles  # noqa: E402
from table_qa.ingest.questions import load_questions  # noqa: E402
from table_qa.logging_setup import setup_logging  # noqa: E402
from table_qa.runner import Runner, WorkDir  # noqa: E402
from table_qa.schema import AnswerRecord  # noqa: E402


def _backfill(results: dict[str, AnswerRecord]) -> None:
    import pandas as pd

    res_path = ROOT / "output" / "result.xlsx"
    res = pd.read_excel(res_path)
    for qid, rec in results.items():
        res.loc[res["id"].astype(str) == qid, "answer"] = rec.value
    res.to_excel(res_path, index=False)

    dbg_path = ROOT / "output" / "result_debug.xlsx"
    if dbg_path.exists():
        dbg = pd.read_excel(dbg_path)
        for qid, rec in results.items():
            mask = dbg["id"].astype(str) == qid
            dbg.loc[mask, "answer"] = rec.value
            dbg.loc[mask, "confidence"] = rec.confidence
            dbg.loc[mask, "score"] = rec.confidence_score
            dbg.loc[mask, "events"] = ";".join(rec.confidence_events)
        dbg.to_excel(dbg_path, index=False)
    print(f"已回填 result.xlsx / result_debug.xlsx（{len(results)} 题）")


def main() -> None:
    args = [a for a in sys.argv[1:]]

    # --from-disk：仅回填，不答题
    if args and args[0] == "--from-disk":
        from table_qa.config import get_settings as _gs
        wd = WorkDir(_gs().paths.abs_path(_gs().paths.runs_dir))
        if len(args) > 1 and args[1] != "all":
            qids = args[1:]
            records = {}
            for qid in qids:
                p = wd.answers / f"{qid}.json"
                if p.exists():
                    records[qid] = AnswerRecord(**json.loads(
                        p.read_text(encoding="utf-8")))
        else:
            records = {}
            for p in wd.answers.glob("*.json"):
                try:
                    records[p.stem] = AnswerRecord(**json.loads(
                        p.read_text(encoding="utf-8")))
                except Exception as e:  # noqa: BLE001
                    print(f"跳过损坏答案 {p.name}: {e}")
        _backfill(records)
        return

    qids = args
    if not qids:
        print("用法: python scripts/rerun_answer.py <qid>... | --from-disk <qid>...|all")
        return
    setup_logging()
    s = get_settings()
    runner = Runner(s)
    profiles = load_profiles(s)
    questions, _ = load_questions(s)
    qmap = {q.id: q for q in questions}
    wd = WorkDir(s.paths.abs_path(s.paths.runs_dir))

    for qid in qids:                       # 删旧答案突破 checkpoint
        ans = wd.answers / f"{qid}.json"
        if ans.exists():
            ans.unlink()

    targets = [qmap[qid] for qid in qids if qid in qmap]
    workers = min(s.runner.answer_concurrency, max(len(targets), 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(
            lambda q: runner._answer_one_safe(q, profiles, wd), targets))

    results = {r.question_id: r for r in records}
    for qid, rec in results.items():
        print(f"qid={qid}: value={str(rec.value)[:80]} conf={rec.confidence}"
              f"({rec.confidence_score}) events={rec.confidence_events}")
    _backfill(results)


if __name__ == "__main__":
    main()
