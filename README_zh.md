# PKU AI 助教

自动批改 [course.pku.edu.cn](https://course.pku.edu.cn)（Blackboard Learn）上的学生作业，导出 Excel 供人工复审核，并将已批成绩提交回平台。

**工作流：** 爬取提交 → LLM 对照评分标准批改 → 人工审核（TUI 或 Excel） → 提交成绩

---

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) 包管理器
- LLM API 密钥（OpenAI 兼容或 Anthropic 原生）
- PKU IAAA 账号（学号 + 密码）

---

## 安装

**1. 克隆并安装依赖**

```bash
git clone <repo-url>
cd PKU-AI-TA
uv sync
```

**2. 配置凭据**

```bash
cp .env.example .env
# 编辑 .env 填入你的凭据
```

关键配置项：

| 变量 | 说明 |
|---|---|
| `LLM_PROVIDER` | `openai`（OpenAI 兼容）或 `anthropic`（原生 API） |
| `OPENAI_API_KEY` | API 密钥 |
| `OPENAI_BASE_URL` | API 地址（默认 `https://api.minimaxi.com/v1`） |
| `TA_MODEL` | 模型名称，如 `MiniMax-M3` |
| `PKU_USERNAME` | 北大统一认证学号 |
| `PKU_PASSWORD` | 密码 |
| `COURSE_ID` | Blackboard 课程 ID，如 `_98024_1`（从 URL 获取） |
| `STUDENT_WHITELIST` | 要批改的学生学号，逗号分隔；留空 = 全部 |
| `REVIEW_THRESHOLD` | 置信度低于此值 → 标记为需审核（默认 `0.75`） |
| `TA_THREADS` | 并行批改线程数（默认 `4`） |

**3. 准备评分标准**

创建 `rubric.md`，描述评分细则：

```markdown
# 第五次作业评分标准（100 分）

## 7.4 证明 N(f) 也是简单容量网络（15 分）
- 理解辅助网络结构（5 分）
- 划分两类边并进行讨论（5 分）
- 核心证明入度/出度为 1（5 分）
```

**4. 准备学生名单**（可选）

```
# student_list（每行一个学号）
2300012345
2300012346
```

---

## 使用方法

### 查看作业列表

```bash
uv run python main.py list
```

输出每个作业的 `gradeBookPK`、提交数、已批改数、未批改数。

### 第一步：批改

```bash
# 批改全部学生
uv run python main.py grade --course _98024_1 --column 426252 --rubric rubric5.md

# 只批改名单中的学生
uv run python main.py grade --course _98024_1 --column 426252 --rubric rubric5.md \
  --student-list student_list --out scores_426252.xlsx

# 限 N 人试跑（测试评分标准）
uv run python main.py grade --course _98024_1 --column 426252 --rubric rubric5.md --limit 5

# 继续上次中断的任务
uv run python main.py grade --course _98024_1 --column 426252 --rubric rubric5.md --resume

# 8 线程并行批改
uv run python main.py grade --course _98024_1 --column 426252 --rubric rubric5.md --threads 8
```

| 参数 | 说明 |
|---|---|
| `--course` | Blackboard 课程 ID（或设置 `COURSE_ID`） |
| `--column` | 作业 `gradeBookPK` |
| `--rubric` | 评分标准 Markdown 文件 |
| `--student-list` | 每行一个学号的名单文件 |
| `--out` | 输出 Excel 文件（默认 `scores.xlsx`） |
| `--threads` | 并行线程数 |
| `--due-date` | 截止时间（ISO 8601），不填则自动抓取 |
| `--resume` / `-r` | 继续上次中断的批改 |
| `--regrade-unapproved` | 保留已批改的，重新批改未通过的 |

### 修正晚交信息

修正已有 Excel 中的晚交记录（修复时区 Bug），不重新批改：

```bash
# 使用 Blackboard 截止时间
uv run python main.py recheck-late --scores scores_426252.xlsx --column 426252

# 手动指定晚交截止日（当天 23:59:59，覆盖原 DDL）
uv run python main.py recheck-late --scores scores_426252.xlsx --column 426252 --late-cutoff 2026-05-13
```

### 第二步：人工审核

推荐使用终端交互界面（TUI）：

```bash
# 只审需复核的学生
uv run python main.py review --needs-review

# 审全部
uv run python main.py review --all

# 自动通过满分，再审其余
uv run python main.py review --auto-approve --needs-review
```

**详细操作指南：[docs/tui-review-zh.md](docs/tui-review-zh.md)**

| 快捷键 | 功能 | 是否翻页 |
|---|---|---|
| `a` | 通过并进入下一位 | 否 |
| `n` | 编辑审阅笔记——**在此写扣分理由** | 是 |
| `o` | 打开提交文件 | 是 |
| `r` | 打开评分标准 | 是 |
| `u` | 切换 Uncertain Parts 自动追加到笔记 | 是 |
| `R` | 检索已通过学生的笔记关键字（如 `7.4`） | 是 |
| `j` | 按姓名或学号跳转到学生 | 否 |
| `s` | 跳过，下一位 | 否 |

核分通过笔记完成：写 `扣X分：原因` → 保存 → 系统自动解析扣分 → 提示覆盖成绩 → 确认 → 通过。

### 第三步：提交成绩

```bash
uv run python main.py submit --course _98024_1 --column 426252 --scores scores.xlsx

# 预览（不实际提交）
uv run python main.py submit --course _98024_1 --column 426252 --scores scores.xlsx --dry-run
```

---

## TUI 详细工作流

详细的操作指南请参见 **[docs/tui-review-zh.md](docs/tui-review-zh.md)**，包括：

- 笔记驱动的核分流程
- Uncertain Parts 处理
- 晚交信息处理
- Recall 检索过往批改记录
- Jump 快速跳转

---

## 提交文件处理

| 文件类型 | 处理方式 |
|---|---|
| 文字型 PDF | `pypdf` 提取文字 → 发送给 LLM |
| 扫描/图片型 PDF | `pymupdf` 渲染为图片 → 视觉模型 |
| JPEG / PNG | 直接作为图片发送 |
| Word（.docx） | `python-docx` 提取文字 |

---

## 开发

```bash
# 安装开发依赖
uv sync --extra dev

# 运行测试
uv run pytest tests/ -v
```
