"""LLM 客户端单例：全项目唯一的模型调用入口。

职责（设计文档 §5.2 qwen_engine 收敛于此）：
- DashScope OpenAI 兼容协议（langchain-openai ChatOpenAI / OpenAIEmbeddings）
- 多 key 轮转（当前单 key，结构预留）
- 限流信号量 + tenacity 指数退避
- token 用量按 phase/question_id 分类记录（成本统计与 Web 面板数据源）

所有引擎与答题模块只允许通过 get_llm_hub() 获取实例，禁止自行创建模型客户端。
"""

from __future__ import annotations

import base64
import threading
import time
from collections import defaultdict
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from table_qa.config import Settings, get_settings
from table_qa.schema import UsageRecord

_TIMEOUT = (60, 180)  # (连接, 读取) 秒——视觉模型整页识别耗时较长
_EMBED_BATCH_SIZE = 20  # DashScope embedding 单次 input 上限（超限 400 InvalidParameter）


class LLMError(RuntimeError):
    """模型调用失败（重试耗尽）。"""


class LLMHub:
    """单例 LLM 网关。通过 get_llm_hub() 获取。"""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        cfg = settings.dashscope
        if not settings.api_key:
            raise LLMError(
                "DASHSCOPE_API_KEY 未配置：请在项目根目录 .env 中填写，"
                "或设置环境变量后重试（参考 .env.example）"
            )
        self._text = self._build_client(cfg.text_model, settings)
        self._vision = self._build_client(cfg.vision_model, settings)
        self._vision_fallback = self._build_client(cfg.fallback_model, settings)
        # check_embedding_ctx_length=False：规避兼容网关的分词预处理差异（实测经验）
        # embedder 恒走通用通道（embedding 不在 Token Plan 白名单）
        self._embedder = OpenAIEmbeddings(
            model=cfg.embedding_model,
            api_key=settings.api_key,
            base_url=cfg.base_url,
            check_embedding_ctx_length=False,
            timeout=_TIMEOUT,
        )
        self._sem_text = threading.Semaphore(cfg.text_concurrency)
        self._sem_vision = threading.Semaphore(cfg.vision_concurrency)
        self._usage_lock = threading.Lock()
        self.usage_records: list[UsageRecord] = []
        # LLM 对话留痕（chat 视图）：与 usage_records 同锁同生命周期
        self.call_records: list[dict] = []

    # ------------------------------------------------------------------
    # 调用接口
    # ------------------------------------------------------------------

    def chat(self, prompt: str, *, phase: str, question_id: str | None = None,
             model: str | None = None) -> str:
        """文本模型调用。返回模型输出文本。"""
        client = self._pick_client(model)
        sem = self._sem_text if client is self._text else self._sem_vision
        messages = [HumanMessage(content=prompt)]
        t0 = time.time()
        with sem:
            resp = self._invoke_with_retry(client, messages)
        self._record_usage(resp, phase=phase, question_id=question_id,
                           prompt=prompt, latency=round(time.time() - t0, 1))
        return _text_of(resp)

    def vision(self, image_path: Path, prompt: str, *, phase: str,
               question_id: str | None = None, use_fallback: bool = False) -> str:
        """视觉模型调用（图片 + 文本提示）。use_fallback=True 时使用轻量模型。"""
        client = self._vision_fallback if use_fallback else self._vision
        image_b64, media_type = _encode_image(image_path)
        messages = [HumanMessage(content=[
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
            {"type": "text", "text": prompt},
        ])]
        t0 = time.time()
        with self._sem_vision:
            resp = self._invoke_with_retry(client, messages)
        self._record_usage(resp, phase=phase, question_id=question_id,
                           prompt=prompt, latency=round(time.time() - t0, 1),
                           image=str(image_path))
        return _text_of(resp)

    def embed(self, texts: list[str], *, phase: str = "embed",
              question_id: str | None = None) -> list[list[float]]:
        """向量化（DashScope text-embedding-v3，兼容模式）。

        DashScope 单次 input 上限 20 条（077.pdf 57 张表实测 400），
        超限自动分批调用后拼接，调用方无感。"""
        vectors: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[i:i + _EMBED_BATCH_SIZE]
            vectors.extend(self._embedder.embed_documents(batch))
            with self._usage_lock:
                self.usage_records.append(UsageRecord(
                    phase=phase, model=self._s.dashscope.embedding_model,
                    question_id=question_id,
                    input_tokens=sum(len(t) for t in batch),
                ))
        return vectors

    def embed_query(self, text: str, *, question_id: str | None = None) -> list[float]:
        """单条查询向量化（检索用）。"""
        vector = self._embedder.embed_query(text)
        with self._usage_lock:
            self.usage_records.append(UsageRecord(
                phase="retrieve", model=self._s.dashscope.embedding_model,
                question_id=question_id, input_tokens=len(text),
            ))
        return vector

    # ------------------------------------------------------------------
    # 用量统计
    # ------------------------------------------------------------------

    def usage_summary(self) -> dict[str, dict[str, int]]:
        """按 phase 分类汇总 token：{phase: {input/output/total/calls}}。"""
        summary: dict[str, defaultdict[str, int]] = {}
        with self._usage_lock:
            records = list(self.usage_records)
        for r in records:
            bucket = summary.setdefault(r.phase, defaultdict(int))
            bucket["input"] += r.input_tokens
            bucket["output"] += r.output_tokens
            bucket["total"] += r.total_tokens
            bucket["calls"] += 1
        return {k: dict(v) for k, v in summary.items()}

    def usage_since(self, marker: int) -> list[UsageRecord]:
        """获取 marker 索引之后新增的用量（答题阶段按题记录用）。"""
        with self._usage_lock:
            return list(self.usage_records[marker:])

    @property
    def calls_marker(self) -> int:
        with self._usage_lock:
            return len(self.call_records)

    def calls_since(self, marker: int, question_id: str | None = None) -> list[dict]:
        """获取 marker 之后新增的 LLM 对话留痕；提供 question_id 时过滤该题
        （并行答题时各线程调用交错，按题过滤保证归属正确）。"""
        with self._usage_lock:
            calls = list(self.call_records[marker:])
        if question_id:
            calls = [c for c in calls if c.get("question_id") == question_id]
        return calls

    @property
    def usage_marker(self) -> int:
        with self._usage_lock:
            return len(self.usage_records)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_client(self, model: str, settings: Settings) -> ChatOpenAI:
        """按 channel 配置构建客户端，实现通道切换：

        - auto       模型命中 token_plan.models 白名单且 Key 已配置 → 套餐通道
        - token_plan 主力模型全走套餐（Key 缺失时警告并回落通用）
        - general    全走通用通道
        embedding 恒走通用通道（不在套餐白名单）。
        """
        cfg = settings.dashscope
        tp_key = settings.token_plan_key
        use_tp = False
        if cfg.channel == "token_plan":
            use_tp = True
            if not tp_key:
                logger.warning("channel=token_plan 但 TOKEN_PLAN_API_KEY 未配置，回落通用通道")
            elif model not in set(cfg.token_plan.models):
                logger.warning("模型 {} 不在 token_plan.models 白名单，套餐通道可能 404", model)
        elif cfg.channel == "auto":
            use_tp = bool(tp_key) and model in set(cfg.token_plan.models)

        if use_tp and tp_key:
            base_url, api_key = cfg.token_plan.base_url, tp_key
        else:
            base_url, api_key = cfg.base_url, settings.api_key
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=_TIMEOUT,
        )

    def _pick_client(self, model: str | None) -> ChatOpenAI:
        if model == self._s.dashscope.fallback_model:
            return self._vision_fallback
        if model == self._s.dashscope.vision_model:
            return self._vision
        return self._text

    def _retry_policy(self):
        return retry(
            reraise=True,
            retry=retry_if_exception_type((ConnectionError, TimeoutError)),
            stop=stop_after_attempt(self._s.dashscope.max_retries),
            wait=wait_exponential(multiplier=2, min=4, max=60),
        )

    def _invoke_with_retry(self, client: ChatOpenAI, messages: list) -> object:
        @self._retry_policy()
        def _do() -> object:
            t0 = time.time()
            try:
                return client.invoke(messages)
            except Exception as e:  # langchain/openai 各种异常统一转重试友好类型
                if _is_retryable(e):
                    raise ConnectionError(str(e)) from e
                raise LLMError(str(e)) from e
            finally:
                logger.debug("LLM 调用时延 {:.1f}s model={}", time.time() - t0,
                             getattr(client, "model_name", "?"))
        return _do()

    def _record_usage(self, resp: object, *, phase: str, question_id: str | None,
                      prompt: str | None = None, latency: float | None = None,
                      image: str | None = None) -> None:
        meta = getattr(resp, "usage_metadata", None) or {}
        inp = int(meta.get("input_tokens", 0))
        out = int(meta.get("output_tokens", 0))
        model = getattr(resp, "response_metadata", {}).get("model_name", "?")
        with self._usage_lock:
            self.usage_records.append(UsageRecord(
                phase=phase, model=str(model), question_id=question_id,
                input_tokens=inp, output_tokens=out,
            ))
            if prompt is not None:   # chat/vision 调用 → 追加对话留痕
                self.call_records.append({
                    "question_id": question_id, "phase": phase, "model": str(model),
                    "prompt": prompt[:8000], "response": _text_of(resp)[:8000],
                    "latency_s": latency, "input_tokens": inp, "output_tokens": out,
                    "image": image,
                })


def _is_retryable(e: BaseException) -> bool:
    text = str(e).lower()
    return any(k in text for k in ("timeout", "timed out", "connection", "rate limit",
                                   "429", "502", "503", "504"))


def _text_of(resp: object) -> str:
    content = getattr(getattr(resp, "content", None), "text", None)
    if content is None:
        content = str(getattr(resp, "content", ""))
    return content.strip()


def _encode_image(path: Path) -> tuple[str, str]:
    """读取图片并 base64 编码，返回 (b64, mime)。Pillow 统一转码 webp 等格式。"""
    from PIL import Image  # 局部导入避免模块级副作用

    media_map = {"JPEG": "jpeg", "PNG": "png", "WEBP": "webp", "BMP": "bmp"}
    with Image.open(path) as img:
        fmt = img.format or "PNG"
        if fmt not in media_map:  # 未知格式统一转 PNG
            buf_path = path
            img.convert("RGB").save(buf_path.with_suffix(".png"))
            fmt = "PNG"
        data = path.read_bytes()
    return base64.b64encode(data).decode(), f"image/{media_map.get(fmt, 'png')}"


_hub: LLMHub | None = None
_hub_lock = threading.Lock()


def get_llm_hub() -> LLMHub:
    """获取全局唯一 LLMHub 单例。"""
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = LLMHub(get_settings())
    return _hub
