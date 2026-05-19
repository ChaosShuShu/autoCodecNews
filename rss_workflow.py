#!/usr/bin/env python3
"""
RSS 订阅 → 翻译 → 日报 工作流

用法:
    python rss_workflow.py                 # 正常运行
    python rss_workflow.py --force         # 重新处理今日所有文章
    python rss_workflow.py --days 3        # 补处理过去 3 天文章
"""
import os
import re
import json
import hashlib
import sqlite3
import argparse
from datetime import datetime, timezone
from pathlib import Path


import yaml
import feedparser
import trafilatura
import httpx
from openai import OpenAI
from jinja2 import Environment, FileSystemLoader


# ─── 配置加载 ────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DAILY_DIR = DATA_DIR / "daily"
ARTICLES_DIR = DATA_DIR / "articles"
DB_PATH = DATA_DIR / "articles.db"
TEMPLATES_DIR = BASE_DIR / "templates"


def load_config():
    with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env():
    """从 .env 文件加载环境变量（如果存在）"""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


def get_llm_client(config):
    """获取 LLM 客户端，支持 OpenAI 兼容接口和 Ollama"""
    provider = config["llm"]["provider"]
    if provider == "ollama":
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        return OpenAI(
            api_key="ollama",
            base_url=f"{ollama_url}/v1",
        )
    else:
        return OpenAI(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )


# ─── 数据库：去重 ─────────────────────────────────────────

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            url TEXT UNIQUE,
            feed TEXT,
            title TEXT,
            published TEXT,
            fetched_at TEXT,
            status TEXT DEFAULT 'new'
        )
    """)
    conn.commit()
    return conn


def is_article_seen(conn, url):
    cur = conn.execute("SELECT 1 FROM articles WHERE url = ?", (url,))
    return cur.fetchone() is not None


def mark_article(conn, url, feed, title, published):
    article_id = hashlib.md5(url.encode()).hexdigest()[:12]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO articles (id, url, feed, title, published, fetched_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 'processed')
    """, (article_id, url, feed, title, published, now))
    conn.commit()
    return article_id


def get_today_articles(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur = conn.execute(
        "SELECT id, url, feed, title, published FROM articles WHERE date(fetched_at) = ? ORDER BY published DESC",
        (today,),
    )
    return cur.fetchall()


# ─── RSS 抓取 ────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.6367.113 Mobile Safari/537.36"
)

FEED_TIMEOUT = 20  # 单个 feed 超时（秒）


def fetch_feed_xml(url: str) -> str | None:
    """用 httpx 抓取 feed XML，带超时和 User-Agent，返回原始 XML 文本"""
    try:
        with httpx.Client(
            timeout=httpx.Timeout(FEED_TIMEOUT, connect=10),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
    except httpx.TimeoutException:
        print(f"⏱️ 超时")
    except httpx.HTTPStatusError as e:
        print(f"HTTP {e.response.status_code}")
    except httpx.RequestError as e:
        print(f"网络错误: {e.__class__.__name__}")
    except Exception as e:
        print(f"未知错误: {e}")
    return None


def parse_feed_entries(xml_text: str, feed_name: str, seen_urls: set) -> list[dict]:
    """解析 XML 文本为文章列表"""
    articles = []
    parsed = feedparser.parse(xml_text)

    # feedparser 的 bozo 位为 True 说明 XML 有瑕疵，
    # 但只要解析出了条目就不算失败
    if parsed.bozo and not parsed.entries:
        return articles

    for entry in parsed.entries:
        article_url = entry.get("link", "").strip()
        if not article_url or article_url in seen_urls:
            continue
        seen_urls.add(article_url)

        articles.append({
            "feed": feed_name,
            "url": article_url,
            "title": entry.get("title", "无标题").strip(),
            "published": entry.get("published", entry.get("updated", "")),
            "summary": entry.get("summary", ""),
        })

    return articles


def fetch_feeds(config):
    """抓取所有 RSS feed，返回去重后的文章列表"""
    feeds_config = config["feeds"]
    articles = []
    seen_urls = set()
    total = len(feeds_config)

    for idx, feed_cfg in enumerate(feeds_config, 1):
        name = feed_cfg["name"]
        url = feed_cfg["url"]
        print(f"  [{idx}/{total}] 📡 {name} ... ", end="", flush=True)

        xml_text = fetch_feed_xml(url)
        if xml_text is None:
            continue

        entries = parse_feed_entries(xml_text, name, seen_urls)
        if not entries:
            print("⚠️ 无有效条目")
            continue

        articles.extend(entries)
        print(f"{len(entries)} 篇")

    # 按优先级+发布时间排序
    priority_map = {cfg["name"]: cfg.get("priority", 9) for cfg in feeds_config}
    articles.sort(
        key=lambda a: (priority_map.get(a["feed"], 9), a.get("published", "")),
        reverse=False,
    )
    return articles


# ─── 正文提取 ────────────────────────────────────────────

def extract_content(url, timeout=30):
    """提取文章正文，返回纯文本"""
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, output_format="txt",
                                       include_comments=False,
                                       include_tables=False)
            return text.strip() if text else None
    except Exception:
        pass
    return None


def clean_text(text):
    """清理多余的空白字符"""
    if not text:
        return ""
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


# ─── LLM 翻译 + 摘要 ─────────────────────────────────────

SYSTEM_PROMPT = """你是一个专业的音视频技术文章翻译助手。

给定一篇英文技术文章的内容，请完成以下任务：

1. 将文章标题翻译成中文
2. 用 200 字左右的中文总结文章核心内容
3. 提取 3-5 个中文关键词

请严格按照以下 JSON 格式返回（不要包含 ```json 标记）：
{
    "title_cn": "中文标题",
    "summary": "200字左右的中文核心内容总结",
    "keywords": ["关键词1", "关键词2", "关键词3"]
}

注意：
- 如果是中文文章则不需要翻译标题，直接用原标题
- 如果文章内容太少无法总结，summary 置为空字符串
- 技术术语保留英文并附中文翻译，如 "SVC（可伸缩视频编码）"
"""


def translate_and_summarize(client, model, title, content):
    """调用 LLM 同时完成翻译 + 摘要"""
    # 限制输入长度（token 估算：约 4 字符/token）
    max_chars = 12000
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n[文章过长，已截断]"

    prompt = f"""## 文章标题
{title}

## 文章内容
{content}"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
            timeout=60,
        )
        result = json.loads(resp.choices[0].message.content)
        return result
    except Exception as e:
        print(f"      ⚠️ LLM 调用失败: {e}")
        return {
            "title_cn": title,
            "summary": "",
            "keywords": [],
        }


# ─── 文章保存 ────────────────────────────────────────────

def save_article(article_id, article, cn_data, content):
    """保存单篇文章为 HTML"""
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("article.html")

    html = template.render(
        id=article_id,
        title=article["title"],
        title_cn=cn_data.get("title_cn", article["title"]),
        summary=cn_data.get("summary", ""),
        keywords=cn_data.get("keywords", []),
        feed=article["feed"],
        url=article["url"],
        published=article["published"],
        content=content,
    )

    filepath = ARTICLES_DIR / f"{article_id}.html"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    return filepath


# ─── 日报生成 ────────────────────────────────────────────

def generate_daily_digest(today_articles):
    """生成每日日报 HTML"""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("daily_digest.html")

    articles_data = []
    for art in today_articles:
        article_id, url, feed, title, published = art
        article_html = ARTICLES_DIR / f"{article_id}.html"
        cn_html_path = f"../articles/{article_id}.html"

        # 如果译文已生成，读取摘要信息
        summary = ""
        title_cn = title
        keywords = []
        if article_html.exists():
            with open(article_html) as f:
                html_content = f.read()
                # 从 HTML meta 中提取
                m = re.search(r'<meta name="title-cn" content="([^"]+)"', html_content)
                if m:
                    title_cn = m.group(1)
                m = re.search(r'<meta name="summary" content="([^"]+)"', html_content)
                if m:
                    summary = m.group(1)
                m = re.search(r'<meta name="keywords" content="([^"]+)"', html_content)
                if m:
                    keywords = m.group(1).split(",")

        articles_data.append({
            "id": article_id,
            "title": title,
            "title_cn": title_cn,
            "summary": summary,
            "keywords": keywords,
            "feed": feed,
            "url": url,
            "published": published,
            "cn_path": cn_html_path,
        })

    html = template.render(
        date=today,
        article_count=len(articles_data),
        articles=articles_data,
    )

    filepath = DAILY_DIR / f"{today}.html"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n  📝 日报已生成: {filepath}")
    return filepath


# ─── 主流程 ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RSS → 翻译 → 日报 工作流")
    parser.add_argument("--force", action="store_true", help="强制重新处理今日所有文章")
    parser.add_argument("--days", type=int, default=0, help="补处理过去 N 天的文章")
    args = parser.parse_args()

    load_env()
    config = load_config()
    conn = init_db()

    print(f"🚀 RSS 工作流启动 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"   翻译引擎: {config['llm']['provider']} / {config['llm']['model']}")
    print()

    # 1. 抓取 RSS
    print("📥 正在抓取 RSS 订阅源...")
    articles = fetch_feeds(config)
    print(f"\n   共获取 {len(articles)} 篇去重文章")

    # 2. 过滤新文章
    new_articles = []
    for art in articles:
        if args.force or not is_article_seen(conn, art["url"]):
            new_articles.append(art)

    print(f"   其中 {len(new_articles)} 篇新文章需要处理")
    print()

    if not new_articles:
        print("✅ 没有新文章，跳过处理。")
        # 仍然生成日报（复用已处理文章）
        today_ids = get_today_articles(conn)
        if today_ids:
            gen_path = generate_daily_digest(today_ids)
            print(f"\n💡 基于今日已处理的 {len(today_ids)} 篇文章生成了日报")
        return

    # 3. 处理新文章（限制数量）
    max_articles = config["output"].get("max_articles_per_day", 10)
    to_process = new_articles[:max_articles]

    if len(new_articles) > max_articles:
        print(f"   ⚠️ 超出每日限制 ({max_articles})，仅处理前 {max_articles} 篇")

    # 4. 初始化 LLM
    client = get_llm_client(config)
    model = config["llm"]["model"]
    timeout_s = config["extraction"].get("timeout", 30)

    processed = 0
    for i, art in enumerate(to_process, 1):
        print(f"\n[{i}/{len(to_process)}] {art['feed']}")
        print(f"   📰 {art['title'][:80]}")

        # 4a. 提取正文
        if config["extraction"]["enabled"]:
            print(f"   🔍 提取正文...", end=" ", flush=True)
            content = extract_content(art["url"], timeout_s)
            if content:
                content = clean_text(content)
                print(f"{len(content)} 字符")
            else:
                print("⚠️ 提取失败，使用 feed 摘要")
                content = art.get("summary", "")
        else:
            content = art.get("summary", "")

        # 4b. LLM 翻译 + 摘要
        print(f"   🤖 LLM 翻译+摘要...", end=" ", flush=True)
        cn_data = translate_and_summarize(client, model, art["title"], content)

        title_cn = cn_data.get("title_cn", art["title"])
        summary = cn_data.get("summary", "")
        kw = cn_data.get("keywords", [])
        print(f"✓ ({title_cn[:30]})")

        if kw:
            print(f"   🏷️  {' · '.join(kw)}")

        # 4c. 入库 + 保存
        article_id = mark_article(conn, art["url"], art["feed"], art["title"], art["published"])
        save_article(article_id, art, cn_data, content)
        processed += 1

    print(f"\n{'='*50}")
    print(f"✅ 处理完成: {processed}/{len(to_process)} 篇")

    # 5. 生成日报
    today_ids = get_today_articles(conn)
    if today_ids:
        gen_path = generate_daily_digest(today_ids)
    else:
        print("\n⚠️ 今日无文章，未生成日报")

    conn.close()
    print(f"\n📂 数据目录: {DATA_DIR.absolute()}")
    print(f"📖 打开日报: {gen_path}" if today_ids else "")


if __name__ == "__main__":
    main()
