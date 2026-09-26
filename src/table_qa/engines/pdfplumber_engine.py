"""pdfplumber 确定性抽表引擎（A/B 类数字原生 PDF 主源）。

零成本、零幻觉：单元格文本直接来自 PDF 文本层。
M1 范围：网格表抽取（rowspan/colspan 重建为 M2 的 rects/lines 分析）。
v2 半网格表修复：lines 策略对"仅部分列有边框"的表会丢列（只剩单列数字），
检测退化单列表后用 y 带 + text 纵向聚类 / lines 横向分行定向重提，
表头/表尾行不在画线包围内，从 band 文本词按列 x 区间归位补全。
"""

from __future__ import annotations

import html as html_lib

from loguru import logger

from table_qa.engines.base import TableEngine
from table_qa.ingest.loader import PageAsset
from table_qa.schema import FileProfile, NormalizedTable

# 半网格表修复策略：纵向按文本对齐聚类分列（找回无边框列），
# 横向仍用画线分行（行边界精确，数字不跨行粘连）
_REPAIR_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "lines"}
# 退化表判定阈值：单列且 ≥4 行（正常业务表极少为单列多行）
_MIN_DEGENERATE_ROWS = 4
# band 在残表 bbox 上下各扩展的高度（pt）：容纳表头/表尾文本行
_BAND_PAD_TOP = 16.0
_BAND_PAD_BOTTOM = 14.0


def _is_degenerate(grid: list[list[str | None]]) -> bool:
    """退化单列表判定：lines 策略丢列后的典型指纹（1 列 × 多行数字）。"""
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)
    return n_cols <= 1 and n_rows >= _MIN_DEGENERATE_ROWS


class PdfplumberEngine(TableEngine):
    name = "pdfplumber"

    def __init__(self, profile: FileProfile) -> None:
        self._profile = profile

    def recognize(self, page: PageAsset, table_hint: str | None = None) -> list[NormalizedTable]:
        import pdfplumber

        tables_out: list[NormalizedTable] = []
        with pdfplumber.open(self._profile.path) as pdf:
            plumber_page = pdf.pages[page.page_no - 1]
            for found in plumber_page.find_tables():
                grid = found.extract()
                if not grid or not any(any(c and c.strip() for c in row) for row in grid):
                    continue
                if _is_degenerate(grid):
                    repaired = self._repair_degenerate(plumber_page, found.bbox)
                    if repaired:
                        logger.info(
                            "pdfplumber 半网格表修复: {} 第{}页 bbox=({:.0f},{:.0f})"
                            " {}行x{}列 -> {}行x{}列",
                            self._profile.file_name, page.page_no,
                            found.bbox[0], found.bbox[1],
                            len(grid), max(len(r) for r in grid),
                            len(repaired), max(len(r) for r in repaired))
                        grid = repaired
                    else:
                        logger.warning(
                            "pdfplumber 半网格表修复失败（保留原残表）: {} 第{}页 "
                            "bbox=({:.0f},{:.0f})",
                            self._profile.file_name, page.page_no,
                            found.bbox[0], found.bbox[1])
                else:
                    # 侧列补全（Q44 教训）：中间数值区有边框、两侧无边框列
                    # （单位名称/计提理由）被丢——band 内 bbox 左右侧游离词
                    # 按 y 对齐回填为新列
                    augmented = self._augment_columns(plumber_page, found, grid)
                    if augmented is not grid:
                        grid = augmented
                table_html = self._grid_to_html(grid)
                title = self._table_title(plumber_page, found.bbox)
                table = self._make_table(page, title or table_hint,
                                         table_html, confidence="high")
                table.source_engine = self.name
                tables_out.append(table)
        logger.info("pdfplumber 抽表 {}: 第{}页 {} 张表",
                    self._profile.file_name, page.page_no, len(tables_out))
        return tables_out

    # ------------------------------------------------------------------
    # 半网格表修复
    # ------------------------------------------------------------------

    @staticmethod
    def _table_title(page, bbox: tuple) -> str | None:
        r"""表格 bbox 上方最近文本行作表名（Q44 教训：表名是最强检索锚，
        '3．单项计提坏账准备的应收账款'直接区分同主题的 3 张表）。

        窗口 36pt 内从下往上找编号标题（^\d+． 优先——财务附注通例）；
        无编号标题时退回最后一行，但排除数值残留词（'期末余额'是
        上一表的表头残留，实测 6x5 表曾误取）。"""
        import re as _re

        _, top, _, _ = bbox
        above = page.crop((0, max(0.0, top - 36), page.width, max(0.0, top - 2)))
        lines = [ln.strip() for ln in (above.extract_text() or "").split("\n")
                 if ln.strip()]
        if not lines:
            return None
        for ln in reversed(lines):                       # 编号标题优先
            if _re.match(r"^\d+[．.]", ln) and len(ln) <= 40:
                return ln
        last = lines[-1]
        if (len(last) > 40 or _re.fullmatch(r"[\d\s]+", last)
                or last in ("期末余额", "期初余额", "单位：元", "单位:元")
                or last.startswith(("注释", "注：", "注:"))):
            return None
        return last

    def _augment_columns(self, page, table, grid: list[list[str | None]]):
        """侧列补全：bbox 左/右侧的游离词按 y 对齐回填为新列。

        场景（006.pdf 单项计提表）：中间数值列有边框、两侧无边框列
        （单位名称 x< bbox.x0、计提理由 x> bbox.x1）被 lines 策略丢弃。
        判定：左右侧游离词 ≥ 行数一半才补（防表外文本误入）。
        行对齐：词中心 y 距最近行中心 ≤ 15pt 才归行。007 教训：按词
        top + 行底容差（rb+3）会把下一行行名吸进上一行——"1．期初余"
        top 距 row0 底仅 1.5pt 即触发，错位逐行传染；中心距离法对长
        行名换行（词中心距行中心实测 ≤6pt）与 006 双行表头上层词
        （高出行顶 ~8pt，中心距 ~12pt 仍最近 row0）均正确。
        """
        x0, top, x1, bottom = table.bbox
        band = page.crop((0, max(0.0, top - _BAND_PAD_TOP),
                          page.width, bottom + _BAND_PAD_BOTTOM))
        n_rows = len(table.rows)
        if not n_rows:
            return grid
        centers = [(r.bbox[1] + r.bbox[3]) / 2 for r in table.rows]
        left: dict[int, list[str]] = {}
        right: dict[int, list[str]] = {}
        for w in band.extract_words():
            if w["x1"] <= x0 + 2:                      # bbox 左侧游离词
                side = left
            elif w["x0"] >= x1 - 2:                    # 右侧
                side = right
            else:
                continue
            wc = (w["top"] + w["bottom"]) / 2
            i = min(range(n_rows), key=lambda k: abs(wc - centers[k]))
            if abs(wc - centers[i]) > 15:              # 表外词（节标题等）丢弃
                continue
            side.setdefault(i, []).append(w["text"])
        add_left = len(left) >= max(2, n_rows // 2)
        add_right = len(right) >= max(2, n_rows // 2)
        if not add_left and not add_right:
            return grid                                # 侧词不足，非侧列丢失
        out: list[list[str | None]] = []
        for i, r in enumerate(grid):
            new = list(r)
            if add_left:
                new = ["".join(left.get(i, []))] + new
            if add_right:
                new = new + ["".join(right.get(i, []))]
            out.append(new)
        if out == grid:
            return grid
        logger.info("pdfplumber 侧列补全: {} 左+{}行 右+{}行",
                    self._profile.file_name, len(left), len(right))
        return out

    def _repair_degenerate(self, page, bbox: tuple) -> list[list[str | None]] | None:
        """y 带 + text 纵向策略重提半网格表，恢复 ≥2 列才算成功。

        band = 残表 bbox 全宽上下扩展；text 聚类找回无边框列，lines 保持
        行边界。画线区外的表头/表尾行由 band 内文本词按列 x 区间归位补全。
        失败返回 None，调用方保留原残表（不劣化）。
        """
        _, top, _, bottom = bbox
        band = page.crop((0, max(0.0, top - _BAND_PAD_TOP),
                          page.width, bottom + _BAND_PAD_BOTTOM))
        found = band.find_tables(table_settings=_REPAIR_SETTINGS)
        if not found:
            # 零线条表（006.pdf 金融资产表）：text/lines 混合策略的横向
            # lines 无锚可依 → 手动词聚类重建（y 分束 + 表头列锚 + 跨行拼接）
            rebuilt = self._rebuild_from_words(band)
            if rebuilt and max(len(r) for r in rebuilt) >= 2:
                return rebuilt
            return None
        table = found[0]
        grid = table.extract()
        if not grid or max((len(r) for r in grid), default=0) < 2:
            rebuilt = self._rebuild_from_words(band)
            if rebuilt and max(len(r) for r in rebuilt) >= 2:
                return rebuilt
            return None
        col_bounds = self._col_bounds(table)
        if not col_bounds:
            return grid
        rows_top = table.rows[0].bbox[1] if table.rows else top
        rows_bottom = table.rows[-1].bbox[3] if table.rows else bottom
        header = self._words_row(band, band.bbox[1], rows_top, col_bounds)
        tail = self._words_row(band, rows_bottom, band.bbox[3], col_bounds)
        return ([header] if header else []) + grid + ([tail] if tail else [])

    @staticmethod
    def _rebuild_from_words(region, y_gap: float = 12.0,
                            anchor_tol: float = 90.0) -> list[list[str]] | None:
        """零线条表手动重建：y 分束 + 表头列锚 + 跨行项目名拼接。

        pdfplumber text 策略对此类表聚类失效（长项目名宽度与列间隙相近，
        006.pdf 金融资产表实测 text/text 仍单列）。手动方案：
        - y 分束：top 差 < 12pt 同束（跨行名/数值行 y 差 5-7pt，条目间 ~15pt）
        - 列锚：第一束（表头）各词 x0，数据词归最近锚（±90pt）
        - 同束同列多词拼接（"分类为…损益的"+"金融资产小计"）
        """
        words = region.extract_words()
        if len(words) < 3:
            return None
        words = sorted(words, key=lambda w: (w["top"], w["x0"]))
        bands: list[list[dict]] = []
        for w in words:
            if bands and w["top"] - bands[-1][0]["top"] < y_gap:
                bands[-1].append(w)
            else:
                bands.append([w])
        # 锚束 = 第一个含 ≥2 词的束（007 教训：band pad 会把单词条表外
        # 标题"注释12．固定资产"包进首束，单锚 → ncol<2 误判失败）
        anchor_band = next((b for b in bands if len(b) >= 2), None)
        if anchor_band is None:
            return None
        anchors = sorted(w["x0"] for w in anchor_band)
        ncol = len(anchors)
        if ncol < 2:
            return None
        rows: list[list[str]] = []
        for b in bands:
            cells: list[list[str]] = [[] for _ in range(ncol)]
            for w in sorted(b, key=lambda x: x["x0"]):
                best = min(range(ncol), key=lambda i: abs(w["x0"] - anchors[i]))
                if abs(w["x0"] - anchors[best]) <= anchor_tol:
                    cells[best].append(w["text"])
            rows.append(["".join(c) for c in cells])
        # 剔除表外注释混入行（"注释12．固定资产"/"注：上表中…"，
        # band pad 会把紧邻的节标题与脚注包进来）
        rows = [r for r in rows
                if not (r[0].startswith(("注释", "注：", "注:"))
                        and not any(r[1:]))]
        return rows or None

    @staticmethod
    def _col_bounds(table) -> list[tuple[float, float] | None]:
        """重试表首行单元格的列 x 区间（None=空占位列），供词归位。"""
        if not table.rows:
            return []
        return [(c[0], c[2]) if c else None for c in table.rows[0].cells]

    @staticmethod
    def _words_row(region, y_min: float, y_max: float,
                   col_bounds: list[tuple[float, float] | None]) -> list[str] | None:
        """y 区间内文本词按最近列 x0 锚定归位为一行（表头/表尾补全）。

        用 x0 锚定而非中心点：财务表头词相对数据列常整体左偏
        （006.pdf 实测中心差 ~38pt 会错列，x0 差 ≤31pt 且列首精确对齐）。
        extract_words 已按 x 升序，列内保持原序拼接；全空返回 None。
        """
        words = [w for w in region.extract_words()
                 if y_min <= w["top"] and w["bottom"] <= y_max]
        if not words:
            return None
        cells = [""] * len(col_bounds)
        for w in words:
            candidates = [(abs(w["x0"] - b[0]), i)
                          for i, b in enumerate(col_bounds) if b]
            if not candidates:
                continue
            _, idx = min(candidates)
            cells[idx] += w["text"]
        return cells if any(c.strip() for c in cells) else None

    @staticmethod
    def _grid_to_html(grid: list[list[str | None]]) -> str:
        """pdfplumber 二维文本矩阵 → HTML（转义，空格规范化）。"""
        parts = ["<table>"]
        for row in grid:
            parts.append("<tr>")
            for cell in row:
                text = html_lib.escape((cell or "").strip())
                parts.append(f"<td>{text}</td>")
            parts.append("</tr>")
        parts.append("</table>")
        return "".join(parts)
