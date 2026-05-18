# RSS 订阅 → 翻译 → 日报 工作流

音视频/编解码技术文章自动抓取、翻译、汇总。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp .env.template .env
# 编辑 .env 填入你的 API Key
```

支持 OpenAI / DeepSeek / Qwen 等兼容接口，也支持本地 Ollama。

### 3. 运行

```bash
# 手动运行一次
python rss_workflow.py

# 查看日报
open data/daily/2026-05-18.html
```

## 目录结构

```
autoCodecDoc/
├── config.yaml              # RSS 订阅源 + 配置
├── .env                     # API Key 配置
├── rss_workflow.py          # 主工作流脚本
├── templates/
│   ├── daily_digest.html    # 日报模板
│   └── article.html         # 单篇文章页模板
├── data/
│   ├── articles.db          # SQLite 去重数据库
│   ├── daily/               # 每日日报 HTML
│   └── articles/            # 单篇文章 HTML
└── requirements.txt
```

## 配置说明

编辑 `config.yaml`：

```yaml
feeds:
  - name: "FFmpeg Blog"
    url: "https://ffmpeg.org/blog.rss"
    priority: 1
  - name: "Netflix Tech"
    url: "https://netflixtechblog.com/feed"
    encoding: "html"      # 部分站点需要 html 提取

# 翻译引擎: openai / deepseek / ollama
llm:
  provider: "openai"      # 兼容 deepseek/qwen 等
  model: "gpt-4o-mini"    # 或 deepseek-chat / qwen-turbo

output:
  max_articles_per_day: 10
  summary_length: 200      # 摘要字数
```

`.env` 文件：

```bash
# OpenAI 兼容接口
OPENAI_API_KEY=sk-xxxx
OPENAI_BASE_URL=https://api.openai.com/v1

# 或 DeepSeek
# OPENAI_BASE_URL=https://api.deepseek.com/v1
# OPENAI_API_KEY=sk-xxxx

# 或本地 Ollama（不需要 API Key，但质量略低）
# OLLAMA_URL=http://localhost:11434
# LLM_PROVIDER=ollama
```
