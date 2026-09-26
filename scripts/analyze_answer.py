"""分析指定题目的完整答题轨迹（逐步核实辅助）。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\analyze_answer.py 17 19
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.config import get_settings  # noqa: E402
from table_qa.ingest.questions import load_questions  # noqa: E402


def main() -> None:
    qids = sys.argv[1:]
    settings = get_settings()
    qs, _ = load_questions(settings)
    qmap = {q.id: q for q in qs}
    ans_dir = settings.paths.abs_path(settings.paths.runs_dir) / "work" / "answers"

    for qid in qids:
        q = qmap.get(qid)
        print("=" * 88)
        print(f"Q{qid} [{q.question_type if q else '?'}] 文件: {q.file_name if q else '?'}"
              f"  格式: {q.answer_format if q else '?'}")
        print(f"题干: {q.question if q else '?'}")
        p = ans_dir / f"{qid}.json"
        if not p.exists():
            print("!! 无答案文件")
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        print(f"答案: {d['value']}")
        print(f"置信: {d['confidence']} ({d['confidence_score']})  事件: {d['confidence_events']}")
        for s in d.get("steps", []):
            data = s.get("data") or {}
            print(f"\n--- [{s['step']}] {s['title']} ({s['status']}) ---")
            if s["step"] == "retrieve":
                for h in data.get("hits", []):
                    print(f"  命中: {h.get('table_name') or '-'} p{h.get('page')} "
                          f"sim={h.get('similarity')} adj={h.get('adjusted_score')}")
            elif s["step"] == "select":
                print(f"  选中: {data.get('selected')} p{data.get('page', '?')} "
                      f"{data.get('rows')}x{data.get('cols')} engine={data.get('engine')} "
                      f"匹配分={data.get('score')}")
            elif s["step"] == "answer":
                if "code" in data:
                    print(f"  执行: {'沙箱OK' if data.get('exec_ok') else '沙箱失败/直算'}")
                    print(f"  代码:\n{data.get('code', '')[:600]}")
                print(f"  取值: {json.dumps(data.get('value'), ensure_ascii=False)[:200]}")
                if data.get("locate"):
                    print(f"  定位: {json.dumps(data['locate'], ensure_ascii=False)[:200]}")
                if data.get("reason"):
                    print(f"  理由: {data['reason'][:200]}")
            elif s["step"] == "range":
                print(f"  方式: {data.get('method')} ranges: p={data.get('page_range')} "
                      f"r={data.get('row_range')} c={data.get('col_range')}")
        print()


if __name__ == "__main__":
    main()
