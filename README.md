# table-qa · 多模态表格识别与问答流水线

天池大赛参赛项目：读取题目清单 `tests.xlsx`（908 题，structure / extract / thinking 三类题型），
对每道题在其对应的 PDF / 图片文件中完成**表格结构恢复、内容提取或推理计算**，
按官方模板（`id | answer` 两列）生成可自动评分的提交文件。

> 设计文档：[docs/design.md](docs/design.md)（含完整架构决策与数据勘察结论）

---

## 1. 核心架构

```
tests.xlsx ─► 题目解析与修复（文件名补零/脏行修复）─► 按文件分组(sha1去重)
                                                          │
files/ ─► 分类路由 ┬─ A/B 类数字PDF ─► pdfplumber 确定性抽表（零成本零幻觉）
                  ├─ C/D 类图片/扫描 ─► Qwen-VL 视觉识别
                  └─ F 类非线性可视化 ─► VLM 语义重建（树图/热力图/卡片）
                                                          │
              识别缓存（跨 run 持久）─► ChromaDB RAG 入库（元数据+业务注释+表格HTML）
                                                          │
              答题（LangGraph 条件路由）：
                structure ─► 范围解析 ─► 确定性切片 ─► 结构 JSON
                extract   ─► LLM 定位取值 ─► 矩阵回查（防幻觉）
                thinking  ─► LLM 生成 pandas 代码 ─► AST 沙箱确定性执行
                                                          │
              格式化（四枚举序列化+精度指令）─► 置信度打分 ─► result.xlsx
```

**关键设计**：

| 机制 | 说明 |
|---|---|
| 分类路由 | L2 运行时探测为主路径（PDF 文本密度二分 + 图片 OpenCV 线检测判 F 类），不依赖预置知识，适配未知真实赛题 |
| LLM 单例 | [llm_client.py](src/table_qa/llm_client.py) 是全项目唯一模型调用入口：限流信号量 + tenacity 退避 + token 按 phase 分类统计 |
| 计算与识别分离 | thinking 题算术全部由沙箱代码在真实表格数据上执行（AST 预检禁 import/IO + 子进程 15s 超时），LLM 只负责定位与写代码 |
| 防幻觉 | extract 取值必须在表格矩阵中回查到，失败记 `extract_unverified` 事件并扣置信度 |
| 置信度 | 初始 1.0 按事件扣分（RAG 未命中 -0.1 / 提取未验证 -0.2 / 沙箱降级 -0.2 ...），`>=0.8 high / >=0.5 medium / <0.5 low`；调试界面高亮低分题，提交文件不含置信度 |
| 断点续跑 | 每题答案落盘 `runs/{ts}/answers/{qid}.json`，重跑自动跳过已完成题；识别结果按文件内容 sha1 缓存（010≡056 只识别一次） |
| 答案永不为空 | 降级链末端输出 schema 合法空框架（structure 空 1x1 表 / number 0），保证可提交 |

## 2. 环境与安装

- Python **3.11+**（开发环境 3.12）
- 无需 GPU（PaddleOCR 本地引擎为 M2 可选项）

```powershell
# 0. 进入项目根目录（所有命令都必须在此目录执行）
cd D:\workspace\multimodal_table_recognition

# 1. 创建虚拟环境（项目已带 .venv 时跳过）
python -m venv .venv

# 2. 安装（含 langgraph / chromadb / pdfplumber / fastapi 等全部依赖）
.venv\Scripts\python -m pip install -e .

# 3. 开发工具（可选：pytest / ruff / mypy）
.venv\Scripts\python -m pip install -e ".[dev]"
```

## 3. 配置

### 3.1 API Key（必需）

在项目根目录创建 `.env`（参考 [.env.example](.env.example)）——两个 Key 分开管理：

```ini
DASHSCOPE_API_KEY=sk-xxxxxxxx        # 通用通道（百炼控制台获取）
TOKEN_PLAN_API_KEY=sk-sp-xxxxxxxx    # 套餐通道（可选；华北2 Token Plan 专属）
```

环境变量优先于 `.env` 文件。

### 3.2 通道切换 [configs/config.yaml](configs/config.yaml)

`dashscope.channel` 一项控制 LLM 走哪个计费通道：

| 值 | 行为 | 适用场景 |
|---|---|---|
| `auto`（默认） | 模型在 `token_plan.models` 白名单内且套餐 Key 已配 → 走套餐；否则走通用 | 日常：主力吃套餐、embedding 吃通用免费额度 |
| `token_plan` | 视觉/文本/兜底全走套餐（Key 缺失自动回落通用并告警） | 套餐额度充足、想完全不动免费额度 |
| `general` | 全走通用通道 | 套餐到期/排查通道问题 |

Key 分离原则：`.env` 里两个 Key 各自独立，删掉 `TOKEN_PLAN_API_KEY` 一行即整体退回通用通道，零代码改动。

### 3.3 其他配置

| 配置块 | 关键项 | 说明 |
|---|---|---|
| dashscope | recognition_model / answer_model | 表格识别主力（多模态看图）/ 答题推理（纯文本），temperature=0 确定性调用 |
| dashscope | vision_concurrency / text_concurrency | 并发信号量（限流保护） |
| paths | files_dir / tests_xlsx | 官方数据位置 |
| pdf | dpi / text_density_threshold | 页图渲染精度 / 数字 PDF 判定阈值（chars/页 > 50） |
| vectorstore | top_k / min_similarity | RAG 检索参数 |
| output | show_confidence | `true` 生成调试文件；正式提交用 `--submit` 自动关闭 |

## 4. 快速开始

```powershell
# 0. 数据就位（已完成）：data/tests.xlsx、data/submit-template.xlsx、files/（92 个样本）

# 1. 数据链路自检（无需 API Key）
.venv\Scripts\python scripts\smoke_data.py
#    预期：908 题合法、22 处修复命中、路由分布 B220/C600/D20/F68（L2 运行时探测）

# 2. 跑 3 题冒烟（需要 API Key）
.venv\Scripts\table-qa run --limit 3

# 3. 启动 Web 调试界面
.venv\Scripts\table-qa web
#    浏览器打开 http://127.0.0.1:8000
```

## 5. CLI 命令

**方式一：激活 venv（推荐，一次激活裸命令直用）**

```powershell
# 首次使用需放行脚本（一次性，仅当前用户）
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

.\.venv\Scripts\Activate.ps1     # 提示符出现 (table-qa) 前缀即成功
```

```powershell
# 两阶段用法（推荐）：
table-qa ingest       # 阶段 1：纯识别（灌缓存 + 向量库），可先在 Web 界面抽查找质量
table-qa run          # 阶段 2：答题——识别命中缓存；中断后直接重跑即续（自动跳过已完成题）

# 一步到位（不先 ingest）：识别 + 答题一条命令，识别过的文件自动走缓存
table-qa run

# 限量 / 指定题目
table-qa run --limit 10
table-qa run --only 1,2,3

# 提交模式：只生成 result.xlsx（id|answer 两列），不含置信度调试文件
table-qa run --submit

# 详细日志
table-qa run --limit 5 -v
```

**方式二：不激活，直接全路径调用**

```powershell
.venv\Scripts\table-qa run --limit 10
.venv\Scripts\table-qa web
```

目录职责（只关心 `output/`，其余是中间过程）：

| 目录/文件 | 说明 |
|---|---|
| `output/result.xlsx` | **最终提交文件**（id \| answer，每次运行覆盖更新） |
| `output/result_debug.xlsx` | 调试文件（含 confidence / score / 事件，`--submit` 时不生成） |
| `output/cost_report.csv` | token 按 phase 分类统计（recognize/extract/thinking_code/...） |
| `runs/work/answers/{qid}.json` | 每题完整 trace（断点续跑依据，无需关心） |
| `logs/table-qa_{日期}.log` | 集中式滚动日志：按天分文件 + 单文件 20MB 滚动，保留 30 天；每行含 `[题目id|文件名]` 上下文与线程号，并行时可归位 |

## 6. Web 调试界面

`table-qa web` 后打开 `http://127.0.0.1:8000`，专为**排查答案错误与大模型幻觉**设计：

- **左侧题目列表**：按题型 / 已答 / 低置信度筛选，关键词搜索；已答题显示置信度徽章（低分红色）
- **点选题目 → 运行此题**：单题触发完整流水线（识别缓存命中则秒过）
- **右侧 trace 五步展开**：
  1. **RAG 检索**：query、top-k 命中表名/页码/相似度
  2. **范围解析**：页/行/列范围与解析方式（regex/llm）
  3. **表格选择**：选中表预览（HTML 渲染）——比对原图即可发现识别错误
  4. **答题**：extract 显示取值/定位依据/回查验证；thinking 显示生成代码+沙箱执行结果
  5. **格式化**：最终答案、置信分、扣分事件清单
- **顶部成本条**：累计 token（输入/输出/调用次数），15 秒自动刷新
- **重载 Prompts**：修改 `prompts/*.yaml` 后点击，无需重启

## 7. Prompt 管理

全部 prompt 为 YAML，按用途分四类目录，**改完在 Web 界面点"重载 Prompts"即生效**：

```
prompts/
├── meta.yaml                      # 全局默认参数
├── recognize/                     # 识别：整页识别 / 分歧单元格修复
├── indexing/                      # 向量化：业务注释生成
├── question/                      # 题目理解：范围解析 / F类语义重建
└── answer/                        # 答题：extract / thinking_code
```

prompt 内容指纹参与识别缓存 key——改 prompt 自动失效对应缓存层。

## 8. 目录结构

```
multimodal_table_recognition/
├── configs/config.yaml            # 全局配置
├── prompts/                       # prompt 模板（YAML）
├── output/                        # ★ 最终产物（result.xlsx 等，每次运行覆盖更新）
├── logs/                          # 集中式滚动日志（按天 + 20MB 滚动，保留 30 天）
├── data/                          # 纯官方输入（真实赛题时整体替换）
│   ├── tests.xlsx / submit-template.xlsx
│   └── files/                     # 92 个样本文件
├── src/table_qa/
│   ├── cli.py                     # CLI 入口（run / ingest / web）
│   ├── config.py / schema.py      # 配置 / 强类型数据模型
│   ├── llm_client.py              # LLM 唯一入口（单例，双通道路由）
│   ├── prompts.py                 # YAML prompt 管理
│   ├── graph.py                   # LangGraph 双图编排
│   ├── runner.py / cache.py       # 运行编排 / 表格缓存
│   ├── ingest/                    # 题目修复 / 文件路由 / 页图加载
│   ├── engines/                   # pdfplumber / qwen-vl 引擎
│   ├── tables/                    # HTML↔矩阵 / DataFrame 转换
│   ├── indexing/                  # ChromaDB RAG
│   ├── answer/                    # 三题型答题 / 沙箱 / 格式化
│   └── web/                       # FastAPI 调试界面
├── scripts/                       # smoke_data 等开发辅助脚本
├── cache/                         # 识别缓存（跨 run 持久，可整体删除重建）
├── vectorstore/                   # ChromaDB（可整体删除重建）
├── runs/work/                     # 中间过程（answers trace，无需关心）
└── tests/                         # 单元测试（20 个用例）
```

## 9. 测试

```powershell
.venv\Scripts\python -m pytest tests -q
# 覆盖：rowspan/colspan 展开、切片截断、结构 JSON、数值清洗、
#       DataFrame 转换、沙箱安全（禁 import/open/dunder）与执行
```

## 10. 常见问题

| 问题 | 处理 |
|---|---|
| `DASHSCOPE_API_KEY 未配置` | 填写项目根 `.env`（见 §3.1） |
| 想强制重新识别 | 删除 `cache/tables/` 对应文件（或整个 `cache/`） |
| 想重建 RAG 库 | 删除 `vectorstore/` 目录 |
| 断点续跑 | 天然支持：直接重跑同一命令，已完成题目自动跳过（`runs/work/answers/`）；识别层缓存全局持久；重跑个别题删对应 `answers/{qid}.json`；全部重来删 `runs/work/` |
| 中途改了 prompt | Web 界面点"重载 Prompts"；CLI 重新运行即可（缓存按指纹自动失效） |
| Windows 控制台中文乱码 | 程序已内置 UTF-8 处理；若外部工具乱码，设置 `PYTHONIOENCODING=utf-8` |
| 真实赛题数据 | 替换 `data/tests.xlsx` 与 `data/files/`，其余零改动（路由全靠 L2 运行时探测） |
