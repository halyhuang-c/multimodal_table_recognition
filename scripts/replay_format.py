"""全量 format 重放（零 LLM 调用）：把 B 类 formatter 修复（NaN→null、
复合/枚举空元素区分、string list 防粘连）+ C 类拆分（thinking 直算旧
字符串按复合题分隔符拆分）应用到 runs/work/answers 全部 908 题。

原理：答案 JSON 的 steps[answer].data.value 保存了 raw value（未截断、
未格式化），重走 format_answer 即可重放；confidence/events/usage 不变。

用法（项目根目录）:
    .venv\\Scripts\\python scripts\\replay_format.py            # 重放并回写
    .venv\\Scripts\\python scripts\\replay_format.py --dry-run  # 只统计不写
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loguru import logger  # noqa: E402

from table_qa.answer.formatter import (  # noqa: E402
    format_answer, split_compound_text,
)
from table_qa.config import get_settings  # noqa: E402
from table_qa.graph import _empty_value  # noqa: E402
from table_qa.ingest.questions import load_questions  # noqa: E402
from table_qa.schema import AnswerRecord  # noqa: E402

logger.remove()


def _split_compound_string(raw: object, question_type: str,
                           question_text: str) -> object:
    """C 类重放：thinking 直算旧字符串按复合题分隔符拆分。

    仅处理 thinking 题（extract 值来自 LLM JSON 输出，单值字符串是合法值
    不能拆，如 "3,907,669.31"）；拆分与守卫复用 formatter.split_compound_text
    （与 _parse_direct 单一实现源，防两处漂移）。
    """
    if question_type != "thinking" or not isinstance(raw, str):
        return raw
    parts = split_compound_text(raw, question_text or "")
    return parts if parts is not None else raw


def main() -> None:
    dry = "--dry-run" in sys.argv
    s = get_settings()
    questions, _ = load_questions(s)
    qmap = {q.id: q for q in questions}
    ans_dir = s.paths.abs_path(s.paths.runs_dir) / "work" / "answers"

    stats = {"missing": 0, "no_raw": 0, "unchanged": 0, "changed": 0}
    changes: list[tuple[str, str, str]] = []
    records: dict[str, AnswerRecord] = {}

    for q in questions:
        p = ans_dir / f"{q.id}.json"
        if not p.exists():
            stats["missing"] += 1
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        answer_step = next((st for st in d.get("steps", [])
                            if st.get("step") == "answer"), None)
        if answer_step is not None and "value" in (answer_step.get("data") or {}):
            raw = answer_step["data"]["value"]
            precision = answer_step["data"].get("precision")
            raw2 = _split_compound_string(raw, q.question_type, q.question)
            value = format_answer(raw2, q.answer_format, precision, q)
            if not str(value).strip():
                value = _empty_value(q.answer_format)
        else:
            stats["no_raw"] += 1           # no_table 空框架等：无 raw
            # 旧跑 "None" 字面量泄漏清理（qid=746-748 no_table）：value None
            # 曾被 str(None)="None" 绕过兜底写入字面量，归一到合法空框架；
            # 其余 no_raw（structure 题框架 JSON 等）保持原值不动
            cur = d.get("value")
            value = (cur if cur is not None and str(cur).strip() != "None"
                     else _empty_value(q.answer_format))

        if value != d.get("value"):
            stats["changed"] += 1
            changes.append((q.id, str(d.get("value"))[:60], str(value)[:60]))
            d["value"] = value
            fmt_step = next((st for st in d.get("steps", [])
                             if st.get("step") == "format"), None)
            if fmt_step is not None:
                fmt_step["data"]["value"] = value[:200]
            if not dry:
                p.write_text(json.dumps(d, ensure_ascii=False),
                             encoding="utf-8")
        else:
            stats["unchanged"] += 1
        records[q.id] = AnswerRecord(**d)

    print(f"重放统计: {stats}")
    for qid, old, new in changes[:40]:
        print(f"  qid={qid}\n    旧: {old}\n    新: {new}")
    if len(changes) > 40:
        print(f"  ... 另 {len(changes) - 40} 处变更略")

    if dry:
        print("dry-run：未写文件")
        return

    # 回填 submission.xlsx / result_debug.xlsx
    import pandas as pd

    res_path = ROOT / "output" / "submission.xlsx"
    if not res_path.exists():
        res_path = ROOT / "output" / "result.xlsx"   # 旧文件名兼容
    res = pd.read_excel(res_path)
    for qid, rec in records.items():
        res.loc[res["id"].astype(str) == qid, "answer"] = rec.value
    res.to_excel(res_path, index=False)

    dbg_path = ROOT / "output" / "result_debug.xlsx"
    if dbg_path.exists():
        dbg = pd.read_excel(dbg_path)
        for qid, rec in records.items():
            mask = dbg["id"].astype(str) == qid
            dbg.loc[mask, "answer"] = rec.value
        dbg.to_excel(dbg_path, index=False)
    print(f"已回填 {res_path.name} / result_debug.xlsx（{len(records)} 题）")


if __name__ == "__main__":
    main()
