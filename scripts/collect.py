#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日资讯自动采集脚本（GitHub Actions 上运行，无需本机开机）

流程：
  1. 抓取公开源（RSS / Hacker News API 等），按关键词预筛出候选条目
  2. 与近 7 天已发布条目去重
  3. 有 ZHIPU_API_KEY → 调用智谱 GLM-4-Flash（永久免费档）做分类、去噪、中文摘要拆解
     无 key / 调用失败 → 规则分类兜底（真实条目 + 截断摘要，不编造任何内容）
  4. 写入 site/data/daily/YYYY-MM-DD.json 并更新 site/data/index.json（倒序，保留 30 期）

用法：
  python scripts/collect.py            # 正式运行
  python scripts/collect.py --dry-run  # 只抓取与统计，不写文件
"""
import os
import re
import json
import html
import argparse
import datetime
import pathlib
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    requests = None
    urllib.request.DEFAULT_TIMEOUT = 30

try:
    import feedparser
except ImportError:
    feedparser = None

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
DATA = SITE / "data"
DAILY = DATA / "daily"

TZ = datetime.timezone(datetime.timedelta(hours=8))  # Asia/Shanghai
TODAY = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
NOW_ISO = datetime.datetime.now(TZ).isoformat(timespec="seconds")
KEEP_DAYS = 30
MAX_ITEMS = 20
PER_SOURCE_CAP = 10          # 普通 RSS 每源最多入池条数
HN_CAP = 4                   # HN 降权：每期最多入池条数
MIN_ITEMS = 8                # 少于此数时视为"内容不足"，仍生成但不重复补旧

# ---------------- 数据源（全部公开免费，无需 key） ----------------
# 中文社媒（抖音/小红书）没有公开合规 RSS，靠必应/谷歌新闻的关键词索引间接覆盖其内容；
# B站/YouTube 可在下面 BILIBILI_UIDS / YOUTUBE_CHANNELS 里填 ID 直连。

# 新闻搜索 RSS：按关键词抓全球媒体报道（含大量社媒博主变现内容的转载页）
NEWS_QUERIES_ZH = [
    "AI 副业 变现", "AI 赚钱 案例", "AI 变现 博主",
    "免费 token 额度 领取", "大模型 免费额度", "AI 羊毛 白嫖",
]
NEWS_QUERIES_EN = [
    "AI side hustle income", "free LLM API credits", "creator AI monetization",
]

# 想直连 B站 / YouTube 的博主：把 ID 填进来即可（可留空）
# B站: 在 UP 主主页链接 space.bilibili.com/后面的数字；YouTube: 频道页源码里搜 "channel_id" 得到 UC 开头的 ID
BILIBILI_UIDS = []          # 例: ["9469745"]
YOUTUBE_CHANNELS = []       # 例: ["UCXuqSBlHAE6Xw-yeJA0Tunw"]

RSSHUB_BASES = ["https://rsshub.app", "https://rss.injahow.cn"]


def _news_sources():
    out = []
    for q in NEWS_QUERIES_ZH:
        out.append({"name": f"必应新闻·{q}", "platform": "中文媒体", "type": "rss",
                    "url": "https://www.bing.com/news/search?q=" + urllib.parse.quote(q) + "&format=RSS&setmkt=zh-CN"})
        out.append({"name": f"谷歌新闻·{q}", "platform": "中文媒体", "type": "rss",
                    "url": "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"})
    for q in NEWS_QUERIES_EN:
        out.append({"name": f"Bing News·{q}", "platform": "英文媒体", "type": "rss",
                    "url": "https://www.bing.com/news/search?q=" + urllib.parse.quote(q) + "&format=RSS&setmkt=en-US"})
    return out


def _social_sources():
    out = []
    for uid in BILIBILI_UIDS:
        for base in RSSHUB_BASES:
            out.append({"name": f"B站UP{uid}", "platform": "B站", "type": "rss",
                        "url": f"{base}/bilibili/user/video/{uid}"})
            break
    for ch in YOUTUBE_CHANNELS:
        out.append({"name": f"YouTube{ch[:8]}", "platform": "YouTube", "type": "rss",
                    "url": f"https://www.youtube.com/feeds/videos.xml?channel_id={ch}"})
    return out


SOURCES = _news_sources() + _social_sources() + [
    {"name": "量子位", "platform": "公众号/网站", "type": "rss", "url": "https://www.qbitai.com/feed"},
    {"name": "少数派", "platform": "网站", "type": "rss", "url": "https://sspai.com/feed"},
    {"name": "爱范儿", "platform": "网站", "type": "rss", "url": "https://www.ifanr.com/feed"},
    {"name": "Hacker News", "platform": "Hacker News", "type": "hn",
     "url": "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=30&query="},
    {"name": "Reddit r/SideProject", "platform": "Reddit", "type": "rss", "url": "https://www.reddit.com/r/SideProject/.rss"},
]

# HN 用多组关键词查询，提高命中
HN_QUERIES = [
    "AI side hustle", "free credits", "indie AI revenue",
]

MONEY_KW = ["变现", "赚钱", "副业", "月入", "接单", "收入", "出单", "涨粉变现",
            "monetiz", "income", "revenue", "freelance", "side hustle",
            "make money", "earning", "sold", "mrr", "arpu"]
FREE_KW = ["免费", "白嫖", "羊毛", "额度", "体验卡", "算力券", "补贴", "送", "赠送",
           "free tier", "free credits", "free api", "free token", "coupon",
           "voucher", "giveaway", "waive", "free plan"]
DROP_KW = ["招聘", "salary job", "hiring"]


def log(*a):
    print("[collect]", *a, flush=True)


def http_get(url, timeout=25):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ai-news-bot/1.0",
        "Accept": "*/*",
    }
    if requests:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def clean(s, limit=220):
    if not s:
        return ""
    s = html.unescape(str(s))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def fetch_rss(src):
    out = []
    if feedparser:
        d = feedparser.parse(src["url"])
        entries = d.entries or []
        for e in entries[:40]:
            out.append({
                "title": clean(e.get("title"), 120),
                "url": e.get("link", ""),
                "desc": clean(e.get("summary") or e.get("description"), 220),
                "published": (e.get("published") or "")[:40],
            })
    else:
        raw = http_get(src["url"])
        root = ET.fromstring(re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+);)", "&amp;", raw))
        for item in root.iter("item"):
            t = item.findtext("title") or ""
            l = item.findtext("link") or ""
            de = item.findtext("description") or ""
            out.append({"title": clean(t, 120), "url": l.strip(),
                        "desc": clean(de, 220), "published": ""})
    return [x for x in out if x["title"] and x["url"]]


def fetch_hn(src):
    out = []
    for q in HN_QUERIES:
        try:
            txt = http_get(src["url"] + urllib.parse.quote(q))
            data = json.loads(txt)
            for h in data.get("hits", [])[:10]:
                url = h.get("url") or ("https://news.ycombinator.com/item?id=" + str(h.get("objectID", "")))
                if not h.get("title"):
                    continue
                out.append({
                    "title": clean(h["title"], 120),
                    "url": url,
                    "desc": clean(h.get("story_text") or (h.get("comment_text") or ""), 220),
                    "published": (h.get("created_at") or "")[:40],
                })
        except Exception as e:
            log("HN query failed:", q, e)
    return out


def classify(title, desc):
    """无 LLM 时的规则分类。返回 category 或 None。"""
    text = (title + " " + desc).lower()
    if any(k in text for k in DROP_KW):
        return None
    if any(k in text for k in MONEY_KW):
        return "money"
    if any(k in text for k in FREE_KW):
        return "free"
    return None


def collect_candidates():
    seen_urls = set()
    candidates = []
    for src in SOURCES:
        try:
            if src["type"] == "rss":
                items = fetch_rss(src)
            else:
                items = fetch_hn(src)
            cap = HN_CAP if src["type"] == "hn" else PER_SOURCE_CAP
            kept = 0
            for it in items:
                key = it["url"].rstrip("/")
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                cat = classify(it["title"], it["desc"])
                if not cat:
                    continue
                candidates.append({
                    "title": it["title"], "url": it["url"],
                    "source": src["name"], "platform": src["platform"],
                    "category": cat, "desc": it["desc"],
                })
                kept += 1
                if kept >= cap:
                    break
            log(f"source {src['name']}: {len(items)} fetched, {kept} candidates")
        except Exception as e:
            log(f"source {src['name']} FAILED: {e}")
    return candidates


def existing_urls():
    """近 7 天已发布的 url，用于去重。"""
    urls = set()
    if not DAILY.exists():
        return urls
    for f in DAILY.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("date", "") >= (datetime.datetime.now(TZ) - datetime.timedelta(days=7)).strftime("%Y-%m-%d"):
                for it in d.get("items", []):
                    if it.get("url"):
                        urls.add(it["url"].rstrip("/"))
        except Exception:
            pass
    return urls


def llm_pick(candidates):
    """调用 GLM-4-Flash 从候选中挑选并生成日报条目。失败返回 None。"""
    api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not api_key or not requests:
        return None
    model = os.environ.get("ZHIPU_MODEL", "glm-4-flash").strip()

    pool = []
    for i, c in enumerate(candidates):
        pool.append({"i": i, "title": c["title"], "url": c["url"],
                     "source": c["source"], "desc": c["desc"][:150]})

    sys_prompt = (
        "你是「AI 变现日报」的编辑。站点面向想用 AI 赚钱的个人/独立创作者，"
        "严禁把企业级/公司主体的商业化新闻当成个人变现路径。\n"
        "从候选条目中挑选 12-20 条生成日报，规则：\n"
        "1. category 四选一：money(个人用AI变现的路径/案例，占比最高，优先选中文媒体和社媒博主内容)、"
        "free(免费Token/API额度/算力券/补贴，注明截止日期)、industry(行业动态)、tip(避坑实操)。\n"
        "2. money 类必填：difficulty(低/中/高)、amount(博主已赚金额区间，如 $300-2000/月，原文没披露就写'未披露，同类约 …')、"
        "cycle(变现周期如 2-4 周)、takeaway(变现方式小结，40-70字，讲清赚的是谁的钱)、"
        "steps(新手入手步骤，3-5步一行文字，用①②③④⑤连接)。没有依据的数字保守估计，禁止夸大。\n"
        "3. 来源配额：Hacker News / Reddit 的条目全场合计最多 2 条，其余名额给中文来源。\n"
        "4. summary 用中文 80-160 字，保留原文关键数字；title 用中文重写，不超 30 字。\n"
        "5. url 必须原样使用候选里的 url，禁止编造。\n"
        "6. 只输出 JSON 数组，不要输出任何其他文字。每项字段："
        '{"category","title","summary","source","url","tags":[],"platform","difficulty","amount","cycle","takeaway","steps"}'
        "，其中非 money 类的 platform/difficulty/amount/cycle/takeaway/steps 可为空字符串。\n"
        "7. 信息量不足或与主题无关的候选直接丢弃。"
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "候选条目：\n" + json.dumps(pool, ensure_ascii=False)},
        ],
        "temperature": 0.3,
        "max_tokens": 8000,
    }
    try:
        r = requests.post(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json"},
            json=body, timeout=180,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\[.*\]", content, re.S)
        if not m:
            log("LLM returned no JSON array")
            return None
        items = json.loads(m.group(0))
        valid_urls = {c["url"].rstrip("/") for c in candidates}
        ok = []
        for it in items:
            if not it.get("url") or it["url"].rstrip("/") not in valid_urls:
                continue
            if it.get("category") not in ("money", "free", "industry", "tip"):
                continue
            it.setdefault("tags", [])
            it.setdefault("platform", "")
            it.setdefault("difficulty", "")
            it.setdefault("amount", "")
            it.setdefault("cycle", "")
            it.setdefault("takeaway", "")
            it.setdefault("steps", "")
            ok.append(it)
        log(f"LLM mode: {len(ok)} items accepted")
        return ok or None
    except Exception as e:
        log("LLM call failed:", e)
        return None


def fallback_items(candidates):
    """无 LLM：规则分类 + 截断摘要（内容全部来自真实抓取，不编造）。"""
    items = []
    for c in candidates:
        items.append({
            "category": c["category"],
            "title": c["title"],
            "summary": c["desc"] or "(来源未提供摘要，点击原文查看)",
            "source": c["source"],
            "url": c["url"],
            "tags": [],
            "platform": c["platform"],
            "difficulty": "", "amount": "", "cycle": "",
            "takeaway": "", "steps": "",
        })
    return items[:MAX_ITEMS]


def write_report(items, mode):
    n = len(items)
    cnt = {}
    for it in items:
        cnt[it["category"]] = cnt.get(it["category"], 0) + 1
    report = {
        "date": TODAY,
        "title": f"AI 变现日报 - {TODAY}",
        "generatedAt": NOW_ISO,
        "itemCount": n,
        "summary": (f"云端自动生成（{'AI 编辑' if mode == 'llm' else '聚合'}模式）："
                    f"变现路径 {cnt.get('money', 0)} 条 · 免费额度 {cnt.get('free', 0)} 条 · "
                    f"行业动态 {cnt.get('industry', 0)} 条 · 避坑 {cnt.get('tip', 0)} 条。"),
        "items": items,
    }
    DAILY.mkdir(parents=True, exist_ok=True)
    (DAILY / f"{TODAY}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

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
    issues = issues[:KEEP_DAYS]
    keep = set(dates)
    idx.update({"latest": TODAY, "updatedAt": NOW_ISO,
                "dates": dates, "issues": issues})
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")

    # 清理超过 30 期的旧文件
    if DAILY.exists():
        for f in DAILY.glob("*.json"):
            if f.stem < (datetime.datetime.now(TZ) - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d"):
                f.unlink()

    log("report written:", TODAY, n, "items; dates kept:", len(dates), "keep set:", len(keep))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="当天日报已存在时强制重新生成")
    args = ap.parse_args()

    log("date:", TODAY)

    # 当天已有日报 -> 跳过（防止一天内的第二次定时触发覆盖/重复）
    today_file = DAILY / f"{TODAY}.json"
    if today_file.exists() and not args.force and not args.dry_run:
        log("today's report already exists -> skip (用 --force 可强制重生成)")
        return

    candidates = collect_candidates()
    log("total candidates:", len(candidates))

    seen = existing_urls()
    candidates = [c for c in candidates if c["url"].rstrip("/") not in seen]
    log("after dedup vs last 7 days:", len(candidates))
    if len(candidates) < MIN_ITEMS:
        log(f"warning: candidates < {MIN_ITEMS}, today's report will be thin (真实数据有多少发多少，不凑数)")

    if not candidates:
        log("NO candidates today -> skip (旧一期保留，不生成空日报)")
        return

    items = llm_pick(candidates)
    mode = "llm"
    if not items:
        log("falling back to rule-based mode")
        items = fallback_items(candidates)
        mode = "rule"

    if args.dry_run:
        log("DRY-RUN: would write", len(items), "items, mode =", mode)
        for it in items[:5]:
            log("  -", it["category"], it["title"][:40])
        return

    write_report(items, mode)


if __name__ == "__main__":
    main()
