"""题目清单解析与修复：文件名补零 / 脏行题型修复 / 校验。

实证数据问题（勘察结论）：
- 3 处文件名笔误：58.pdf/59.pdf/0060.pdf → 058.pdf/059.pdf/060.pdf
- 1 条脏行（id=63）：question_type 被问题文本占用
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pandas as pd
from loguru import logger

from table_qa.config import Settings
from table_qa.schema import AnswerFormat, Question, QuestionType

_VALID_TYPES: set[str] = {"structure", "extract", "thinking"}
_VALID_FORMATS: set[str] = {"string", "number", "json_array", "json"}

# 题干常见"计算/比较/判断"动词 → thinking；否则 extract
_THINKING_PAT = re.compile(r"计算|求和|求差|比例|增长|平均|最高|最低|最大|最小|几个|多少个|排名|比较|判断|分差|合计|之和")


def _fix_file_name(name: str, files_dir: Path) -> tuple[str, bool]:
    """文件名补零归一化：58.pdf → 058.pdf。找不到文件时返回原名。"""
    if (files_dir / name).exists():
        return name, False
    m = re.match(r"^(\d+)(\.\w+)$", name.strip())
    if m:
        padded = f"{int(m.group(1)):03d}{m.group(2)}"
        if (files_dir / padded).exists():
            return padded, True
    return name, False


def _fix_question_type(raw: object, question: str, answer_format: str) -> QuestionType:
    """脏行修复：question_type 非枚举值时按题意推断。"""
    val = str(raw or "").strip()
    if val in _VALID_TYPES:
        return val  # type: ignore[return-value]
    # 推断：json + "恢复/结构" → structure；含计算动词 → thinking；否则 extract
    if answer_format == "json" and re.search(r"恢复|结构", question):
        return "structure"
    if _THINKING_PAT.search(question):
        return "thinking"
    return "extract"


def _normalize_answer_format(fmt_raw: str) -> tuple[str, bool]:
    """answer_format 列规范化为四枚举之一。返回 (枚举, 是否经过推断)。

    官方将该列定义为"对答案格式的补充说明"（自然语言），样本数据恰好是
    枚举值。兼容两形态：枚举直用；自然语言按关键词推断；原文保留在
    Question.format_note（精度/单位信息不丢失）。
    """
    if fmt_raw in _VALID_FORMATS:
        return fmt_raw, False
    text = fmt_raw.lower()
    # 数组判定在前："JSON数组" 应归 json_array 而非 json
    if re.search(r"数组|列表|多个值|多值|多个结果|逐一列出|按顺序列出", text):
        return "json_array", True
    if re.search(r"json|结构", text):
        return "json", True
    if re.search(r"数值|数字|金额|百分比|小数|整数|数量", text):
        return "number", True
    return "string", True


def load_questions(settings: Settings) -> tuple[list[Question], list[dict]]:
    """读取 tests.xlsx → (合法题目列表, 跳过行明细)。绝不因单行异常中断全局。"""
    xlsx = settings.paths.abs_path(settings.paths.tests_xlsx)
    files_dir = settings.paths.abs_path(settings.paths.files_dir)
    df = pd.read_excel(xlsx, dtype=str).fillna("")

    questions: list[Question] = []
    skipped: list[dict] = []
    seen_ids: set[str] = set()

    for row_idx, row in df.iterrows():
        qid = str(row.get("id", "")).strip()
        issues: list[str] = []

        # 1) id 唯一性
        if not qid:
            skipped.append({"excel_row": row_idx + 2, "reason": "id 为空", "row": row.to_dict()})
            continue
        if qid in seen_ids:
            skipped.append({"excel_row": row_idx + 2, "reason": f"id 重复: {qid}", "row": row.to_dict()})
            continue
        seen_ids.add(qid)

        # 2) 文件名补零修复
        raw_name = str(row.get("file_name", "")).strip()
        file_name, fixed = _fix_file_name(raw_name, files_dir)
        if fixed:
            issues.append(f"file_name: {raw_name}->{file_name}")
        if not (files_dir / file_name).exists():
            skipped.append({"excel_row": row_idx + 2, "reason": f"文件不存在: {file_name}",
                            "row": row.to_dict()})
            continue

        # 3) 脏行题型修复
        raw_type = row.get("question_type", "")
        question_text = str(row.get("question", "")).strip()
        qtype = _fix_question_type(raw_type, question_text, str(row.get("answer_format", "")))
        if str(raw_type).strip() != qtype:
            issues.append(f"question_type 修复: {raw_type}->{qtype}")
        if not question_text:
            skipped.append({"excel_row": row_idx + 2, "reason": "question 为空", "row": row.to_dict()})
            continue

        # 4) answer_format 规范化：官方语义是"对答案格式的补充说明"，
        #    可能是自然语言（如"百分比，保留一位小数"），兼容枚举与文本两种形态
        fmt_raw = str(row.get("answer_format", "")).strip()
        fmt, inferred = _normalize_answer_format(fmt_raw)
        if inferred:
            issues.append(f"answer_format 推断: {fmt_raw or '(空)'} -> {fmt}")

        hint = str(row.get("table_hint", "")).strip() or None

        questions.append(Question(
            id=qid,
            file_name=file_name,
            question_type=qtype,
            question=question_text,
            table_hint=hint,
            answer_format=fmt,  # type: ignore[arg-type]
            format_note=fmt_raw if fmt_raw != fmt else "",
            repaired=issues,
        ))
        if issues:
            logger.info("题目 {} 修复: {}", qid, "; ".join(issues))

    logger.info("题目加载完成: {} 合法 / {} 跳过", len(questions), len(skipped))
    return questions, skipped


def write_skipped_csv(skipped: list[dict], path: Path) -> None:
    """跳过行落盘（审计用）。"""
    if not skipped:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["excel_row", "reason", "raw"])
        for s in skipped:
            w.writerow([s["excel_row"], s["reason"], str(s["row"])])


def group_by_file(questions: list[Question]) -> dict[str, list[Question]]:
    """按目标文件分组（识别阶段按文件去重的依据）。"""
    groups: dict[str, list[Question]] = {}
    for q in questions:
        groups.setdefault(q.file_name, []).append(q)
    return groups
