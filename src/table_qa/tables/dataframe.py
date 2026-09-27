"""grid → DataFrame：thinking 题计算与 extract 校验的数据底座。

规则（设计文档 §5.4 dataframe.py）：
- 展开矩阵本身已复制合并格文本（每行自包含，天然满足字段继承）
- 首行为表头；数值清洗：全角转半角、去千分位、括号负数、破折号→NaN
- 单位注（"单位：万元"）从表格文本捕获
"""

from __future__ import annotations

import re

import pandas as pd

from table_qa.schema import Cell, NormalizedTable

_UNIT_PAT = re.compile(r"单位[:：]\s*([万亿千百]?元|[％%])")
_DASH = {"—", "－", "–", "-", "——", "――"}


def to_halfwidth(s: str) -> str:
    """全角转半角（数字与常见符号）。"""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def clean_number(text: str) -> float | int:
    """数字清洗：可转数值则转（千分位/全角/括号负数/百分号/欧式小数逗号），
    不可转抛 ValueError。"""
    s = to_halfwidth(text).strip()
    if s in _DASH or not s:
        raise ValueError(text)
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    # 欧式小数逗号（葡语区 "9,3"=9.3）：千分位分组恒为 3 位数字，
    # 逗号后跟 1~2 位只可能是小数——先转点再剥其余逗号，防 df 层
    # 被污染成 93/94（qid=304/308 实测 max 算成 94）
    if re.fullmatch(r"-?\d{1,3}[，,]\d{1,2}", s):
        s = s.replace("，", ".").replace(",", ".")
    else:
        s = s.replace(",", "").replace("，", "").replace(" ", "")
    if s.endswith("%"):
        s = s[:-1]
    v = float(s)
    if v.is_integer() and "." not in s:
        v = int(v)
    return -v if neg else v


def detect_unit_note(table: NormalizedTable) -> str | None:
    """从表格文本中捕获单位注（如"单位：万元"）。"""
    for row in table.grid[:3]:  # 表头区
        for cell in row:
            m = _UNIT_PAT.search(cell.text)
            if m:
                return m.group(1)
    return None


def _flatten_header(cells: list[Cell], above: list[str]) -> list[str]:
    """多级表头展平：有上方层级时用 "父:子" 命名（合并格继承父级）。"""
    headers: list[str] = []
    for i, cell in enumerate(cells):
        base = cell.text.replace("\n", " ").strip() or f"col{i}"
        parent = above[i] if i < len(above) and above[i] else ""
        headers.append(f"{parent}:{base}" if parent and parent not in base else base)
    return headers


def grid_to_dataframe(grid: list[list[Cell]]) -> pd.DataFrame:
    """展开矩阵 → DataFrame。

    - 首行为表头；若第二行仍疑似表头（含合并格且文本非数值），再展平一层为 "父:子"
    - 数值列自动清洗（不可转的保留原文本）
    - 空矩阵返回空 DataFrame
    """
    if not grid or not grid[0]:
        return pd.DataFrame()

    header_rows = 1
    # 二级表头启发：需结构证据（首行或次行含跨列合并锚点）且次行全非数值。
    # 仅"全文本"不足为据——目录/名单类表格首条数据全为文字，会被误吸进表头
    # （实测案例：AI 工具清单表首行数据 OpenAI/AgentKit/美国/... 被展平成
    #   "国别:美国" 列名，首条数据丢失导致计数少 1）。
    if len(grid) > 2:
        second = grid[1]
        has_merge = any(c.is_anchor and c.colspan > 1 for c in second)
        parent_merge = any(c.is_anchor and c.colspan > 1 for c in grid[0])
        all_text = all(isinstance(clean_number_safe(c.text), str) for c in second)
        if (has_merge or parent_merge) and all_text:
            header_rows = 2

    if header_rows == 2:
        level1 = _flatten_header(grid[0], [""] * len(grid[0]))
        columns = _flatten_header(grid[1], level1)
        data_rows = grid[2:]
    else:
        columns = _flatten_header(grid[0], [""] * len(grid[0]))
        data_rows = grid[1:]

    if not data_rows:
        return pd.DataFrame(columns=columns)

    # 列名去重：表头重复文本（如两个"本期"）会让 df[col] 返回 DataFrame，
    # 下游布尔判断触发 "truth value of a Series is ambiguous"（qid=22 实测）
    seen: dict[str, int] = {}
    uniq_columns: list[str] = []
    for c in columns:
        if c in seen:
            seen[c] += 1
            uniq_columns.append(f"{c}.{seen[c]}")
        else:
            seen[c] = 0
            uniq_columns.append(c)
    columns = uniq_columns

    # 行列对齐：短行补空串，长行截断（VLM 偶发多格）
    width = len(columns)
    records = []
    for r in data_rows:
        texts = [c.text for c in r]
        texts = (texts + [""] * width)[:width]
        records.append(texts)
    df = pd.DataFrame(records, columns=columns)

    # 数值清洗（逐格）：可转数值的格转 number（千分位/全角/括号负数/百分号），
    # 空串转 None(NaN)，其余保留文本。
    # 不做整列 all-or-nothing：含空单元格的数值列（如资产负债表"流动负债："
    # 分组行无金额）会因空串整列保持字符串，pd.to_numeric 对千分位字符串
    # （"1,740,487,431.56"）恒返回 NaN，沙箱计算全空（qid=15 实测）。
    for col in df.columns:
        df[col] = df[col].map(_cell_value)
    return df


def _cell_value(text: str) -> float | int | str | None:
    """单格清洗：数值→number，空串→None(NaN)，其余保留文本。"""
    v = clean_number_safe(text)
    return None if v == "" else v


def clean_number_safe(text: str) -> float | int | str:
    """clean_number 的安全版：不可转时返回原文本（去首尾空白）。"""
    try:
        return clean_number(text)
    except (ValueError, TypeError):
        return text.strip()
