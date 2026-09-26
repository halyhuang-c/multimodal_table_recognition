"""配置加载：YAML 主配置 + .env 密钥。

用法：
    settings = get_settings()          # 单例，自动定位 configs/config.yaml
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# 项目根目录 = src/table_qa/ 向上两级
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TokenPlanCfg(BaseModel):
    """Token Plan 独立通道（华北2 专属 Key + Base URL，与通用通道分开）。"""
    base_url: str = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    models: list[str] = Field(default_factory=list)   # 套餐白名单（auto 模式按此路由）


class DashScopeCfg(BaseModel):
    # 通道选择：auto=按白名单逐模型路由 / token_plan=主力模型全走套餐 / general=全走通用
    channel: str = "auto"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    recognition_model: str = "qwen-vl-max"
    answer_model: str = "qwen-plus"
    embedding_model: str = "text-embedding-v3"
    token_plan: TokenPlanCfg = Field(default_factory=TokenPlanCfg)
    temperature: float = 0.0
    max_tokens: int = 8192
    vision_concurrency: int = 4
    text_concurrency: int = 8
    max_retries: int = 5


class PathsCfg(BaseModel):
    files_dir: Path = Path("files")
    tests_xlsx: Path = Path("data/tests.xlsx")
    cache_dir: Path = Path("cache")
    vector_dir: Path = Path("vectorstore")
    runs_dir: Path = Path("runs")          # 中间过程根（runs/work/answers）
    output_dir: Path = Path("output")      # 最终产物目录（result.xlsx 等，覆盖更新）
    prompts_dir: Path = Path("prompts")

    def abs_path(self, p: Path) -> Path:
        return p if p.is_absolute() else PROJECT_ROOT / p


class PdfCfg(BaseModel):
    dpi: int = 220
    text_density_threshold: int = 50


class VectorCfg(BaseModel):
    top_k: int = 3
    min_similarity: float = 0.3


class RunnerCfg(BaseModel):
    checkpoint: bool = True
    answer_concurrency: int = 8


class OutputCfg(BaseModel):
    show_confidence: bool = True


class Settings(BaseModel):
    dashscope: DashScopeCfg = Field(default_factory=DashScopeCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    pdf: PdfCfg = Field(default_factory=PdfCfg)
    vectorstore: VectorCfg = Field(default_factory=VectorCfg)
    runner: RunnerCfg = Field(default_factory=RunnerCfg)
    output: OutputCfg = Field(default_factory=OutputCfg)

    @property
    def api_key(self) -> str:
        """密钥来源：环境变量 > 项目根 .env。"""
        return _env_key("DASHSCOPE_API_KEY")

    @property
    def token_plan_key(self) -> str:
        """Token Plan 专属 Key：环境变量 > 项目根 .env；未配置返回空（全走通用通道）。"""
        return _env_key("TOKEN_PLAN_API_KEY")


def _env_key(name: str) -> str:
    key = os.getenv(name, "")
    if key:
        return key
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return ""


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_settings(config_path: str | None = None) -> Settings:
    """加载配置（单例）。config_path 便于测试注入。"""
    path = Path(config_path) if config_path else PROJECT_ROOT / "configs" / "config.yaml"
    data = _load_yaml(path)
    return Settings(**data)
