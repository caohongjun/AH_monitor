# -*- coding: utf-8 -*-
"""config.py — 全部配置与词库：改词库 / 自选标的 / 常量只需动这个文件"""
from datetime import datetime, timedelta, timezone
from typing import Dict, List


# ============================ 全局配置 ============================

CN_TZ = timezone(timedelta(hours=8))          # 北京时间
NOW = datetime.now(CN_TZ)                     # 脚本运行时刻（北京时间）
TODAY = NOW.date()                            # 当天日期，用于"仅保留今日快讯"
TODAY_START = datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=CN_TZ)

MAX_EVENTS = 100                              # 看板最多展示条数
OUTPUT_HTML = "index.html"                    # 输出文件
QUOTE_BATCH = 60                              # 腾讯行情单次批量查询数量

# 统一浏览器请求头，降低被简单反爬拦截的概率
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 负面/突发关键词词库：分组名即看板上展示的标签
# 注意：命中逻辑为"标题+正文"子串包含，词库刻意保持宽口径，宁可多报
KEYWORD_GROUPS: Dict[str, List[str]] = {
    "监管调查": [
        "被查", "调查", "立案", "处罚", "罚单", "警示函", "监管函", "关注函",
        "问询函", "约谈", "整改", "通报批评", "公开谴责", "留置", "被捕",
        "逮捕", "刑拘", "刑事拘留", "公诉", "判刑", "被执行", "失信",
        "退市风险", "终止上市", "暂停上市", "内控被否",
    ],
    "诉讼纠纷": [
        "诉讼", "仲裁", "纠纷", "索赔", "起诉", "被告", "侵权", "违约",
        "合同纠纷", "股权纠纷", "财产保全", "冻结", "查封", "专利侵权",
    ],
    "传闻辟谣": [
        "传闻", "辟谣", "澄清", "否认", "网传", "谣言", "不实", "紧急回应",
        "回应称", "假的", "市场传言",
    ],
    "举报公关": [
        "举报", "实名举报", "绯闻", "出轨", "性骚扰", "性侵", "情妇",
        "争议", "抵制", "翻车", "道歉", "虚假宣传", "欺诈", "造假",
        "财务造假", "抄袭", "剽窃", "维权",
    ],
    "经营风险": [
        "宕机", "爆雷", "暴雷", "裁员", "破产", "停产", "跑路", "资金链",
        "债务违约", "债券违约", "商誉减值", "巨亏", "预亏", "业绩变脸",
        "业绩下修", "召回", "安全事故", "火灾", "爆炸", "停产整顿",
        "环保处罚", "欠薪",
    ],
    "高管异动": [
        "辞职", "辞去", "离职", "坠楼", "身亡", "去世", "猝死", "失联",
        "被抓", "被带走", "配合调查", "董事长出事",
    ],
    "股价异动": [
        "暴跌", "闪崩", "跌停", "大跌", "跳水", "跌超", "熔断",
    ],
}

# 运行统计：每个源抓到几条 / 命中几条，最终展示在页脚
SOURCE_STATS: Dict[str, Dict[str, int]] = {}

# 标签对应的 Tailwind 配色（与 HTML 模板中保持一致）
TAG_COLORS: Dict[str, str] = {
    "监管调查": "bg-red-500/15 text-red-300 border-red-500/30",
    "诉讼纠纷": "bg-orange-500/15 text-orange-300 border-orange-500/30",
    "传闻辟谣": "bg-yellow-500/15 text-yellow-300 border-yellow-500/30",
    "举报公关": "bg-pink-500/15 text-pink-300 border-pink-500/30",
    "经营风险": "bg-rose-500/15 text-rose-300 border-rose-500/30",
    "高管异动": "bg-purple-500/15 text-purple-300 border-purple-500/30",
    "股价异动": "bg-amber-500/15 text-amber-300 border-amber-500/30",
}

# 全局长连接会话
HIGH_RISK_TAGS = {"监管调查", "经营风险", "高管异动"}

# 市场要闻筛选词库（纯规则，无 AI）：
# 核心词：命中一条即视为重大（宏观决策机构、重大资本动作、市场级事件）
MARKET_CORE_KEYWORDS = [
    "国务院", "证监会", "央行", "中国人民银行", "美联储", "财政部", "发改委",
    "降准", "降息", "加息", "LPR", "MLF", "IPO", "退市", "熔断", "并购重组",
    "万亿", "关税", "制裁", "刺激计划", "地产新政",
]
# 一般词：单独命中不计重大，需与一般词/核心词合计命中 2 个以上才入选
MARKET_KEYWORDS = [
    "重磅", "重大", "新规", "获批", "批准", "停牌", "复牌", "涨跌停",
    "北向资金", "融资融券", "回购", "增持", "减持", "国家战略", "规划",
]

# 国内要点强特征词：命中才归"国内要点"（不含裸"央行"与"国内"——前者海外也有央行，
# 后者会撞上"国内生产总值"；配合"海外词优先"的判断顺序，"美国财政部"等也不会误判）
MARKET_DOMESTIC_KEYWORDS = [
    "中国", "我国", "内地", "人民币", "A股", "沪深", "北交所", "港股",
    "港股通", "证监会", "财政部", "发改委", "工信部", "商务部", "国务院", "国常会",
    "国资委", "网信办", "金融监管总局", "外汇局", "中国央行", "中国人民银行",
    "逆回购", "LPR", "MLF", "中概", "中美", "楼市", "房地产", "一线城市", "医保", "社保",
]

# 海外要点词库：命中国内强特征词之外的海外实体词归入"海外要点"分组
MARKET_GLOBAL_KEYWORDS = [
    "美联储", "美股", "纳斯达克", "纳指", "道琼斯", "道指", "标普", "欧央行", "欧洲央行",
    "日本央行", "日经", "原油", "WTI", "布伦特", "OPEC", "黄金", "比特币", "加密货币",
    "俄乌", "中东", "特朗普", "美国", "美元", "美债", "欧元", "日元", "英镑", "日本",
    "韩国", "印度", "英国", "德国", "法国", "欧盟", "全球", "海外", "国际",
    "秘鲁", "智利", "阿根廷", "巴西", "墨西哥", "加拿大", "澳大利亚", "澳洲",
    "瑞士", "瑞典", "挪威", "荷兰", "意大利", "西班牙", "土耳其", "南非",
    "印尼", "泰国", "越南", "马来西亚", "菲律宾", "新加坡", "尼日利亚",
    "以色列", "伊朗", "沙特", "乌克兰", "俄罗斯", "IMF", "世界银行",
    "特斯拉", "苹果", "英伟达", "微软", "谷歌", "Meta", "亚马逊",
]

# 速览区市场要闻展示条数
HEADLINES_MAX = 8


# 每组要点展示条数（国内/海外各一组）
BRIEF_GROUP_MAX = 6

NAV_ITEMS = [
    ("index.html", "🔥", "今日热榜"),
    ("finance.html", "📈", "财经与股市"),
]

# ============================ 信息源采集规则（每个源独立配置） ============================
# 各源刷新频率不同，时间策略与条数各自独立，不使用统一规则。
#   time_rule:
#     "today_yesterday" — 仅爬北京时间当天数据；当天无更新则回退爬前一天；
#                         昨天仍无数据则该模块为空（不展示），绝不爬更早数据，防止历史数据爆炸
#     "realtime"        — 实时榜单/热榜快照（页面本身即"当前"内容），不按日期过滤
#   limit: 过滤后每个模块最多展示条数（超出部分在卡片内部滚动）
SOURCE_RULES = {
    # —— 今日热榜 · 社会舆情 ——
    "今日头条":       {"time_rule": "realtime", "limit": 20},
    "微博热搜":       {"time_rule": "realtime", "limit": 20},
    "百度热搜":       {"time_rule": "realtime", "limit": 20},
    "Reddit":         {"time_rule": "realtime", "limit": 20},
    "BBC":          {"time_rule": "today_yesterday", "limit": 20},
    "财经要点":       {"time_rule": "today_yesterday", "limit": 20},
    # —— 今日热榜 · 科技动态 ——
    "36kr":          {"time_rule": "today_yesterday", "limit": 20},
    "量子位":         {"time_rule": "today_yesterday", "limit": 20},
    "a16z":         {"time_rule": "recent_7d", "limit": 20},
    "ai-bot":        {"time_rule": "today_yesterday", "limit": 20},
    "白鲸出海":       {"time_rule": "today_yesterday", "limit": 20},
    "微信公众号":     {"time_rule": "realtime", "limit": 15},
    # —— 今日热榜 · 游戏与产品 ——
    "GameLook":      {"time_rule": "today_yesterday", "limit": 20},
    "Product Hunt":  {"time_rule": "realtime", "limit": 20},   # PH 每日榜单固定展示昨日榜
    "GitHub Trending": {"time_rule": "realtime", "limit": 25},
    # —— 海外产品页（global）——
    "Steam 新品":     {"time_rule": "today_yesterday", "limit": 20},
    "TechCrunch":    {"time_rule": "today_yesterday", "limit": 20},
    "Hacker News":   {"time_rule": "realtime", "limit": 25},
}


def source_rule(source_name: str, key: str, default):
    """读取单个信息源的采集规则配置；未配置的源返回默认值"""
    return SOURCE_RULES.get(source_name, {}).get(key, default)

WATCHLIST = [
    {"name": "招商银行", "tcode": "sh600036", "display": "600036.SH", "match": "600036",
     "aliases": ["招商银行", "招行"]},
    {"name": "长江电力", "tcode": "sh600900", "display": "600900.SH", "match": "600900",
     "aliases": ["长江电力", "长电"]},
    {"name": "贵州茅台", "tcode": "sh600519", "display": "600519.SH", "match": "600519",
     "aliases": ["贵州茅台", "茅台"]},
    {"name": "中国神华", "tcode": "sh601088", "display": "601088.SH", "match": "601088",
     "aliases": ["中国神华", "神华"]},
    {"name": "标普500南方", "tcode": "sh513500", "display": "513500.SH", "match": "513500",
     "aliases": ["标普500", "标普"]},
    {"name": "纳指ETF嘉实", "tcode": "sz159941", "display": "159941.SZ", "match": "159941",
     "aliases": ["纳指", "纳斯达克"]},
]

# 每个持仓最多展示的相关舆情事件条数
WATCH_RELATED_MAX = 2


GLOBAL_TAG_RULES = [
    ("游戏", ["game", "steam", "playstation", "xbox", "nintendo", "rpg", "gaming", "游戏",
             "arcade", "puzzle", "roguelike", "indie game"]),
    ("AI", [" ai ", "ai-", "-ai", "gpt", "llm", "agent", "openai", "anthropic", "gemini",
            "artificial intelligence", "machine learning", "人工智能", "大模型",
            "assistant", "chatbot", "copilot", "automation", "voice ai", "writing ai"]),
    ("融资收购", ["funding", "raises", "investment", "series ", "valuation", "acquisition",
                 "acquires", "merger", "ipo", "融资", "收购"]),
    ("应用", ["app", "ios", "android", "mobile", "应用", "上线", "tracker", "planner",
             "habit", "health", "finance", "budget", "fitness", "sleep", "weather",
             "shopping", "social", "browser", "widgets"]),
    ("工具", ["tool", "editor", "ide", "sdk", "framework", "plugin", "extension", "工具",
             "插件", "builder", "dashboard", "productivity", "workflow", "api", "code",
             "developer", "design", "notes", "screenshot", "analytics", "crm",
             "template", "generator", "converter"]),
    ("芯片", ["chip", "gpu", "nvidia", "cpu", "semiconductor", "芯片"]),
    ("安全", ["security", "breach", "hack", "vulnerability", "漏洞", "安全"]),
    ("开源", ["open source", "open-source", "github", "开源"]),
    ("音频视频", ["video", "audio", "podcast", "music", "recording", "transcri", "subtitle",
                 "voice", "sound", "视频", "音频"]),
]

# AI 公司/产品名单（国内外主流 AI 公司，用于"谁发布了什么"的产品发布判定）
AI_COMPANIES = ["openai", "anthropic", "google", "gemini", "meta", "llama", "microsoft",
                "copilot", "xai", "grok", "mistral", "nvidia", "英伟达", "apple", "苹果",
                "amazon", "aws", "perplexity", "midjourney", "runway", "stability",
                "character.ai", "deepseek", "深度求索", "智谱", "月之暗面", "kimi",
                "阿里", "通义", "千问", "qwen", "百度", "文心", "腾讯", "混元", "华为",
                "盘古", "字节", "豆包", "doubao", "科大讯飞", "讯飞", "星火", "商汤",
                "旷视", "minimax", "零一万物", "百川", "面壁", "阶跃星辰", "昆仑万维",
                "天工", "秘塔", "快手", "可灵", "联想", "小米", "美团", "网易", "京东", "蚂蚁"]

# 泛 AI 词（AI 资讯流判定：公司名或泛词命中即算 AI 相关）
AI_CORE = ["ai", "人工智能", "大模型", "llm", "gpt", "claude", "gemini", "deepseek",
           "qwen", "通义", "kimi", "豆包", "llama", "agent", "智能体", "openai",
           "anthropic", "文心", "copilot", "midjourney", "sora", "diffusion", "多模态",
           "机器人", "算力", "芯片", "英伟达", "nvidia"]

# 发布动词（产品发布 = AI 公司名 + 发布动词双命中）
AI_PRODUCT_VERBS = ["发布", "开源", "上线", "推出", "公测", "首发", "亮相", "开售",
                    "launch", "release", "open-sourc", "announc", "unveil", "debut",
                    "roll out", "introduc", "available", "unveils", "released"]


