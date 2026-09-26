# 复杂表格理解问答系统 · 设计文档

> 版本：v0.5（草案，待评审）
> 日期：2026-09-20
> 状态：设计阶段，未开始编码；数据勘察已完成

**变更记录**

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-09-20 | 初稿：架构、模块划分、里程碑 |
| v0.2 | 2026-09-20 | 深化：融合对齐算法、三题型时序、沙箱规范、格式归一规则、并发模型、降级矩阵、工程规范、prompt 草案 |
| v0.3 | 2026-09-20 | 对齐需求：Excel/Word 原生通道；页范围解析与题目-表格匹配；容量规划；CLI 规格；badcase 归因闭环；确定性调用；持久缓存；答案兜底 |
| v0.4 | 2026-09-20 | **数据勘察结论回灌**：真实规模（908题/90文件/答题主导成本）；submit 模板确认；answer_format 四枚举；pdfplumber 引擎与分类路由；文件名/脏行自动修复；内容去重；xlsx/docx 降级 backlog |
| v0.5 | 2026-09-20 | 删除人工补录（无二次机会），D/F 类转全自动尽力恢复策略；精简冗余章节 |

---

## 1. 项目概述

### 1.1 目标

构建一条离线批处理流水线：读取天池大赛题目清单 `tests.xlsx`（908 题），对每道题在其对应的目标文件中完成**表格结构恢复、内容提取或推理计算**，按 `submit-template.xlsx` 模板（`id` + `answer` 两列）生成提交文件。

### 1.2 已确认的关键决策（含勘察实证）

| 决策项 | 结论 |
|---|---|
| 项目形态 | 离线批处理流水线（CLI 驱动） |
| 技术路线 | **分类路由 + 多引擎融合**：pdfplumber（数字PDF确定性抽表）+ PaddleOCR（本地GPU）+ Qwen-VL（云端VLM）+ PDF文本层（校验源） |
| 云端模型 | 阿里 DashScope：qwen-vl-max（主力）/ qwen-vl-plus（兜底）/ qwen-plus（文本推理） |
| 真实规模 | **908 题**（extract 652 / thinking 216 / structure 39 / 脏行 1），90 个唯一文件（内容去重后 89，010≡056） |
| 成本结构 | 识别阶段极轻（~90 文件），**答题阶段主导成本**（908 次文本调用）→ 答题质量与复核优先 |
| 输出格式 | 已确认：`id` + `answer` 两列（submit-template.xlsx） |
| answer_format | **四值枚举**：`string`(396) / `number`(365) / `json_array`(107) / `json`(40) |
| 真值 | tests.xlsx 的 answer 列**全空** → 本地评分用"双引擎一致+规则自检+人工抽检"三角验证，另建小规模人工标注集 |
| 已有资源 | DashScope Key；92 个样本文件 + 完整分类清单（A/B/C/D/F 五类逐文件标注） |

### 1.3 成功标准

1. 908 题全部产出 schema 合法答案（按 answer_format 枚举校验通过率 100%）；
2. 三类题型在人工抽检中达到可用精度（M1 基线 → M3 显著提升）；
3. 任何一道题的答案都可回溯到中间产物（badcase 可归因、可分类统计）；
4. 成本与耗时可量化、可控制。

---

## 2. 需求规格（勘察实证版）

### 2.1 输入

- **题目清单** `data/tests.xlsx`（908 行），实际列结构：

| 列 | 说明 |
|---|---|
| id | 唯一，字符串数字（'1'~'908'） |
| file_name | 3 处笔误：`58.pdf/59.pdf/0060.pdf` → 补零归一化为 `058.pdf/059.pdf/060.pdf`（题目文本可证实映射） |
| question_type | 枚举 + 1 条脏行（id=63，question_type 被问题文本占用 → 按 answer_format=json_array + 题意修复为 extract） |
| question | 均长 37 字，口语化中文；可能内嵌文件名、表名、精度指令（"保留两位小数"） |
| table_hint | 73% 非空，多为表名（"应收账款表""Table 1 DeepSeek 基座模型对比"） |
| answer_format | 100% 填充，四值枚举 |
| answer | **全空** |

- **目标文件** `data/files/`（92 个，编号 001~094 缺 042/050）：pdf=25 / png=31 / jpg=33 / webp=1；**无 xlsx/docx**（原生通道降级为 backlog）。

- **开发期调研结论**（用于了解这批样本的分布特征；L1 清单机制已移除，运行时全走 L2 探测）：

| 类别 | 数量 | 特征 | 路由策略 |
|---|---|---|---|
| A 数字原生PDF·规整有线表 | 9 | 文本层、单层表头、横竖线清晰 | **pdfplumber 确定性抽表**为主源，VLM 校验 |
| B 数字原生PDF·多级/分组表头 | 13 | 文本层、多级表头/分组带/小计行 | pdfplumber 抽表 + 重建多级表头，VLM 校验 |
| C 清晰数字截图 | 45 | 锐利无畸变，网格线/行底色清晰 | PaddleOCR + VLM 双引擎融合；过滤 UI 控件 |
| D 扫描/拍照件 | 19 | 旋转/模糊/红章/打码/手写/曲面 | 预处理（纠偏去噪去章）→ 双引擎；打码/手写走 repair 裁图聚焦VLM → 仍不可辨认时输出 [MASK] 占位符 |
| F 非线性可视化布局 | 6 | 日程卡片/热力图/树图/信息图，无表格线 | **VLM 语义重建专prompt**（含 089 矩形树图等极端情况，全自动尽力恢复），禁用线检测 |

已知干扰名单（来自分类清单）：水印（001/018/025/032/094）、红章打码（002-005/051/052）、UI控件/Excel界面（082/084/092）、嵌入图片（031/034）、多语种 10 个（挪威029/斯洛伐克030/葡031/西018/立陶宛019/阿拉伯语抬头013,014/荷法英054）、长文本单元格 19 个（保留换行）、内容去重（010≡056）。

### 2.2 输出（模板已确认）

`runs/{timestamp}/result.xlsx`：两列 `id | answer`，与 submit-template.xlsx 一致：
- `json`（structure 题）：结构 JSON 字符串
- `json_array`：JSON 数组字符串
- `number`：归一化数值（题干有精度指令按指令，如"保留两位小数"）
- `string`：归一化文本

### 2.3 路由决策树（关键问题：PDF 走文本还是走 OCR？）

任何文件进来，**先判断文件类型，再决定走哪条识别链路**——不是所有 PDF 都走同一条路：

```
文件扩展名路由
│
├── xlsx/xls → 原生解析（openpyxl）—— 不走 PDF 也不走 OCR
├── docx     → 原生解析（python-docx）—— 不走 PDF 也不走 OCR
├── 图片     → 直接走 OCR+VLM 双引擎融合
│
└── pdf ──► 第一步：检测"有没有文本层"
    │
    │   方法：pdfplumber 打开 → 所有 page.extract_text()
    │   chars_per_page = 总字符数 / 页数
    │   阈值：> 50 → 数字原生PDF（有文本层）
    │         ≤ 50 → 扫描/图片型PDF（本质是图片套 PDF 壳）
    │
    ├── 数字原生PDF（有文本层）
    │   │
    │   ├─ pdfplumber.extract_tables() 尝试抽表
    │   │   ├─ 成功 → pdfplumber 确定性抽表为主源
    │   │   │         （零成本、零幻觉、数字精度来自文本层）
    │   │   │         VLM 仅作校验方，不做主识别
    │   │   │
    │   │   └─ 失败（表格为空/极稀疏）
    │   │       → pdfplumber 文本层继续用（数字校验源）
    │   │       → 结构识别交给 VLM
    │   │
    │   └─ 判定多级/单层表头：
    │       pdfplumber 抽完后看合并单元格数 + 表头行数
    │       合并≥2处 或 表头≥2行 → B 类（多级，需重建）
    │       否则 → A 类（规整有线表）
    │
    └── 扫描/图片型PDF（无文本层）
        │
        ├─ 渲染为 220DPI 页图
        │
        ├─ 判断是否非线性布局（F 类）：
        │   OpenCV 检测表格线 vs 色块数量
        │   线极少、色块多 → F 类 → semantic_rebuild.md 专 prompt
        │
        └─ 否则 → OCR+VLM 双引擎融合
```

**单层路由：L2 运行时自动探测**（真实赛题数据未知，不依赖任何预置清单）：

| 机制 | 定位 | 判定 |
|---|---|---|
| L2 运行时自动探测 | **唯一路由路径**，无需预置知识 | 按决策树判定：PDF 文本密度→B/D；图片 OpenCV 长直线数<3→F（色彩信号实测不可靠已弃用） |

L2 实测（92 样本对照调研真值）：引擎分支路由正确率——pdfplumber 分支 22/22=100%；图片 F 类直接命中 2/6，其余 4 个（热力图/树图/信息图线数不少）由 **qwen_engine 空结果重试链**兜底自动切 semantic_rebuild；D→C 13 个 M1 阶段同引擎零影响（M2 加清晰度检测后改进）。

**L2 自动探测判定指标**：

| 判定项 | 方法 | 阈值 |
|---|---|---|
| PDF 是否有文本层 | `len(pdfplumber.extract_text()) / 页数` | > 50 → 有文本层 |
| pdfplumber 能否抽表 | `extract_tables()` 返回列表长度 | ≥ 1 → 能 |
| 是否多级表头 | 合并单元格数 + 表头行数 | 合并≥2 或 表头≥2 → 多级 |
| 是否非线性布局 | OpenCV 横线/竖线 vs 色块数量 | 线极少、色块多 → F 类 |
| 是否需 D 类预处理 | 拉普拉斯方差清晰度 + 旋转角估计 | 清晰 → C，模糊/偏斜 → D |

核心原则：**能走 pdfplumber（有文本层）就绝对不走 OCR**——零成本、零幻觉、数字精度来自 PDF 原生文本层。只有确认无文本层时才进入 OCR/VLM 链路。

---

## 3. 总体架构

### 3.1 流水线

```
tests.xlsx ─► ① 题目解析与修复 ─► 按文件分组(内容sha1去重)
                文件名补零修复 · 脏行题型修复 · 92文件分类路由
                      │
files/+分类清单 ─► ② 预处理与路由
                A/B类PDF → pdfplumber确定性抽表 + VLM校验
                C类图片  → 双引擎(PaddleOCR+Qwen-VL)融合
                D 类扫描  → 纠偏/去噪/去章 → 双引擎；打码/手写走 repair 裁图聚焦VLM，
                           仍不可辨认时输出占位符再由答题阶段做 best-effort 猜测
                F 类可视化 → VLM语义重建(专prompt，全自动尽力恢复)
                      ▼
                ③ 识别缓存（按 文件内容sha1+页 持久缓存）
                      ▼
                ④ 融合仲裁（C/D类）
                结构骨架择优 · 三源文本投票 · 分歧→裁图聚焦二次识别
                数字类：pdfplumber/文本层/OCR 一致 → 覆盖 VLM
                      ▼
                ⑤ 规范化：跨页续表合并 · 单位行处理
                   → 规范HTML + DataFrame 双表示
                      ▼
                ⑥ 答题（908次，成本主战场）
                公共前置：页/行/列范围解析 + table_hint选表
                structure → 确定性切片转JSON
                extract   → LLM定位取值(带复核预算)
                thinking  → LLM生成pandas代码 → 沙箱执行+复算
                      ▼
                ⑦ answer_format四枚举序列化 + 自检 → 降级重试 → result.xlsx
```

### 3.2 三题型端到端时序

**公共前置（三题型共享）**
```
题目 → [range_parse] 解析 {page_range?, row_range?, col_range?}
     → 按页范围筛选候选表 → [table_select] table_hint匹配表名/关键词打分选表
     → 未指明页码时取该文件全部表作为候选
```

**structure 题（39 题，几乎全为局部题：'前1行'/'前3列'/'表头行'）**
```
→ 取目标表 → 确定性切片 → 展开矩阵 → StructureAdapter 序列化(json)
→ validate: 索引边界、rowspan/colspan 不越界、行列数吻合
```

**extract 题（652 题，成本与精度主战场）**
```
→ 取表HTML(含表名/单位注) → LLM(extract prompt)：输出定位依据+取值
→ 确定性校验：取值必须存在于矩阵 → 不通过触发复核(换模型/带页图重问)
→ formatter 按 answer_format 序列化(string/number/json_array)
```

**thinking 题（216 题）**
```
→ 取表 → DataFrame(表头展平+前向填充)
→ LLM(thinking_code prompt)：生成 pandas 代码
→ sandbox 执行 → 加总类第二算法复算 → formatter(含题干精度指令解析)
→ 沙箱失败 → LLM 直算降级 + low
```

### 3.3 设计原则

1. **规范表示用 HTML**：`rowspan/colspan` 天然表达合并关系；确定性代码转 JSON / DataFrame。
2. **确定性优先**：A/B 类 PDF 走 pdfplumber 确定性抽表；thinking 算术全部沙箱代码执行；LLM 只做感知与语义。
3. **识别按文件内容缓存**：sha1 去重（010≡056 只识别一次），跨 run 持久化。
4. **一切中间产物落盘**：页图、引擎原始输出、融合中间态、每题 trace。
5. **答案永不为空**：降级链末端也输出 schema 合法答案。
6. **确定性调用**：`temperature=0`，重跑结果一致，便于 badcase 复现。
7. **分类路由**：文件类别决定引擎组合，不搞一刀切。

---

## 4. 核心数据模型（pydantic v2）

```python
Question:
    id: str
    file_name: str            # 修复后
    question_type: Literal["structure", "extract", "thinking"]  # 含脏行修复
    question: str
    table_hint: str | None
    answer_format: Literal["string", "number", "json_array", "json"]

FileProfile:                  # 文件档案（分类清单导入）
    file_id: str              # 内容 sha1 前12位（天然去重 010≡056）
    path: Path
    category: Literal["A", "B", "C", "D", "F"]
    language: str             # zh/en/multi...
    quirks: list[str]         # 水印/红章/UI控件/长文本/旋转...
    dedup_of: str | None

ParsedRange:
    page_range: tuple[int, int] | None   # 1-based
    row_range: tuple[int, int] | None    # 0-based
    col_range: tuple[int, int] | None

NormalizedTable:              # 规范表格（系统核心资产）
    file_id: str
    table_name: str | None
    pages: list[int]
    html: str
    n_rows: int; n_cols: int
    unit_note: str | None
    grid: list[list[CellRef]]
    confidence: Literal["high", "medium", "low"]

AnswerRecord:
    question_id: str
    value: str                # 按 answer_format 序列化后的最终答案
    confidence: Literal["high", "medium", "low"]
    error_tag: str | None     # badcase 分类（见10.4）
    trace: TraceRef
```

---

## 5. 模块详细设计

```
multimodal_table_recognition/
├── pyproject.toml                 # uv 管理，ruff+mypy+pytest 配置
├── configs/config.yaml + .env
├── prompts/                       # prompt 模板，版本化
├── data/                          # 纯官方输入（真实赛题时整体替换）
│   ├── tests.xlsx                 # 从天池目录复制进来
│   ├── submit-template.xlsx
│   └── files/                     # 92 个样本文件
├── src/table_qa/
│   ├── schema.py / config.py
│   ├── ingest/      questions.py · loader.py · preprocess.py · categorize.py
│   ├── engines/     base.py · pdfplumber_engine.py · paddle_engine.py
│   │                · qwen_engine.py · textlayer_engine.py
│   ├── fusion/      align.py · arbitrate.py · repair.py
│   ├── tables/      html_table.py · merge_pages.py · dataframe.py
│   ├── answer/      select.py · structure.py · extract.py · thinking.py
│   │                · sandbox.py · formatter.py
│   └── cli.py
├── scripts/         local_eval.py · cost_report.py · preview.py · smoke_data.py
├── cache/           # 持久识别/融合缓存
├── runs/            # 每次运行产物
└── tests/
```

### 5.1 ingest

- **questions.py**：读取 tests.xlsx 并做**三类修复**——
  1. 文件名补零归一化（`58.pdf→058.pdf` 等，规则：去前导零匹配 + 题干中的文件名交叉验证）；
  2. 脏行题型修复（question_type 非枚举值时，按题意+answer_format 推断，id=63 → extract）；
  3. 校验（id 唯一、文件存在、枚举合法），异常行进 `skipped.csv` 不中断全局。
- **categorize.py**：L2 运行时探测分类（PDF 文本密度 → B/D；图片 OpenCV 结构线 → C/F）+ sha1 内容去重，零预置知识、零 token。
- **loader.py**：按类别路由加载——A/B 类 PDF 用 pdfplumber 读文本层与表格线框；C/D/F 类图片与 PDF 渲染页图（DPI 可配默认 220）；图片 Pillow 载入。
- **preprocess.py**：D 类专项——EXIF 修正、纠偏（PaddleOCR 方向分类 + Hough 兜底）、去噪、红章/打码区域检测（标记后 repair 链路聚焦重识别）；多语种文件配置 OCR 语言包；F 类不做线检测预处理。

### 5.2 engines

- **base.py**：`TableEngine.recognize(page) -> EngineResult` 抽象，可插拔可开关。
- **pdfplumber_engine.py（A/B 类主源）**：确定性抽表——`page.extract_tables()` 得到单元格矩阵，结合 `rects/lines` 重建合并关系（跨行跨列格），输出 HTML + 文本；**零成本零幻觉**，数字精度来自文本层。多级表头按分组带/小计行重建层级（B 类 13 个的核心工作）。
- **paddle_engine.py**：PP-StructureV3，独立 worker 进程（GPU 推理阻塞事件循环，ProcessPoolExecutor 桥接）；输出 HTML + 单元格 bbox + 置信度；语言包按文件语言配置。
- **qwen_engine.py**：DashScope OpenAI 兼容协议；图片 base64；限流信号量 + tenacity 退避 + 多 key 轮转；`temperature=0`；token 用量记录。F 类走专用语义重建 prompt。
- **textlayer_engine.py**：PyMuPDF/pdfplumber words 行聚类，作校验源。

### 5.3 fusion（C/D 类核心；A/B 类为 pdfplumber 主源 + VLM 校验）

#### 5.3.1 对齐算法（align.py）

1. **行签名对齐**：列数 + 首末列文本摘要做行签名，`difflib.SequenceMatcher` 行级对齐，吸收行数差异；
2. **单元格对齐**：行对齐后按列索引对齐，`rapidfuzz` token 相似度（阈值 0.85）；
3. **bbox 辅助**：OCR bbox 仅用于 repair 裁图。

#### 5.3.2 分歧裁决矩阵（arbitrate.py）

| 分歧类型 | 裁决规则 | 置信度 |
|---|---|---|
| 文本分歧·数字/金额 | pdfplumber/文本层 与 OCR 一致 → 覆盖 VLM；否则 repair | high |
| 文本分歧·普通文本 | ≥2 源一致通过；各执一词 → repair | repair后 high |
| 结构分歧·行列数 | A/B类信 pdfplumber；C类边框完整信OCR，否则信VLM | medium |
| 结构分歧·合并关系 | VLM 优先；与 bbox 行边界矛盾 → repair | medium |
| 表名/单位注 | VLM 优先，其他源兜底 | — |

- **repair.py**：分歧单元格裁图（bbox 外扩 8px）→ VLM 聚焦识别，最多 2 轮；仍分歧 → 数字信确定性源、语义信 VLM，标 low。
- **D 类打码/手写**：走 repair 链路，VLM 尽最大努力辨认；仍不可辨认时输出固定占位符 `[MASK]`，答题阶段 best-effort 处理（thinking 题打码金额缺失则跳过该项/输出 0，extract 题则输出 `[MASK]`），保证答案不为空。

### 5.4 tables

- **html_table.py**：HTML ↔ 展开矩阵双向转换；lxml 宽容解析 + 失败重试。
- **merge_pages.py**：跨页续表三条件加权（列数 0.4 + 表头匹配 0.4 + 续表标记 0.2，≥0.6 判续表）；字段继承：DataFrame 层前向填充、HTML 层保留 rowspan，两语义显式区分。
- **dataframe.py**：多级表头 `父:子` 展平；单位行/表注捕获进 unit_note；数字清洗（千分位/全角/括号负数/破折号→NaN）；**长文本单元格保留换行**（19 个文件依赖此行为）。

### 5.5 answer

- **select.py**：题目-表格匹配打分（hint 匹配表名 0.5 + 题干关键词与表头重叠 0.3 + 页范围 0.2）；hint 形态实证为表名（"应收账款表""Table 1 DeepSeek 基座模型对比"）；低分并列时 top-2 交 LLM 判别。
- **structure.py**：ParsedRange → 确定性切片 → adapter 序列化。实证题面：`恢复一下 第一行 和 前3列的表格结构`、`恢复前两行和第一列`、`恢复…表头行结构`。
- **extract.py**：LLM 输出定位依据+取值 → 矩阵回查校验 → 失败复核（换 qwen-vl-max 带页图重问）。题面实证：`扣子的产品功能是什么？`、`应收账款,应收票据…上期末各是多少？`（注意：含**非财务表**——产品功能表等，prompt 不得假设财务语境）。
- **thinking.py**：pandas 代码生成 → 沙箱 → 复算 → formatter。题面实证：`属于美国的产品有多少个？`（计数）、`请计算…三者平均分，保留两位小数`（精度指令在题干）。
- **formatter.py**：**answer_format 四枚举序列化**（string/number/json_array/json）+ 题干精度指令解析（"保留N位小数"正则，LLM 兜底）+ 归一规则表（金额/数量/百分比/日期/单位换算/文本）。
- **validate.py**：structure 边界一致性；thinking 第二算法复算（差值>0.01 重试）；extract 回查；失败进降级链。

#### 5.5.1 沙箱规范（sandbox.py）

| 项 | 约束 |
|---|---|
| 可用符号 | `df`/`pd`/`np` + 内置白名单（len/sum/round/max/min/sorted/abs） |
| 执行方式 | `exec` + 受限 globals，CPU 5s 超时（子进程 watchdog），512MB |
| 禁止 | import / open / 网络 / eval / `__` 访问（AST 预检） |
| 输出 | 最后表达式或 `result`；类型 str/int/float/list |

#### 5.5.2 格式归一规则表（默认值，题干精度指令与 answer_format 优先）

| 类型 | 默认规则 |
|---|---|
| number | 题干有"保留N位小数"按指令；否则整数不带小数、小数去尾零 |
| 金额 | 半角千分位两位小数；括号负数转 − |
| 百分比 | 两位小数保留百分号 |
| 日期 | 归一 `YYYY-MM-DD` |
| 单位换算 | unit_note=万元 且题目要求元 → ×10⁴；题目未提 → 原值原单位 |
| 文本 | 全角转半角、trim、空白折叠（**保留单元格内换行**——长文本文件依赖） |

### 5.6 runner

- **并发模型**：asyncio 主循环；识别阶段按文件并发（受 GPU/API 双重约束）；答题阶段按题并发（文本/视觉独立信号量）。
- **两级存储**：`cache/` 持久缓存（key = `sha1(file)+page+engine+prompt_version`）；`runs/{ts}/` 单次产物；断点续跑读上次 answers 跳过已完成题。
- **成本统计**：token 按 阶段×题型×模型 汇总。

### 5.7 运行产物

```
cache/
├── engines/{file_id}/p{n}.{engine}.json
└── fused/{file_id}/table_k.html

runs/20260920-153000/
├── pages/{file_id}/p{n}.png
├── answers/{qid}.json          # 答案+trace
├── result.xlsx · skipped.csv · cost_report.csv · run.log
```

### 5.8 降级与重试矩阵（答案永不为空）

| 故障 | 动作 | 兜底 |
|---|---|---|
| VLM 限流/超时 | tenacity 退避（≤5次） | 切 qwen-vl-plus → 仅本地引擎 |
| VLM 非法输出 | 宽容解析 → 重试1次 | 仅确定性引擎结果 |
| 沙箱失败 | 重生成1次 | LLM 直算 + low |
| validate 不通过 | 降级链重试1次 | 最优结果 + low |
| Paddle 崩溃 | 重启 worker | 该文件纯 VLM |
| 全链路失败 | — | schema 合法空框架（structure 1×1 空表 / number 出 0 / string 空串），保证可提交 |

### 5.9 CLI 规格

```
table-qa run     --tests data/tests.xlsx --files data/files [--only qid,...] [--limit N]
table-qa resume  --run runs/xxx
table-qa preview --file data/files/001.pdf [--page 3]     # 原图 vs 重建表并排
table-qa eval    --run runs/xxx [--gt data/manual_gt.xlsx] # 人工标注集评分/一致性报告
table-qa cost    --run runs/xxx
```

---

## 6. Prompt 管理

- `prompts/*.md`，jinja 插槽；prompt_version 写入缓存 key 与 trace。
- 草案见附录 B。实证校准点：题目含非财务表（产品表/模型跑分表）、口语化中文、表名型 hint。

---

## 7. structure 题答案 JSON（自设计 schema，adapter 可切换）

```json
{
  "row_count": 12,
  "col_count": 8,
  "cells": [
    { "row": 0, "col": 0, "rowspan": 2, "colspan": 1, "text": "项目" },
    { "row": 0, "col": 1, "rowspan": 1, "colspan": 3, "text": "2025年" }
  ]
}
```

- 0 起索引；局部题裁剪后序列化（切断的合并格按边界截断 rowspan/colspan）；只记录锚点格。
- 官方 schema 公布后仅新增 adapter。**注意实证**：39 道 structure 题中多数只要求"表头行"或"前N行M列"的局部结构。

---

## 8. 输出格式（已确认）

`id | answer` 两列（与 submit-template.xlsx 一致）。序列化规则：

| answer_format | 序列化 |
|---|---|
| string | 文本（归一规则表） |
| number | 数值（题干精度指令优先） |
| json_array | JSON 数组字符串 |
| json | structure 结构 JSON 字符串 |

---

## 9. 配置清单（configs/config.yaml）

```yaml
dashscope:
  api_keys: [env:DASHSCOPE_API_KEY]
  vision_model: qwen-vl-max
  fallback_model: qwen-vl-plus
  text_model: qwen-plus
  temperature: 0.0
  max_tokens: 8192
  vision_concurrency: 4
  text_concurrency: 8
engines:
  pdfplumber: { enabled: true }        # A/B类主源
  paddle: { enabled: true, device: gpu, workers: 1 }
  textlayer: { enabled: true }
pdf: { dpi: 220 }
fusion: { text_sim_threshold: 0.85, repair_rounds: 2 }
select: { hint_weight: 0.5, keyword_weight: 0.3, page_weight: 0.2 }
runner:
  checkpoint: true
  answer_concurrency: 8
cache: { dir: cache/, enabled: true }
```

---

## 10. 质量保障与工程规范

### 10.1 代码规范（强制）

- mypy 严格模式门禁；ruff（`E,F,I,B,UP,SIM`）；
- 纯逻辑（HTML 转换、续表判定、归一化、沙箱 AST、匹配打分、文件名修复）单测覆盖 ≥90%；
- 依赖单向：`answer → tables → fusion → engines → ingest`。

### 10.2 测试与评测

| 层 | 手段 |
|---|---|
| 纯逻辑单测 | HTML 展开/还原、续表打分、归一化、沙箱安全、选表打分、文件名/脏行修复 |
| 集成回归 | 从 92 文件按 类别×题型 抽 mini 数据集，`local_eval.py` 一键回归 |
| 人工标注集 | **自建 50 题真值**（answer 列全空，无官方真值）—— stratified 抽样人工作答，作为本地评分基准 |
| 可视化比对 | `preview.py` 原图 vs 重建表并排 |
| 一致性指标 | 多源一致率按类别统计 |

### 10.3 可观测性

loguru 结构化日志；每题 trace（prompt 版本、产物路径、token、置信度、降级路径、repair 轮次）。

### 10.4 badcase 归因闭环

| 层 | 标签 |
|---|---|
| A 识别层 | `ocr_text` / `miss_row` / `extra_row` |
| B 结构层 | `grid_shape` / `merge_wrong` / `cross_page` / `table_select` |
| C 语义层 | `locate_wrong` / `range_parse` / `inherit_wrong` / `unit_wrong` |
| D 计算层 | `code_logic` / `sandbox_fail` |
| E 格式层 | `format_rule` / `serialize` |

迭代循环：`eval 汇总 → 最大占比层 → 针对性修复 → 回归`，禁止盲调。

---

## 11. 里程碑与验收（勘察后修订）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| M1 最小闭环 | 分类清单转路由元数据 + VLM 单引擎 + 三题型 + 两列输出 | 每题型≥5题人工验证正确；产出与模板一致的 result.xlsx |
| M2 多引擎与路由 | pdfplumber（A/B）+ PaddleOCR（C/D）+ 融合仲裁 + D类预处理 + F类语义重建 | 分类别一致率报告；融合后抽检错误率 ≤ 单引擎 |
| M3 精度攻坚 | 人工标注50题评分基线 + badcase 闭环 + 跨页/单位/局部裁剪 | 标注集得分显著超 M1 基线；难点逐类有通过 case |
| M4 全量生产 | 908 题全量、并发/成本调优、断点续跑演练 | 全量成功率100%、成本报表、提交文件生成 |

---

## 12. 风险与应对

| 风险 | 应对 |
|---|---|
| 无官方真值，精度不可测 | 自建 50 题人工标注集 + 多源一致率 + 抽检 |
| F 类（树图/热力图）语义重建上限 | VLM 专 prompt（semantic_rebuild.md） + 答题阶段 best-effort 对含 [UNSURE] 元素的处理 |
| D 类打码/手写不可识别 | repair 聚焦重识别 → [MASK] 占位 → 答题阶段 best-effort（结构题保留 [MASK]、extract 题输出 [MASK]、thinking 题跳过该项或输出 0） |
| 多语种 OCR | PaddleOCR 语言包按文件配置；VLM 多语种能力强，作主源之一 |
| DashScope 限流 | 多 key + 退避 + Batch API 备选（识别阶段仅 ~90 文件，压力小） |
| 官方 structure schema 未知 | adapter 隔离；json 枚举已确认，风险收窄为字段命名 |
| B 类多级表头重建复杂 | pdfplumber 线框 + VLM 校验双保险，坏例走 repair |

---

## 13. 技术栈与依赖

Python 3.11+ / uv · PyMuPDF · **pdfplumber** · Pillow · OpenCV · PaddleOCR 3.x（paddlepaddle-gpu，多语言包）· openai（DashScope 兼容）· pandas · openpyxl · pydantic v2 · pydantic-settings · tenacity · loguru · lxml/bs4 · rapidfuzz · jinja2 · pytest · ruff · mypy

---

## 14. 容量规划（按勘察实据重算）

识别阶段（轻）：90 唯一文件 − 22 个 A/B 类走免费 pdfplumber ≈ 68 文件走 VLM/Paddle；均 ~2 页 ≈ 140 页 × 10~20s / 并发4 ≈ **10~20 分钟**，成本可忽略量级。

答题阶段（主成本）：908 题 × (表格 HTML 输入 + 推理) ≈ 3~8s/题 / 并发8 → **10~20 分钟**；extract 652 题若有 20% 走复核，增量约 130 次调用。成本估算公式：`cost ≈ 908 × avg_input_tokens × 单价`，M1 实测后出精确数。

调优杠杆：缓存命中率（单文件 28 题 → 表格识别只 1 次）→ 复核触发阈值 → Batch API。

---

## 附录 A：开放问题

1. structure 题官方 JSON 字段命名（answer_format=json 已确认，仅字段结构待公布，adapter 隔离）；
2. 评分细则（决定复核预算分配：若 extract 1 分/题、structure 高分，则复核向 structure/thinking 倾斜）；
3. D 类 [MASK] 与 F 类 [UNSURE] 在各题型下的最佳答案形态需 M1 实测校准。

## 附录 B：核心 Prompt 草案

**B.1 table_recognition.md（VLM 整页识别）**

```
你是表格识别专家。请识别图片中的{{table_hint_clause}}表格，输出规范 HTML：
- 仅输出 <table>…</table>，不含任何解释；
- 合并单元格必须用 rowspan/colspan 表达；
- 多级表头按原层级用合并单元格表达，不要展平；
- 单元格文本原样保留（含全角字符、单位、括号、换行），不要自行补全或纠正数字；
- 页眉、页脚、脚注、页码、水印、红章不要纳入表格；网页/Excel 截图中的按钮、
  下拉框、复选框、公式栏等 UI 控件必须过滤；
- 数字必须逐位仔细核对，禁止推测性填充；打码/模糊不可辨认处输出固定占位符 [MASK]；
- 若页面有多张表，同时输出各表表名（表格上方最近的标题文本）。
```

**B.2 cell_repair.md（分歧单元格聚焦识别）**

```
图片是表格中某个单元格的放大裁剪。请只输出这个单元格内的文字，逐字转写：
- 数字逐位核对，区分 0/O、1/l/I、全角/半角；
- 空单元格输出空字符串；不可辨认输出 [MASK]；
- 不要输出任何解释或标点修饰。
```

**B.3 extract.md（内容提取）**

```
下面是一张表格的 HTML 表示{{unit_clause}}。用户问题：{{question}}

请以 JSON 输出：
{"locate": {"row_header": "...", "col_header": "...", "header_path": "..."},
 "value": "...", "reason": "一句话"}

要求：value 必须原样来自表格文本（多值则数组）；定位依据必须真实存在于表头/行标题；
表格可能是财务/产品/模型跑分等任意领域，按题面语义理解，不要假设特定领域。
```

**B.4 thinking_code.md（推理代码生成）**

```
DataFrame `df` 是表格数据（多级表头已展平为 "父:子" 列名，合并格已前向填充，
数字已转数值）。列名：{{columns}}，前3行：{{head}}。

用户问题：{{question}}

请生成 pandas 代码完成计算：
- 只用 df/pd/np 与内置函数；
- 最后的表达式或 result 变量即答案（str/int/float/list）；
- 计数题用 len()；平均/比例注意题目要求的精度由外部处理，代码返回原始数值；
- 若需单位换算，显式乘系数。
只输出代码，不要解释。
```

**B.5 range_parse.md（范围解析：页/行/列）**

```
将表格题目中的范围描述解析为 JSON：
{"page_range": [start, end] | null, "row_range": [start, end] | null, "col_range": [start, end] | null}
页码 1 起始、行列索引 0 起始，均含端点；未提及的维度填 null；
"第一行"=前1行；"表头行"=row_range 取表头行数；"整张表"三个均 null。
题目：{{question}} 补充说明：{{answer_format}}
```

**B.6 semantic_rebuild.md（F 类·非线性可视化语义重建）**

```
图片是非线性的可视化布局（日程卡片/热力图/矩形树图/信息图等），没有表格线。
请按语义重建为二维表格，输出规范 HTML：
- 先判断最适合的行/列维度（如 日×时段、公司×指标、编号×属性）；
- 色块/图形编码的数值按标注文本还原，不猜测颜色映射；
- 卡片标题、标签、图例作为表头或属性列；
- 无法确定语义的元素输出 [UNSURE:描述]；
- 仅输出 <table>…</table>。
```

## 附录 C：数据勘察结论（已完成）+ 剩余工作

**已完成**（详见本文档 §1.2/§2.1）：
- tests.xlsx 全量分析：908 题、题型/格式分布、脏行与文件名笔误定位；
- submit-template.xlsx 确认两列输出；
- 分类清单全量解读：五类路由、干扰名单、多语种、去重关系。

**M1 开工前剩余**（均已完成；其中分类清单产物与 L1 查表机制已随后移除，现为纯 L2 路由）：
1. 分类清单 → 路由元数据（已移除）；
2. PDF 页数/文本层密度统计（A/B 类 pdfplumber 抽表可行性验证，抽 3 个文件试抽）；
3. 渲染抽查 10 个代表文件（A/B/C/D/F 各 2）人工确认分类准确性。
