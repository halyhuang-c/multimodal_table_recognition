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


def slice_grid(grid: list[list[Cell]], row_range: tuple[int, int] | None,
               col_range: tuple[int, int] | None) -> list[list[Cell]]:
    """按行列范围切片（structure 局部题）。切断的合并格按边界截断跨度。"""
    r0, r1 = row_range or (0, max(len(grid) - 1, 0))
    max_c = max((len(r) for r in grid), default=0)
    c0, c1 = col_range or (0, max(max_c - 1, 0))
    r0, r1 = max(0, r0), min(r1, len(grid) - 1)
    c0, c1 = max(0, c0), min(c1, max_c - 1)
    if r1 < r0 or c1 < c0:
        return []

    out: list[list[Cell]] = []
    for r in range(r0, r1 + 1):
        row_out: list[Cell] = []
        for c in range(c0, c1 + 1):
            src = grid[r][c] if c < len(grid[r]) else Cell(text="", row=r, col=c)
            rs = min(src.row + src.rowspan - 1, r1) - r + 1   # 截断跨度
            cs = min(src.col + src.colspan - 1, c1) - c + 1
            if src.is_anchor and (src.row, src.col) == (r, c):
                row_out.append(src.model_copy(update={"rowspan": rs, "colspan": cs}))
            elif src.is_anchor:
                # 锚点在切片外但跨度进入切片：降级为普通格（不再表达合并）
                row_out.append(Cell(text=src.text, row=r, col=c, rowspan=rs, colspan=cs,
                                    is_anchor=(src.row >= r0 and src.col >= c0)))
            else:
                row_out.append(Cell(text=src.text, row=r - r0, col=c - c0,
                                    rowspan=min(rs, 1), colspan=min(cs, 1), is_anchor=False))
        out.append(row_out)

    # 重排行列索引到切片坐标系
    for r, row in enumerate(out):
        for c, cell in enumerate(row):
            if cell.is_anchor:
                cell.row, cell.col = r, c
    return out


def grid_to_structure_json(grid: list[list[Cell]]) -> dict:
    """展开矩阵 → structure 题 JSON（设计文档 §7 自设计 schema，adapter 可切换）。"""
    anchors: list[dict] = []
    for row in grid:
        for cell in row:
            if cell.is_anchor:
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
