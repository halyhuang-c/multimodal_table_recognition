"""答案格式化：answer_format 四枚举序列化 + 题干精度指令。

规则（设计文档 §5.5.2）：
- number：题干"保留N位小数"优先；整数不带小数、小数去尾零
- string：全角转半角、trim、连续空白折叠
- json_array / json：JSON 字符串（ensure_ascii=False）
"""

from __future__ import annotations

import json
import re

from table_qa.tables.dataframe import to_halfwidth


def format_answer(value: object, answer_format: str, precision: int | None = None,
                  q: object | None = None) -> str:
    """按 answer_format 序列化最终答案（写入 result.xlsx 的字符串）。

    q（Question 对象）用于复合题判定：复合题（一题多问按字段顺序）保留空占位
    防元素错位；枚举题（"列出所有X"）过滤空元素防空串混入。

    None / NaN 入口拦截返回空串：交由调用方空值兜底（graph.format_node
    与 replay_format 均有 `if not str(value).strip(): value = _empty_value(...)
    归一合法空框架）。此前 str(None)="None" / repr(nan)="nan" 是非空字符串，
    会绕过兜底把 Python 字面量泄漏进最终答案（qid=157/495 extract value
    为 null、qid=746-748 no_table value 为 None 均中招，xlsx 里被 pandas
    当默认 NA 读成空）。
    """
    if value is None or (isinstance(value, float) and value != value):
        return ""
    if answer_format == "number":
        return _format_number(value, precision)
    if answer_format == "json_array":
        return _format_json_array(value, q)
    if answer_format == "json":
        return _format_json(value)
    return _format_string(value, q)


def _format_number(value: object, precision: int | None) -> str:
    try:
        num = float(str(value).replace(",", "").replace("，", "").strip())
    except (ValueError, TypeError):
        return str(value).strip()   # 无法数值化时保留原文（答案永不为空）
    if precision is not None:
        return f"{num:.{precision}f}"
    if num.is_integer() and abs(num) < 1e15:
        return str(int(num))
    return repr(num).rstrip("0").rstrip(".") if "." in repr(num) else str(num)


def _format_json_array(value: object, q: object | None = None) -> str:
    items = value if isinstance(value, (list, tuple)) else [value]
    items = [_json_safe(_clean_item(v) if isinstance(v, str) else v) for v in items]
    if not _is_compound_question(q):
        # 枚举题过滤空元素（qid=8 整列空串混入）；复合题保留占位防错位
        items = [v for v in items if not _is_empty_item(v)]
    return json.dumps(items, ensure_ascii=False)


def _json_safe(v: object) -> object:
    """NaN → None：json.dumps 会输出字面量 NaN（非法 JSON），必须转 null（qid=268）。"""
    if isinstance(v, float) and v != v:
        return None
    return v


def _is_empty_item(v: object) -> bool:
    """空元素判定：None / NaN / 空白串（数值 0 与字符串 "0" 不算空）。"""
    if v is None:
        return True
    if isinstance(v, float) and v != v:
        return True
    return isinstance(v, str) and not v.strip()


_COMPOUND_MARKS = ("按表中顺序", "分别是", "以下字段", "依次是", "依次为", "对应的是")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SPLIT_RE = re.compile(r"[、，,;；]|\n+")
# 千分位分组数值（含全角逗号写法）：如 "188,661,393.95" 是一个值不是多值粘连
_GROUPED_NUMBER_RE = re.compile(r"-?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?")


def _is_compound_question(q: object | None) -> bool:
    """复合题判定（一题多问、答案按字段/期间顺序逐项对应）。

    复合题的空元素是"该字段无值"的占位，过滤会导致后续元素错位
    （qid=5 应收票据/应收款项融资表内为空，过滤后 46M 会被误读为
    应收票据的值；qid=249 3Q/4Q 表内无数据同理）；
    枚举题（"列出所有X"）的空元素是整列提取的空格残留，应过滤。

    判定：显式标记词，或"各"字模式（各是多少/各季度/各分类——
    "各"即逐项对位作答；qid=5/18/21/249/35/38/58 均属此类）。
    """
    if q is None:
        return True   # 题干未知时保守不过滤：错位比空串残留更糟
    text = getattr(q, "question", "") or (q.get("question") if isinstance(q, dict) else "") or ""
    text = str(text)
    return any(m in text for m in _COMPOUND_MARKS) or "各" in text


def split_compound_text(s: str, question_text: str) -> list[str] | None:
    """复合题直算文本兜底拆分：多问答案粘连为单字符串时按分隔符拆开。

    thinking._parse_direct 与 scripts/replay_format.py 共用此实现
    （单一实现源，防两处守卫漂移）。返回 None 表示不可拆，调用方保留原串。

    守卫（qid=338 误拆致歉长句）：
    - 仅显式复合标记触发（"各"字按位题不触发：财务数值列表用逗号
      分隔时与千分位无法区分，宁可不拆）；
    - 整串是单个千分位数值时不拆——它是一个值不是多值粘连；
    - 含句号"。"不拆——句子是解释/致歉文本，不是值列表；
    - 拆出片段含中文且超 12 字不拆——散文片段（纯数值/字母片段
      如 "GLM-OCR TEDs_TEST" 不受长度限制）。
    """
    if not question_text or not any(m in question_text for m in _COMPOUND_MARKS):
        return None
    s = s.strip()
    if _GROUPED_NUMBER_RE.fullmatch(s):
        return None
    if "。" in s:
        return None
    parts = [p.strip() for p in _SPLIT_RE.split(s) if p.strip()]
    if len(parts) < 2:
        return None
    if any(len(p) > 12 and _CJK_RE.search(p) for p in parts):
        return None
    return parts


def _format_json(value: object) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)   # 已经是 JSON 字符串则保持结构
        except json.JSONDecodeError:
            return value
    return json.dumps(value, ensure_ascii=False)


def _format_string(value: object, q: object | None = None) -> str:
    if isinstance(value, (list, tuple)):
        return _format_json_array(value, q)
    s = _clean_item(value)
    # 非 str（int/float 等）必须转字符串：上层 format_node 会对返回值调 .strip()
    return s if isinstance(s, str) else str(s)


def _clean_item(v: object) -> object:
    if not isinstance(v, str):
        return v
    s = to_halfwidth(v).strip()
    return re.sub(r"[ \t]+", " ", s)
