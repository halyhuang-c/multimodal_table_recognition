"""在 tests.xlsx 中定位题目（qid / 目标文件 / 题干），并显示已生成的答案。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\find_question.py [关键词]
默认关键词：应收账款
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.config import get_settings  # noqa: E402
from table_qa.ingest.questions import load_questions  # noqa: E402

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "应收账款"


def main() -> None:
    settings = get_settings()
    qs, _ = load_questions(settings)
    for q in qs:
        if KEYWORD in q.question:
            print(f"qid={q.id} 文件={q.file_name} 题型={q.question_type}")
            print(f"  题干: {q.question}")
            ans_path = ROOT / "runs" / "work" / "answers" / f"{q.id}.json"
            if ans_path.exists():
                data = json.loads(ans_path.read_text(encoding="utf-8"))
                ans = data.get("answer", data)
                print(f"  已生成答案: {str(ans)[:120]}")
            else:
                print("  尚无已生成答案")


if __name__ == "__main__":
    main()
