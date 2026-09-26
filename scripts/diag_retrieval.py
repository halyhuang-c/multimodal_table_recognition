"""诊断 RAG 检索：定位目标表在 cosine 排名 / 过滤 / 重排各阶段的位置。

用法（项目根目录执行）:
    .venv\\Scripts\\python scripts\\diag_retrieval.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.config import get_settings
from table_qa.indexing.vector_store import (  # noqa: E402
    TableVectorStore,
    _expand_query,
    _extract_header_from_chunk,
    _tokenize_chinese,
)
from table_qa.llm_client import get_llm_hub  # noqa: E402

QUERY = "应收账款表，1年以内账款，期末与期初余额是多少？"


def main() -> None:
    s = get_settings()
    store = TableVectorStore(s)
    llm = get_llm_hub()
    total = store._collection.count()
    if total == 0:
        print("向量库为空，请先执行入库/reindex。")
        return

    expanded = _expand_query(QUERY)
    print("=" * 90)
    print("原始 query :", QUERY)
    print("扩展 query :", expanded)
    print("query 分词 :", _tokenize_chinese(QUERY))
    print(f"库内 chunk 总数: {total} | top_k={s.vectorstore.top_k} "
          f"| min_similarity={s.vectorstore.min_similarity}")

    # ---- 阶段0：关键词扫描 + 可疑 chunk 全文（纯本地，零 token）----
    print("=" * 90)
    print("阶段0a 全库关键词扫描（含'1年以内' 或 '期初余额' 的 chunk）:")
    all_res = store._collection.get(include=["documents", "metadatas"])
    kws_all = ("应收账款", "1年以内", "1-2年", "2-3年", "账龄", "期末余额", "期初余额")
    targets: list[tuple[str, dict, str]] = []
    for cid, doc, meta in zip(all_res["ids"], all_res["documents"],
                              all_res["metadatas"]):
        if "1年以内" not in doc and "期初余额" not in doc:
            continue
        kws = [kw for kw in kws_all if kw in doc]
        truncated = "是!" if not doc.rstrip().endswith("</table>") else "否"
        print(f"  {cid} {meta['file_name']} p{meta['page']} {meta['rows']}x{meta['cols']}"
              f" 长度={len(doc)} 截断={truncated} 含={kws}")
        targets.append((cid, dict(meta), doc))

    print("=" * 90)
    print("阶段0b 目标 chunk 全文:")
    for cid, meta, doc in targets:
        print(f"----- {cid} {meta['file_name']} p{meta['page']}"
              f" {meta['rows']}x{meta['cols']} -----")
        print(doc)
        print()

    # ---- 阶段1：原始 cosine 排名（拉全库 top 30，不过滤）----
    vector = llm.embed_query(expanded, question_id="diag")
    k = min(30, total)
    res = store._collection.query(
        query_embeddings=[vector], n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    print("=" * 90)
    print(f"阶段1 全库 cosine 排名（前 {k}，未做任何过滤）:")
    for rank, (doc, meta, dist) in enumerate(
            zip(res["documents"][0], res["metadatas"][0],
                res["distances"][0]), 1):
        sim = 1.0 - float(dist)
        marks = ""
        if "应收账款" in doc:
            marks += " ★应收账款"
        if ("1年以内" in doc) or ("1 年以内" in doc) or ("一年以内" in doc):
            marks += " ★1年以内"
        if "期末余额" in doc:
            marks += " ●期末余额"
        if "期初余额" in doc:
            marks += " ●期初余额"
        dropped = "  <== 会被 min_similarity 过滤!" if sim < s.vectorstore.min_similarity else ""
        print(f"  #{rank:2d} sim={sim:.4f} {meta['file_name']} p{meta['page']}"
              f" 「{meta.get('table_name', '')}」{meta['rows']}x{meta['cols']}"
              f"{marks}{dropped}")

    # ---- 阶段2：正式 retrieve 管线（过滤+重排+截断）----
    print("=" * 90)
    print(f"阶段2 正式 retrieve() 输出（宽召回 {min(s.vectorstore.top_k * 2, total)} 条）:")
    hits = store.retrieve(QUERY, question_id="diag")
    for rank, h in enumerate(hits, 1):
        m = h["metadata"]
        print(f"  #{rank} sim={h['similarity']} bonus={h.get('rerank_bonus', 0)}"
              f" adj={h.get('adjusted_score', '-')} {m['file_name']} p{m['page']}"
              f" 「{m.get('table_name', '')}」")
        print(f"      表头: {' | '.join(_extract_header_from_chunk(h['chunk_text'])[:8])}")
    print("=" * 90)


if __name__ == "__main__":
    main()
