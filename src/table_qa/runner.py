"""运行编排：识别（按文件，缓存去重）→ 答题（按题，checkpoint）→ 产物输出。

目录结构（用户只关心 output/）：
- output/                最终产物，每次运行覆盖更新：
    submission.xlsx     提交文件：id | answer（每题一行，无法作答的题填空字符串）
    result_debug.xlsx   调试文件：含置信度/分数/事件（show_confidence=true 时生成）
    cost_report.csv     token 按 phase 分类统计
- runs/work/             中间过程（无需关心）：
    answers/{qid}.json   每题 AnswerRecord（断点续跑依据）
    skipped.csv          题目清单异常行
- logs/                  集中式滚动日志（见 logging_setup）
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from loguru import logger

from table_qa.config import Settings, get_settings
from table_qa.graph import PipelineContext, build_answer_graph, build_ingest_graph
from table_qa.ingest.categorize import load_profiles
from table_qa.ingest.questions import group_by_file, load_questions, write_skipped_csv
from table_qa.llm_client import get_llm_hub
from table_qa.logging_setup import setup_logging
from table_qa.prompts import get_prompt_manager
from table_qa.schema import AnswerRecord, Question, TraceStep, UsageRecord, score_to_confidence


class WorkDir:
    """固定工作目录（中间过程）：answers trace 持续累积，支持天然断点续跑。

    与按时间戳分目录的旧方案不同：所有运行共享 runs/work/，
    已有 answers/{qid}.json 即跳过，无需 --resume 手动指定。
    """

    def __init__(self, base: Path) -> None:
        self.path = base / "work"
        self.answers = self.path / "answers"
        self.answers.mkdir(parents=True, exist_ok=True)


class Runner:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.ctx = PipelineContext(self.s)
        self.ingest_graph = build_ingest_graph(self.ctx)
        self.answer_graph = build_answer_graph(self.ctx)
        self.llm = get_llm_hub()
        self.pm = get_prompt_manager()

    # ------------------------------------------------------------------ 主流程
    def ingest(self, *, limit: int | None = None, only: list[str] | None = None) -> None:
        """纯识别阶段：题目涉及文件全部识别入库（缓存 + 向量库），不答题。

        两阶段工作流：先 `table-qa ingest` 灌满缓存与 RAG 库，之后 `table-qa run`
        识别全部秒过缓存、直接进入答题。
        """
        questions, _ = load_questions(self.s)
        if only:
            only_set = set(only)
            questions = [q for q in questions if q.id in only_set]
        if limit:
            questions = questions[:limit]
        profiles = load_profiles(self.s)
        files_needed = {q.file_name for q in questions}
        logger.info("纯识别模式: {} 题涉及的 {} 个文件", len(questions), len(files_needed))
        self._ingest_files(files_needed, profiles)

    def reindex(self, annotate: bool = True) -> None:
        """向量库重建：从识别缓存读表 → 补业务注释 → 新模板重新 embedding 入库。

        幂等：已是新模板的文件跳过（file_is_new_template 检测）——中断后重跑
        只处理剩余文件；annotate 结果写回识别缓存，重跑不重复生成。
        annotate=False：完全不调 LLM（chunk 用表头兑底），额度紧张时用。
        """
        profiles = load_profiles(self.s)
        fp = self.pm.fingerprint()
        total_tables, total_files, skipped = 0, 0, 0
        for name, profile in sorted(profiles.items()):
            if profile.dedup_of:          # 副本文件与原件内容相同，重复入库无意义
                continue
            tables = self.ctx.store.load(profile.file_id)
            if not tables:
                logger.warning("无识别缓存，跳过 reindex: {}", name)
                continue
            with logger.contextualize(q="-", f=name):
                if self.ctx.vector.file_is_new_template(name, len(tables)):
                    skipped += 1
                    continue
                n = self.ctx.vector.upsert(tables, annotate=annotate)
                if annotate and any(t.business_annotation for t in tables):
                    self.ctx.store.save(profile.file_id, tables, fp)
                total_tables += n
                total_files += 1
        logger.info("reindex 完成: 新入库 {} 文件 {} 表（已是新版跳过 {} 文件，annotate={}）",
                    total_files, total_tables, skipped, annotate)

    def run(self, *, limit: int | None = None, only: list[str] | None = None) -> Path:
        """全量/限量运行。返回 output/submission.xlsx 路径（最终产物固定目录）。"""
        wd = WorkDir(self.s.paths.abs_path(self.s.paths.runs_dir))
        output_dir = self._output_dir()
        questions, skipped = load_questions(self.s)
        write_skipped_csv(skipped, wd.path / "skipped.csv")
        if only:
            only_set = set(only)
            questions = [q for q in questions if q.id in only_set]
        if limit:
            questions = questions[:limit]
        logger.info("本次运行 {} 题（输出目录 {}）", len(questions), output_dir)

        profiles = load_profiles(self.s)
        files_needed = {q.file_name for q in questions}
        self._ingest_files(files_needed, profiles)

        # 答题并行（answer_concurrency 生效；futures 按提交顺序收集，result 行序稳定）
        from concurrent.futures import ThreadPoolExecutor

        records: list[AnswerRecord] = []
        workers = min(self.s.runner.answer_concurrency, max(len(questions), 1))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(self._answer_one_safe, q, profiles, wd)
                           for q in questions]
                records = [f.result() for f in futures]
        else:
            records = [self._answer_one_safe(q, profiles, wd) for q in questions]
        return self._write_outputs(records, output_dir, skipped)

    def _output_dir(self) -> Path:
        out = self.s.paths.abs_path(self.s.paths.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        return out

    # ------------------------------------------------------------------ 识别
    def _ingest_files(self, file_names: set[str], profiles: dict) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        fp = self.pm.fingerprint()
        cache_hits = 0
        tasks: list[tuple[str, object]] = []
        for name in sorted(file_names):
            profile = profiles.get(name)
            if profile is None:
                logger.error("文件档案缺失: {}", name)
                continue
            if profile.dedup_of:   # 内容去重：跳过 056 这类副本
                logger.info("跳过重复文件 {} (≡ {})", name, profile.dedup_of)
                continue
            if self.ctx.store.exists(profile.file_id, fp):
                cache_hits += 1
                continue
            tasks.append((name, profile))

        workers = min(self.s.dashscope.vision_concurrency, max(len(tasks), 1))
        if workers > 1 and tasks:
            logger.info("并行识别: {} 文件 × {} 并发（缓存命中 {}）", len(tasks), workers, cache_hits)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._ingest_one, name, profile): name
                           for name, profile in tasks}
                for fut in as_completed(futures):
                    fut.result()   # 异常已在 _ingest_one 内捕获
        else:
            for name, profile in tasks:
                self._ingest_one(name, profile)
        logger.info("识别阶段完成: {} 文件（缓存命中 {}）", len(file_names), cache_hits)

    def _ingest_one(self, name: str, profile: object) -> None:
        """单文件识别任务（并行 worker 内执行，失败不中断全局）。"""
        with logger.contextualize(q="-", f=name):   # 日志归属：哪个文件
            logger.info("开始识别: {} [{}类]", name, profile.category)
            try:
                self.ingest_graph.invoke({"profile": profile})
            except Exception as e:  # noqa: BLE001 - 单文件失败降级，其余继续
                logger.error("识别失败 {}: {}", name, e)

    # ------------------------------------------------------------------ 答题
    def _answer_one_safe(self, q: Question, profiles: dict, wd: WorkDir) -> AnswerRecord:
        """单题答题任务（并行 worker 内执行，异常降级为空框架记录，不中断全局）。"""
        with logger.contextualize(q=q.id, f=q.file_name):   # 日志归属：哪题/哪个文件
            try:
                rec = self._answer_one(q, profiles, wd)
            except Exception as e:  # noqa: BLE001 - 保住 submission.xlsx 行完整性
                logger.error("答题异常，输出空框架: {}", e)
                rec = AnswerRecord(question_id=q.id, value="", confidence="low",
                                   confidence_score=0.2, confidence_events=["runner_error"])
            logger.info("完成: conf={} score={:.2f} value={:.60s}",
                        rec.confidence, rec.confidence_score, rec.value)
        return rec

    def _answer_one(self, q: Question, profiles: dict, wd: WorkDir) -> AnswerRecord:
        answer_path = wd.answers / f"{q.id}.json"
        if self.s.runner.checkpoint and answer_path.exists():
            return AnswerRecord(**_load_json(answer_path))   # 断点续跑

        marker = self.llm.usage_marker
        call_marker = self.llm.calls_marker
        profile = profiles.get(q.file_name)
        if profile is None:
            raise RuntimeError(f"文件档案缺失: {q.file_name}（题目 {q.id}）")
        state = self.answer_graph.invoke({
            "question": q,
            "profile": profile,
            "events": [],
            "steps": [],
        })
        events = list(state.get("events", []))
        score = state.get("confidence_score", _score(events))
        rec = AnswerRecord(
            question_id=q.id,
            value=state.get("answer_value", ""),
            confidence=score_to_confidence(score),
            confidence_score=score,
            confidence_events=events,
            steps=state.get("steps", []),
            usage=self.llm.usage_since(marker),
            llm_calls=self.llm.calls_since(call_marker, q.id),
        )
        answer_path.write_text(rec.model_dump_json(indent=2), encoding="utf-8")
        return rec

    # ------------------------------------------------------------------ 输出
    def _write_outputs(self, records: list[AnswerRecord], output_dir: Path,
                       skipped: list[dict] | None = None) -> Path:
        """输出提交文件：每题一行、不删题、id 唯一（官方提交规范）。

        跳过行（文件不存在/question 为空）id 合法且不与已答 id 冲突时，
        以空字符串补入提交——官方"无法作答的题 answer 填空字符串"；
        id 为空 / id 重复的行无法补（补了即违反 id 唯一性）。
        """
        import pandas as pd

        rows = [(r.question_id, r.value) for r in records]
        seen = {qid for qid, _ in rows}
        for s in skipped or []:
            qid = str((s.get("row") or {}).get("id", "")).strip()
            if qid and qid not in seen:
                rows.append((qid, ""))
                seen.add(qid)
                logger.info("跳过题补空答案: {} ({})", qid, s.get("reason"))

        submission_path = output_dir / "submission.xlsx"
        pd.DataFrame(rows, columns=["id", "answer"]).to_excel(submission_path, index=False)
        logger.info("提交文件: {}（{} 行 = 已答 {} + 补空 {}）",
                    submission_path, len(rows), len(records), len(rows) - len(records))

        if self.s.output.show_confidence:
            debug_path = output_dir / "result_debug.xlsx"
            pd.DataFrame({
                "id": [qid for qid, _ in rows],
                "answer": [v for _, v in rows],
                "confidence": [r.confidence for r in records] + [""] * (len(rows) - len(records)),
                "score": [r.confidence_score for r in records] + [None] * (len(rows) - len(records)),
                "events": [";".join(r.confidence_events) for r in records] + [None] * (len(rows) - len(records)),
            }).to_excel(debug_path, index=False)
            logger.info("调试文件: {}", debug_path)

        self._write_cost_report(output_dir)
        return submission_path

    def _write_cost_report(self, output_dir: Path) -> None:
        summary = self.llm.usage_summary()
        rows = [(phase, d["input"], d["output"], d["total"], d["calls"])
                for phase, d in sorted(summary.items())]
        total_in = sum(r[1] for r in rows)
        total_out = sum(r[2] for r in rows)
        with (output_dir / "cost_report.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["phase", "input_tokens", "output_tokens", "total_tokens", "calls"])
            w.writerows(rows)
            w.writerow(["TOTAL", total_in, total_out, total_in + total_out,
                        sum(r[4] for r in rows)])
        logger.info("token 统计: 输入 {} / 输出 {}（{} 类调用）", total_in, total_out, len(rows))


def _score(events: list[str]) -> float:
    from table_qa.graph import _PENALTY
    return max(0.2, round(1.0 - sum(_PENALTY.get(e, 0.1) for e in events), 2))


def _load_json(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding="utf-8"))
