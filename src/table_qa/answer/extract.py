"""extract 题：LLM 定位取值 + 矩阵回查校验。"""

from __future__ import annotations

import json
import re

from loguru import logger

from table_qa.llm_client import LLMError, get_llm_hub
from table_qa.prompts import get_prompt_manager
from table_qa.schema import NormalizedTable, Question


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("输出中无 JSON")
    return json.loads(m.group(0))


def _normalize(s: str) -> str:
    """比对用规范化：全角转半角、去空白与常见符号。"""
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        out.append(ch)
    return re.sub(r"[\s,，。%％]", "", "".join(out))


def answer_extract(question: Question, table: NormalizedTable) -> dict:
    """LLM 提取。返回 {value, locate, reason, verified}。

    verified=False 表示取值未在表格矩阵中回查到（幻觉信号，扣置信度）。
    """
    pm = get_prompt_manager()
    unit_clause = f"表内单位注：{table.unit_note}。" if table.unit_note else ""
    prompt = pm.render(
        "extract",
        unit_clause=unit_clause,
        question=question.question,
        table_html=table.html[:8000],   # 超大表截断（prompt 成本控制）
    )
    raw = get_llm_hub().chat(prompt, phase="extract", question_id=question.id)
    try:
        data = _extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        # 模型输出纯文本无 JSON：追加强约束重试一次（仍失败由上层降级）
        logger.warning("extract 输出无 JSON，强约束重试 qid={}", question.id)
        raw = get_llm_hub().chat(
            prompt + "\n\n注意：只输出一个 JSON 对象（{\"value\": ...}），禁止任何其他文字。",
            phase="extract_retry", question_id=question.id)
        data = _extract_json(raw)

    value = data.get("value", "")
    # 多值：value 可能已是 list；字符串中逗号分隔的按题目语义保留原样
    texts = {c.text for row in table.grid for c in row}
    norm_texts = {_normalize(t) for t in texts}

    def _verify(v: object) -> bool:
        if not isinstance(v, str) or not v.strip():
            return False
        nv = _normalize(v)
        if not nv:
            return False
        return nv in norm_texts

    if isinstance(value, list):
        verified = all(_verify(v) for v in value) and len(value) > 0
    else:
        verified = _verify(value)

    if not verified:
        logger.warning("extract 回查失败 qid={} value={!r}", question.id, value)
    return {"value": value, "locate": data.get("locate", {}), "reason": data.get("reason", ""),
            "verified": verified}
