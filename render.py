# -*- coding: utf-8 -*-
"""render.py — 页面渲染层：舆情看板 / 今日要点 / AI 资讯页 / 海外产品页"""
import html
from datetime import datetime
from typing import Dict, List, Optional

from config import (CN_TZ, NOW, TODAY, TODAY_START, MAX_EVENTS, OUTPUT_HTML,
                    QUOTE_BATCH, BROWSER_HEADERS, KEYWORD_GROUPS, TAG_COLORS,
                    SOURCE_STATS, HIGH_RISK_TAGS, MARKET_CORE_KEYWORDS,
                    MARKET_KEYWORDS, MARKET_GLOBAL_KEYWORDS, BRIEF_GROUP_MAX,
                    HEADLINES_MAX, NAV_ITEMS, WATCHLIST, WATCH_RELATED_MAX,
                    GLOBAL_TAG_RULES, AI_CORE, AI_PRODUCT_VERBS)
from utils import (norm_title, zh_tags, is_ai_item, is_ai_product,
                   dedupe_and_sort, log)
from processor import summarize, build_market_headlines, build_watchlist


def _brief_rows(items: List[dict]) -> str:
    """要点条目渲染：时间 + 一行标题（点击跳原文）+ 来源，精要风格"""
    if not items:
        return '<div class="text-xs text-slate-600 py-3">今日暂无命中要闻词库的快讯</div>'
    return "".join(
        f'<a href="{html.escape(r["url"] if r["url"].startswith(("http://", "https://")) else "#", quote=True)}" '
        f'target="_blank" class="flex items-baseline gap-2 py-1.5 '
        f'border-b border-slate-800/50 last:border-0 hover:bg-slate-800/40 '
        f'rounded px-1 -mx-1 transition-colors" '
        f'title="{html.escape(r["title"] or r["content"][:60])}">'
        f'<span class="font-mono text-[11px] text-slate-500 shrink-0">{r["time"]:%H:%M}</span>'
        f'<span class="text-[13px] text-slate-200 truncate flex-1">'
        f'{html.escape(r["title"] or r["content"][:60])}</span>'
        f'<span class="text-[10px] text-slate-600 shrink-0">{html.escape(r["source"])}</span></a>'
        for r in items
    )


def render_briefing_block(brief: dict) -> str:
    """渲染"今日要点"板块：国内要点 / 海外要点 两栏，页面第一个板块"""
    return f"""
    <section class="mb-6 bg-gradient-to-br from-slate-900 to-slate-900/40 border border-slate-800
                    rounded-xl p-4 sm:p-5">
      <div class="flex items-center gap-2 mb-4">
        <span class="text-amber-400 text-lg">📰</span>
        <h2 class="text-base font-bold text-slate-100">今日要点</h2>
        <span class="text-xs text-slate-500">国内外重大新闻 · 按重要性排序 · 精要速览</span>
      </div>
      <div class="grid md:grid-cols-2 gap-5">
        <div>
          <div class="text-xs font-semibold text-cyan-300/90 mb-2 tracking-wide">国内要点</div>
          {_brief_rows(brief["domestic"])}
        </div>
        <div>
          <div class="text-xs font-semibold text-fuchsia-300/90 mb-2 tracking-wide">海外要点</div>
          {_brief_rows(brief["global"])}
        </div>
      </div>
    </section>
    """


def render_negative_overview(summary: dict) -> str:
    """渲染"负面舆情概览"三栏：跌幅榜 / 事件类型分布 / 重点事件"""
    # —— 栏 1：跌幅榜 ——
    if summary["losers"]:
        loser_rows = "".join(
            f'<a href="#evt-{e["eid"]}" class="flex items-center justify-between gap-2 '
            f'py-1.5 border-b border-slate-800/60 last:border-0 hover:bg-slate-800/40 '
            f'rounded px-1 -mx-1 transition-colors">'
            f'<span class="text-sm text-slate-200 truncate">{e["quote"]["name"]}'
            f'<span class="text-[10px] font-mono text-slate-500 ml-1">{e["stock"]["display"]}</span></span>'
            f'<span class="text-sm font-mono shrink-0 {pct_classes(e["quote"]["pct"])} '
            f'px-1.5 py-0.5 rounded border">{fmt_pct(e["quote"]["pct"])}</span></a>'
            for e in summary["losers"]
        )
    else:
        loser_rows = '<div class="text-xs text-slate-600 py-3">今日暂无带行情标的</div>'

    # —— 栏 2：事件类型分布（CSS 条形） ——
    if summary["tag_rank"]:
        max_count = max(c for _, c in summary["tag_rank"])
        tag_rows = "".join(
            f'<div class="flex items-center gap-2 py-1">'
            f'<span class="text-xs text-slate-400 w-16 shrink-0 text-right">#{html.escape(tag)}</span>'
            f'<div class="flex-1 h-3 bg-slate-800/60 rounded overflow-hidden">'
            f'<div class="h-full rounded" style="width:{cnt / max_count * 100:.0f}%;'
            f'background:linear-gradient(90deg,#b45309,#f59e0b)"></div></div>'
            f'<span class="text-xs font-mono text-slate-300 w-6 shrink-0">{cnt}</span></div>'
            for tag, cnt in summary["tag_rank"]
        )
    else:
        tag_rows = '<div class="text-xs text-slate-600 py-3">今日无命中标签</div>'

    # —— 栏 3：重点事件 Top5 ——
    if summary["top_events"]:
        top_rows = "".join(
            f'<a href="#evt-{e["eid"]}" class="block py-1.5 border-b border-slate-800/60 '
            f'last:border-0 hover:bg-slate-800/40 rounded px-1 -mx-1 transition-colors">'
            f'<div class="flex items-center gap-1.5 text-[11px] text-slate-500 mb-0.5">'
            f'<span class="font-mono">{e["time"]:%H:%M}</span>'
            + "".join(
                f'<span class="px-1 rounded {TAG_COLORS.get(t, "bg-slate-800 text-slate-400")} '
                f'border border-slate-800">#{html.escape(t)}</span>'
                for t in e["tags"][:2]
            )
            + (f'<span class="font-mono {pct_classes(e["quote"]["pct"])} px-1 rounded border shrink-0">'
               f'{fmt_pct(e["quote"]["pct"])}</span>' if e["quote"] and e["quote"]["pct"] is not None else "")
            + f'</div>'
            f'<div class="text-sm text-slate-200 truncate">'
            f'{html.escape(e["title"] or e["content"][:40])}</div></a>'
            for e in summary["top_events"]
        )
    else:
        top_rows = '<div class="text-xs text-slate-600 py-3">今日无事件</div>'

    return f"""
    <section class="mb-6">
      <div class="flex items-center gap-2 mb-3">
        <span class="text-rose-400 text-lg">📌</span>
        <h2 class="text-base font-bold text-slate-100">负面舆情概览</h2>
        <span class="text-xs text-slate-500">点击条目可跳转到对应事件详情</span>
      </div>
      <div class="grid md:grid-cols-3 gap-5">
        <div>
          <div class="text-xs font-semibold text-rose-400/90 mb-2 tracking-wide">跌幅榜 TOP5（同一标的取最新）</div>
          {loser_rows}
        </div>
        <div>
          <div class="text-xs font-semibold text-amber-400/90 mb-2 tracking-wide">事件类型分布</div>
          {tag_rows}
        </div>
        <div>
          <div class="text-xs font-semibold text-cyan-400/90 mb-2 tracking-wide">重点事件 TOP5（规则评分）</div>
          {top_rows}
        </div>
      </div>
    </section>
    """

def render_nav(active: str) -> str:
    """渲染顶部导航条：当前页高亮（Tailwind 无 JS 实现）"""
    links = "".join(
        f'<a href="{file}" class="px-3 py-1.5 rounded-lg text-sm font-medium '
        + (
            "bg-amber-500/20 text-amber-300 border border-amber-500/40"
            if file == active
            else "text-slate-400 hover:text-slate-100 hover:bg-slate-800/60 border border-transparent"
        )
        + f' transition-colors">{icon} {label}</a>'
        for file, icon, label in NAV_ITEMS
    )
    return f'<nav class="flex flex-wrap items-center gap-1.5 mb-6">{links}</nav>'

def render_watchlist_block(watchlist: List[dict]) -> str:
    """渲染"重点关注"板块：每个自选标的一张卡片（行情 + 最值得关注的事件）"""
    cards = []
    for w in watchlist:
        quote = w["quote"]
        # 行情区：接口失败或停牌时显示占位（名称始终用配置名，不用接口简称）
        if quote and quote["price"]:
            quote_html = (
                f'<div class="flex items-baseline gap-2 mt-1">'
                f'<span class="text-xl font-bold font-mono text-slate-100">'
                f'{html.escape(str(quote["price"]))}</span>'
                f'<span class="text-sm font-mono px-1.5 py-0.5 rounded border '
                f'{pct_classes(quote["pct"])}">{fmt_pct(quote["pct"])}</span></div>'
            )
        else:
            quote_html = (
                '<div class="text-sm text-slate-600 mt-1">'
                '行情暂无（可能已收盘）</div>'
            )
        name_html = html.escape(w["name"])

        # 信息区：负面事件（红点、锚点跳转详情）优先，普通快讯（灰点、跳原文）补位
        info_rows = []
        for e in w["related"]:
            title = html.escape(e["title"] or e["content"][:30])
            info_rows.append(
                f'<a href="#evt-{e["eid"]}" class="flex items-center gap-1.5 '
                f'text-xs text-slate-300 hover:text-amber-300 truncate py-0.5" title="{title}">'
                f'<span class="w-1.5 h-1.5 rounded-full bg-rose-500 shrink-0"></span>'
                f'<span class="font-mono text-slate-500 shrink-0">{e["time"]:%H:%M}</span>'
                f'<span class="truncate">{title}</span></a>'
            )
        for row in w.get("latest_news", []):
            title = html.escape(row["title"] or row["content"][:30])
            safe_row_url = row["url"] if row["url"].startswith(("http://", "https://")) else "#"
            info_rows.append(
                f'<a href="{html.escape(safe_row_url, quote=True)}" target="_blank" '
                f'class="flex items-center gap-1.5 text-xs text-slate-400 '
                f'hover:text-slate-200 truncate py-0.5" title="{title}">'
                f'<span class="w-1.5 h-1.5 rounded-full bg-slate-600 shrink-0"></span>'
                f'<span class="font-mono text-slate-500 shrink-0">{row["time"]:%H:%M}</span>'
                f'<span class="truncate">{title}</span></a>'
            )
        if info_rows:
            related_html = (
                f'<div class="mt-2 pt-2 border-t border-slate-800/80 space-y-0.5">'
                f'{"".join(info_rows)}</div>'
            )
        else:
            related_html = (
                '<div class="mt-2 pt-2 border-t border-slate-800/80">'
                '<div class="text-xs text-slate-600">今日无相关资讯</div></div>'
            )

        cards.append(
            f'<div class="bg-slate-900/70 border border-slate-800 rounded-lg p-3.5 '
            f'hover:border-slate-700 transition-colors">'
            f'<div class="flex items-center justify-between gap-1">'
            f'<span class="text-sm font-semibold text-slate-100 truncate">{name_html}</span>'
            f'<span class="text-[10px] font-mono text-slate-500">{w["display"]}</span></div>'
            f'{quote_html}{related_html}</div>'
        )

    return f"""
    <section class="mb-6">
      <div class="flex items-center gap-2 mb-3">
        <span class="text-cyan-400 text-lg">⭐</span>
        <h2 class="text-base font-bold text-slate-100">重点关注</h2>
        <span class="text-xs text-slate-500">自选标的行情与当日相关舆情（可在 crawler.py 的 WATCHLIST 中调整）</span>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        {"".join(cards)}
      </div>
    </section>
    """

# ============================ HTML 渲染 ============================

def fmt_pct(pct: Optional[float]) -> str:
    """涨跌幅格式化：+1.23% / -1.49%"""
    if pct is None:
        return "--"
    return f"{pct:+.2f}%"


def pct_classes(pct: Optional[float]) -> str:
    """按涨跌幅返回徽章样式：下跌红色警示，跌幅超 3% 加底加粗"""
    if pct is None:
        return "bg-slate-500/10 text-slate-400 border-slate-500/20"
    if pct <= -3:
        return "bg-rose-500/20 text-rose-300 border-rose-500/50 font-bold"
    if pct < 0:
        return "bg-rose-500/10 text-rose-400 border-rose-500/25"
    if pct > 0:
        return "bg-emerald-500/10 text-emerald-400 border-emerald-500/25"
    return "bg-slate-500/10 text-slate-300 border-slate-500/25"


def card_accent(pct: Optional[float]) -> str:
    """卡片左侧强调条：跌幅超 3% 高亮红色"""
    if pct is not None and pct <= -3:
        return "border-l-rose-500"
    return "border-l-slate-800"


def render_card(event: dict) -> str:
    """渲染单条事件卡片 HTML"""
    stock = event["stock"]
    quote = event["quote"]
    pct = quote["pct"] if quote else None

    # 标的区块：名称优先取行情接口返回的官方简称；新三板无行情接口，用中性占位
    if stock:
        if quote:
            name = html.escape(quote["name"])
        elif stock["market"] == "新三板":
            name = '<span class="text-slate-400 font-normal text-sm">新三板挂牌公司</span>'
        else:
            name = '<span class="text-slate-500">未知名称</span>'
        stock_html = (
            f'<span class="font-semibold text-cyan-300">{name}</span>'
            f'<span class="ml-1.5 text-xs font-mono text-cyan-500/80">'
            f'{html.escape(stock["display"])}</span>'
        )
        if stock["market"] == "A股":
            market_badge = (
                '<span class="text-[10px] px-1.5 py-0.5 rounded border '
                'border-cyan-500/30 bg-cyan-500/10 text-cyan-300">A股</span>'
            )
        elif stock["market"] == "港股":
            market_badge = (
                '<span class="text-[10px] px-1.5 py-0.5 rounded border '
                'border-fuchsia-500/30 bg-fuchsia-500/10 text-fuchsia-300">港股</span>'
            )
        else:  # 新三板
            market_badge = (
                '<span class="text-[10px] px-1.5 py-0.5 rounded border '
                'border-amber-500/30 bg-amber-500/10 text-amber-300">新三板</span>'
            )
    else:
        stock_html = '<span class="text-slate-500 text-sm">未识别标的</span>'
        market_badge = (
            '<span class="text-[10px] px-1.5 py-0.5 rounded border '
            'border-slate-600 bg-slate-700/30 text-slate-400">无代码</span>'
        )

    # 行情区块：现价 + 涨跌幅
    if quote:
        quote_html = (
            f'<span class="text-sm font-mono text-slate-300 mr-2">'
            f'¥{html.escape(quote["price"])}</span>'
            f'<span class="inline-flex items-center px-2 py-0.5 rounded-md border '
            f'text-sm font-mono {pct_classes(pct)}">{fmt_pct(pct)}</span>'
        )
    else:
        quote_html = (
            '<span class="inline-flex items-center px-2 py-0.5 rounded-md border '
            'text-xs font-mono bg-slate-500/10 text-slate-500 border-slate-500/20">'
            '行情暂无</span>'
        )

    # 关键词标签
    tag_html = "".join(
        f'<span class="inline-block px-2 py-0.5 rounded border text-xs '
        f'{TAG_COLORS.get(tag, "bg-slate-500/15 text-slate-300 border-slate-500/30")}">'
        f'#{html.escape(tag)}</span>'
        for tag in event["tags"]
    )

    # 标题 + 摘要：无独立标题时只显示摘要
    title_html = ""
    if event["title"]:
        title_html = (
            f'<h3 class="text-[15px] font-semibold text-slate-100 leading-snug">'
            f'{html.escape(event["title"])}</h3>'
        )
    summary = event["content"]
    if len(summary) > 140:  # 精要控制：摘要截断到 140 字，避免长篇大论
        summary = summary[:140].rstrip() + "…"
    # 摘要若与标题完全重复则不再重复展示
    if summary and summary.strip() == event["title"].strip():
        summary = ""

    safe_url = event["url"] if event["url"].startswith(("http://", "https://")) else "#"
    search_text = html.escape(
        f"{event['title']} {event['content']} "
        f"{quote['name'] if quote else ''} {stock['display'] if stock else ''}"
        .lower()
    )

    return f"""
    <article id="evt-{event['eid']}" class="event-card bg-slate-900/70 border border-slate-800 border-l-2 {card_accent(pct)}
                    rounded-lg p-4 hover:border-slate-700 transition-colors"
             data-market="{stock['market'] if stock else 'NONE'}"
             data-tags="{' '.join(event['tags'])}"
             data-search="{search_text}">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs mb-2">
        <span class="font-mono text-slate-400">{event['time']:%Y-%m-%d %H:%M}</span>
        <span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">{html.escape(event['source'])}</span>
        {market_badge}
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
        <div class="flex items-center flex-wrap gap-1.5">{stock_html}</div>
        <div class="flex items-center whitespace-nowrap">{quote_html}</div>
      </div>
      {title_html}
      {f'<p class="text-sm text-slate-400 leading-relaxed mt-1.5">{html.escape(summary)}</p>' if summary else ''}
      <div class="flex flex-wrap items-center justify-between gap-2 mt-3">
        <div class="flex flex-wrap gap-1.5">{tag_html}</div>
        <a href="{html.escape(safe_url)}" target="_blank" rel="noopener noreferrer"
           class="text-xs px-2.5 py-1 rounded-md bg-slate-800 hover:bg-amber-500/20
                  hover:text-amber-300 text-slate-300 border border-slate-700
                  hover:border-amber-500/40 transition-colors shrink-0">
          查看原文 ↗
        </a>
      </div>
    </article>
    """


def render_finance_page(events: List[dict], news_rows: List[dict]) -> str:
    """渲染财经与股市页（原舆情监控看板）：今日要点 → 负面概览 → 关注标的 → 事件列表"""
    # 统计卡片数据
    a_codes = {e["stock"]["display"] for e in events if e["stock"] and e["stock"]["market"] == "A股"}
    hk_codes = {e["stock"]["display"] for e in events if e["stock"] and e["stock"]["market"] == "港股"}

    cards_html = "\n".join(render_card(e) for e in events)
    if not events:
        cards_html = """
        <div class="text-center py-24 text-slate-500">
          <div class="text-5xl mb-4">🛡️</div>
          <p class="text-lg">今日各数据源暂无命中负面/突发关键词的舆情</p>
          <p class="text-sm mt-2 text-slate-600">下次构建时间：北京时间 18:00（也可在 GitHub Actions 手动触发）</p>
        </div>
        """

    # 页脚：各数据源抓取/命中明细
    source_lines = "".join(
        f'<span class="inline-flex items-center gap-1 text-xs px-2 py-1 rounded '
        f'bg-slate-900 border border-slate-800 text-slate-400">'
        f'{html.escape(name)}'
        f'<b class="text-slate-200 font-mono">{st["fetched"]}</b>/'
        f'<b class="text-amber-400/80 font-mono">{st["matched"]}</b></span>'
        for name, st in SOURCE_STATS.items()
    )

    # 所有出现过的标签（用于顶部标签筛选按钮）
    all_tags = sorted({tag for e in events for tag in e["tags"]})
    tag_buttons = "".join(
        f'<button onclick="toggleTag(this,\'{tag}\')" '
        f'class="tag-filter text-xs px-2.5 py-1 rounded-full border border-slate-700 '
        f'text-slate-400 hover:border-amber-500/50 hover:text-amber-300 transition-colors">'
        f'#{tag}</button>'
        for tag in all_tags
    )

    template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>财经与股市 · A/港股舆情监控</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
         "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
  .event-card[hidden] { display: none; }
  html { scroll-behavior: smooth; }
  /* 速览区点击跳转后，目标卡片高亮闪烁提示 */
  .event-card:target { border-color: #f59e0b; box-shadow: 0 0 0 2px rgba(245,158,11,.35); }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-thumb { background: #334155; border-radius: 4px; }
</style>
</head>
<body class="bg-slate-950 text-slate-200 min-h-screen">
<div class="max-w-[1600px] mx-auto px-4 sm:px-6 py-6">

  <!-- 页头 -->
  <header class="border-b border-slate-800 pb-5 mb-6">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 class="text-xl sm:text-2xl font-bold tracking-tight">
          <span class="text-amber-400">财经与股市</span>
          <span class="text-slate-100">· A/港股舆情监控看板</span>
        </h1>
        <p class="text-xs text-slate-500 mt-1.5">
          多源财经快讯 · 负面关键词命中 · 腾讯行情叠加 · 每日 18:00（北京时间）自动构建
        </p>
      </div>
      <div class="text-right text-xs text-slate-500">
        <div>数据更新时间</div>
        <div class="font-mono text-sm text-slate-300 mt-0.5">__BUILD_TIME__ <span class="text-slate-600">UTC+8</span></div>
      </div>
    </div>
  </header>

  <!-- 顶部导航（三页切换） -->
__NAV__

  <!-- 板块：A/港股舆情监控 -->
  <div class="flex items-center gap-2 mb-4">
    <span class="text-rose-400 text-lg">📉</span>
    <h2 class="text-base font-bold text-slate-100">A/港股舆情监控</h2>
    <span class="text-xs text-slate-500">负面关键词命中 + 实时行情叠加</span>
  </div>

  <!-- 统计卡片 -->
  <section class="grid grid-cols-3 gap-3 sm:gap-4 mb-6">
    <div class="bg-slate-900/70 border border-slate-800 rounded-lg p-4">
      <div class="text-xs text-slate-500 mb-1">今日捕获事件</div>
      <div class="text-2xl sm:text-3xl font-bold font-mono text-amber-400">__TOTAL__</div>
    </div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-lg p-4">
      <div class="text-xs text-slate-500 mb-1">涉及 A 股标的</div>
      <div class="text-2xl sm:text-3xl font-bold font-mono text-cyan-300">__A_COUNT__</div>
    </div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-lg p-4">
      <div class="text-xs text-slate-500 mb-1">涉及港股标的</div>
      <div class="text-2xl sm:text-3xl font-bold font-mono text-fuchsia-300">__HK_COUNT__</div>
    </div>
  </section>

  <!-- 负面舆情概览（跌幅榜 / 类型分布 / 重点事件） -->
__NEGATIVE__

  <!-- 重点关注（自选标的行情 + 当日相关舆情） -->
__WATCHLIST__

  <!-- 筛选栏 -->
  <section class="mb-5 space-y-3">
    <div class="flex flex-wrap items-center gap-2">
      <div class="flex rounded-lg overflow-hidden border border-slate-700 text-xs">
        <button onclick="setMarket('ALL',this)" data-market-btn="ALL"
                class="market-btn px-3 py-1.5 bg-amber-500/20 text-amber-300">全部</button>
        <button onclick="setMarket('A股',this)" data-market-btn="A股"
                class="market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200">A股</button>
        <button onclick="setMarket('港股',this)" data-market-btn="港股"
                class="market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200">港股</button>
        <button onclick="setMarket('NONE',this)" data-market-btn="NONE"
                class="market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200">未识别</button>
      </div>
      <input id="searchBox" type="text" placeholder="搜索公司 / 代码 / 关键词…"
             oninput="applyFilters()"
             class="flex-1 min-w-[180px] bg-slate-900 border border-slate-800 rounded-lg
                    px-3 py-1.5 text-sm text-slate-200 placeholder-slate-600
                    focus:outline-none focus:border-amber-500/50">
      <button onclick="resetFilters()"
              class="text-xs px-3 py-1.5 rounded-lg border border-slate-700
                     text-slate-400 hover:text-slate-200">重置</button>
    </div>
    <div class="flex flex-wrap gap-1.5" id="tagBar">__TAG_BUTTONS__</div>
  </section>

  <!-- 事件列表 -->
  <main id="eventList" class="space-y-3">
__CARDS__
  </main>

  <div id="noResult" hidden
       class="text-center py-16 text-slate-600 text-sm">当前筛选条件下没有事件</div>

  <!-- 页脚 -->
  <footer class="mt-10 pt-5 border-t border-slate-800 text-xs text-slate-500 space-y-3">
    <div class="flex flex-wrap gap-1.5">
      <span class="text-slate-600 mr-1 py-1">数据源（抓取/命中）：</span>
      __SOURCE_LINES__
    </div>
    <p class="leading-relaxed">
      说明：本看板由 GitHub Actions 定时自动抓取公开财经快讯并经关键词规则生成，
      内容版权归原作者所有；"伪负面"指传闻、辟谣、澄清等未经证实或已被否认的信息，
      本页不对信息真实性负责，<span class="text-slate-400">不构成任何投资建议</span>。
    </p>
  </footer>
</div>

<script>
// 当前筛选状态：市场维度 + 标签集合 + 搜索词
var state = { market: 'ALL', tags: new Set(), q: '' };

// 切换市场筛选
function setMarket(m, btn) {
  state.market = m;
  document.querySelectorAll('.market-btn').forEach(function (b) {
    b.className = 'market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200';
  });
  btn.className = 'market-btn px-3 py-1.5 bg-amber-500/20 text-amber-300';
  applyFilters();
}

// 切换关键词标签（支持多选，再点一次取消）
function toggleTag(btn, tag) {
  if (state.tags.has(tag)) {
    state.tags.delete(tag);
    btn.className = 'tag-filter text-xs px-2.5 py-1 rounded-full border border-slate-700 '
      + 'text-slate-400 hover:border-amber-500/50 hover:text-amber-300 transition-colors';
  } else {
    state.tags.add(tag);
    btn.className = 'tag-filter text-xs px-2.5 py-1 rounded-full border '
      + 'border-amber-500/50 bg-amber-500/15 text-amber-300 transition-colors';
  }
  applyFilters();
}

// 根据当前状态过滤全部卡片
function applyFilters() {
  state.q = document.getElementById('searchBox').value.trim().toLowerCase();
  var visible = 0;
  document.querySelectorAll('.event-card').forEach(function (card) {
    var okMarket = state.market === 'ALL' || card.dataset.market === state.market;
    var cardTags = (card.dataset.tags || '').split(' ');
    var okTag = true;
    state.tags.forEach(function (t) { if (cardTags.indexOf(t) === -1) okTag = false; });
    var okQ = !state.q || (card.dataset.search || '').indexOf(state.q) !== -1;
    var show = okMarket && okTag && okQ;
    card.hidden = !show;
    if (show) visible++;
  });
  document.getElementById('noResult').hidden = visible !== 0;
}

// 一键重置筛选
function resetFilters() {
  state = { market: 'ALL', tags: new Set(), q: '' };
  document.getElementById('searchBox').value = '';
  document.querySelectorAll('.tag-filter').forEach(function (b) {
    b.className = 'tag-filter text-xs px-2.5 py-1 rounded-full border border-slate-700 '
      + 'text-slate-400 hover:border-amber-500/50 hover:text-amber-300 transition-colors';
  });
  var btns = document.querySelectorAll('.market-btn');
  btns.forEach(function (b) {
    if (b.dataset.marketBtn === 'ALL') {
      b.className = 'market-btn px-3 py-1.5 bg-amber-500/20 text-amber-300';
    } else {
      b.className = 'market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200';
    }
  });
  applyFilters();
}
</script>
</body>
</html>
"""

    return (
        template
        .replace("__BUILD_TIME__", NOW.strftime("%Y-%m-%d %H:%M"))
        .replace("__TOTAL__", str(len(events)))
        .replace("__A_COUNT__", str(len(a_codes)))
        .replace("__HK_COUNT__", str(len(hk_codes)))
        .replace("__TAG_BUTTONS__", tag_buttons)
        .replace("__NAV__", render_nav("finance.html"))
        .replace("__NEGATIVE__", render_negative_overview(summarize(events)))
        .replace("__WATCHLIST__", render_watchlist_block(build_watchlist(events, news_rows)))
        .replace("__CARDS__", cards_html)
        .replace("__SOURCE_LINES__", source_lines)
    )

def _feed_item_html(item: dict) -> str:
    """资讯条目渲染：时间 + 标题（跳原文）+ 来源 + 中文标签；带 tagline（如 PH 产品）时渲染两行"""
    time_str = item["time"].strftime("%m-%d %H:%M") if item["time"] else "--:--"
    tags = " ".join(
        f'<span class="px-1 rounded bg-cyan-500/10 text-cyan-300/80 border border-cyan-500/20 text-[10px]">{html.escape(t)}</span>'
        for t in zh_tags(f"{item['title']} {item.get('tagline', '')} {item['summary']}")
    )
    row = f"""
      <a href="{html.escape(item['url'] if item['url'].startswith(("http://", "https://")) else "#", quote=True)}"
         target="_blank" rel="noopener noreferrer"
         class="flex items-baseline gap-2 py-2 rounded px-1 -mx-1
                hover:bg-slate-800/40 transition-colors"
         title="{html.escape(item['title'])}">
        <span class="font-mono text-[11px] text-slate-500 shrink-0">{time_str}</span>
        <span class="text-[13px] text-slate-200 {'truncate flex-1' if not item.get('tagline') else 'flex-1'}">{html.escape(item["title"])}</span>
        {tags}
        <span class="text-[10px] text-slate-600 shrink-0">{html.escape(item["source"])}</span>
      </a>"""
    if item.get("tagline"):
        en_tip = html.escape(item.get("tagline_en", ""), quote=True)
        return f"""
    <div class="border-b border-slate-800/50 last:border-0">{row}
      <div class="text-[11px] text-slate-500 pl-[72px] pb-2 -mt-1 pr-2" title="{en_tip}">↳ {html.escape(item["tagline"])}</div>
    </div>"""
    return row


def _info_page_shell(title: str, icon: str, active: str, subtitle: str,
                     sections: str, build_time: str) -> str:
    """AI 页 / 海外产品页共用 HTML 骨架（与舆情页同一风格）"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  /* 模块内部滚动区域：深色细滚动条 */
  .scroll-thin::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  .scroll-thin::-webkit-scrollbar-track {{ background: transparent; }}
  .scroll-thin::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 3px; }}
  .scroll-thin::-webkit-scrollbar-thumb:hover {{ background: #475569; }}
  .scroll-thin {{ scrollbar-width: thin; scrollbar-color: #334155 transparent; }}
</style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">
<div class="max-w-[1600px] mx-auto px-4 sm:px-6 py-6">

  <header class="mb-4">
    <h1 class="text-xl sm:text-2xl font-bold text-slate-50">{icon} {title}</h1>
    <div class="text-xs text-slate-500 mt-1">{subtitle}</div>
  </header>

  {render_nav(active)}

{sections}

  <footer class="mt-10 pt-5 border-t border-slate-800/60 flex flex-wrap justify-between gap-3 text-xs text-slate-600">
    <div>数据更新时间 {build_time} <span class="text-slate-700">UTC+8</span> · 自动构建于 GitHub Actions</div>
    <div><a href="index.html" class="hover:text-amber-400">返回舆情监控</a></div>
  </footer>
</div>
</body>
</html>
"""


def render_ai_page(domestic: List[dict], overseas: List[dict], products: List[dict]) -> str:
    """渲染 AI 资讯页：国内 AI 资讯 / 海外 AI 资讯 / AI 产品发布三区块"""
    def block(items: List[dict], empty_hint: str) -> str:
        rows = "\n".join(_feed_item_html(it) for it in items) or \
            f'<div class="text-xs text-slate-600 py-3">{empty_hint}</div>'
        return f'<div class="bg-slate-900/70 border border-slate-800 rounded-xl p-4">{rows}</div>'

    sections = f"""
  <section class="mb-6">
    <div class="flex items-center gap-2 mb-3">
      <span class="text-emerald-400 text-lg">🇨🇳</span>
      <h2 class="text-base font-bold text-slate-100">国内 AI 资讯</h2>
      <span class="text-xs text-slate-500">量子位 / ai-bot快讯 · 时间倒序</span>
    </div>
    {block(domestic, "今日暂无国内 AI 资讯")}
  </section>

  <section class="mb-6">
    <div class="flex items-center gap-2 mb-3">
      <span class="text-cyan-400 text-lg">🌐</span>
      <h2 class="text-base font-bold text-slate-100">海外 AI 资讯</h2>
      <span class="text-xs text-slate-500">TechCrunch / Hacker News · 时间倒序</span>
    </div>
    {block(overseas, "今日暂无海外 AI 资讯")}
  </section>

  <section class="mb-6">
    <div class="flex items-center gap-2 mb-3">
      <span class="text-amber-400 text-lg">🚀</span>
      <h2 class="text-base font-bold text-slate-100">AI 产品发布</h2>
      <span class="text-xs text-slate-500">国内外公司新产品 / 新模型发布动态</span>
    </div>
    {block(products, "今日暂无 AI 产品发布动态")}
  </section>
"""
    return _info_page_shell("AI 资讯看板", "🤖", "ai.html",
                            "AI 行业资讯与产品发布监控 · 量子位 / ai-bot快讯 / TechCrunch / Hacker News",
                            sections, NOW.strftime("%Y-%m-%d %H:%M"))


def render_global_page(groups: dict) -> str:
    """渲染海外产品页：Product Hunt 精选 + 游戏 / 工具 / 应用 等分组资讯"""
    group_icons = {"PH精选": "🚀", "游戏": "🎮", "工具": "🛠️", "应用": "📱", "更多": "📦"}
    group_hint = {"PH精选": "海外新产品发布榜单 · 每日精选"}
    sections = ""
    for name in ("PH精选", "游戏", "工具", "应用", "更多"):
        items = groups.get(name) or []
        rows = "\n".join(_feed_item_html(it) for it in items) or \
            '<div class="text-xs text-slate-600 py-3">今日暂无相关资讯</div>'
        hint = group_hint.get(name, f"共 {len(items)} 条")
        sections += f"""
  <section class="mb-6">
    <div class="flex items-center gap-2 mb-3">
      <span class="text-lg">{group_icons.get(name, "📦")}</span>
      <h2 class="text-base font-bold text-slate-100">{name}</h2>
      <span class="text-xs text-slate-500">{hint}</span>
    </div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-xl p-4 max-h-[520px] overflow-y-auto scroll-thin pr-2">{rows}</div>
  </section>
"""
    return _info_page_shell("海外产品资讯", "🌏", "global.html",
                            "Product Hunt / Steam / GameLook / 白鲸出海 / TechCrunch / Hacker News",
                            sections, NOW.strftime("%Y-%m-%d %H:%M"))


# ========================= 今日热榜页 =========================

# 各信息源的 favicon 域名（用 Google favicon 服务获取真实图标）
_SOURCE_FAVICON = {
    "今日头条":  "toutiao.com",
    "微博热搜":  "weibo.com",
    "百度热搜":  "baidu.com",
    "财经要点":  "eastmoney.com",
    "36kr":      "36kr.com",
    "量子位":    "qbitai.com",
    "ai-bot":    "ai-bot.cn",
    "白鲸出海":  "baijing.cn",
    "GameLook":  "gamelook.com.cn",
    "Product Hunt": "producthunt.com",
    "GitHub Trending": "github.com",
}

# 点击模块标题时跳转的信息源页面地址（未配置的源如聚合类"财经要点"标题不加链接）
_SOURCE_HOME = {
    "今日头条":  "https://www.toutiao.com/",
    "微博热搜":  "https://s.weibo.com/top/summary",
    "百度热搜":  "https://top.baidu.com/board?tab=realtime",
    "36kr":      "https://www.36kr.com/",
    "量子位":    "https://www.qbitai.com/",
    "ai-bot":    "https://ai-bot.cn/daily-ai-news/",
    "白鲸出海":  "https://www.baijing.cn/",
    "GameLook":  "http://www.gamelook.com.cn/",
    "Product Hunt": "https://www.producthunt.com/",
    "GitHub Trending": "https://github.com/trending",
}


def _hot_module(source_name: str, items: List[dict]) -> str:
    """渲染单个信息源模块：标题行（favicon+名称+条数 | 更新时间）+ 编号列表。
    模块整体固定等高，列表展示该源抓到的全部条目，超出高度时在列表内部滚动。
    信息源名称是可点击超链接，新标签打开该源对应页面（聚合源无地址时显示纯文本）"""
    domain = _SOURCE_FAVICON.get(source_name, "")
    icon_html = (f'<img src="https://favicon.im/{domain}" '
                 f'class="w-5 h-5 rounded object-contain" onerror="this.style.display=\'none\'" alt="">')
    home_url = _SOURCE_HOME.get(source_name, "")
    if home_url:
        name_html = (f'<a href="{html.escape(home_url, quote=True)}" target="_blank" '
                     f'rel="noopener noreferrer" title="打开 {source_name} 页面" '
                     f'class="text-sm font-bold text-slate-100 hover:text-amber-400 transition-colors">'
                     f'{source_name}</a>')
    else:
        name_html = f'<span class="text-sm font-bold text-slate-100">{source_name}</span>'
    update_time = NOW.strftime("%m月%d日 %H:%M")
    rows = []
    for idx, it in enumerate(items, 1):
        title = html.escape(it.get("title", ""))
        url = it.get("url", "#")
        if not url.startswith(("http://", "https://")):
            url = "#"
        # 描述：优先 summary / tagline，否则显示热度
        desc = it.get("summary") or it.get("tagline") or ""
        hot = it.get("hot", "")
        if not desc and hot:
            desc = f"热度 {hot}"
        desc_html = f'<div class="text-xs text-slate-500 mt-0.5 truncate">{html.escape(str(desc)[:80])}</div>' if desc else ""
        hot_badge = f'<span class="text-[10px] text-slate-600 ml-auto shrink-0">{html.escape(str(hot))}</span>' if hot and not desc else ""
        rows.append(f"""
          <a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer"
             class="block py-2.5 border-b border-slate-800/40 last:border-0 hover:bg-slate-800/30 rounded px-1 -mx-1 transition-colors">
            <div class="flex items-start gap-2">
              <span class="text-sm font-bold text-slate-600 shrink-0 mt-0.5 w-5 text-center">{idx}</span>
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2">
                  <span class="text-[13px] text-slate-200 truncate">{title}</span>
                  {hot_badge}
                </div>
                {desc_html}
              </div>
            </div>
          </a>""")
    body = "\n".join(rows) if rows else '<div class="text-xs text-slate-600 py-6 text-center">暂无数据</div>'
    return f"""
    <div class="bg-slate-900/60 border border-slate-800 rounded-xl p-4 flex flex-col h-[540px]">
      <div class="flex items-center justify-between mb-3 pb-2 border-b border-slate-800/60 shrink-0">
        <div class="flex items-center gap-2">
          {icon_html}
          {name_html}
          <span class="text-[10px] text-slate-500 bg-slate-800/70 rounded px-1.5 py-0.5">{len(items)} 条</span>
        </div>
        <span class="text-[11px] text-slate-500">{update_time}</span>
      </div>
      <div class="flex-1 min-h-0 overflow-y-auto scroll-thin pr-1">{body}</div>
    </div>"""


def _hot_section(title: str, icon: str, modules: dict) -> str:
    """渲染一个板块（含多个信息源模块，响应式 2~3 列）"""
    module_html = "\n".join(_hot_module(name, items) for name, items in modules.items())
    return f"""
  <section class="mb-8">
    <div class="flex items-center gap-2 mb-4">
      <span class="text-xl">{icon}</span>
      <h2 class="text-lg font-bold text-slate-50">{title}</h2>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
      {module_html}
    </div>
  </section>"""


def render_hotboard_page(data: dict) -> str:
    """渲染今日热榜页：社会舆情 / 科技动态 / 游戏与产品 三大板块"""
    sections = (
        _hot_section("社会舆情", "👥", data.get("social", {}))
        + _hot_section("科技动态", "💡", data.get("tech", {}))
        + _hot_section("游戏与产品", "🎮", data.get("gaming", {}))
    )
    return _info_page_shell("今日热榜", "🔥", "index.html",
                            "今日头条 / 微博热搜 / 36kr / 量子位 / ai-bot / GameLook / Product Hunt / GitHub Trending",
                            sections, NOW.strftime("%Y-%m-%d %H:%M"))


