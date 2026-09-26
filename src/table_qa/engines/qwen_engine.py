"""Qwen-VL 视觉识别引擎（C/D/F 类主源）。

- prompt 来自 prompts/recognize/table_recognition.yaml（F 类换 semantic_rebuild）
- 空结果重试一次（换 semantic_rebuild prompt，F 类兜底）
- table_hint 注入 prompt 提升多表页面的目标表命中率
"""

from __future__ import annotations

from loguru import logger

from table_qa.engines.base import TableEngine
from table_qa.ingest.loader import PageAsset
from table_qa.llm_client import LLMError, get_llm_hub
from table_qa.prompts import get_prompt_manager
from table_qa.schema import FileCategory, NormalizedTable
from table_qa.tables.html_table import extract_tables


class QwenEngine(TableEngine):
    name = "qwen-vl"

    def __init__(self) -> None:
        self._llm = get_llm_hub()
        self._pm = get_prompt_manager()

    def _safe_vision(self, image_path, prompt: str, phase: str,
                     **kwargs) -> str | None:
        """VLM 调用容错：失败（额度耗尽/网络/超时重试耗尽）返回 None 而非抛出，
        由重试链继续尝试或整体降级为空表（触发 no_table 扣分），绝不中断全局 run。"""
        try:
            return self._llm.vision(image_path, prompt, phase=phase, **kwargs)
        except LLMError as e:
            logger.warning("VLM 调用失败（{}）：{}", phase, str(e)[:160])
            return None

    def recognize(self, page: PageAsset, table_hint: str | None = None,
                  category: FileCategory | None = None) -> list[NormalizedTable]:
        if page.image_path is None:
            logger.warning("视觉引擎收到无页图资产: {} p{}", page.file_name, page.page_no)
            return []

        prompt_name = "semantic_rebuild" if category == "F" else "table_recognition"
        hint_clause = f"目标表为「{table_hint}」，" if table_hint else ""
        prompt = self._pm.render(prompt_name, table_hint_clause=hint_clause)

        raw = self._safe_vision(page.image_path, prompt, "recognize")
        pairs = extract_tables(raw) if raw else []
        if not pairs:
            # 空结果重试：换语义重建 prompt（VLM 找不到线框表 = F 类强信号）。
            # 真实赛题分类未知，此路径是 F 类兜底。
            logger.warning("VLM 输出无表格（{} p{}），semantic_rebuild 重试",
                           page.file_name, page.page_no)
            semantic_prompt = self._pm.render("semantic_rebuild")
            raw = self._safe_vision(page.image_path, semantic_prompt, "recognize_retry")
            pairs = extract_tables(raw) if raw else []

        tables: list[NormalizedTable] = []
        for name, table_html in pairs:
            table = self._make_table(page, name or table_hint, table_html,
                                     confidence="medium")
            table.source_engine = self.name
            tables.append(table)
        logger.info("VLM 识别 {}: 第{}页 {} 张表（hint={}）",
                    page.file_name, page.page_no, len(tables), table_hint)
        return tables
