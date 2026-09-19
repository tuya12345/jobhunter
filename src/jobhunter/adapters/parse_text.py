"""岗位文本的纯函数解析：薪资 / 城市 / 经验 / 学历 / 是否实习。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么单独一层「纯文本解析」                                               ║
╚══════════════════════════════════════════════════════════════════════════╝

招聘网站的详情页结构千变万化（而且随时改版），但"从一段文字里认出薪资/城市/经验"
这件事是**与页面结构无关**的。把它拆出来有三个实际好处：

    · **可测**：不需要浏览器、不需要网络，喂字符串就能断言（本项目 P2 的测试主力）；
    · **可复用**：`--manual` 手动粘贴的 JD、批量导入的文本、将来某个适配器抓到的
      字段，都走同一套解析，口径一致；
    · **改版免疫**：页面选择器失效时，这一层完全不受影响。

★ 一条纪律：**认不出就留空，绝不猜**。

    薪资、城市、经验这些字段会被 P3 的匹配打分直接使用（"期望深圳"对不上"北京"就是
    方向性错误）。猜错的代价远大于留空 —— 留空时匹配层会走"未知，不扣分"的分支，
    而猜错会让用户被推一堆根本不该投的岗位。

★ 另一条纪律：**原文一律保留**。

    除了数值化字段，每个字段都同时留下"原文"（`salary_text`、`experience`…）。
    解析规则将来会改，原文留着重跑一遍就能刷新所有派生字段，不必重新抓取
    （既省时间，也少给目标站点添麻烦）。
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "KNOWN_CITIES",
    "extract_city",
    "extract_education",
    "extract_experience",
    "extract_salary",
    "looks_like_intern",
    "normalize_for_dedup",
    "normalize_text",
    "strip_html",
]

#: 常见求职城市（按"长名字优先"排列，避免"东莞"被"东"截断这类问题）
KNOWN_CITIES: tuple[str, ...] = (
    "北京",
    "上海",
    "深圳",
    "广州",
    "杭州",
    "成都",
    "武汉",
    "南京",
    "西安",
    "苏州",
    "东莞",
    "佛山",
    "珠海",
    "中山",
    "惠州",
    "长沙",
    "重庆",
    "天津",
    "厦门",
    "合肥",
    "郑州",
    "青岛",
    "大连",
    "福州",
    "济南",
    "宁波",
    "无锡",
    "常州",
    "南昌",
    "昆明",
    "贵阳",
    "南宁",
    "哈尔滨",
    "沈阳",
    "长春",
    "石家庄",
    "太原",
    "兰州",
    "乌鲁木齐",
    "海口",
    "三亚",
    "呼和浩特",
    "银川",
    "西宁",
    "香港",
    "澳门",
    "台北",
)


# ---------------------------------------------------------------------------
# 基础文本处理
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """规范化文本：全角转半角、统一空白。

    ★ 为什么需要全角转半角：中文招聘页面里 `１５-２５Ｋ`、`（深圳）` 这种全角写法很常见，
      不全角归一的话，后面所有正则都要写两遍（全角一遍、半角一遍），
      迟早会漏。先归一，再解析，规则只需要写一套。

    用 `unicodedata.normalize("NFKC", ...)` 是最省事的做法：
    它同时处理全角字母数字、全角标点、部分兼容字符（如 `Ｋ`→`K`）。
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    # 统一各种空白（含不换行空格）为普通空格，并压缩连续空白
    normalized = normalized.replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", normalized).strip()


def strip_html(html: str) -> str:
    """把 HTML 变成纯文本（给"JD 正文"用）。

    ★ 为什么不用第三方 HTML→文本库：
      这里的需求只有一个 —— 把标签去掉、把 `<br>`/`</p>`/`</li>` 变成换行。
      引入 bs4/lxml 的完整解析器会带来额外依赖，而 P2 的 JD 正文其实来自
      已经解析好的 DOM（Playwright 侧用 `inner_text()`），这条路径主要服务
      测试 fixture 与手工粘贴的 HTML 片段。

    顺序很重要：先处理"块级标签转换行"，再去标签，否则段落会粘成一整行。
    """
    if not html:
        return ""
    text = html
    # 0) 注释整段丢掉。
    #    ★ 为什么必须显式处理（实测踩过）：模板里常有注释残留（`… -->`），
    #      去标签的正则不会动注释内容，于是 JD 正文里会混进
    #      `· 岗位名 - 公司 - 站点 -->` 这种半截模板文字。
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    # 1) 脚本/样式整段丢掉（它们的文本不是内容）
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    # 2) 块级标签换成换行（保住段落结构）
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article)>", "\n", text)
    # 3) 列表项前缀（让"· 负责 xx"这种结构更容易被后续解析认出）
    text = re.sub(r"(?i)<li[^>]*>", "\n· ", text)
    # 4) 去掉剩下的所有标签
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    # 5) 反转义常见实体（顺序：&amp; 最后处理，避免二次转义）
    for entity, char in (
        ("&nbsp;", " "),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&mdash;", "—"),
        ("&middot;", "·"),
        ("&amp;", "&"),
    ):
        text = text.replace(entity, char)
    # 6) 逐行清理
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# 薪资
# ---------------------------------------------------------------------------
#: 薪资写法（都被真实招聘页面用过）：
#:     15-25K      15~25k      1.5-2.5万      20K-40K      13薪/14薪
#:     8千-1.2万   6000-8000元/月             面议/薪资面议
#:
#: ★★ 为什么要求"带单位"（实测踩过的坑）：
#:   原来把单位写成可选（`[kK千万]?`），于是 `20190806-145804` 这种
#:   **时间戳 / 内部 ID** 也被当成薪资区间抽了出来（页面里恰好有 "K"，
#:   连上下文检查都没拦住）。后果很隐蔽：`jh job list` 里薪资显示成
#:   `20190806-145804`，而 P4 的"按钱筛"会基于这个数字下判断。
#:
#:   现在的规则：**整个区间必须带单位，或者带"元/薪/月"这类钱的特征**。
#:   代价是 `15-25` 这种裸数字区间不再被识别 —— 那本来就需要靠上下文猜，
#:   而"猜错的代价（把时间戳当薪资）远大于留空"。
_SALARY_RANGE_RE = re.compile(
    # ★ 单位与数字之间**不留 `\s*`**：留了的话正则会在 `15-25K` 里从 `-` 之后
    #   重新开始匹配，把区间切成单个 `25K`（实测踩过，区间信息就丢了）。
    #   要求"单位紧贴数字"符合真实写法（`15-25K`、`1.5-2.5万`），
    #   而 `15 - 25 K` 这种带空格的写法本来也极少见。
    r"(?P<low>\d+(?:\.\d+)?)(?P<low_unit>[kK千万])?\s*[-~—–至到]\s*"
    r"(?P<high>\d+(?:\.\d+)?)(?P<high_unit>[kK千万])?"
    r"\s*(?:元|块)?(?:/月|每月|月薪)?"
)
_SALARY_SINGLE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[kK千万])(?!\d)")
#: "13薪 / 14薪 / 15薪" → 发几个月
_SALARY_MONTHS_RE = re.compile(r"(?P<months>1[0-9])\s*薪")
#: 薪资相关的上下文词（用来确认"这一段确实是在说钱"，避免把"3-5年"当薪资）
_SALARY_CONTEXT = ("薪", "k", "K", "万", "元", "工资", "月薪", "日薪", "时薪")

#: 单位换算：元/月
_UNIT_MULTIPLIER = {"k": 1000, "K": 1000, "千": 1000, "万": 10000}


def _to_yuan(value: str, unit: str | None, fallback_unit: str | None) -> int | None:
    """把 "15" + "K" 换算成 15000。单位缺失时用 fallback（区间里另一端的单位）。"""
    try:
        number = float(value)
    except ValueError:
        return None
    resolved = unit or fallback_unit
    if resolved in _UNIT_MULTIPLIER:
        return int(number * _UNIT_MULTIPLIER[resolved])
    # 没有单位：`6000-8000` 这种就是元；而 `15-25` 多半是 K（中文招聘页面的习惯）
    if number < 100:
        return int(number * 1000)
    return int(number)


def extract_salary(text: str) -> tuple[str | None, int | None, int | None]:
    """从文本里抽薪资。

    Returns:
        `(原文, 月薪下限(元), 月薪上限(元))`。认不出时返回 `(原文或 None, None, None)`。

    ★ 为什么必须数值化：`jh today` 要能按钱筛、`jh offer compare` 要能横向比。
      只留 "15-25K·14薪" 这种原文，比较就只能靠人眼。
    ★ 为什么同时留原文：数值化规则将来会改（比如遇到"日薪"），
      原文留着才能离线重算，不必重新抓页面。
    """
    normalized = normalize_text(text)
    if not normalized:
        return None, None, None

    # 找"含薪资上下文"的那一小段，避免误抓正文里的普通数字区间
    if not any(token in normalized for token in _SALARY_CONTEXT):
        return None, None, None

    months_match = _SALARY_MONTHS_RE.search(normalized)
    months_text = f"{months_match.group('months')}薪" if months_match else ""

    # 区间薪资：必须"带单位"或"带元/月这类钱的特征"。
    # 只用"上下文里有 K"来兜底是不够的（实测：时间戳所在的那段文字里恰好有 K）。
    #
    # ★ 注意"单位只写在高位"是最常见的写法：`15-25K` 里的 `K` 同时管两边。
    #   所以判据是"**至少有一边带单位**"，而不是"两边都带" ——
    #   要求两边都带会把 `15-25K` 直接漏掉（实测踩过）。
    for range_match in _SALARY_RANGE_RE.finditer(normalized):
        low_unit = range_match.group("low_unit")
        high_unit = range_match.group("high_unit")
        tail = range_match.group(0)
        has_money_tail = bool(re.search(r"(元|块|/月|每月|月薪)", tail))
        if not (low_unit or high_unit or has_money_tail):
            continue
        # 高位单位是共享单位（`15-25K` 的 K 管两边）
        shared_unit = high_unit or low_unit
        low = _to_yuan(range_match.group("low"), low_unit, shared_unit)
        high = _to_yuan(range_match.group("high"), high_unit, shared_unit)
        # 上限比下限小 → 大概率是"5-8千"这类写法被识别反了，交换一下更安全
        if low is not None and high is not None and low > high:
            low, high = high, low
        # 数值离谱（比如把 8 位数字当薪资）→ 丢掉，宁可留空
        if low is not None and not (500 <= low <= 2_000_000):
            continue
        display = tail.strip()
        if months_text and months_text not in display:
            display = f"{display}·{months_text}"
        return display, low, high

    single_match = _SALARY_SINGLE_RE.search(normalized)
    if single_match:
        value = _to_yuan(single_match.group("value"), single_match.group("unit"), None)
        display = single_match.group(0).strip()
        if months_text:
            display = f"{display}·{months_text}"
        return display, value, value

    # 只认出"14薪"但没有数字 → 不算薪资（宁可留空也不编）
    return None, None, None


# ---------------------------------------------------------------------------
# 城市 / 经验 / 学历 / 实习
# ---------------------------------------------------------------------------
def extract_city(text: str) -> str | None:
    """从文本里认城市。

    支持的真实写法：`深圳`、`深圳·南山区`、`广东深圳`、`深圳市南山区`、
    `深圳 南山`、`工作地点：深圳市南山区科技园`。
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    for city in KNOWN_CITIES:
        if city in normalized:
            return city
    return None


#: 经验要求写法：`3-5年`、`3年以上`、`1年以内`、`经验不限`、`应届生`、`无经验`
_EXPERIENCE_RANGE_RE = re.compile(r"\d+\s*[-~—–至到]\s*\d+\s*年")
_EXPERIENCE_ABOVE_RE = re.compile(r"\d+\s*年(?:以上|及以上|\+)")
_EXPERIENCE_BELOW_RE = re.compile(r"\d+\s*年(?:以内|以下)")
_EXPERIENCE_UNLIMITED_RE = re.compile(r"(经验不限|不限经验|无需经验|无经验|经验无要求)")
_EXPERIENCE_FRESH_RE = re.compile(r"(应届生?|应届毕业生|在校生|实习)")


def extract_experience(text: str) -> str | None:
    """认经验要求，返回原文片段（如 `3-5年`、`经验不限`、`应届生`）。

    ★ 不把它翻译成数字：`经验不限`、`应届生`、`3年以上` 这几种写法的语义差别很大，
      硬压成"最少几年"会丢掉信息。P3 的硬条件维度会按需判断
      （而且评审明确要求"硬条件不满足是**大幅降分**而非淘汰"）。
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    for pattern in (
        _EXPERIENCE_RANGE_RE,
        _EXPERIENCE_ABOVE_RE,
        _EXPERIENCE_BELOW_RE,
        _EXPERIENCE_UNLIMITED_RE,
        _EXPERIENCE_FRESH_RE,
    ):
        match = pattern.search(normalized)
        if match:
            return match.group(0).strip()
    return None


_DEGREE_ORDER = ("博士", "硕士", "研究生", "本科", "学士", "大专", "专科", "中专", "高中")
#: ★ 学历正则必须**先长后短**，并且带上可选的"及以上/以上/学历"后缀。
#:   踩过的坑：`("博士","硕士",…,"本科","学士",…)` 这种顺序里 `学士` 排在 `本科` 之后，
#:   在 "本科及以上" 上会先匹配到 `本科`（正确），但在 "学士学位" 上又会因为
#:   `本科|学士` 的交替顺序产生难解释的结果。显式按长度降序 + 非捕获分组最稳。
_DEGREE_RE = re.compile(
    r"(?:" + "|".join(sorted(_DEGREE_ORDER, key=len, reverse=True)) + r")"
    r"(?:及以上|以上|学历|学位)?"
)


def extract_education(text: str) -> str | None:
    """认学历要求（本科 / 大专及以上 / 硕士 …）。"""
    normalized = normalize_text(text)
    if not normalized:
        return None
    match = _DEGREE_RE.search(normalized)
    return match.group(0).strip() if match else None


#: 实习岗的强信号
_INTERN_STRONG = ("实习生", "实习岗", "暑期实习", "日常实习", "实习期")
#: 兼职/日结——**不是**实习，别混为一谈
_PARTTIME_HINTS = ("兼职", "日结", "小时工", "临时工")


def looks_like_intern(text: str) -> bool | None:
    """判断是不是实习岗。

    Returns:
        True / False / **None**（判断不了）。三者语义不同：
        `None` 表示"没看到足够信号"，绝不能当成 False ——
        那会把实习岗误标成全职，用户按"全职"筛的时候就把实习机会漏掉了。

    ★ 为什么不用"标题里有没有'实习'两个字"这么简单：真实标题可能是
      "AI 应用开发（2027 届，可转正）"这种**隐性实习**，
      而正文里写"有实习经历者优先"的却是全职岗。所以这里：
        · 强信号（实习生/实习岗…）→ True
        · 兼职/日结 → False（这是另一类，不是实习）
        · 只有"实习"两字出现在正文里 → None（信息不足，不猜）
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    if any(hint in normalized for hint in _INTERN_STRONG):
        return True
    if any(hint in normalized for hint in _PARTTIME_HINTS):
        return False
    if "实习" in normalized:
        # "有实习经历者优先" 这类是要求，不是岗位性质 → 判不出来
        if re.search(r"实习经历|实习经验", normalized):
            return None
        return True
    return None


# ---------------------------------------------------------------------------
# 去重用的归一化
# ---------------------------------------------------------------------------
#: 归一化时要去掉的内容（对"是不是同一个岗位"没有区分度）
_SALARY_NOISE_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*[-~—–至到]\s*\d+(?:\.\d+)?\s*[kK千万]?\s*(?:元)?(?:/月)?)|"
    r"(\d+\s*薪)|(薪资面议)|(面议)"
)
#: 归一化时要去掉的"修饰噪音"（HR 会随手改，但岗位没变）
_TITLE_NOISE_WORDS = (
    "急招",
    "急聘",
    "高薪",
    "诚聘",
    "招聘",
    "热招",
    "新增",
    "诚招",
    "（急聘）",
    "(急聘)",
    "双休",
    "五险一金",
    "包吃住",
)


#: 括号注释（公司的分公司/简称、职位的补充说明）
_PARENTHETICAL_RE = re.compile(r"[（(][^（()）]*[)）]")
#: 公司名的组织形式后缀（对"是不是同一家公司"没有区分度）
_COMPANY_SUFFIX_RE = re.compile(
    r"(有限责任公司|有限公司|股份有限公司|科技有限公司|公司|集团|corporation|inc|ltd|llc)"
)


def _strip_parenthetical(text: str) -> str:
    """去掉**内含城市名**的括号注释。

    ★ 这里的边界很关键（实测踩过两次，两个方向都错）：
        · 去掉**所有**括号 → `Python开发工程师（实习）` 与 `Python开发工程师`
          变成同一个岗位 —— 而实习和全职是**两个不同的岗位**，合并会造成错投；
        · 一个都不去     → `某某科技（深圳）有限公司` 里的"深圳"卡在字符串中间，
          与 HR 在别处写的 `某某科技有限公司` 对不上，同一岗位被当成两个。

      折中且安全的规则：**只去掉"括号里出现已知城市名"的那种注释**。
      城市是分公司标记，对"是不是同一个岗位"确实没有区分度；
      而"（实习）""（校招）""（急聘）"这类必须保留（前两个改性质，后一个虽无区分度
      但交给 `_TITLE_NOISE_WORDS` 单独处理，不该在这里一刀切）。
    """

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(0)[1:-1]
        return "" if any(city in inner for city in KNOWN_CITIES) else match.group(0)

    return _PARENTHETICAL_RE.sub(_replace, text)


def _clean_company_for_dedup(company: str) -> str:
    """公司名比对公司名时的归一化：去掉城市括号注释与组织形式后缀。

    ★ 为什么要去括号：真实公司名常写成 `某某科技（深圳）有限公司`，
      而 HR 在别处可能只写 `某某科技有限公司`。不去的话括号里的城市名
      会留在字符串中间，两者对不上 → 同一岗位被判成两个。
    """
    text = _strip_parenthetical(company)
    text = _COMPANY_SUFFIX_RE.sub("", text)
    return text


def normalize_for_dedup(*parts: str) -> str:
    """把公司 / 职位 / 城市等字段归一化成"用于比对"的字符串（供 dedup hash 用）。

    ★★ 这是去重第三层（归一化 hash）的核心，规则直接决定"会不会把同一个岗位当成两个"。

    处理的真实噪音（HR 改标题不改岗位是常态）：
        · 大小写、空格、全半角差异     → `AI Agent` vs `ai  agent`
        · 薪资写进标题                 → `Python开发（15-25K）` vs `Python开发`
        · "急招/高薪/诚聘"等修饰词      → `急招Python开发` vs `Python开发`
        · 公司的城市括号与"有限公司"    → `某科技（深圳）有限公司` vs `某科技有限公司`

    ★ 刻意**不做**的事：不删"（实习）/（校招）"这类词。
      它们改变岗位性质，删了会把实习岗和全职岗合并 —— 那是会造成错投的错误。
    """
    cleaned_parts: list[str] = []
    for part in parts:
        if not part:
            continue
        text = normalize_text(part)
        # 公司字段做额外清理（城市括号 + 组织形式后缀）
        text = _clean_company_for_dedup(text)
        text = text.lower()
        text = _SALARY_NOISE_RE.sub("", text)
        for word in _TITLE_NOISE_WORDS:
            text = text.replace(word.lower(), "")
        # 去掉所有非字母数字与汉字的字符（括号、标点、分隔符、空格）
        text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
        if text:
            cleaned_parts.append(text)
    return "|".join(cleaned_parts)
