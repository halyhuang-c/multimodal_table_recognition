"""Web 调试界面（FastAPI）。

用途：加载 tests.xlsx → 选择单题运行 → 查看 RAG/识别/答题全链路 trace，
定位答案错误与大模型幻觉。核心逻辑复用 Runner，不重复实现。

启动：table-qa web  →  http://127.0.0.1:8000
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from table_qa.config import get_settings
from table_qa.logging_setup import setup_logging
from table_qa.schema import AnswerRecord

app = FastAPI(title="table-qa 调试台")

_lock = threading.Lock()       # 单题运行互斥（避免并发识别同一文件）
_init_lock = threading.Lock()  # Runner 懒初始化互斥（并发首请求会重复创建
                               # chromadb PersistentClient，触发 SQLite 锁竞态）


class _State:
    runner: Any = None
    profiles: dict = {}
    questions: list = []
    records: dict[str, AnswerRecord] = {}


def _ensure_runner() -> None:
    """懒初始化（首次请求时加载，避免 import 即触发 API Key 校验）。"""
    if _State.runner is not None:
        return
    with _init_lock:
        if _State.runner is not None:
            return
        setup_logging()
        import json

        from table_qa.ingest.categorize import load_profiles
        from table_qa.ingest.questions import load_questions
        from table_qa.runner import Runner

        settings = get_settings()
        runner = Runner(settings)
        profiles = load_profiles(settings)
        questions, _ = load_questions(settings)
        # 预加载固定工作目录的答题记录：CLI 跑完的结果在界面直接可见
        records: dict[str, AnswerRecord] = {}
        from table_qa.runner import WorkDir

        wd = WorkDir(settings.paths.abs_path(settings.paths.runs_dir))
        for f in wd.answers.glob("*.json"):
            try:
                records[f.stem] = AnswerRecord(**json.loads(
                    f.read_text(encoding="utf-8")))
            except Exception as e:  # noqa: BLE001 - 单文件损坏不阻塞界面
                logger.warning("答案文件损坏，忽略 {}: {}", f.name, e)
        # 全部成功后一次性发布，失败时不留半初始化状态
        _State.profiles = profiles
        _State.questions = questions
        _State.records = records
        _State.runner = runner


@app.get("/")
def index() -> FileResponse:
    static = Path(__file__).parent / "static" / "index.html"
    return FileResponse(static)


_FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            b'<rect width="16" height="16" rx="3" fill="#2563eb"/>'
            b'<rect x="3" y="4" width="10" height="1.6" fill="#fff"/>'
            b'<rect x="3" y="7" width="10" height="1.6" fill="#fff" opacity=".85"/>'
            b'<rect x="3" y="10" width="7" height="1.6" fill="#fff" opacity=".7"/></svg>')


@app.get("/favicon.ico")
def favicon() -> Response:
    """内联 SVG 图标，避免浏览器默认请求 404 刷日志。"""
    return Response(content=_FAVICON, media_type="image/svg+xml")


@app.get("/api/questions")
def api_questions() -> JSONResponse:
    _ensure_runner()
    from table_qa.cache import TableStore

    store = TableStore(_State.runner.s)
    ingested_map: dict[str, bool] = {}   # file_id → 缓存文件存在（同文件多题去重读盘）
    items = []
    for q in _State.questions:
        profile = _State.profiles.get(q.file_name)
        rec = _State.records.get(q.id)
        ingested = False
        if profile and not profile.dedup_of:
            if profile.file_id not in ingested_map:
                # 文件存在性判定（非指纹）：改过 prompts 不影响"已入库"状态展示
                ingested_map[profile.file_id] = bool(store.load(profile.file_id))
            ingested = ingested_map[profile.file_id]
        items.append({
            "id": q.id,
            "file_name": q.file_name,
            "question_type": q.question_type,
            "question": q.question,
            "table_hint": q.table_hint,
            "answer_format": q.answer_format,
            "category": profile.category if profile else "?",
            "repaired": q.repaired,
            "answered": rec is not None,
            "ingested": ingested,
            "confidence": rec.confidence if rec else None,
            "score": rec.confidence_score if rec else None,
        })
    return JSONResponse({"total": len(items), "items": items})


@app.post("/api/ingest/{qid}")
def api_ingest(qid: str, force: bool = False) -> JSONResponse:
    """阶段一：只识别入库，不答题。

    force=True：先删识别缓存 + 清向量库该文件旧 chunk，再全量重识别入库
    （改引擎代码后用）；force=False：缓存命中则秒过（幂等）。
    """
    _ensure_runner()
    from table_qa.cache import TableStore

    q = next((x for x in _State.questions if x.id == qid), None)
    if q is None:
        raise HTTPException(404, f"题目不存在: {qid}")
    with _lock:
        profile = _State.profiles.get(q.file_name)
        if profile is None:
            raise HTTPException(400, f"文件档案缺失: {q.file_name}")
        if force:
            store = TableStore(_State.runner.s)
            cache_path = store._path(profile.file_id)
            if cache_path.exists():
                cache_path.unlink()
                logger.info("[ingest] 已删识别缓存: {}", cache_path)
            _State.runner.ctx.vector._collection.delete(
                where={"file_name": q.file_name})
            logger.info("[ingest] 已清向量库 {} 旧 chunk", q.file_name)
        _State.runner._ingest_files({q.file_name}, _State.profiles)
    return JSONResponse({"ok": True, "file_name": q.file_name})


@app.post("/api/answer/{qid}")
def api_answer(qid: str) -> JSONResponse:
    """阶段二：只答题，不重新识别。

    删旧答案（清 checkpoint）后用现有识别缓存 + 向量库重答；
    未入库（识别缓存缺失）返回 400，先走阶段一。
    """
    _ensure_runner()
    from table_qa.cache import TableStore
    from table_qa.runner import WorkDir

    q = next((x for x in _State.questions if x.id == qid), None)
    if q is None:
        raise HTTPException(404, f"题目不存在: {qid}")
    with _lock:
        profile = _State.profiles.get(q.file_name)
        if profile is None:
            raise HTTPException(400, f"文件档案缺失: {q.file_name}")
        store = TableStore(_State.runner.s)
        # 只查缓存文件存在性，不校验 prompt 指纹——答题只需缓存+向量库物理在；
        # 指纹不匹配（改过 prompts）不代表数据不可用，刷新识别走「重新入库」
        if not store.load(profile.file_id):
            raise HTTPException(400, f"尚未识别入库: {q.file_name}，请先执行「识别入库」")
        wd = WorkDir(_State.runner.s.paths.abs_path(_State.runner.s.paths.runs_dir))
        ans_path = wd.answers / f"{q.id}.json"
        if ans_path.exists():
            ans_path.unlink()
            logger.info("[answer] 已删旧答案: {}", ans_path)
        _State.records.pop(q.id, None)
        rec = _State.runner._answer_one(q, _State.profiles, wd)
        _State.records[qid] = rec
    return JSONResponse(_serialize_record(rec, q))


@app.get("/api/record/{qid}")
def api_record(qid: str) -> JSONResponse:
    _ensure_runner()
    rec = _State.records.get(qid)
    if rec is None:
        raise HTTPException(404, "尚未作答")
    q = next((x for x in _State.questions if x.id == qid), None)
    return JSONResponse(_serialize_record(rec, q))


@app.get("/api/cost")
def api_cost() -> JSONResponse:
    _ensure_runner()
    return JSONResponse({"summary": _State.runner.llm.usage_summary()})


@app.get("/api/vector/stats")
def api_vector_stats() -> JSONResponse:
    """向量库统计（纯本地读，无 token 消耗）。"""
    _ensure_runner()
    return JSONResponse(_State.runner.ctx.vector.stats())


@app.get("/api/vector/chunks")
def api_vector_chunks(file_name: str = "", limit: int = 20,
                      offset: int = 0) -> JSONResponse:
    """分页浏览向量库 chunk（纯本地读）。"""
    _ensure_runner()
    limit = max(1, min(limit, 100))
    data = _State.runner.ctx.vector.browse(file_name or None, limit, max(0, offset))
    return JSONResponse(data)


@app.get("/api/vector/search")
def api_vector_search(q: str, file_name: str = "", k: int = 8) -> JSONResponse:
    """语义检索向量库（复用答题 RAG 链路，每次消耗 1 次 embedding 调用）。"""
    _ensure_runner()
    q = q.strip()
    if not q:
        raise HTTPException(400, "查询词为空")
    hits = _State.runner.ctx.vector.retrieve(q, file_name or None,
                                             question_id="web_search")
    return JSONResponse({"query": q, "hits": hits[:max(1, min(k, 20))]})


@app.post("/api/prompts/reload")
def api_prompts_reload() -> JSONResponse:
    _ensure_runner()
    _State.runner.pm.reload()
    return JSONResponse({"ok": True, "fingerprint": _State.runner.pm.fingerprint()})


def _json_safe(obj: Any) -> Any:
    """NaN/Inf → None：JSONResponse 用 allow_nan=False，记录里残留的
    float('nan')（如旧版 thinking 答案 value）会直接 500。"""
    if isinstance(obj, float):
        return None if (obj != obj or obj in (float("inf"), float("-inf"))) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _serialize_record(rec: AnswerRecord, q: Any) -> dict:
    return _json_safe({
        "question_id": rec.question_id,
        "value": rec.value,
        "confidence": rec.confidence,
        "confidence_score": rec.confidence_score,
        "confidence_events": rec.confidence_events,
        "error_tag": rec.error_tag,
        "steps": [s.model_dump() for s in rec.steps],
        "usage": [u.model_dump() for u in rec.usage],
        "llm_calls": rec.llm_calls,
        "question": {
            "id": q.id, "question": q.question, "question_type": q.question_type,
            "answer_format": q.answer_format, "table_hint": q.table_hint,
            "file_name": q.file_name,
        } if q else None,
    })


static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
