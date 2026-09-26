# -*- coding: utf-8 -*-
"""无 API Key 的数据链路冒烟：题目修复 + 文件路由 + 表格缓存层。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from table_qa.config import get_settings
from table_qa.ingest.categorize import load_profiles
from table_qa.ingest.questions import group_by_file, load_questions


def main() -> int:
    settings = get_settings()
    questions, skipped = load_questions(settings)
    profiles = load_profiles(settings)

    # 1) 题目修复验证
    repaired = [q for q in questions if q.repaired]
    print(f"\n[1] 题目: {len(questions)} 合法 / {len(skipped)} 跳过 / {len(repaired)} 有修复")
    for q in repaired[:5]:
        print(f"    #{q.id} {q.repaired}")

    # 2) 笔误文件映射验证
    fixed = [q for q in questions if any("file_name" in r for r in q.repaired)]
    print(f"[2] 文件名笔误修复: {[(q.id, q.file_name) for q in fixed]}")

    # 3) 文件路由分布
    from collections import Counter
    cats = Counter(profiles[q.file_name].category for q in questions
                   if q.file_name in profiles)
    print(f"[3] 题目→文件路由: {dict(sorted(cats.items()))}")
    missing = [q.file_name for q in questions if q.file_name not in profiles]
    print(f"    档案缺失: {missing[:5] or '无'}")

    # 4) 去重检查
    dups = [(p.file_name, p.dedup_of) for p in profiles.values() if p.dedup_of]
    print(f"[4] 内容去重: {dups}")

    # 5) 按文件分组（缓存收益）
    groups = group_by_file(questions)
    top = max(groups.items(), key=lambda kv: len(kv[1]))
    print(f"[5] 单文件最多题数: {top[0]} = {len(top[1])} 题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
