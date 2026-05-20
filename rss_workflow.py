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
import html
import hashlib
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta
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


def normalize_base_url(raw: str) -> str:
    """修正 BASE_URL：去掉多余的 /chat/completions 后缀，确保以 /v1 结尾"""
    url = raw.strip().rstrip("/")
    # 去掉多余的 /chat/completions（用户可能在 .env 里填了完整地址）
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    # 确保以 /v1 结尾
    if not url.endswith("/v1"):
        url += "/v1"
    return url


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
        raw_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        return OpenAI(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            base_url=normalize_base_url(raw_url),
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


def mark_article(conn, url, feed, title, published, status="processed"):
    article_id = hashlib.md5(url.encode()).hexdigest()[:12]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO articles (id, url, feed, title, published, fetched_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (article_id, url, feed, title, published, now, status))
    conn.commit()
    return article_id


def get_today_articles(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur = conn.execute(
        "SELECT id, url, feed, title, published FROM articles WHERE date(fetched_at) = ? ORDER BY published DESC",
        (today,),
    )
    return cur.fetchall()


# ─── 文章过滤 ────────────────────────────────────────────

def parse_rss_date(published: str, entry) -> datetime | None:
    """从 RSS 条目中解析发布日期"""
    # feedparser 提供 parsed 结构
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        from time import mktime
        return datetime.fromtimestamp(mktime(entry.published_parsed))
    # 无解析结果时尝试手动
    if published:
        for fmt in [
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
        ]:
            try:
                return datetime.strptime(published.strip(), fmt)
            except ValueError:
                continue
    return None


def filter_by_date(articles: list[dict], max_days: int) -> list[dict]:
    """过滤出 max_days 天内的文章，max_days=0 不限制"""
    if max_days <= 0:
        return articles
    cutoff = datetime.now(timezone.utc).astimezone() - timedelta(days=max_days)
    kept, skipped = 0, 0
    result = []
    for art in articles:
        pub = parse_rss_date(art.get("published", ""), art.get("_entry"))
        if pub is None:
            # 无法解析日期，默认保留
            result.append(art)
            kept += 1
        elif pub.astimezone() >= cutoff:
            result.append(art)
            kept += 1
        else:
            skipped += 1
    if skipped:
        print(f"   ⏳ 跳过 {skipped} 篇超期文章（{max_days} 天前）")
    return result


def is_relevant_article(article: dict, config: dict) -> tuple[bool, list[str]]:
    """检查文章是否与音视频/编解码相关，返回 (是否相关, 命中的关键词列表)"""
    keywords = config.get("filter", {}).get("keywords", [])
    if not keywords:
        return True, []  # 未配置关键词，全部保留

    min_match = config.get("filter", {}).get("min_match", 2)
    text = f"{article['title']} {article.get('summary', '')}".lower()
    matched = []

    for kw in keywords:
        # 使用 word-boundary 匹配，防止短词/缩写作为子串误匹配
        # 如 "NS" 不会匹配 "transaction"，"Intel" 不会匹配 "intelligence"
        pattern = r'\b' + re.escape(kw.lower()) + r'\b'
        if re.search(pattern, text):
            matched.append(kw)
            if len(matched) >= min_match:
                break  # 达到阈值即可提前退出

    is_relevant = len(matched) >= min_match
    return is_relevant, matched


def filter_by_relevance(articles: list[dict], config: dict) -> tuple[list[dict], list[dict]]:
    """按主题过滤，返回 (相关文章列表, 不相关文章列表)"""
    min_match = config.get("filter", {}).get("min_match", 2)
    relevant, irrelevant = [], []
    for art in articles:
        is_rel, matched = is_relevant_article(art, config)
        if is_rel:
            art["_kw_matched"] = matched  # 记录命中关键词，供日志输出
            relevant.append(art)
        else:
            art["_kw_matched"] = matched
            irrelevant.append(art)
    return relevant, irrelevant


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
            "_entry": entry,  # 保留原始 entry 用于日期解析
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
4. 将全文翻译成中文（保留技术术语的英文原名，如 "SVC（可伸缩视频编码）"）

请严格按照以下 JSON 格式返回（不要包含 ```json 标记）：
{
    "title_cn": "中文标题",
    "summary": "200字左右的中文核心内容总结",
    "keywords": ["关键词1", "关键词2", "关键词3"],
    "translation": "全文中文翻译..."
}

注意：
- 如果是中文文章则不需要翻译，translation 返回原文
- 如果文章内容太少无法总结，summary 置为空字符串
- 技术术语保留英文并附中文翻译，如 "SVC（可伸缩视频编码）"
- translation 要完整翻译文章内容，不要省略
"""


def translate_and_summarize(client, model, title, content):
    """调用 LLM 同时完成翻译 + 摘要"""
    # 限制输入长度（token 估算：约 4 字符/token）
    # 模型上下文窗口较大，适当放宽限制
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
            timeout=120,
        )
        # 尝试解析 JSON，如果失败做一次清理再重试
        raw = resp.choices[0].message.content.strip()
        # 去掉可能的 ```json 包裹
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # 某些模型会返回含无效转义的字符串，尝试修复
            raw = raw.replace("\n", "\\n").replace("\r", "\\r")
            raw = raw.replace("\\'", "'")
            result = json.loads(raw)
        return result
    except Exception as e:
        print(f"      ⚠️ LLM 调用失败: {e}")
        return {
            "title_cn": title,
            "summary": "",
            "keywords": [],
            "translation": "",
        }


# ─── 文章保存 ────────────────────────────────────────────

def save_article(article_id, article, cn_data, content):
    """保存单篇文章为 HTML"""
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("article.html")

    rendered = template.render(
        id=article_id,
        title=article["title"],
        title_cn=cn_data.get("title_cn", article["title"]),
        summary=cn_data.get("summary", ""),
        keywords=cn_data.get("keywords", []),
        translation=cn_data.get("translation", ""),
        feed=article["feed"],
        url=article["url"],
        published=article["published"],
        content=content,
    )

    filepath = ARTICLES_DIR / f"{article_id}.html"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(rendered)
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
                # 从 HTML meta 中提取（注意反转义 HTML 实体）
                m = re.search(r'<meta name="title-cn" content="([^"]+)"', html_content)
                if m:
                    title_cn = html.unescape(m.group(1))
                m = re.search(r'<meta name="summary" content="([^"]+)"', html_content)
                if m:
                    summary = html.unescape(m.group(1))
                m = re.search(r'<meta name="keywords" content="([^"]+)"', html_content)
                if m:
                    keywords = [html.unescape(k) for k in m.group(1).split(",")]

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

    rendered = template.render(
        date=today,
        article_count=len(articles_data),
        articles=articles_data,
    )

    filepath = DAILY_DIR / f"{today}.html"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(rendered)

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

    # 2a. 按日期过滤（--days 优先于 config）
    config_max_days = config.get("filter", {}).get("max_days", 0)
    max_days = args.days if args.days > 0 else config_max_days
    if max_days > 0:
        source = "命令行 --days" if args.days > 0 else "配置文件"
        print(f"   📅 仅处理 {max_days} 天内的文章（来源: {source}）")
    articles = filter_by_date(articles, max_days)

    # 2b. 按主题过滤 → 分离相关/不相关
    relevant, irrelevant = filter_by_relevance(articles, config)

    # 输出匹配统计
    min_match = config.get("filter", {}).get("min_match", 2)
    if articles:
        kw_total = sum(len(a.get("_kw_matched", [])) for a in relevant)
        kw_avg = kw_total / len(relevant) if relevant else 0
        print(f"   🔑 min_match={min_match} | 相关{len(relevant)}篇(均命中{kw_avg:.1f}个关键词) | 不相关{len(irrelevant)}篇")

    # 输出丢弃的文章标题
    if irrelevant:
        print(f"\n   🎯 以下文章因命中关键词不足 {min_match} 个已丢弃:")
        for art in irrelevant:
            title_short = art["title"][:80].replace("\n", " ")
            kw_str = ", ".join(art.get("_kw_matched", [])[:5])
            kw_info = f" (仅命中: {kw_str})" if kw_str else ""
            print(f"      ❌ [{art['feed']}] {title_short}{kw_info}")

    # 不相关文章仍需入库（防后续重复抓取）
    for art in irrelevant:
        mark_article(conn, art["url"], art["feed"], art["title"], art["published"], status="irrelevant")

    # 后续只处理相关文章
    articles = relevant

    # 3. 过滤新文章
    new_articles = []
    for art in articles:
        if args.force or not is_article_seen(conn, art["url"]):
            new_articles.append(art)

    print(f"\n   其中 {len(new_articles)} 篇新文章需要处理")
    print()

    if not new_articles:
        print("✅ 没有新文章，跳过处理。")
        # 仍然生成日报（复用已处理文章）
        today_ids = get_today_articles(conn)
        if today_ids:
            gen_path = generate_daily_digest(today_ids)
            print(f"\n💡 基于今日已处理的 {len(today_ids)} 篇文章生成了日报")
        return

    # 4. 处理新文章（限制数量）
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
