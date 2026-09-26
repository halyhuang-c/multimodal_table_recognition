"""RAG 向量库：ChromaDB 本地持久 + 业务注释生成 + 检索。

设计（v0.6）：一个 NormalizedTable = 一个 chunk。
chunk 文本 = 元数据摘要 + 业务注释 + 表格 HTML；
元数据结构化过滤（file_name）+ 语义检索（question + table_hint）。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from loguru import logger
from table_qa.config import Settings, get_settings
from table_qa.llm_client import LLMError, get_llm_hub
from table_qa.prompts import get_prompt_manager
from table_qa.schema import NormalizedTable

_COLLECTION = "tables"

# 中文停用词（疑问词/虚词）——query 扩展时去掉以减少噪音
_STOPWORDS = set(
    "的 了 在 是 和 与 或 对 就 都 还 也 什么 多少 哪些 哪个 如何 怎么 为 让 给 从 到 把"
    " 请问 请 一下 分别 各 及 等 以上 以下 合计 总额 总值 总".split()
)
# 表头采样在 chunk 里重复进入 embedding 输入的次数——放大列名关键词权重
_HEADER_WEIGHT = 3
_JPUNCT = re.compile(r"[，。？；：、,\.?:;！!（）()【】\[\]\s]+")


def _tokenize_chinese(text: str) -> list[str]:
    """简单中文分词：按标点/空白切分 + 长串滑窗切 n-gram。无 jieba 依赖。"""
    parts = [p.strip() for p in _JPUNCT.split(text) if p.strip()]
    tokens: list[str] = []
    for p in parts:
        if len(p) <= 4:
            tokens.append(p)
        else:
            tokens.append(p)
            for n in (2, 3):
                for i in range(len(p) - n + 1):
                    sub = p[i:i + n]
                    if sub not in _STOPWORDS:
                        tokens.append(sub)
    return [t for t in tokens if t not in _STOPWORDS and t.strip()]


def _expand_query(query: str) -> str:
    """方案 B：零额度 query 扩展——原始题干 + 去重关键词集合拼接后送 embedding。
    原始题干保留语义框架，去噪关键词提供名词锚点，规则分词零 token 消耗。"""
    seen: set[str] = set()
    deduped: list[str] = []
    for w in _tokenize_chinese(query):
        if w not in seen:
            seen.add(w)
            deduped.append(w)
    ext = " ".join(deduped)
    return f"{query} {ext}" if ext else query


def _extract_header_from_chunk(chunk_text: str) -> list[str]:
    """从 chunk 文本中提取'表头: xxx'采样行（重排锚点）。"""
    for line in chunk_text.split("\n"):
        if line.startswith("表头: "):
            return [c.strip() for c in line[4:].split("|") if c.strip()]
    return []


def _rerank_hits(hits: list[dict], query: str, bonus_per_hit: float = 0.05,
                 header_weight: float = 1.0, annotation_weight: float = 0.5) -> list[dict]:
    """方案 C：关键词 overlap 重排——query 关键词与表头/业务注释分别计分，
    按 similarity + bonus 重排。bonus 0.05 为经验值，可调。

    overlap 判定为子串包含（非全等）：query 侧是 2/3-gram 滑窗 token，
    header 侧是完整列名（如"期末余额"），全等求交恒为空——包含判定才能
    让"期末"命中"期末余额"列。"""
    q_tokens = set(_tokenize_chinese(query))
    if not q_tokens:
        return hits

    def overlap(tokens: set[str], targets: set[str]) -> int:
        n = 0
        for t in tokens:
            for h in targets:
                if t in h or h in t:
                    n += 1
                    break
        return n

    for hit in hits:
        chunk = hit.get("chunk_text", "")
        headers = set(_extract_header_from_chunk(chunk))
        ann_line = ""
        for line in chunk.split("\n"):
            if line.startswith("业务: "):
                ann_line = line[4:]
                break
        ann_tokens = set(_tokenize_chinese(ann_line)) if ann_line else set()
        bonus = bonus_per_hit * (
            overlap(q_tokens, headers) * header_weight
            + overlap(q_tokens, ann_tokens) * annotation_weight
        )
        hit["rerank_bonus"] = round(bonus, 4)
        hit["adjusted_score"] = round(hit["similarity"] + bonus, 4)
    hits.sort(key=lambda h: h.get("adjusted_score", h["similarity"]), reverse=True)
    return hits


def _chunk_text(table: NormalizedTable) -> str:
    """拼接 chunk 文本（含方案 E）：
    - 业务注释：优先 LLM annotate()，失败时从表头拼'含列: xxx/yyy/zzz'兜底
    - 表头采样重复 _HEADER_WEIGHT 次进入 embedding 输入，抬升列名关键词权重
    - HTML 截断到 6000 字符（embedding 输入限制）
    """
    header_cells = [c.text for c in table.grid[0]][:12] if table.grid else []
    meta = [
        f"文件: {table.file_name}",
        f"表名: {table.table_name or '-'}",
        f"页码: {table.page}",
        f"行列: {table.n_rows}x{table.n_cols}",
        f"单位: {table.unit_note or '-'}",
    ]
    annotation = table.business_annotation or ""
    if not annotation and header_cells:    # 方案 E：注释失败 → 表头兜底
        annotation = "含列: " + " / ".join(header_cells[:5])
    if annotation:
        meta.append(f"业务: {annotation}")
    if header_cells:                       # 表头采样 × 权重
        header_str = " | ".join(h.replace("\n", " ") for h in header_cells)
        for _ in range(_HEADER_WEIGHT):
            meta.append("表头: " + header_str)
    html = table.html[:6000]
    return "\n".join(meta) + "\n" + html


class TableVectorStore:
    """表格向量库（进程内单例语义：每个进程一个实例即可）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        import chromadb
        import threading

        self._s = settings or get_settings()
        persist_dir = self._s.paths.abs_path(self._s.paths.vector_dir)
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION, metadata={"hnsw:space": "cosine"},
        )
        self._write_lock = threading.Lock()   # 并行识别时串行化 collection 写入
        self._llm = get_llm_hub()
        self._pm = get_prompt_manager()

    # ------------------------------------------------------------------
    # 注释与入库
    # ------------------------------------------------------------------

    def annotate(self, table: NormalizedTable) -> str:
        """LLM 生成一句话业务注释（失败返回空串，不阻塞入库）。"""
        html_head = table.html[:2500]
        try:
            prompt = self._pm.render("annotate", html_head=html_head)
            return self._llm.chat(prompt, phase="annotate").strip()
        except LLMError as e:
            logger.warning("注释生成失败（不影响入库）: {}", e)
            return ""

    def upsert(self, tables: list[NormalizedTable], annotate: bool = True) -> int:
        """批量入库（幂等：id = file_id:page:idx）。返回成功条数。

        annotate=False：跳过 LLM 注释生成（chunk 由 _chunk_text 表头兜底），
        用于额度紧张或注释模型慢/挂死的场景。"""
        if not tables:
            return 0
        for t in tables:
            if not t.business_annotation and annotate:
                t.business_annotation = self.annotate(t) or None
        texts = [_chunk_text(t) for t in tables]
        try:
            vectors = self._llm.embed(texts, phase="index")
        except LLMError as e:
            logger.error("向量化失败，跳过 {} 张表: {}", len(tables), e)
            return 0
        ids = [f"{t.file_id}:p{t.page}:{i}" for i, t in enumerate(tables)]
        metas = [{
            "file_id": t.file_id, "file_name": t.file_name,
            "table_name": t.table_name or "", "page": t.page,
            "rows": t.n_rows, "cols": t.n_cols,
            "engine": t.source_engine, "confidence": t.confidence,
        } for t in tables]
        # ChromaDB 不允许 metadata 值为 None
        with self._write_lock:
            self._collection.upsert(ids=ids, embeddings=vectors,
                                    documents=texts, metadatas=metas)
            total = self._collection.count()
        logger.info("向量入库: {} 张表（库内共 {} chunk）", len(tables), total)
        return len(tables)

    def delete_file(self, file_id: str) -> int:
        """删除某文件全部 chunk（重新识别前清理）。

        VLM 与 pdfplumber 对同一文件识别出的表数量/索引可能不同，
        upsert 幂等无法覆盖消失的旧 chunk，必须显式删除。返回删除条数。"""
        with self._write_lock:
            ids = (self._collection.get(where={"file_id": file_id}, include=[])
                   .get("ids") or [])
            if ids:
                self._collection.delete(ids=ids)
        if ids:
            logger.info("向量删除: file_id={} 清除 {} chunk（库内剩 {}）",
                        file_id, len(ids), self._collection.count())
        return len(ids)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def retrieve(self, query: str, file_name: str | None = None,
                 question_id: str | None = None) -> list[dict]:
        """语义检索 top-k 表 chunk（方案 B+C）：
        B. 零额度规则分词 query 扩展 → embedding 输入更聚焦
        C. 宽召回（top_k × 2）+ 关键词 overlap 重排 → 防相似度差距被压平
        最终按 adjusted_score 截到 config.top_k 返回。
        """
        raw_k = self._s.vectorstore.top_k
        k_broad = min(raw_k * 2, max(self._collection.count(), 1))
        if self._collection.count() == 0:
            return []
        expanded = _expand_query(query)
        vector = self._llm.embed_query(expanded, question_id=question_id)
        where = {"file_name": file_name} if file_name else None
        res = self._collection.query(
            query_embeddings=[vector], n_results=k_broad, where=where,
            include=["documents", "metadatas", "distances"],
        )
        hits: list[dict] = []
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0],
                                   res["distances"][0], strict=False):
            sim = 1.0 - float(dist)
            if sim < self._s.vectorstore.min_similarity:
                continue
            hits.append({"metadata": dict(meta), "similarity": round(sim, 4),
                         "chunk_text": doc})
        if not hits and res["documents"][0] and file_name:
            # 定向检索兜底（qid=17 教训）：题已限定文件，阈值过滤全灭会整链崩溃
            # （no_table→[null]），此时取原始最高分（单表文件无选错风险）。
            doc, meta, dist = (res["documents"][0][0], res["metadatas"][0][0],
                               res["distances"][0][0])
            hits.append({"metadata": dict(meta), "similarity": round(1.0 - float(dist), 4),
                         "chunk_text": doc})
            logger.warning("RAG 定向检索阈值全灭，兜底 top1: file={} qid={} sim={}",
                           file_name, question_id, hits[0]["similarity"])
        if len(hits) > 1:
            hits = _rerank_hits(hits, query)
            hits = hits[:raw_k]
        logger.info("RAG 检索: qid={} 命中 {} 条 (top adjusted={}, query {}->{} chars)",
                    question_id, len(hits),
                    hits[0].get("adjusted_score", "-") if hits else "-",
                    len(query), len(expanded))
        return hits

    def file_is_new_template(self, file_name: str, n_tables: int) -> bool:
        """幂等检测：该文件全部 chunk 是否已是新模板（含 ≥_HEADER_WEIGHT 行'表头: '）。
        reindex 断点续跑依据——已重建的文件不再重复 embedding。"""
        if self._collection.count() == 0:
            return False
        docs = self._collection.get(where={"file_name": file_name},
                                    include=["documents"]).get("documents") or []
        if not docs or len(docs) != n_tables:
            return False
        return all(
            sum(1 for ln in d.split("\n") if ln.startswith("表头: ")) >= _HEADER_WEIGHT
            for d in docs
        )

    def tables_of_file(self, file_id: str) -> list[dict]:
        """按文件取全部已入库 chunk（答题阶段表格来源之一）。"""
        if self._collection.count() == 0:
            return []
        res = self._collection.get(where={"file_id": file_id},
                                   include=["documents", "metadatas"])
        return [dict(m) for m in res["metadatas"]]

    # ------------------------------------------------------------------
    # Web 调试浏览（纯本地读，不产生 token 消耗）
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """库内统计：总数 + 按文件计数 + 按识别引擎计数。"""
        if self._collection.count() == 0:
            return {"total": 0, "files": 0, "by_file": {}, "by_engine": {}}
        res = self._collection.get(include=["metadatas"])
        metas = [m for m in res["metadatas"] if m]
        by_file: Counter = Counter(m.get("file_name", "?") for m in metas)
        by_engine: Counter = Counter(m.get("engine", "?") for m in metas)
        return {
            "total": len(metas),
            "files": len(by_file),
            "by_file": dict(sorted(by_file.items())),
            "by_engine": dict(by_engine.most_common()),
        }

    def browse(self, file_name: str | None = None,
               limit: int = 20, offset: int = 0) -> dict:
        """分页浏览库内 chunk（含 chunk_text 全文）。"""
        where = {"file_name": file_name} if file_name else None
        if self._collection.count() == 0:
            return {"total": 0, "items": []}
        # count() 不支持 where 过滤，用全量 get 的元数据计数（库规模仅数百级）
        all_metas = self._collection.get(where=where, include=["metadatas"])["metadatas"]
        total = len(all_metas)
        res = self._collection.get(where=where, limit=limit, offset=offset,
                                   include=["documents", "metadatas"])
        items = [{
            "id": cid,
            "file_name": m.get("file_name", "?"),
            "table_name": m.get("table_name", ""),
            "page": m.get("page", 0),
            "rows": m.get("rows", 0),
            "cols": m.get("cols", 0),
            "engine": m.get("engine", "?"),
            "confidence": m.get("confidence", 0),
            "chunk_text": doc,
        } for cid, m, doc in zip(res["ids"], res["metadatas"], res["documents"],
                                 strict=False)]
        return {"total": total, "items": items}
