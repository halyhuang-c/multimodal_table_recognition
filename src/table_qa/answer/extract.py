"""extract 题：LLM 定位取值 + 矩阵回查校验 + 视觉降级。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from loguru import logger

from table_qa.config import get_settings
from table_qa.llm_client import LLMError, get_llm_hub
from table_qa.prompts import get_prompt_manager
from table_qa.schema import FileProfile, NormalizedTable, Question

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


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
    fmt_note = f"答案格式要求：{question.format_note}。" if question.format_note else ""
    prompt = pm.render(
        "extract",
        unit_clause=unit_clause,
        question=question.question,
        table_html=table.html[:8000],   # 超大表截断（prompt 成本控制）
        format_note=fmt_note,
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


def value_empty(value: object) -> bool:
    """extract 结果值是否为空（""/[]/None——触发视觉降级的条件）。"""
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0
    return not str(value).strip()


def vision_extract(question: Question, profile: FileProfile,
                   table: NormalizedTable) -> dict | None:
    """extract 视觉降级：答案不在表格矩阵（图像属性/卡片字段/最左侧表
    头等）时 VLM 看原图直答。qid=484 等 17 题实测：extract LLM 明确答
    "表中未找到"，但答案在原图上。

    输入图：图片文件用原文件；PDF 用 ingest 渲染的页图（选中表所在页
    优先，逐页最多 3 张）。返回与 answer_extract 同构 dict（vision 标
    记降级来源）或 None（全部页都取不到值）。
    """
    images = _vision_images(profile, table)
    if not images:
        return None
    pm = get_prompt_manager()
    hints = {"string": "一个简短文本值", "number": "一个数值",
             "json_array": "JSON 数组", "json": "JSON 结构"}
    prompt = pm.render("extract_vision", question=question.question,
                       format_hint=hints.get(question.answer_format, "一个简短文本值"))
    hub = get_llm_hub()
    for img in images:
        try:
            raw = hub.vision(img, prompt, phase="extract_vision", question_id=question.id)
            data = _extract_json(raw)
        except (LLMError, ValueError, json.JSONDecodeError) as e:
            logger.warning("视觉降级失败 qid={} {}: {}", question.id, img.name, e)
            continue
        value = data.get("value", "")
        if not value_empty(value):
            logger.info("视觉降级命中 qid={} img={} value={!r}", question.id, img.name, value)
            return {"value": value, "locate": {"source": "vision", "image": img.name},
                    "reason": f"表格矩阵未取到，VLM 看原图直答：{data.get('reason', '')}",
                    "verified": False, "vision": True}
    return None


def _vision_images(profile: FileProfile, table: NormalizedTable) -> list[Path]:
    """视觉降级输入图：图片原文件 / PDF 渲染页图（选中表页优先）。"""
    if profile.path.suffix.lower() in _IMAGE_EXTS:
        return [profile.path] if profile.path.exists() else []
    pages_dir = (get_settings().paths.abs_path(get_settings().paths.cache_dir)
                 / "pages")
    first = pages_dir / f"{profile.file_id}_p{table.page}.png"
    ordered: list[Path] = [first] if first.exists() else []
    ordered.extend(p for p in sorted(pages_dir.glob(f"{profile.file_id}_p*.png"))
                   if p not in ordered)
    return ordered[:3]
