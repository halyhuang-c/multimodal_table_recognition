"""表格 HTML 解析与变换：本模块为纯函数库，无 IO、无模型调用（单测覆盖对象）。

核心概念：
- grid：展开矩阵 list[list[Cell]]，合并区域用同一 Cell 的副本填充（is_anchor=False）
- 锚点格：合并区域左上角的原始 Cell，携带 rowspan/colspan
- 结构 JSON：只序列化锚点格（设计文档 §7）
"""

from __future__ import annotations

import re
from typing import Any

from lxml import html as lxml_html

from table_qa.schema import Cell

_TABLE_PAT = re.compile(r"<table.*?</table>", re.S | re.I)
_NAME_PAT = re.compile(r"TABLE_NAME:\s*(.+)")


def extract_tables(raw: str) -> list[tuple[str | None, str]]:
    """从 VLM 输出提取 (表名, table HTML) 列表。

    容忍前置解释文本；多表场景依赖 "TABLE_NAME: xxx" 标注。
    """
    results: list[tuple[str | None, str]] = []
    # 去掉 markdown 代码围栏
    cleaned = re.sub(r"```(?:html)?", "", raw)
    blocks = _TABLE_PAT.findall(cleaned)
    for block in blocks:
        # 表名 = 该 table 前最近的 TABLE_NAME 标注
        idx = cleaned.find(block)
        prefix = cleaned[max(0, idx - 200):idx]
        names = _NAME_PAT.findall(prefix)
        name = names[-1].strip() if names else None
        if name == "-":
            name = None
        results.append((name, block))
    return results


def html_to_grid(table_html: str) -> list[list[Cell]]:
    """HTML <table> → 展开矩阵。标准 rowspan/colspan 网格填充算法。

    宽容处理：缺 tr/td 标签闭合由 lxml 修复；空表格返回 []。
    """
    try:
        root = lxml_html.fragment_fromstring(table_html, parser=lxml_html.HTMLParser(recover=True))
    except Exception:
        return []
    if root.tag != "table":
        tables = root.xpath(".//table")
        if not tables:
            return []
        root = tables[0]

    rows = root.xpath(".//tr")
    if not rows:
        return []

    # 占位矩阵：先求规模再回填（合并格跨行跨列需要预占位）
    grid: list[list[Cell | None]] = []
    pending: list[tuple[int, int, int, int, str]] = []  # (r, c, rs, cs, text)

    for r, tr in enumerate(rows):
        while len(grid) <= r:
            grid.append([])
        cells = [el for el in tr if el.tag in ("td", "th")]
        c = 0
        for el in cells:
            # 跳过被上方合并格占用的列
            while True:
                while len(grid[r]) <= c:
                    grid[r].append(None)
                if grid[r][c] is None:
                    break
                c += 1
            text = _cell_text(el)
            rs = _span(el.get("rowspan"), 1)
            cs = _span(el.get("colspan"), 1)
            cell = Cell(text=text, row=r, col=c, rowspan=rs, colspan=cs, is_anchor=True)
            for dr in range(rs):
                for dc in range(cs):
                    rr, cc = r + dr, c + dc
                    while len(grid) <= rr:
                        grid.append([])
                    while len(grid[rr]) <= cc:
                        grid[rr].append(None)
                    grid[rr][cc] = cell
            c += cs

    # 展开矩阵：非锚点位置转为副本（pydantic 模型不可变共享语义不安全）
    out: list[list[Cell]] = []
    for r, row in enumerate(grid):
        out_row: list[Cell] = []
        for c, cell in enumerate(row):
            if cell is None:
                out_row.append(Cell(text="", row=r, col=c))  # 漏格补空
            elif cell.is_anchor and (cell.row, cell.col) == (r, c):
                out_row.append(cell)
            else:
                out_row.append(cell.model_copy(update={"row": r, "col": c, "is_anchor": False}))
        if out_row:
            out.append(out_row)
    return out


def _cell_text(el: Any) -> str:
    """单元格文本：保留 <br> 换行，折叠多余空白。"""
    for br in el.xpath(".//br"):
        br.tail = "\n" + (br.tail or "")
    text = el.text_content() or ""
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _span(val: str | None, default: int) -> int:
    try:
        v = int(val) if val else default
        return max(1, v)
    except ValueError:
        return default


def grid_to_structure_json(grid: list[list[Cell]],
                           row_range: tuple[int, int] | None = None,
                           col_range: tuple[int, int] | None = None) -> dict:
    """展开矩阵 → structure 题官方 JSON（赛题说明 4.1）。

    - row_count / col_count 始终为**完整表格**的逻辑行列数；
    - 局部恢复按“行集 ∪ 列集”过滤：锚点格左上角落在行区间**或**列区间即输出
      （如“前1行和前1列”= 第一行的全部格 + 第一列的全部格）；
    - row / col / rowspan / colspan 一律保持全表原值（不重排、不截断）；
    - 只输出锚点格，被合并覆盖的位置不输出占位格。
    """
    anchors: list[dict] = []
    for row in grid:
        for cell in row:
            if not cell.is_anchor:
                continue
            match_rows = row_range is None or row_range[0] <= cell.row <= row_range[1]
            match_cols = col_range is None or col_range[0] <= cell.col <= col_range[1]
            if row_range is not None and col_range is not None:
                keep = match_rows or match_cols   # 行集 ∪ 列集
            else:
                keep = match_rows and match_cols  # 单约束（另一维恒真）
            if not keep:
                continue
            anchors.append({
                "row": cell.row, "col": cell.col,
                "rowspan": cell.rowspan, "colspan": cell.colspan,
                "text": cell.text,
            })
    return {
        "row_count": len(grid),
        "col_count": max((len(r) for r in grid), default=0),
        "cells": anchors,
    }


def grid_to_html(grid: list[list[Cell]]) -> str:
    """展开矩阵 → 规范 HTML（答题 prompt 输入用，只写锚点格标签）。"""
    parts = ["<table>"]
    for row in grid:
        parts.append("<tr>")
        for cell in row:
            if not cell.is_anchor:
                continue
            attrs = []
            if cell.rowspan > 1:
                attrs.append(f'rowspan="{cell.rowspan}"')
            if cell.colspan > 1:
                attrs.append(f'colspan="{cell.colspan}"')
            attr_s = (" " + " ".join(attrs)) if attrs else ""
            text = cell.text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            parts.append(f"<td{attr_s}>{text}</td>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)
