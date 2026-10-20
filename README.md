# 中山市土地交易结果公告采集工具

> 基于 Playwright 的自动化数据采集工具，从中山市公共资源交易平台采集土地使用权交易结果公告。

## 技术栈

- **Python 3.14** + `uv` 包管理
- **Playwright** — 浏览器自动化与 API 响应监听
- **Pydantic** — 数据模型与验证
- **Click** — CLI 接口
- **structlog** — 结构化日志
- **openpyxl** — Excel 导出
- **Ruff + Mypy + pytest** — 代码质量门禁

## 安装

```bash
# 克隆仓库后
cd 04_comprehensive_zhongshan
uv sync
# 安装浏览器（首次）
uv run playwright install chromium
```

## 快速开始

### 小范围测试（2条，约2分钟）

```bash
uv run python -m zhongshan_land_scraper.main --phase 1
```

### 采集前10页（100条，约15-20分钟）

```bash
uv run python -m zhongshan_land_scraper.main --pages 10 --delay 2.0
```

### 可视化录屏（非无头模式）

```bash
uv run python -m zhongshan_land_scraper.main --pages 3 --no-headless --delay 2.0
```

### 仅采集列表（不进入详情页，快速获取标题列表）

```bash
uv run python -m zhongshan_land_scraper.main --pages 10 --list-only
```

### 断点续传（默认启用）

```bash
# 中断后重新运行，自动跳过已采集记录
uv run python -m zhongshan_land_scraper.main --pages 10 --delay 2.0

# 强制从头开始（忽略断点）
uv run python -m zhongshan_land_scraper.main --pages 10 --no-resume
```

## CLI 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--pages` | 10 | 最大采集页数 |
| `--limit` | None | 最大采集条数（不限） |
| `--output` | output | 输出目录 |
| `--headless/--no-headless` | False | 无头模式（默认显示浏览器） |
| `--browser-channel` | chrome | 浏览器渠道 |
| `--resume/--no-resume` | True | 断点续传 |
| `--list-only` | False | 仅采集列表 |
| `--delay` | 1.2 | 操作间隔秒数（>=1.0） |
| `--phase` | None | 阶段1=测试2条 |

## 输出文件

运行后 `output/` 目录包含：

```
output/
├── zhongshan_land_transactions.json   # 结构化 JSON
├── zhongshan_land_transactions.csv    # CSV 表格
├── zhongshan_land_transactions.xlsx   # Excel（含超链接、冻结首行、自动筛选）
├── seen_ids.json                      # 断点续传检查点
├── error_records.json                 # 错误记录
└── raw/
    ├── list_page_01.json ~ list_page_10.json  # 每页列表原始数据
    ├── details/          # 每条记录的 HTML / JSON / TXT / richtext
    └── screenshots/      # 详情页截图
```

## 质量门禁

```bash
# Ruff 检查 + 格式化
uv run ruff check .
uv run ruff format .

# Mypy 类型检查（strict 模式）
uv run mypy src/

# 运行测试
uv run pytest tests/ -v
```

## 项目结构

```
04_comprehensive_zhongshan/
├── src/zhongshan_land_scraper/
│   ├── __init__.py
│   ├── main.py           # CLI 入口
│   ├── scraper.py        # Playwright 采集核心
│   ├── models.py         # Pydantic 数据模型
│   ├── parser.py         # 详情页字段解析
│   ├── output_writer.py  # JSON/CSV/Excel 导出
│   └── resume.py         # 断点续传管理
├── tests/
│   └── test_core.py      # 核心功能单元测试
├── docs/
│   ├── 网络请求分析.md
│   ├── 数据核验说明.md
│   ├── 长期运行方案.md
│   ├── 任务四_技术说明.md
│   └── 任务四_个人心得模板.md
├── evidence/
│   └── README.md
├── output/               # 采集输出（.gitignore 忽略）
├── pyproject.toml
├── .gitignore
├── .env.example
└── README.md
```

## 合规声明

- 使用系统 Chrome 浏览器，让网站自行建立匿名会话
- 正常点击筛选条件，不逆向签名算法
- 禁止硬编码或保存 Cookie/签名值
- 出现 90004 时仅新建一次浏览器上下文，不无限重试
- 页面操作间隔 >= 1.2 秒，禁止并发访问
- 代码中不包含任何 Cookie、Token、Nonce、Signature、Timestamp 或 API Key

## 数据来源

- **网站**: 中山市公共资源交易平台 `ygp.gdzwfw.gov.cn`
- **公告类型**: 土地使用权交易结果公告 (tradingProcess=3742)
- **筛选条件**: 土地矿业 -> 土地使用权交易结果公告 -> 中山市 (siteCode=442000)


