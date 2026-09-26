"""ops 解释器本地单测（不调 LLM）：用 002/006 真实表验证 6 个 op。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.answer.ops import execute_op
from table_qa.cache import TableStore
from table_qa.config import get_settings
from table_qa.tables.dataframe import grid_to_dataframe

s = get_settings()
store = TableStore(s)

# 002.pdf（file_id eb7119786bb6）：合并资产负债表（续）45x4
t002 = [t for t in store.load("eb7119786bb6") if t.n_rows == 45][0]
df2 = grid_to_dataframe(t002.grid)

# 003.pdf：合并利润表 57x4（file_id 24943fdb15f1）
t003 = store.load("24943fdb15f1")[0]
df3 = grid_to_dataframe(t003.grid)

# 006.pdf：账龄表 10x4（file_id ceb650b448e6）
t006 = [t for t in store.load("ceb650b448e6") if t.n_rows == 10 and t.n_cols == 4][0]
df6 = grid_to_dataframe(t006.grid)

cases = [
    ("cell 信用减值损失本期", df3,
     {"op": "cell", "row": "信用减值损失", "col": "本期金额"}, -5323072.70),
    ("cells 信用减值损失本期+上期", df3,
     {"op": "cells", "refs": [
         {"row": "信用减值损失", "col": "本期金额"},
         {"row": "信用减值损失", "col": "上期金额"}]},
     [-5323072.70, -9220946.25]),
    ("diff Q15 权益-母公司", df2,
     {"op": "diff",
      "a": {"row": "股东权益合计", "col": "期末余额"},
      "b": {"row": "归属于母公司股东权益合计", "col": "期末余额"}}, 135088205.15),
    ("subitems Q14 应付债券", df2,
     {"op": "subitems", "item": "应付债券"}, ["优先股", "永续债"]),
    ("subitems Q19 营业总收入区块（子项折叠）", df3,
     {"op": "subitems", "item": "一、营业总收入"},
     ["减：营业成本", "税金及附加", "销售费用", "管理费用", "研发费用", "财务费用",
      "加：其他收益", "投资收益（损失以“-”号填列）",
      "以摊余成本计量的金融资产终止确认收益", "净敞口套期收益（损失以“-”号填列）",
      "公允价值变动收益（损失以“-”号填列）", "信用减值损失（损失以“-”号填列）",
      "资产减值损失（损失以“-”号填列）", "资产处置收益（损失以“-”号填列）"]),
    ("subitems 财务费用子项区（tab丢失层级恢复）", df3,
     {"op": "subitems", "item": "财务费用"},
     ["利息费用", "利息收入"]),
    ("subitems 二、营业利润区块", df3,
     {"op": "subitems", "item": "二、营业利润"},
     ["加：营业外收入", "减：营业外支出"]),
    ("subitems 冒号分组 非流动负债（资产负债表，子项折叠）", df2,
     {"op": "subitems", "item": "非流动负债："},
     ["长期借款", "应付债券", "租赁负债", "长期应付款",
      "长期应付职工薪酬", "预计负债", "递延收益", "递延所得税负债", "其他非流动负债"]),
    ("subitems 括号序号区块（一）不能重分类", df3,
     {"op": "subitems", "item": "（一）不能重分类进损益的其他综合收益"},
     ["1．重新计量设定受益计划变动额", "2．权益法下不能转损益的其他综合收益",
      "3．其他权益工具投资公允价值变动", "4．企业自身信用风险公允价值变动", "5．其他"]),
    ("cells Q26 1年以内期末+期初", df6,
     {"op": "cells", "refs": [
         {"row": "1年以内", "col": "期末余额"},
         {"row": "1年以内", "col": "期初余额"}]},
     [160112139.83, 180032578.58]),
    ("const 常量", df3, {"op": "const", "value": 1}, 1),
    ("seq 复合（Q148 形态）", df3,
     {"op": "seq", "ops": [
         {"op": "const", "value": "[MASK]"},
         {"op": "const", "value": 1},
         {"op": "const", "value": "是"},
         {"op": "cell", "row": "信用减值损失", "col": "本期金额"}]},
     ["[MASK]", 1, "是", -5323072.7]),
    ("count 含其中：的行数", df3,
     {"op": "count", "contains": "其中："}, 3),
    ("sum 账龄小计核对(1年以内+5年以上期末)", df6,
     {"op": "sum", "col": "期末余额", "rows": ["1年以内", "5年以上"]},
     round(160112139.83 + 13470966.56, 2)),
]

fails = 0
for name, df, op, expect in cases:
    try:
        got = execute_op(op, df)
        ok = got == expect
        if not ok:
            fails += 1
        print(f"{'PASS' if ok else 'FAIL'} {name}: got={got} expect={expect}")
    except Exception as e:  # noqa: BLE001
        fails += 1
        print(f"ERROR {name}: {type(e).__name__}: {e}")
print(f"\n{'全部通过' if fails == 0 else f'{fails} 个失败'}")
