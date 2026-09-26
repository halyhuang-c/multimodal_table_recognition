"""postcheck：答题质量报告生成器。

用法（项目根目录执行）:
    .venv\\Scripts\\python postcheck\\gen_quality_report.py

数据源:
    output/submission.xlsx     提交口径答案（旧名 result.xlsx 自动兼容）
    output/result_debug.xlsx   置信度/得分/事件链
    configs/config.yaml 定位的 tests.xlsx（题目元信息）

输出:
    postcheck/quality_report.html（自包含单文件，离线可看，覆盖更新）
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

from table_qa.config import get_settings
from table_qa.ingest.questions import load_questions

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output"
REPORT = Path(__file__).resolve().parent / "quality_report.html"

# 判定口径
BAD = {"", "none", "null", "nan"}                       # 硬无效：空 / None / null / NaN
COP_OUT = re.compile(r"数据未提供|数据缺失|无法计算|未提供|无法确定|无法判断")  # 疑似：文本性拒答
QTYPE_ORDER = ["structure", "extract", "thinking"]
QT_CN = {"structure": "结构还原", "extract": "信息抽取", "thinking": "思考计算"}


def _load_evidence(qid: str) -> str:
    """从答题轨迹提取核实依据：选中表 → 定位行列 → 理由（逐步核实用）。"""
    settings = get_settings()
    p = (settings.paths.abs_path(settings.paths.runs_dir) / "work" / "answers"
         / f"{qid}.json")
    if not p.exists():
        return ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    parts: list[str] = []
    for s in d.get("steps", []):
        step, data = s.get("step"), s.get("data") or {}
        if step == "select":
            parts.append(f"选表: {data.get('selected', '-')} "
                         f"p{data.get('page', '?')} {data.get('rows', '?')}x{data.get('cols', '?')}")
        elif step == "answer":
            loc = data.get("locate") or {}
            if loc:
                loc_s = " × ".join(str(v) for v in loc.values() if v)
                parts.append(f"定位: {loc_s[:80]}")
            if data.get("reason"):
                parts.append(f"理由: {str(data['reason'])[:100]}")
            if data.get("exec_ok") is not None and "code" in data:
                code = str(data.get("code", ""))
                if code.lstrip().startswith('{"op"'):
                    parts.append("op执行" if data["exec_ok"] else "op失败降级")
                else:
                    parts.append("沙箱执行" if data["exec_ok"] else "直算降级")
    return " ｜ ".join(parts)


def _load_rows() -> list[dict]:
    settings = get_settings()
    qs, _ = load_questions(settings)
    qmap = {q.id: q for q in qs}

    res_path = OUT_DIR / "submission.xlsx"
    if not res_path.exists():
        res_path = OUT_DIR / "result.xlsx"   # 旧文件名兼容
    res = pd.read_excel(res_path, keep_default_na=False)
    dbg = pd.read_excel(OUT_DIR / "result_debug.xlsx", keep_default_na=False)
    res["key"] = res["id"].map(lambda i: str(int(i)))
    dbg["key"] = dbg["id"].map(lambda i: str(int(i)))
    dmap = dbg.set_index("key")

    rows: list[dict] = []
    for r in res.itertuples():
        q = qmap.get(r.key)
        d = dmap.loc[r.key] if r.key in dmap.index else None
        ans = str(r.answer).strip()
        invalid = ans.lower() in BAD
        suspect = False
        reason = ""
        if not invalid:
            if COP_OUT.search(ans):
                suspect, reason = True, "文本性拒答"
            elif ans[:1] in "[{":
                try:  # JSON 全空判定：[null] / [] / {} 等
                    parsed = json.loads(ans)
                    if parsed is None or parsed == [] or parsed == {} or (
                        isinstance(parsed, list) and all(x is None for x in parsed)
                    ):
                        suspect, reason = True, "JSON 全空"
                except (json.JSONDecodeError, ValueError):
                    pass
        try:
            score = float(d["score"]) if d is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        rows.append(dict(
            qid=r.key,
            file=q.file_name if q else "?",
            qtype=q.question_type if q else "?",
            question=q.question if q else "",
            answer=ans,
            conf=str(d["confidence"]) if d is not None else "-",
            score=score,
            events=str(d["events"]) if d is not None else "",
            invalid=invalid,
            suspect=suspect,
            suspect_reason=reason,
            evidence=_load_evidence(r.key),
        ))
    return rows


def _bar(pct: float, color: str) -> str:
    return (f'<div class="bar"><div class="fill" style="width:{min(pct, 100):.1f}%;'
            f'background:{color}"></div></div>')


def build_html(rows: list[dict]) -> str:
    total = len(rows)
    invalid_rows = [r for r in rows if r["invalid"]]
    suspect_rows = [r for r in rows if r["suspect"]]
    valid_rows = [r for r in rows if not r["invalid"] and not r["suspect"]]
    rate = 100 * len(valid_rows) / total if total else 0

    # ---- 题型分布 ----
    by_type: dict[str, Counter] = {t: Counter() for t in QTYPE_ORDER}
    for r in rows:
        by_type.setdefault(r["qtype"], Counter())[("invalid" if r["invalid"] else
                                                  "suspect" if r["suspect"] else "ok")] += 1
    type_html = ""
    for t in QTYPE_ORDER:
        c = by_type.get(t, Counter())
        n = sum(c.values()) or 1
        type_html += (
            f'<tr><td class="mono">{t}</td><td>{QT_CN.get(t, "")}</td><td class="num">{n}</td>'
            f'<td class="num ok">{c["ok"]}</td><td class="num warn">{c["suspect"]}</td>'
            f'<td class="num bad">{c["invalid"]}</td>'
            f'<td>{_bar(100 * c["ok"] / n, "#16a34a")}</td></tr>'
        )

    # ---- 无效根因 ----
    causes = Counter()
    for r in invalid_rows:
        cs = [c for c in r["events"].split(";") if c] or ["unknown"]
        for c in cs:
            causes[c] += 1
    max_cause = max(causes.values(), default=1)
    cause_html = "".join(
        f'<tr><td class="mono">{html.escape(c)}</td><td class="num">{n}</td>'
        f'<td>{_bar(100 * n / max_cause, "#dc2626")}</td></tr>'
        for c, n in causes.most_common()
    )

    # ---- 无效按文件 ----
    file_inv = Counter(r["file"] for r in invalid_rows)
    file_all = Counter(r["file"] for r in rows)
    max_fi = max(file_inv.values(), default=1)
    file_html = "".join(
        f'<tr><td class="mono">{html.escape(f)}</td><td class="num">{file_all[f]}</td>'
        f'<td class="num bad">{n}</td>'
        f'<td>{_bar(100 * n / max_fi, "#dc2626")}</td></tr>'
        for f, n in file_inv.most_common()
    )

    # ---- 置信度分布（有效答案）----
    confs = Counter(r["conf"] for r in valid_rows)
    conf_total = sum(confs.values()) or 1
    conf_html = "".join(
        f'<tr><td class="mono">{html.escape(k)}</td><td class="num">{v}</td>'
        f'<td>{_bar(100 * v / conf_total, "#2563eb")}</td></tr>'
        for k, v in confs.most_common()
    )

    # ---- 全量文件表 ----
    fstat: dict[str, dict] = {}
    for r in rows:
        s = fstat.setdefault(r["file"], dict(n=0, inv=[], sus=[], scores=[]))
        s["n"] += 1
        if r["invalid"]:
            s["inv"].append(r["qid"])
        elif r["suspect"]:
            s["sus"].append(r["qid"])
        else:
            s["scores"].append(r["score"])
    file_rows = ""
    for f, s in sorted(fstat.items(), key=lambda kv: (-len(kv[1]["inv"]), -len(kv[1]["sus"]), kv[0])):
        ok = s["n"] - len(s["inv"]) - len(s["sus"])
        avg = sum(s["scores"]) / len(s["scores"]) if s["scores"] else 0.0
        file_rows += (
            f'<tr><td class="mono">{html.escape(f)}</td><td class="num">{s["n"]}</td>'
            f'<td class="num">{ok}</td><td class="num warn">{len(s["sus"])}</td>'
            f'<td class="num bad">{len(s["inv"])}</td>'
            f'<td class="num">{100 * ok / s["n"]:.0f}%</td>'
            f'<td class="num">{avg:.2f}</td>'
            f'<td class="mono small">{" ".join(s["inv"] + s["sus"])}</td></tr>'
        )

    # ---- 无效明细（可筛选）----
    all_causes = sorted(causes)
    detail = ""
    for r in sorted(invalid_rows, key=lambda x: int(x["qid"])):
        cs = ";".join(c for c in r["events"].split(";") if c) or "unknown"
        detail += (
            f'<tr class="row" data-qtype="{r["qtype"]}" data-cause="{html.escape(cs)}">'
            f'<td class="mono">{r["qid"]}</td><td class="mono">{html.escape(r["file"])}</td>'
            f'<td class="mono">{r["qtype"]}</td>'
            f'<td class="q">{html.escape(r["question"][:80])}</td>'
            f'<td class="mono small">{html.escape(cs)}</td>'
            f'<td class="mono small">{html.escape(r["answer"][:40])}</td></tr>'
        )

    # ---- 疑似空答明细 ----
    suspect_html = ""
    for r in sorted(suspect_rows, key=lambda x: int(x["qid"])):
        suspect_html += (
            f'<tr><td class="mono">{r["qid"]}</td><td class="mono">{html.escape(r["file"])}</td>'
            f'<td class="mono">{r["qtype"]}</td><td class="mono small">{r["suspect_reason"]}</td>'
            f'<td class="q">{html.escape(r["question"][:70])}</td>'
            f'<td class="mono small">{html.escape(r["answer"][:50])}</td></tr>'
        )
    suspect_section = ""
    if suspect_rows:
        suspect_section = f"""
<section id="suspect"><h2>疑似空答（{len(suspect_rows)} 题）</h2>
<p class="muted">口径：非空答案，但内容为"数据未提供/无法计算"类文本性拒答，或 JSON 全空（如 <code>[null]</code>）。竞赛评分大概率按错误计，建议关注。</p>
<table><thead><tr><th>qid</th><th>文件</th><th>题型</th><th>原因</th><th>问题</th><th>答案</th></tr></thead>
<tbody>{suspect_html}</tbody></table></section>"""

    # ---- 全量答案核实表（逐步核对：依据=选表/定位/理由/执行方式）----
    status_of = lambda r: ("无效" if r["invalid"] else "疑似" if r["suspect"] else "有效")
    verify_html = ""
    for r in sorted(rows, key=lambda x: int(x["qid"])):
        st = status_of(r)
        st_cls = "bad" if st == "无效" else "warn" if st == "疑似" else "ok"
        verify_html += (
            f'<tr class="vrow" data-st="{st}" data-qtype="{r["qtype"]}" '
            f'data-kw="{html.escape(r["qid"] + " " + r["question"] + " " + r["file"]).lower()}">'
            f'<td class="mono">{r["qid"]}</td><td class="{st_cls}"><b>{st}</b></td>'
            f'<td class="mono small">{html.escape(r["file"])}</td>'
            f'<td class="mono small">{r["qtype"]}</td>'
            f'<td class="q small">{html.escape(r["question"][:60])}</td>'
            f'<td class="mono small">{html.escape(r["answer"][:60])}</td>'
            f'<td class="mono small">{html.escape(r["conf"])}</td>'
            f'<td class="small ev">{html.escape(r["evidence"][:220])}</td></tr>'
        )

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    avg_score = sum(r["score"] for r in valid_rows) / len(valid_rows) if valid_rows else 0.0

    return _TPL.format(**locals()) if False else _render(ts, total, len(valid_rows),
        len(invalid_rows), len(suspect_rows), rate, avg_score, type_html, cause_html,
        file_html, conf_html, file_rows, detail, suspect_section, all_causes, verify_html)


def _render(ts, total, ok_n, inv_n, sus_n, rate, avg_score, type_html, cause_html,
            file_html, conf_html, file_rows, detail, suspect_section, all_causes,
            verify_html) -> str:
    cause_btns = "".join(
        f'<button class="fbtn" onclick="flt(\'cause\',\'{c}\',this)">{c}</button>'
        for c in all_causes)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>答题质量报告 · postcheck</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;margin:0;background:#f1f5f9;color:#0f172a;line-height:1.6}}
.wrap{{max-width:1080px;margin:0 auto;padding:24px 20px 60px}}
h1{{font-size:22px;margin:8px 0 4px}} h2{{font-size:17px;margin:28px 0 10px;border-left:4px solid #2563eb;padding-left:8px}}
.muted{{color:#64748b;font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px}}
.card .v{{font-size:26px;font-weight:700}} .card .k{{font-size:12px;color:#64748b}}
.ok{{color:#16a34a}} .bad{{color:#dc2626}} .warn{{color:#d97706}}
table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px;margin-top:8px}}
th,td{{border:1px solid #e2e8f0;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f8fafc;position:sticky;top:0}}
.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.mono{{font-family:Consolas,monospace;white-space:nowrap}} .small{{font-size:12px}} .q{{min-width:200px}}
.ev{{min-width:280px;white-space:normal;word-break:break-all;color:#475569}}
.bar{{background:#e2e8f0;border-radius:4px;height:10px;min-width:120px;overflow:hidden}}
.fill{{height:100%;border-radius:4px}}
.fbtn{{border:1px solid #cbd5e1;background:#fff;border-radius:6px;padding:3px 10px;margin:0 6px 6px 0;cursor:pointer;font-size:12px}}
.fbtn.on{{background:#2563eb;color:#fff;border-color:#2563eb}}
.scroll{{max-height:520px;overflow:auto;border:1px solid #e2e8f0;border-radius:8px;margin-top:8px}}
.scroll table{{margin:0;border:none}} .scroll td,.scroll th{{border-bottom:1px solid #e2e8f0;border-right:none}}
footer{{margin-top:36px;color:#94a3b8;font-size:12px}}
</style></head><body><div class="wrap">
<h1>多模态表格问答 · 答题质量报告</h1>
<p class="muted">生成时间 {ts} ｜ 数据源 output/submission.xlsx + result_debug.xlsx ｜ 判定口径见页脚</p>
<div class="cards">
<div class="card"><div class="v">{total}</div><div class="k">总题数</div></div>
<div class="card"><div class="v ok">{ok_n}</div><div class="k">有效答案</div></div>
<div class="card"><div class="v bad">{inv_n}</div><div class="k">无效答案</div></div>
<div class="card"><div class="v warn">{sus_n}</div><div class="k">疑似空答</div></div>
<div class="card"><div class="v">{rate:.1f}%</div><div class="k">有效率</div></div>
<div class="card"><div class="v">{avg_score:.2f}</div><div class="k">有效题平均得分</div></div>
</div>

<section id="type"><h2>按题型分布</h2>
<table><thead><tr><th>题型</th><th>中文名</th><th>题数</th><th>有效</th><th>疑似</th><th>无效</th><th>有效率</th></tr></thead>
<tbody>{type_html}</tbody></table></section>

<section id="cause"><h2>无效根因分布（{inv_n} 题，含多因叠加）</h2>
<table><thead><tr><th>根因事件</th><th>出现次数</th><th>占比条</th></tr></thead>
<tbody>{cause_html}</tbody></table></section>

<section id="file"><h2>无效答案按文件</h2>
<table><thead><tr><th>文件</th><th>总题数</th><th>无效</th><th>分布</th></tr></thead>
<tbody>{file_html}</tbody></table></section>

<section id="conf"><h2>有效答案置信度分布</h2>
<table><thead><tr><th>置信度</th><th>题数</th><th>占比</th></tr></thead>
<tbody>{conf_html}</tbody></table></section>

<section id="allfile"><h2>全量文件明细（{len(file_rows.split('<tr>')) - 1} 个文件）</h2>
<div class="scroll"><table><thead><tr><th>文件</th><th>题数</th><th>有效</th><th>疑似</th><th>无效</th><th>有效率</th><th>均分</th><th>问题 qid</th></tr></thead>
<tbody>{file_rows}</tbody></table></div></section>

<section id="detail"><h2>无效答案明细（可筛选）</h2>
<div>
<span class="muted">题型：</span><button class="fbtn on" onclick="flt('qtype','all',this)">全部</button>
<button class="fbtn" onclick="flt('qtype','structure',this)">structure</button>
<button class="fbtn" onclick="flt('qtype','extract',this)">extract</button>
<button class="fbtn" onclick="flt('qtype','thinking',this)">thinking</button>
<br><span class="muted">根因：</span><button class="fbtn on" onclick="flt('cause','all',this)">全部</button>{cause_btns}
</div>
<div class="scroll"><table><thead><tr><th>qid</th><th>文件</th><th>题型</th><th>问题</th><th>根因</th><th>原始答案</th></tr></thead>
<tbody>{detail}</tbody></table></div></section>

{suspect_section}

<section id="verify"><h2>全量答案核实（{total} 题，逐步核对用）</h2>
<p class="muted">依据列 = 选表（页码/行列）→ 定位（行×列表头）→ 理由 → 执行方式（沙箱/直算），来自答题轨迹。核对方法：对照依据列打开源文件核数。</p>
<div>
<span class="muted">状态：</span><button class="fbtn on" onclick="vflt('st','all',this)">全部</button>
<button class="fbtn" onclick="vflt('st','有效',this)">有效</button>
<button class="fbtn" onclick="vflt('st','疑似',this)">疑似</button>
<button class="fbtn" onclick="vflt('st','无效',this)">无效</button>
<br><span class="muted">题型：</span><button class="fbtn on" onclick="vflt('qtype','all',this)">全部</button>
<button class="fbtn" onclick="vflt('qtype','structure',this)">structure</button>
<button class="fbtn" onclick="vflt('qtype','extract',this)">extract</button>
<button class="fbtn" onclick="vflt('qtype','thinking',this)">thinking</button>
<input id="vsearch" placeholder="搜索 qid/问题/文件…" style="border:1px solid #cbd5e1;border-radius:6px;padding:3px 10px;margin-left:8px;font-size:12px" oninput="vflt()">
</div>
<div class="scroll"><table><thead><tr><th>qid</th><th>状态</th><th>文件</th><th>题型</th><th>问题</th><th>答案</th><th>置信</th><th>答题依据（逐步核对）</th></tr></thead>
<tbody>{verify_html}</tbody></table></div></section>

<footer>
判定口径：<b>无效</b> = 答案为空 / None / null / NaN；<b>疑似空答</b> = 含"数据未提供/无法计算"类文本或 JSON 全空；其余计为<b>有效</b>。<br>
根因事件来自 result_debug.xlsx 的 events 字段（table_select_low=选表相似度低 / no_table=无可用表 / rag_miss=检索未命中 / extract_unverified=抽取未验证 / mask_in_table=答案被脱敏 / sandbox_fail_direct=沙箱执行失败）。<br>
重新生成：项目根目录执行 <code>.venv\\Scripts\\python postcheck\\gen_quality_report.py</code>
</footer>
<script>
function flt(kind, val, btn) {{
  document.querySelectorAll('.fbtn').forEach(function(b) {{
    if (b.getAttribute('onclick').indexOf(kind + ',') > -1) b.classList.remove('on');
  }});
  btn.classList.add('on');
  document.querySelectorAll('#detail tr.row').forEach(function(tr) {{
    var ok = val === 'all' || tr.dataset[kind].indexOf(val) > -1;
    tr.style.display = ok ? '' : 'none';
  }});
}}
var _vf = {{st: 'all', qtype: 'all'}};
function vflt(kind, val, btn) {{
  if (kind) {{
    document.querySelectorAll('#verify .fbtn').forEach(function(b) {{
      if (b.getAttribute('onclick').indexOf(kind + ',') > -1) b.classList.remove('on');
    }});
    btn.classList.add('on');
    _vf[kind] = val;
  }}
  var kw = (document.getElementById('vsearch').value || '').toLowerCase();
  document.querySelectorAll('#verify tr.vrow').forEach(function(tr) {{
    var ok = (_vf.st === 'all' || tr.dataset.st === _vf.st)
        && (_vf.qtype === 'all' || tr.dataset.qtype === _vf.qtype)
        && (!kw || tr.dataset.kw.indexOf(kw) > -1);
    tr.style.display = ok ? '' : 'none';
  }});
}}
</script>
</div></body></html>"""


def main() -> None:
    rows = _load_rows()
    REPORT.write_text(build_html(rows), encoding="utf-8")
    inv = sum(1 for r in rows if r["invalid"])
    sus = sum(1 for r in rows if r["suspect"])
    print(f"已生成: {REPORT}")
    print(f"统计: 总数 {len(rows)} | 无效 {inv} | 疑似空答 {sus} | 有效 {len(rows) - inv - sus}")


if __name__ == "__main__":
    main()
