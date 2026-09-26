"""Prompt 管理：YAML 分类加载 + jinja2 渲染 + 版本指纹（缓存 key 组成部分）。

目录结构（设计文档 §6 / 用户要求：分类 yaml，便于后期调整）：
  prompts/meta.yaml                    全局默认
  prompts/recognize/*.yaml             识别类
  prompts/indexing/*.yaml              向量化注释类
  prompts/question/*.yaml              题目理解类
  prompts/answer/*.yaml                答题类
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined, select_autoescape

from table_qa.config import PROJECT_ROOT


@dataclass(frozen=True)
class PromptConfig:
    name: str
    version: str
    model: str            # vision / text
    description: str = ""
    template: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    source: str = ""      # 相对 prompts/ 的 yaml 路径（指纹分域用）


class PromptManager:
    """加载 prompts/ 目录下全部 YAML；支持热重载。"""

    def __init__(self, prompts_dir: Path | None = None) -> None:
        self._dir = prompts_dir or (PROJECT_ROOT / "prompts")
        self._env = Environment(
            autoescape=select_autoescape(default=False),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )
        self._cache: dict[str, PromptConfig] = {}
        self._defaults: dict = {}
        self.reload()

    def reload(self) -> None:
        """重新加载全部 prompt 文件（Web 界面"重载 prompts"按钮调用）。"""
        self._cache.clear()
        meta = self._dir / "meta.yaml"
        self._defaults = (yaml.safe_load(meta.read_text(encoding="utf-8")) or {}).get("defaults", {}) \
            if meta.exists() else {}
        for yml in sorted(self._dir.rglob("*.yaml")):
            if yml.name == "meta.yaml":
                continue
            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            name = str(data.get("name", yml.stem))
            self._cache[name] = PromptConfig(
                name=name,
                version=str(data.get("version", "v0")),
                model=str(data.get("model", "text")),
                description=str(data.get("description", "")),
                template=str(data.get("template", "")),
                temperature=data.get("temperature"),
                max_tokens=data.get("max_tokens"),
                source=str(yml.relative_to(self._dir)).replace("\\", "/"),
            )

    def get(self, name: str) -> PromptConfig:
        if name not in self._cache:
            raise KeyError(f"prompt 不存在: {name}（现有: {sorted(self._cache)}）")
        return self._cache[name]

    def render(self, name: str, **variables: object) -> str:
        """渲染 prompt 模板。未提供的变量会显式报错（StrictUndefined）。"""
        cfg = self.get(name)
        tpl = self._env.from_string(cfg.template)
        return tpl.render(**variables)

    def fingerprint(self) -> str:
        """识别相关 prompt 的指纹（8 位）——识别缓存 key 的组成部分。

        只覆盖 prompts/recognize/ 下的模板：识别产物（表格缓存）仅由识别类
        prompt 决定。answer/question/indexing 类 prompt 的修改不应使缓存
        失效（曾把全量 62 文件识别缓存毒化，触发全量重识别烧额度）。
        """
        payload = {n: c.template for n, c in sorted(self._cache.items())
                   if c.source.startswith("recognize/")}
        return hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:8]


_pm: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    """全局单例。"""
    global _pm
    if _pm is None:
        _pm = PromptManager()
    return _pm
