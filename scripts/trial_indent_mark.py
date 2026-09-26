"""VLM 层级标记遵从率试验：验证识别时输出缩进层级的可行性。

试验对象：
- 002.pdf p2（资产负债表续）：'其中：优先股'（应付债券子项）
- 003.pdf p1（利润表）：'其中：利息费用'/'利息收入'（财务费用子项）
每图跑 3 次，统计：
- 召回：二级行被标 lv2 的比例
- 误标：一级行被误标 lv2 的比例
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, "src")

from table_qa.config import get_settings
from table_qa.llm_client import get_llm_hub
from table_qa.logging_setup import setup_logging

PROMPT = """你是表格识别专家。请识别图片中的表格，输出规范 HTML：
- 仅输出 <table>...</table>；合并单元格用 rowspan/colspan 表达；
- 单元格文本原样保留，数字逐位核对，无法辨认输出 [MASK]；
- **层级标记**：表格中行首有缩进（比上一行更靠右）的行，说明它是上一主科目的
  子项/细分——请在该行第一个 <td> 上加 class 标记：缩进一层 class="lv2"，
  再深一层 class="lv3"；无缩进的行不加 class（默认一级）。
  示例：<tr><td>财务费用</td>...</tr><tr><td class="lv2">其中：利息费用</td>...</tr>"""

CASES = [
    ("002.pdf_p2", Path("cache/pages/eb7119786bb6_p2.png"),
     {"lv2_expect": ["其中：优先股", "永续债"],
      "lv1_expect": ["租赁负债", "长期借款", "递延收益"]}),
    ("003.pdf_p1", Path("cache/pages/24943fdb15f1_p1.png"),
     {"lv2_expect": ["其中：利息费用", "利息收入", "其中：对联营"],
      "lv1_expect": ["减：营业成本", "税金及附加", "销售费用", "加：其他收益"]}),
]
RUNS = 3


def main() -> None:
    setup_logging()
    llm = get_llm_hub()
    stats = {}
    for name, img, spec in CASES:
        hit2, tot2, miss1, tot1 = 0, 0, 0, 0
        for run in range(1, RUNS + 1):
            raw = llm.vision(img, PROMPT, phase="indent_trial", question_id=None)
            lv2_cells = re.findall(
                r'<td[^>]*class="lv2"[^>]*>([^<]+)</td>', raw)
            lv2_texts = [c.strip() for c in lv2_cells]
            for e in spec["lv2_expect"]:
                tot2 += 1
                if any(e in t for t in lv2_texts):
                    hit2 += 1
            for e in spec["lv1_expect"]:
                tot1 += 1
                if any(e in t for t in lv2_texts):
                    miss1 += 1
            print(f"[{name} run{run}] lv2标记数={len(lv2_texts)} "
                  f"样本={lv2_texts[:6]}")
        stats[name] = (hit2, tot2, miss1, tot1)

    print("\n===== 汇总 =====")
    for name, (h2, t2, m1, t1) in stats.items():
        print(f"{name}: 二级召回 {h2}/{t2} ({100*h2/max(t2,1):.0f}%)  "
              f"一级误标 {m1}/{t1} ({100*m1/max(t1,1):.0f}%)")


if __name__ == "__main__":
    main()
