"""html_table 纯逻辑单测：rowspan/colspan 展开、切片截断、结构 JSON。"""

from table_qa.tables.html_table import (
    grid_to_html,
    grid_to_structure_json,
    html_to_grid,
    slice_grid,
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


def test_slice_truncates_span() -> None:
    grid = html_to_grid(MERGED)
    # 前 2 行 × 前 1 列：A 跨度完整保留
    sliced = slice_grid(grid, (0, 1), (0, 0))
    assert len(sliced) == 2
    a = sliced[0][0]
    assert a.text == "A" and a.rowspan == 2
    # 只取第 1 行（(0,0)）：A 的 rowspan 截断为 1
    row_only = slice_grid(grid, (0, 0), None)
    a2 = row_only[0][0]
    assert a2.text == "A" and a2.rowspan == 1
    # 只取第 1 列：B 的 colspan 截断为 1
    col_only = slice_grid(grid, None, (0, 0))
    b2 = col_only[2][0]
    assert b2.text == "B" and b2.colspan == 1


def test_grid_html_roundtrip() -> None:
    grid = html_to_grid(MERGED)
    html2 = grid_to_html(grid)
    grid2 = html_to_grid(html2)
    assert [[c.text for c in r] for r in grid] == [[c.text for c in r] for r in grid2]
    # 合并关系保持
    assert grid2[0][0].rowspan == 2
    assert grid2[2][0].colspan == 2
