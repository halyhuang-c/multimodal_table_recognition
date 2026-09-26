"""引擎抽象：识别引擎统一接口。

实现：pdfplumber_engine（A/B 类确定性抽表）、qwen_engine（C/D/F 类视觉识别）。
PaddleOCR 引擎 M2 接入（配置开关预留）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from table_qa.ingest.loader import PageAsset
from table_qa.tables.html_table import html_to_grid
from table_qa.schema import Cell, NormalizedTable


class TableEngine(ABC):
    """页面 → 规范表格列表。"""

    name: str = "base"

    @abstractmethod
    def recognize(self, page: PageAsset, table_hint: str | None = None) -> list[NormalizedTable]:
        """识别一页，可能返回 0~N 张表。"""

    @staticmethod
    def _make_table(page: PageAsset, table_name: str | None, table_html: str,
                    confidence: str = "medium") -> NormalizedTable:
        """从表 HTML 构造 NormalizedTable（展开矩阵 + 单位探测）。"""
        from table_qa.tables.dataframe import detect_unit_note

        grid = html_to_grid(table_html)
        table = NormalizedTable(
            file_id=page.file_id,
            file_name=page.file_name,
            table_name=table_name,
            page=page.page_no,
            html=table_html,
            grid=grid,
            source_engine="unknown",
            unit_note=None,
            confidence=confidence,  # type: ignore[arg-type]
        )
        table.source_engine = "unknown"
        table.unit_note = detect_unit_note(table)
        return table

    @staticmethod
    def _empty_cells(grid: list[list[Cell]]) -> int:
        return sum(1 for row in grid for c in row if not c.text.strip())
