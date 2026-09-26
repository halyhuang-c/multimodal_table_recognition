"""dataframe 与 sandbox 纯逻辑单测。"""

import pytest

from table_qa.answer.sandbox import SandboxViolation, check_code, run_sandbox
from table_qa.tables.dataframe import clean_number, to_halfwidth
from table_qa.tables.html_table import html_to_grid


# ---------------------------------------------------------------------------
# 数值清洗
# ---------------------------------------------------------------------------


def test_clean_number_variants() -> None:
    assert clean_number("1,234.56") == 1234.56
    assert clean_number("(1,234)") == -1234
    assert clean_number("１２３") == 123           # 全角
    assert clean_number("12.30%") == 12.30


def test_clean_number_dash_is_nan() -> None:
    import math
    with pytest.raises(ValueError):
        clean_number("—")


def test_to_halfwidth() -> None:
    assert to_halfwidth("１２３４５") == "12345"
    assert to_halfwidth("（元）") == "(元)"


# ---------------------------------------------------------------------------
# grid → DataFrame
# ---------------------------------------------------------------------------


def test_grid_to_dataframe() -> None:
    from table_qa.tables.dataframe import grid_to_dataframe
    grid = html_to_grid("""
    <table>
      <tr><td>项目</td><td>金额</td></tr>
      <tr><td>收入</td><td>1,000</td></tr>
      <tr><td>成本</td><td>(200)</td></tr>
    </table>""")
    df = grid_to_dataframe(grid)
    assert list(df.columns) == ["项目", "金额"]
    assert df.iloc[0, 0] == "收入"
    assert df.iloc[0, 1] == 1000
    assert df.iloc[1, 1] == -200


def test_all_text_table_not_misread_as_two_level_header() -> None:
    """回归：纯文本数据表（目录/清单类）首条数据不得被吸进表头。

    实测案例：AI 工具清单表（公司/工具/国别/功能全为文字）曾被误判二级表头，
    列名变成 "国别:美国"，首条数据丢失导致"美国产品数"少计 1。
    """
    from table_qa.tables.dataframe import grid_to_dataframe
    grid = html_to_grid("""
    <table>
      <tr><td>公司名称</td><td>工具名称</td><td>国别</td></tr>
      <tr><td>OpenAI</td><td>AgentKit</td><td>美国</td></tr>
      <tr><td>OpenAI</td><td>AgentSDK</td><td>美国</td></tr>
      <tr><td>字节</td><td>扣子</td><td>中国</td></tr>
    </table>""")
    df = grid_to_dataframe(grid)
    assert list(df.columns) == ["公司名称", "工具名称", "国别"]
    assert len(df) == 3                                  # 首条数据未丢失
    assert (df["国别"] == "美国").sum() == 2              # 计数正确


def test_two_level_header_with_colspan_still_flattened() -> None:
    """回归：真二级表头（首行跨列合并）仍应展平为 "父:子" 列名。"""
    from table_qa.tables.dataframe import grid_to_dataframe
    grid = html_to_grid("""
    <table>
      <tr><td colspan="2">收入</td><td colspan="2">成本</td></tr>
      <tr><td>本期</td><td>上期</td><td>本期</td><td>上期</td></tr>
      <tr><td>100</td><td>90</td><td>80</td><td>70</td></tr>
    </table>""")
    df = grid_to_dataframe(grid)
    assert list(df.columns) == ["收入:本期", "收入:上期", "成本:本期", "成本:上期"]
    assert len(df) == 1
    assert df.iloc[0, 0] == 100


def test_duplicate_header_columns_deduped() -> None:
    """回归：重复表头文本（两个"本期"）曾使 df[col] 返回 DataFrame，
    数值清洗时触发 "truth value of a Series is ambiguous"（qid=22 实测）。"""
    from table_qa.tables.dataframe import grid_to_dataframe
    grid = html_to_grid("""
    <table>
      <tr><td>项目</td><td>本期</td><td>本期</td></tr>
      <tr><td>收入</td><td>100</td><td>90</td></tr>
      <tr><td>成本</td><td>50</td><td>40</td></tr>
    </table>""")
    df = grid_to_dataframe(grid)
    assert list(df.columns) == ["项目", "本期", "本期.1"]   # 去重不崩溃
    assert df["本期"].tolist() == [100, 50]                 # 每列都是 Series
    assert df["本期.1"].tolist() == [90, 40]


# ---------------------------------------------------------------------------
# 答案格式化
# ---------------------------------------------------------------------------


def test_format_string_coerces_int() -> None:
    """回归：_format_string 收到 int 曾原样透传，上层 .strip() 崩（qid=47 实测）。"""
    from table_qa.answer.formatter import format_answer
    assert format_answer(18, "string") == "18"
    assert format_answer(3.5, "string") == "3.5"
    assert isinstance(format_answer(0, "string"), str)


# ---------------------------------------------------------------------------
# 沙箱安全
# ---------------------------------------------------------------------------


def test_sandbox_rejects_import() -> None:
    with pytest.raises(SandboxViolation):
        check_code("import os")


def test_sandbox_rejects_open() -> None:
    with pytest.raises(SandboxViolation):
        check_code("result = open('x.txt').read()")


def test_sandbox_rejects_dunder() -> None:
    with pytest.raises(SandboxViolation):
        check_code("result = df.__class__")


def test_sandbox_allows_pandas() -> None:
    check_code("result = df['金额'].sum()")   # 不抛异常


def test_sandbox_runs_sum() -> None:
    import pandas as pd
    df = pd.DataFrame({"x": [1, 2, 3]})
    out = run_sandbox("result = df['x'].sum()", df)
    assert out == 6


def test_sandbox_expression_wrapped() -> None:
    import pandas as pd
    df = pd.DataFrame({"x": [10, 20]})
    out = run_sandbox("df['x'].sum()", df)
    assert out == 30
