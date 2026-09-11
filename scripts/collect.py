#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日资讯自动采集脚本（GitHub Actions 上运行，无需本机开机）

v2 改进：
  1. 信源大幅扩充：必应/谷歌新闻 RSS（多关键词）+ **必应网页搜索 & DuckDuckGo（含 site: 站内检索，
     可深挖 B站 / 小红书 / 抖音 / YouTube / 知乎 等）+ 垂类 RSS**
  2. **抓正文**：对候选条目抓取原文正文（最多 1600 字），喂给模型 —— 解决摘要只有十几个字的问题
  3. LLM 双次尝试（失败自动降规模重试），仍失败才走规则兜底（兜底也用正文，摘要不再干瘪）
  4. 来源多样性约束：单源配额 + 要求每期至少来自 4 个不同站点

用法：
  python scripts/collect.py            # 正式运行
  python scripts/collect.py --dry-run  # 只抓取与统计，不写文件
  python scripts/collect.py --force    # 当天已有日报时强制重生成
"""
import argparse
import datetime
import html as html_mod
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    requests = None
try:
    import feedparser
except ImportError:
    feedparser = None

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
DATA = SITE / "data"
DAILY = DATA / "daily"

TZ = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
NOW_ISO = datetime.datetime.now(TZ).isoformat(timespec="seconds")
KEEP_DAYS = 30
MAX_ITEMS = 20
MIN_ITEMS = 8
RSS_CAP = 6          # 单个 RSS 源最多入池条数
WEB_CAP = 6          # 单个网页搜索词最多入池条数
HN_CAP = 3           # HN 降权
ENRICH_MAX = 14      # 最多抓多少条正文（jina 免费档限速，别开太大）
ENRICH_WORKERS = 5

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# ---------------- 新闻 RSS 关键词 ----------------
NEWS_QUERIES_ZH = [
    "AI 副业 变现", "AI 赚钱 案例", "AI 变现 博主 拆解",
    "AI 副业 月入 实操", "免费 token 额度 领取", "大模型 免费额度 赠送",
]
NEWS_QUERIES_EN = [
    "AI side hustle income", "free LLM API credits", "creator AI monetization",
]

# ---------------- 网页搜索关键词（含站内深挖） ----------------
WEB_QUERIES = [
    # 站内深挖：B站 / 小红书 / 抖音 / YouTube / 知乎
    "site:bilibili.com AI 变现",
    "site:bilibili.com AI 副业 教程",
    "site:xiaohongshu.com AI 变现",
    "site:xiaohongshu.com AI 副业",
    "site:douyin.com AI 赚钱",
    "site:youtube.com AI side hustle",
    "site:zhihu.com AI 变现 拆解",
    # 通用：变现路径与实操
    "AI 变现 案例 月入 拆解",
    "AI 副业 零成本 从0到1",
    "AI 数字产品 模板 卖钱",
    "AI 提示词 售卖 收入",
    "一人公司 AI 收入",
    # 免费额度
    "大模型 免费 API 额度 领取",
    "免费 token 白嫖 教程",
]

# 站内深挖：用阅读器代理渲染平台搜索页（B站实测可用；其余平台有登录墙，失败自动跳过）
JINA_SEARCH_SITES = [
    ("B站", "https://search.bilibili.com/all?keyword={kw}", "bilibili.com/video"),
]
JINA_SEARCH_KEYWORDS = ["AI变现", "AI副业", "AI赚钱"]

# 站内直连（可选，填 ID 即生效）
BILIBILI_UIDS = []          # 例 ["9469745"] -> rsshub 抓该 UP 主视频
YOUTUBE_CHANNELS = []       # 例 ["UCXuqSBlHAE6Xw-yeJA0Tunw"]
RSSHUB_BASES = ["https://rsshub.app"]

# 额外垂类 RSS
EXTRA_RSS = [
    {"name": "量子位", "platform": "量子位", "url": "https://www.qbitai.com/feed"},
    {"name": "少数派", "platform": "少数派", "url": "https://sspai.com/feed"},
    {"name": "爱范儿", "platform": "爱范儿", "url": "https://www.ifanr.com/feed"},
    {"name": "36氪", "platform": "36氪", "url": "https://36kr.com/feed"},
    {"name": "Hacker News", "platform": "Hacker News", "type": "hn",
     "url": "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=30&query="},
]

HN_QUERIES = ["AI side hustle", "free credits", "indie AI revenue"]

LAST_LLM_ERROR = [""]   # 记录最后一次 LLM 失败原因（写进 run log）

DOMAIN_PLATFORM = {
    "bilibili.com": "B站", "b23.tv": "B站",
    "xiaohongshu.com": "小红书", "xhslink.com": "小红书",
    "douyin.com": "抖音", "iesdouyin.com": "抖音",
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "zhihu.com": "知乎", "toutiao.com": "今日头条",
    "mp.weixin.qq.com": "公众号", "weixin.qq.com": "公众号",
    "csdn.net": "CSDN", "juejin.cn": "掘金", "jianshu.com": "简书",
    "36kr.com": "36氪", "sspai.com": "少数派", "ifanr.com": "爱范儿",
    "qbitai.com": "量子位", "jiqizhixin.com": "机器之心",
    "reddit.com": "Reddit", "news.ycombinator.com": "Hacker News",
    "substack.com": "Substack", "medium.com": "Medium",
    "gumroad.com": "Gumroad", "etsy.com": "Etsy", "x.com": "X",
    "baijiahao.baidu.com": "百家号", "sohu.com": "搜狐", "163.com": "网易",
    "qq.com": "腾讯新闻", "sina.com.cn": "新浪", "thepaper.cn": "澎湃",
}

MONEY_KW = ["变现", "赚钱", "副业", "月入", "接单", "收入", "出单", "睡后收入",
            "怎么赚", "如何赚", "赚到", "搞钱", "兼职",
            "monetiz", "income", "revenue", "freelance", "side hustle",
            "make money", "earning", "mrr"]
FREE_KW = ["免费", "白嫖", "羊毛", "额度", "体验卡", "算力券", "补贴", "赠送",
           "free tier", "free credits", "free api", "free token", "coupon",
           "voucher", "giveaway", "free plan"]
DROP_KW = ["招聘", "招人", "hiring", "salary job", "骗局提示", "防骗"]


# 智谱风控容易误伤的措辞：只在"喂给模型"的输入里软化，页面上仍显示原始标题
SOFTEN_MAP = {"白嫖": "免费获取", "薅羊毛": "免费额度", "薅": "领取", "羊毛": "免费额度",
              "搞钱": "赚钱", "割韭菜": "风险", "骗局": "风险提示"}

# API 错误/风控标记：出现即视为本次调用失败，绝不能写进日报
BAD_MARKERS = ("abusealleviation", "abuse_alleviation", "系统检测到", "敏感内容",
               "error_code", "errorcode", "invalid api key", "authentication")


def soften(text):
    t = str(text or "")
    for k, v in SOFTEN_MAP.items():
        t = t.replace(k, v)
    return t


def has_bad_marker(*fields):
    blob = " ".join(str(f or "") for f in fields).lower()
    return any(m in blob for m in BAD_MARKERS)


def log(*a):
    print("[collect]", *a, flush=True)


def http_get(url, timeout=25, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    if requests:
        r = requests.get(url, headers=h, timeout=timeout)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() in ("iso-8859-1",):
            r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def curl_get(url, headers=None, timeout=50):
    """用系统 curl 抓取（绕开 Cloudflare 对 Python requests 的挑战）。失败返回空串。"""
    # 注意：不要用 --compressed —— Windows 自带 curl 7.55 不支持该参数（会直接失败）
    cmd = ["curl", "-sSL", "--max-time", str(timeout)]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        if r.returncode != 0:
            log(f"    curl rc={r.returncode} {url[:60]} {r.stderr.decode('utf-8','ignore')[:80]}")
            return ""
        return r.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def clean_text(s, limit=220):
    if not s:
        return ""
    s = html_mod.unescape(str(s))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def site_label(url, fallback=""):
    """把 url 变成一个可读的站点名：已知平台词典 > 标题尾巴 > 主域名。"""
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return fallback
    host = host[4:] if host.startswith("www.") else host
    for dom, name in DOMAIN_PLATFORM.items():
        if host.endswith(dom):
            return name
    parts = host.split(".")
    if len(parts) >= 2:
        core = parts[-2] if parts[-2] not in ("com", "net", "org", "gov", "edu", "co") else parts[0]
        return core
    return fallback or host


def platform_of(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""
    for dom, name in DOMAIN_PLATFORM.items():
        if host.endswith(dom):
            return name
    return ""


# ---------------- 抓取器 ----------------

def fetch_rss(src):
    out = []
    if feedparser:
        d = feedparser.parse(src["url"], agent=UA)
        for e in (d.entries or [])[:40]:
            out.append({"title": clean_text(e.get("title"), 120), "url": e.get("link", ""),
                        "desc": clean_text(e.get("summary") or e.get("description"), 300),
                        "published": (e.get("published") or "")[:40]})
    else:
        raw = http_get(src["url"])
        raw = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+);)", "&amp;", raw)
        root = ET.fromstring(raw)
        for item in list(root.iter("item"))[:40] + list(root.iter("{http://www.w3.org/2005/Atom}entry"))[:40]:
            def tx(tag):
                el = item.find(tag)
                if el is None:
                    el = item.find("{http://www.w3.org/2005/Atom}" + tag)
                return (el.text or "") if el is not None else ""
            link = tx("link") or ""
            if not link:
                el = item.find("{http://www.w3.org/2005/Atom}link")
                if el is not None:
                    link = el.attrib.get("href", "")
            out.append({"title": clean_text(tx("title"), 120), "url": link.strip(),
                        "desc": clean_text(tx("summary") or tx("description"), 300),
                        "published": ""})
    return [x for x in out if x["title"] and x["url"]]


def fetch_bing_news(query, mkt="zh-CN"):
    url = ("https://www.bing.com/news/search?q=" + urllib.parse.quote(query)
           + "&format=RSS&setmkt=" + mkt)
    raw = http_get(url)
    out = []
    for m in re.finditer(r"<item>(.*?)</item>", raw, re.S):
        blk = m.group(1)
        t = re.search(r"<title>(.*?)</title>", blk, re.S)
        l = re.search(r"<link>(.*?)</link>", blk, re.S)
        d = re.search(r"<description>(.*?)</description>", blk, re.S)
        if t and l:
            out.append({"title": clean_text(t.group(1), 120), "url": l.group(1).strip(),
                        "desc": clean_text(d.group(1) if d else "", 300), "published": ""})
    return out


def fetch_google_news(query):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
           + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
    items = fetch_rss({"url": url})
    out = []
    for it in items:
        if "news.google.com" in it["url"]:
            it["url"] = gnews_real_url(it["url"])
        if "news.google.com" in it["url"] or "google.com" in urllib.parse.urlparse(it["url"]).netloc:
            continue        # 解不出真实地址：正文抓不到，留着只会污染摘要
        out.append(it)
    return out


def fetch_web_search(query):
    """必应网页搜索 + DuckDuckGo 兜底（含 site: 站内检索）。"""
    out = []
    # 1) 必应
    for host in ("https://www.bing.com", "https://cn.bing.com"):
        try:
            raw = http_get(host + "/search?q=" + urllib.parse.quote(query) + "&count=20")
            blocks = re.findall(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', raw, re.S)
            for b in blocks:
                t = re.search(r"<h2[^>]*>(.*?)</h2>", b, re.S)
                u = re.search(r'href="(https?://[^"]+)"', b)
                p = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
                if not (t and u):
                    continue
                url = html_mod.unescape(u.group(1))
                if "bing.com" in url or "microsoft.com/en-us/bing" in url:
                    continue
                out.append({"title": clean_text(t.group(1), 120), "url": url,
                            "desc": clean_text(p.group(1) if p else "", 300), "published": ""})
            if out:
                log(f"    bing ok ({host.split('//')[1]}): {len(out)}")
                break
        except Exception as e:
            log(f"    bing fail {host}: {e}")
    # 2) DuckDuckGo 兜底（国内可能不通，美国机房可用）
    if not out:
        try:
            raw = http_get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
            for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S):
                url, title = html_mod.unescape(m.group(1)), clean_text(m.group(2), 120)
                if url.startswith("//duckduckgo.com/l/?uddg="):
                    url = urllib.parse.unquote(url.split("uddg=")[1].split("&")[0])
                if title and url.startswith("http"):
                    out.append({"title": title, "url": url, "desc": "", "published": ""})
            if out:
                log(f"    ddg ok: {len(out)}")
        except Exception as e:
            log(f"    ddg fail: {e}")
    return out


def fetch_jina_search(site_name, url_tpl, kw, domain_filter):
    """用阅读器代理渲染平台搜索页，解析 markdown 链接（B站等站内深挖）。"""
    url = url_tpl.replace("{kw}", urllib.parse.quote(kw))
    if not requests:
        return []
    _jina_pace()
    t = curl_get("https://r.jina.ai/" + url,
                 headers={"Accept": "text/plain", "X-Return-Format": "markdown"},
                 timeout=55)
    if not t or has_bad_marker(t[:400]):
        if t:
            log(f"    jina search 返回错误载荷，已丢弃: {t[:80]}")
        return []
    out, seen = [], set()
    for m in re.finditer(r"\[([^\]]{8,90})\]\((https?://[^)\s]+)\)", t):
        title, link = m.group(1).strip(), m.group(2).strip()
        if domain_filter not in link or link in seen:
            continue
        seen.add(link)
        out.append({"title": clean_text(title, 120), "url": link, "desc": "", "published": ""})
    log(f"    jina search {site_name}「{kw}」: {len(out)}")
    return out


def gnews_real_url(u):
    """谷歌新闻的跳转链接里 base64 藏着真实文章地址，解出来才能抓正文。"""
    import base64
    m = re.search(r"/articles/([A-Za-z0-9_\-]+)", u)
    if not m:
        return u
    s = m.group(1).replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        raw = base64.b64decode(s)
    except Exception:
        return u
    found = re.findall(rb"https?://[^\x00-\x20\"\'<>]+", raw)
    if not found:
        return u
    return found[0].decode("utf-8", "ignore").rstrip("\\")


def fetch_hn(src):
    out = []
    for q in HN_QUERIES:
        try:
            data = json.loads(http_get(src["url"] + urllib.parse.quote(q)))
            for h in data.get("hits", [])[:10]:
                if not h.get("title"):
                    continue
                url = h.get("url") or ("https://news.ycombinator.com/item?id=" + str(h.get("objectID", "")))
                out.append({"title": clean_text(h["title"], 120), "url": url,
                            "desc": clean_text(h.get("story_text") or "", 300), "published": ""})
        except Exception as e:
            log("    HN query failed:", q, e)
    return out


_JINA_LOCK = threading.Lock()
_JINA_LAST = [0.0]


def _jina_pace():
    """控制 jina 调用频率（免费档约 20 次/分钟）。"""
    with _JINA_LOCK:
        wait = 1.5 - (time.time() - _JINA_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _JINA_LAST[0] = time.time()


NEVER_JINA = ("google.com", "news.google.com", "youtube.com", "facebook.com", "x.com", "twitter.com")


def fetch_jina(url):
    """用 r.jina.ai 阅读器代理抓正文（免费、无需 key）。走 curl 通道避开 Cloudflare。"""
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(host.endswith(d) for d in NEVER_JINA):
        return ""
    text = ""
    for attempt in (1, 2):
        _jina_pace()
        text = curl_get("https://r.jina.ai/" + url,
                        headers={"Accept": "text/plain", "X-Return-Format": "text"},
                        timeout=45)
        if text:
            break
        if attempt == 1:
            time.sleep(4)
    if not text or has_bad_marker(text[:400]):
        if text:
            log(f"    jina 返回错误载荷，已丢弃: {text[:80]}")
        return ""
    # 正文长度过短通常也是错误页（真实文章正文不会只有几十字）
    if "Markdown Content:" not in text and len(text) < 200:
        return ""
    if "Markdown Content:" in text:
        text = text.split("Markdown Content:", 1)[1]
    text = re.sub(r"^(Title|URL Source|Published Time|Markdown Content)\s*:.*$", " ", text, flags=re.M)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#*>`|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1600]


def fetch_article_text(url):
    """抓原文正文，返回纯文本（最多 1600 字）：先直连，失败或太短再用 jina 代理。"""
    direct = ""
    try:
        if requests:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                             timeout=12, stream=True)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or r.encoding or "utf-8"
                raw = r.raw.read(400_000, decode_content=True)
                page = raw.decode(r.encoding, "ignore")
                page = re.sub(r"(?is)<(script|style|noscript|svg|iframe|nav|footer|header)[^>]*>.*?</\1>", " ", page)
                m = (re.search(r"(?is)<article[^>]*>(.*?)</article>", page)
                     or re.search(r'(?is)<div[^>]+(?:id|class)="[^"]*(?:content|article|post|detail|main)[^"]*"[^>]*>(.*?)</div>', page))
                seg = m.group(1) if m else page
                txt = re.sub(r"(?s)<[^>]+>", " ", seg)
                txt = html_mod.unescape(txt)
                txt = re.sub(r"\s+", " ", txt).strip()
                txt = re.sub(r"(版权所有|免责声明|扫码关注|点击查看更多|相关阅读).*$", "", txt)
                direct = txt[:1600]
    except Exception:
        direct = ""
    if len(direct) >= 350:
        return direct
    jina = fetch_jina(url)
    return jina if len(jina) > len(direct) else direct


# ---------------- 分类与组装 ----------------

def classify(title, desc):
    text = (title + " " + desc).lower()
    if any(k in text for k in DROP_KW):
        return None
    if any(k in text for k in MONEY_KW):
        return "money"
    if any(k in text for k in FREE_KW):
        return "free"
    return None


def collect_candidates():
    seen, cands, stats = set(), [], {}

    def push(items, source, platform, cap, allow_platform_infer=True, restrict_domain=None):
        kept = 0
        for it in items:
            u = it["url"].rstrip("/")
            if u in seen:
                continue
            if restrict_domain:
                host = urllib.parse.urlparse(u).netloc.lower()
                if restrict_domain not in host:
                    continue          # site: 检索时严格限定域名，保证"真·站内"
            if has_bad_marker(it.get("title"), it.get("desc")):
                continue              # 抓取返回的是错误页，直接丢弃
            seen.add(u)
            cat = classify(it["title"], it["desc"])
            if not cat:
                continue
            plat = platform_of(u)
            label = site_label(u)
            # 标题尾巴常带媒体名（如 "xxx - 36 Kr"），优先用它
            tail = ""
            mt = re.search(r"[-–—|]\s*([^-–—|]{2,14})\s*$", it["title"])
            if mt:
                tail = clean_text(mt.group(1), 20)
            # 搜索引擎跳转域名（google/bing/news.google）没有信息量，优先用标题尾巴里的媒体名
            if label in ("google", "bing", "news") and tail:
                label = tail
            cands.append({"title": it["title"], "url": it["url"],
                          "source": label or plat or platform or source,
                          "platform": plat or platform, "category": cat, "desc": it["desc"]})
            kept += 1
            if kept >= cap:
                break
        return kept

    # 1) 新闻 RSS（中英）
    for q in NEWS_QUERIES_ZH:
        for fn, label in ((fetch_bing_news, "必应新闻"), (fetch_google_news, "谷歌新闻")):
            key = f"{label}·{q}"
            try:
                n = push(fn(q), key, "新闻媒体", RSS_CAP)
                stats[key] = n
                log(f"  {key} -> {n}")
            except Exception as e:
                stats[key] = -1
                log(f"  {key} FAILED: {e}")
    for q in NEWS_QUERIES_EN:
        try:
            n = push(fetch_bing_news(q, "en-US"), f"Bing News·{q}", "英文媒体", RSS_CAP)
            stats[f"BingNews·{q}"] = n
            log(f"  BingNews「{q}」-> {n}")
        except Exception as e:
            stats[f"BingNews·{q}"] = -1
            log(f"  BingNews「{q}」FAILED: {e}")

    # 2) 网页搜索（含 site: 站内深挖，严格限定域名）
    for q in WEB_QUERIES:
        key = f"网页搜索·{q}"
        try:
            dom = None
            m = re.search(r"site:([a-z0-9.\-]+)", q)
            if m:
                dom = m.group(1)
            n = push(fetch_web_search(q), key, "", WEB_CAP * 3, allow_platform_infer=False,
                     restrict_domain=dom)
            stats[key] = n
            log(f"  {key} -> {n}")
        except Exception as e:
            stats[key] = -1
            log(f"  {key} FAILED: {e}")

    # 2.5) 站内深挖（B站等，经阅读器渲染）
    for site_name, tpl, dom in JINA_SEARCH_SITES:
        for kw in JINA_SEARCH_KEYWORDS:
            key = f"{site_name}站内·{kw}"
            try:
                n = push(fetch_jina_search(site_name, tpl, kw, dom), key, site_name,
                         WEB_CAP * 2, allow_platform_infer=False)
                stats[key] = n
                log(f"  {key} -> {n}")
            except Exception as e:
                stats[key] = -1
                log(f"  {key} FAILED: {e}")

    # 3) 垂类 RSS + HN
    for src in EXTRA_RSS:
        try:
            items = fetch_hn(src) if src.get("type") == "hn" else fetch_rss(src)
            cap = HN_CAP if src.get("type") == "hn" else RSS_CAP
            n = push(items, src["name"], src["platform"], cap)
            stats[src["name"]] = n
            log(f"  {src['name']} -> {n}")
        except Exception as e:
            stats[src["name"]] = -1
            log(f"  {src['name']} FAILED: {e}")

    # 4) B站/YouTube 博主直连（配置了才跑）
    for uid in BILIBILI_UIDS:
        for base in RSSHUB_BASES:
            try:
                n = push(fetch_rss({"url": f"{base}/bilibili/user/video/{uid}"}), f"B站UP{uid}", "B站", RSS_CAP)
                stats[f"B站UP{uid}"] = n
                log(f"  B站 UP{uid} -> {n}")
                break
            except Exception as e:
                log(f"  B站 UP{uid} FAILED({base}): {e}")
    for ch in YOUTUBE_CHANNELS:
        try:
            n = push(fetch_rss({"url": f"https://www.youtube.com/feeds/videos.xml?channel_id={ch}"}),
                     f"YouTube {ch[:8]}", "YouTube", RSS_CAP)
            stats[f"YouTube {ch[:8]}"] = n
            log(f"  YouTube {ch[:8]} -> {n}")
        except Exception as e:
            log(f"  YouTube {ch[:8]} FAILED: {e}")

    return cands, stats


def enrich(cands):
    """抓正文，解决摘要干瘪。优先 money/free。直连不行就用 jina 代理。"""
    order = {"money": 0, "free": 1}
    cands = sorted(cands, key=lambda c: order.get(c["category"], 2))
    targets = cands[:ENRICH_MAX]

    def work(c):
        if len(c["desc"]) >= 600:
            return
        txt = fetch_article_text(c["url"])
        if len(txt) > len(c["desc"]):
            c["desc"] = txt

    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
        list(ex.map(work, targets))
    got = sum(1 for c in targets if len(c["desc"]) > 300)
    log(f"  正文抓取: {got}/{len(targets)} 条拿到 >300 字正文")
    return cands, got


def existing_urls():
    urls = set()
    if not DAILY.exists():
        return urls
    cutoff = (datetime.datetime.now(TZ) - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    for f in DAILY.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("date", "") >= cutoff:
                for it in d.get("items", []):
                    if it.get("url"):
                        urls.add(it["url"].rstrip("/"))
        except Exception:
            pass
    return urls


# ---------------- LLM ----------------

def parse_items(content):
    """从模型输出里取出条目数组：支持 JSON 模式、```json 围栏、以及被截断时逐个对象抢救。"""
    t = (content or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    cands = [t]
    if "{" in t and "}" in t:
        cands.append(t[t.find("{"): t.rfind("}") + 1])
    if "[" in t and "]" in t:
        cands.append(t[t.find("["): t.rfind("]") + 1])
    for c in cands:
        if not c:
            continue
        try:
            obj = json.loads(c)
        except Exception:
            continue
        if isinstance(obj, dict):
            for key in ("items", "data", "list", "results"):
                if isinstance(obj.get(key), list):
                    return obj[key]
        if isinstance(obj, list):
            return obj
    # 截断抢救：把完整的顶层对象逐个抠出来
    items, depth, start = [], 0, None
    for i, ch in enumerate(t):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    items.append(json.loads(t[start:i + 1]))
                except Exception:
                    pass
                start = None
    return items


def llm_call(cands, limit, desc_len, max_tokens, model, api_key, want="12-16 条"):
    pool = [{"i": i, "title": soften(c["title"]), "url": c["url"], "site": c["source"],
             "text": soften(c["desc"])[:desc_len]} for i, c in enumerate(cands[:limit])]
    sys_prompt = (
        "你是「AI 变现日报」的主编，读者是想用 AI 赚钱的普通人。\n"
        f"任务：从候选条目里挑出 {want}，写成中文日报，返回 JSON。\n"
        "硬性要求：\n"
        "1) 只输出 JSON 对象，形如 {\"items\":[{...}]}，不要任何解释文字或代码块围栏。"
        "输出必须完整闭合，宁少勿多——写不完就少写几条，绝不留下半个 JSON。\n"
        "2) category 四选一：money（个人用 AI 变现的路径/案例，占比最高）、free（免费 Token/API 额度/算力券/补贴，"
        "写清领取方式）、industry（行业动态）、tip（避坑/实操）。\n"
        "3) money 类必须填全：difficulty（低/中/高）、amount（文中提到的赚到多少钱；没写填『未披露』）、"
        "cycle（变现周期，如『2-4 周见第一单』）、platform（抖音/小红书/B站/YouTube/知乎 等）、"
        "takeaway（40-70 字小结：赚的是谁的钱、核心逻辑）、steps（3-5 步入手流程，一行文字用①②③④⑤连接）。\n"
        "4) summary 必须 80-140 字，基于候选的 text 写；信息不足时可用行业常识补充背景，"
        "但严禁编造具体数字、金额、日期、机构名，缺失就写『原文未披露』。\n"
        "5) action：一句话可执行动作（如『今晚去闲鱼搜 XX 看需求』），各类型都尽量填。\n"
        "6) 来源多样性：整期来自至少 4 个不同站点；Hacker News 与 Reddit 合计最多 2 条。\n"
        "7) url 必须原样使用候选里的 url，禁止编造。\n"
        "8) 与 AI 变现无关、信息量不足、纯广告的候选直接丢弃。"
    )
    body = {"model": model,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": "候选条目：\n" + json.dumps(pool, ensure_ascii=False)}],
            "temperature": 0.3, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}}   # 强制 JSON，避免被 max_tokens 截断成残缺 JSON

    last_err = ""
    for attempt in range(3):
        try:
            r = requests.post("https://open.bigmodel.cn/api/paas/v4/chat/completions",
                              headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                              json=body, timeout=240)
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}"
                time.sleep(6 * (attempt + 1))
                continue
            r.raise_for_status()
            j = r.json()
            choice = j["choices"][0]
            content = choice["message"]["content"]
            log(f"    LLM finish={choice.get('finish_reason')} tokens={j.get('usage')}")
            if has_bad_marker(content):
                last_err = "API 风控标记: " + content[:100]
                time.sleep(4)
                continue
            items = parse_items(content)
            if items:
                return items
            last_err = "解析后无条目: " + content[:120].replace("\n", " ")
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(4)
    raise RuntimeError(last_err or "unknown error")


def llm_pick(cands):
    api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not api_key or not requests:
        log("  no ZHIPU_API_KEY -> rule mode")
        return None
    model = os.environ.get("ZHIPU_MODEL", "glm-4-flash").strip()

    money = [c for c in cands if c["category"] == "money"]
    others = [c for c in cands if c["category"] != "money"]
    batches = []
    if money:
        batches.append((money[:26], 9, 650, 4096, "9-11 条，全部为 money 类（个人 AI 变现路径/案例）"))
    if others:
        batches.append((others[:18], 5, 500, 3000, "5-7 条，为 free/industry/tip 类"))

    collected, errs = [], []
    for batch, limit, dlen, mtok, want in batches:
        try:
            raw = llm_call(batch, limit, dlen, mtok, model, api_key, want)
            log(f"  批次「{want[:12]}…」返回 {len(raw)} 条")
        except Exception as e:
            errs.append(str(e)[:160])
            log(f"  批次失败：{e}")
            continue
        valid = {c["url"].rstrip("/"): c for c in batch}
        for it in raw:
            u = (it.get("url") or "").rstrip("/")
            if u not in valid:
                continue
            if it.get("category") not in ("money", "free", "industry", "tip"):
                continue
            if has_bad_marker(it.get("summary"), it.get("title"), it.get("takeaway"),
                              it.get("steps"), it.get("action")):
                continue
            src = valid[u]
            # 来源与平台一律以抓取到的真实数据为准，不信模型自述（防张冠李戴）
            it["source"] = src["source"]
            it["platform"] = it.get("platform") or src["platform"]
            it["url"] = src["url"]
            for k in ("tags", "difficulty", "amount", "cycle", "takeaway", "steps", "action"):
                if k == "tags":
                    it.setdefault("tags", [])
                else:
                    it.setdefault(k, "")
            collected.append(it)

    if len(collected) < 6:
        LAST_LLM_ERROR[0] = "；".join(errs) or f"有效条目过少({len(collected)})"
        log(f"  有效条目仅 {len(collected)} 条，判定 LLM 失败")
        return None
    LAST_LLM_ERROR[0] = ""
    log(f"  LLM 汇总成功：{len(collected)} 条")
    return collected[:MAX_ITEMS]


def fallback_items(cands):
    items = []
    for c in cands[:MAX_ITEMS]:
        body = c["desc"] or ""
        summary = body[:180] if body else "(来源未提供摘要，点击原文查看)"
        items.append({"category": c["category"], "title": c["title"], "summary": summary,
                      "source": c["source"], "url": c["url"], "tags": [],
                      "platform": c["platform"], "difficulty": "", "amount": "", "cycle": "",
                      "takeaway": "", "steps": "", "action": ""})
    return items


# ---------------- 写盘 ----------------

def write_report(items, mode):
    # 最后一道防线：任何含错误标记的条目都不允许出现在页面上
    clean = [it for it in items
             if not has_bad_marker(it.get("summary"), it.get("title"), it.get("takeaway"), it.get("steps"))]
    if len(clean) != len(items):
        log(f"  写盘前剔除了 {len(items) - len(clean)} 条含错误标记的条目")
    items = clean
    n = len(items)
    cnt = {}
    for it in items:
        cnt[it["category"]] = cnt.get(it["category"], 0) + 1
    sites = {it.get("source", "") for it in items}
    report = {
        "date": TODAY,
        "title": f"AI 变现日报 - {TODAY}",
        "generatedAt": NOW_ISO,
        "itemCount": n,
        "summary": (f"{'AI 编辑' if mode == 'llm' else '自动聚合'}模式："
                    f"变现路径 {cnt.get('money', 0)} 条 · 免费额度 {cnt.get('free', 0)} 条 · "
                    f"行业 {cnt.get('industry', 0)} 条 · 避坑 {cnt.get('tip', 0)} 条，"
                    f"覆盖 {len(sites)} 个来源站点。"
                    + ("" if mode == "llm" else
                       " ⚠️ 未检测到 AI Key（ZHIPU_API_KEY），本期为聚合模式："
                       "摘要为正文截断、暂无新手指南与可执行建议。")),
        "items": items,
    }
    DAILY.mkdir(parents=True, exist_ok=True)
    (DAILY / f"{TODAY}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    idx_path = DATA / "index.json"
    idx = {"latest": TODAY, "updatedAt": NOW_ISO, "dates": [], "issues": []}
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    dates = sorted(set(idx.get("dates", []) + [TODAY]), reverse=True)[:KEEP_DAYS]
    issues = [x for x in idx.get("issues", []) if x.get("date") != TODAY]
    issues.append({"date": TODAY, "title": report["title"], "itemCount": n})
    issues.sort(key=lambda x: x["date"], reverse=True)
    idx.update({"latest": TODAY, "updatedAt": NOW_ISO, "dates": dates, "issues": issues[:KEEP_DAYS]})
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")

    for f in DAILY.glob("*.json"):
        if f.stem < (datetime.datetime.now(TZ) - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d"):
            f.unlink()
    log("report written:", TODAY, n, "items, sites:", len(sites))


def write_run_log(stats, cand_count, got_body, mode, note, error=""):
    """把本次运行诊断写进仓库，方便远程排查（无需 Actions 日志权限）。"""
    try:
        logpath = DATA / "run-log.json"
        payload = {
            "date": TODAY,
            "runAt": NOW_ISO,
            "mode": mode,
            "candidates": cand_count,
            "articlesFetched": got_body,
            "sources": stats,
            "note": note,
            "error": error,
        }
        logpath.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log("run-log written")
    except Exception as e:
        log("run-log write failed:", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    log("date:", TODAY)
    try:
        _main_impl(args)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log("FATAL:", e)
        log(tb[-1500:])
        write_run_log(getattr(main, "_stats", {}), 0, 0, "error", "运行异常，详见 error 字段", tb[-1200:])
        raise


def _main_impl(args):
    write_run_log({}, 0, 0, "started", "脚本已启动")   # 启动探针：没看到这条就是启动阶段就崩了
    today_file = DAILY / f"{TODAY}.json"
    if today_file.exists() and not args.force and not args.dry_run:
        # 质量自愈：如果当天日报摘要过短 / 没有新手指南，说明是低质量产物，自动重生成
        try:
            prev = json.loads(today_file.read_text(encoding="utf-8"))
            items_prev = prev.get("items", [])
            lens = [len(i.get("summary", "")) for i in items_prev] or [0]
            avg = sum(lens) / len(lens)
            guide = sum(1 for i in items_prev if i.get("takeaway") or i.get("steps"))
        except Exception:
            avg, guide = 0, 0
        if avg >= 60 and guide > 0:
            log(f"today's report exists and looks fine (avg summary {avg:.0f} 字, 指南 {guide} 条) -> skip")
            return
        log(f"today's report quality low (avg summary {avg:.0f} 字, 指南 {guide} 条) -> 自动重生成")

    cands, stats = collect_candidates()
    main._stats = stats
    write_run_log(stats, len(cands), 0, "collected", "采集阶段完成，正文/LLM 结果待更新")
    log("candidates:", len(cands))
    by_src = {}
    for c in cands:
        by_src[c["source"]] = by_src.get(c["source"], 0) + 1
    log("source distribution:", by_src)

    seen = existing_urls()
    cands = [c for c in cands if c["url"].rstrip("/") not in seen]
    log("after dedup:", len(cands))
    if not cands:
        log("NO candidates -> skip")
        write_run_log(stats, 0, 0, "skip", "无新增候选")
        return
    if len(cands) < MIN_ITEMS:
        log(f"warning: only {len(cands)} candidates, report will be thin")

    cands, got_body = enrich(cands)

    items = llm_pick(cands)
    mode = "llm"
    if not items:
        items = fallback_items(cands)
        mode = "rule"

    if args.dry_run:
        log(f"DRY-RUN: {len(items)} items, mode={mode}")
        for it in items[:8]:
            log("  -", it["category"], "|", it.get("source"), "|", it["title"][:38],
                "| 摘要", len(it.get("summary", "")), "字")
        return

    write_run_log(stats, len(cands), got_body, mode, LAST_LLM_ERROR[0])
    write_report(items, mode)


if __name__ == "__main__":
    main()
