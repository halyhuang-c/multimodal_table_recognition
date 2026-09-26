"""structure 题：范围解析（正则优先，LLM 兜底）+ 确定性过滤 + 官方结构 JSON。"""

from __future__ import annotations

import json
import re

from loguru import logger

from table_qa.llm_client import LLMError, get_llm_hub
from table_qa.prompts import get_prompt_manager
from table_qa.schema import NormalizedTable, ParsedRange, Question
from table_qa.tables.html_table import grid_to_structure_json

# 正则白名单：覆盖常见中文范围表述（命中即免 LLM 调用，省 token）
_PATTERNS = [
    (re.compile(r"第\s*(\d+)\s*页\s*(?:到|至|-|—)\s*(\d+)\s*页"), "page"),
    (re.compile(r"第\s*(\d+)\s*页"), "page"),
    (re.compile(r"前\s*([一二两三四五六七八九十\d]+)\s*行"), "rows"),
    (re.compile(r"前\s*([一二两三四五六七八九十\d]+)\s*列"), "cols"),
    (re.compile(r"第\s*([一二两三四五六七八九十\d]+)\s*行"), "row1"),
    (re.compile(r"第\s*([一二两三四五六七八九十\d]+)\s*列"), "col1"),
    (re.compile(r"表头行?"), "header"),
]
_CN_DIGIT = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> int | None:
    """中文/阿拉伯数字转 int。支持 '1'、'十二'、'二十'、'二十三' 等。
    无法解析时返回 None。"""
    try:
        return int(s)
    except ValueError:
        pass
    if "十" in s:
        parts = s.split("十")
        tens = _CN_DIGIT.get(parts[0]) if parts[0] else 1
        ones = _CN_DIGIT.get(parts[1], 0) if len(parts) > 1 else 0
        if tens is not None:
            return tens * 10 + ones
    return _CN_DIGIT.get(s) if len(s) == 1 else None


def parse_range(question: Question) -> tuple[ParsedRange, str]:
    """解析题目范围。返回 (ParsedRange, 方式)——regex 或 llm。"""
    text = question.question
    page = row = col = None

    for pat, kind in _PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        if kind == "page" and page is None:
            g = m.groups()
            page = (int(g[0]), int(g[1])) if len(g) == 2 and g[1] else (int(g[0]), int(g[0]))
        elif kind == "rows" and row is None:
            n = _cn_to_int(m.group(1))
            if n is not None:
                row = (0, n - 1)
        elif kind == "cols" and col is None:
            n = _cn_to_int(m.group(1))
            if n is not None:
                col = (0, n - 1)
        elif kind == "row1" and row is None:
            # “第N行”指单独第 N 行（0 基即 N-1），不是前 N 行
            n = _cn_to_int(m.group(1))
            if n is not None:
                row = (n - 1, n - 1)
        elif kind == "col1" and col is None:
            # “第N列”指单独第 N 列
            n = _cn_to_int(m.group(1))
            if n is not None:
                col = (n - 1, n - 1)
        elif kind == "header" and row is None:
            row = (0, 1)

    # 表头行且提到"第一行/前1行"时保持已解析值
    if row or col or page:
        return ParsedRange(page_range=page, row_range=row, col_range=col), "regex"

    # LLM 兜底
    pm = get_prompt_manager()
    prompt = pm.render("range_parse", question=text, answer_format=question.answer_format)
    try:
        raw = get_llm_hub().chat(prompt, phase="range_parse", question_id=question.id)
        data = json.loads(_extract_json(raw))
        rng = ParsedRange(
            page_range=_tuple_or_none(data.get("page_range")),
            row_range=_tuple_or_none(data.get("row_range")),
            col_range=_tuple_or_none(data.get("col_range")),
        )
        return rng, "llm"
    except (LLMError, json.JSONDecodeError, ValueError) as e:
        logger.warning("范围解析 LLM 兜底失败，按整表处理: {}", e)
        return ParsedRange(), "fallback_full"


def _extract_json(text: str) -> str:
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text


def _tuple_or_none(v: object) -> tuple[int, int] | None:
    if isinstance(v, list | tuple) and len(v) == 2:
        return int(v[0]), int(v[1])
    return None


def answer_structure(question: Question, table: NormalizedTable,
                     rng: ParsedRange) -> tuple[dict, bool]:
    """structure 答题：在全表上按范围过滤 → 官方结构 JSON。返回 (json_dict, 是否局部)。

    官方规范（赛题说明 4.1）：row_count/col_count 与 row/col/rowspan/colspan
    均按完整表格输出，局部恢复只裁剪 cells（“行集 ∪ 列集”的锚点格）。
    """
    is_partial = bool(rng.row_range or rng.col_range)
    js = grid_to_structure_json(table.grid, rng.row_range, rng.col_range)
    return js, is_partial
