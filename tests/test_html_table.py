"""html_table 纯逻辑单测：rowspan/colspan 展开、官方结构 JSON（含局部恢复）。"""

from table_qa.tables.html_table import (
    grid_to_html,
    grid_to_structure_json,
    html_to_grid,
)

SIMPLE = """
<table>
  <tr><td>项目</td><td>2025</td><td>2026</td></tr>
  <tr><td>收入</td><td>100</td><td>200</td></tr>
</table>
"""

MERGED = """
<table>
  <tr><td rowspan="2">A</td><td>1</td></tr>
  <tr><td>2</td></tr>
  <tr><td colspan="2">B</td></tr>
</table>
"""

# 赛题官方示例同构表（3 行 × 4 列，含行/列合并）：
# (0,0)项目 rs=2 | (0,1)金额 cs=2 | (0,3)备注
# (1,1)预算       | (1,2)实际       | (1,3)说明
# (2,0)销售额     | (2,1)100        | (2,2)200 | (2,3)ok
OFFICIAL = """
<table>
  <tr><td rowspan="2">项目</td><td colspan="2">金额</td><td>备注</td></tr>
  <tr><td>预算</td><td>实际</td><td>说明</td></tr>
  <tr><td>销售额</td><td>100</td><td>200</td><td>ok</td></tr>
</table>
"""


def test_simple_grid() -> None:
    grid = html_to_grid(SIMPLE)
    assert len(grid) == 2
    assert [c.text for c in grid[0]] == ["项目", "2025", "2026"]
    assert grid[1][1].text == "100"


def test_rowspan_expansion() -> None:
    grid = html_to_grid(MERGED)
    assert len(grid) == 3
    # A 跨两行：第二行第 0 列应为 A 的副本（非锚点）
    assert grid[0][0].text == "A" and grid[0][0].is_anchor
    assert grid[1][0].text == "A" and not grid[1][0].is_anchor
    assert grid[1][1].text == "2"


def test_colspan_expansion() -> None:
    grid = html_to_grid(MERGED)
    # B 跨两列：第三行两列都是 B
    assert grid[2][0].text == "B" and grid[2][0].is_anchor and grid[2][0].colspan == 2
    assert grid[2][1].text == "B" and not grid[2][1].is_anchor


def test_structure_json_anchors_only() -> None:
    grid = html_to_grid(MERGED)
    js = grid_to_structure_json(grid)
    assert js["row_count"] == 3 and js["col_count"] == 2
    anchors = js["cells"]
    # 锚点格：A(0,0) 1(0,1) 2(1,1) B(2,0) = 4 个（合并副本不重复计）
    assert len(anchors) == 4
    texts = {(c["row"], c["col"]) for c in anchors}
    assert (0, 0) in texts and (1, 1) in texts and (2, 0) in texts
    b = next(c for c in anchors if c["text"] == "B")
    assert b["colspan"] == 2


# ---------------------------------------------------------------------------
# 局部结构恢复（赛题官方规范 4.1）
# ---------------------------------------------------------------------------


def test_partial_union_semantics() -> None:
    """官方示例语义："前1行和前1列" = 第一行全部格 ∪ 第一列全部格。

    锚点格左上角落在行区间**或**列区间即输出——不是矩形交集。
    """
    grid = html_to_grid(OFFICIAL)
    js = grid_to_structure_json(grid, (0, 0), (0, 0))
    cells = {(c["row"], c["col"]): c for c in js["cells"]}
    # 第一行全部：项目(0,0) 金额(0,1) 备注(0,3)
    # 第一列全部：项目(0,0) 销售额(2,0)
    assert set(cells) == {(0, 0), (0, 1), (0, 3), (2, 0)}
    assert cells[(0, 0)]["text"] == "项目"
    assert cells[(0, 1)]["text"] == "金额"
    assert cells[(2, 0)]["text"] == "销售额"


def test_partial_keeps_full_table_geometry() -> None:
    """row_count/col_count 填完整表格逻辑行列数；row/col/跨度不重排不截断。"""
    grid = html_to_grid(OFFICIAL)
    js = grid_to_structure_json(grid, (0, 0), (0, 0))
    assert js["row_count"] == 3       # 完整表格行数，不是 1
    assert js["col_count"] == 4       # 完整表格列数，不是 1
    proj = next(c for c in js["cells"] if c["text"] == "项目")
    assert proj["rowspan"] == 2       # 跨度保持真实合并范围（不截断为 1）
    amount = next(c for c in js["cells"] if c["text"] == "金额")
    assert amount["colspan"] == 2 and amount["col"] == 1   # 坐标按全表 0 基


def test_partial_row_only_keeps_all_columns() -> None:
    """单约束（只有行区间）：该行全部锚点格，跨列合并格保持原 colspan。"""
    grid = html_to_grid(MERGED)
    js = grid_to_structure_json(grid, (2, 2), None)
    cells = {(c["row"], c["col"]): c for c in js["cells"]}
    assert set(cells) == {(2, 0)}
    assert cells[(2, 0)]["colspan"] == 2
    assert js["row_count"] == 3 and js["col_count"] == 2


def test_partial_merged_copy_not_output() -> None:
    """局部范围内被合并覆盖的位置不输出占位格（只输出真实单元格）。"""
    grid = html_to_grid(OFFICIAL)
    # "第2行"（0 基 row 1）：(1,0) 是项目 rowspan 的覆盖位，非锚点不输出
    js = grid_to_structure_json(grid, (1, 1), None)
    cells = {(c["row"], c["col"]): c for c in js["cells"]}
    assert set(cells) == {(1, 1), (1, 2), (1, 3)}
    assert js["row_count"] == 3


def test_grid_html_roundtrip() -> None:
    grid = html_to_grid(MERGED)
    html2 = grid_to_html(grid)
    grid2 = html_to_grid(html2)
    assert [[c.text for c in r] for r in grid] == [[c.text for c in r] for r in grid2]
    # 合并关系保持
    assert grid2[0][0].rowspan == 2
    assert grid2[2][0].colspan == 2
