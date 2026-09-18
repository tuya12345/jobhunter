"""简历服务：导入解析 / 列表 / **拆分方向建议**（P1）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这一层解决什么问题                                                         ║
╚══════════════════════════════════════════════════════════════════════════╝

现状（用户原话）：**只有 1 份简历、没分方向**，用同一份海投所有岗位。

所以 P1 做两件事：
    ① `jh resume add` —— 把 docx 变成**结构化数据**落库（后续匹配/定制/开场白都要用）；
    ② `jh resume split` —— 帮我把"这一份"拆成「AI Agent 方向 / 前端方向」，
       告诉我**哪些项目/技能该前置、哪些该弱化、还缺什么**。

★ 两条硬纪律

  1. **`split` 只给建议，绝不改写简历**。
     真正改简历是 P5 的 `tailor`，而且那一步必须有 FidelityGuard 把关。
     P1 就动手改文件，风险极高且不可逆。

  2. **默认不调 LLM**。
     解析优先走确定性规则（分段词表 + 技能词表），LLM 兜底开关默认**关闭**。
     理由：① 简历是隐私数据，能不往外发就不发；② 规则解析的结果可复现、好测；
     ③ 规则解析失败时会**明确告诉你哪一段没认出来**，而不是悄悄编一段像模像样的内容。
     接口里给 P3 的 LLM 兜底留了位置（见 `ResumeParser.llm_fallback`）。

★ content_hash 为什么是这张表的重点字段

      sha256(规范化后的结构化 JSON)

  它是 LLM 缓存 key 的组成部分（`sha256(prompt_version|model|jd_hash|content_hash)`）。
  没有它就会出现"我改了简历，同一岗位却仍然返回旧匹配分"—— 而"改了简历分数应该变"
  恰恰是这个工具存在的核心理由。所以 hash 里要包含**所有对匹配有影响的内容**，
  但**不能包含** file_path / 导入时间这类"变了也不影响匹配结果"的东西
  （否则每次重新导入都会让缓存失效、白花钱）。

★ 解析不追求"完美"，追求**可解释 + 可降级**

  中文简历的排版千奇百怪（两栏表格、文本框、图标、时间轴样式）。
  与其追求 100% 解析率，不如：
    · 认得出的段落精确解析；
    · 认不出的原文完整保留在 `raw_text`（P3 的 LLM 兜底与 P5 的定制都用得上）；
    · 明确报告"哪几段没认出来"，让用户能自己补。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.models import Resume
from jobhunter.utils.io import from_json, to_json
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "AddResumeResult",
    "DirectionProfile",
    "EducationItem",
    "InternshipItem",
    "ProjectFit",
    "ProjectItem",
    "ResumeContent",
    "ResumeParseError",
    "ResumeSummary",
    "SplitSuggestion",
    "add_resume",
    "analyze_split",
    "compute_content_hash",
    "content_from_json",
    "derive_direction",
    "education_end_year",
    "extract_skills",
    "list_resumes",
    "load_direction_profiles",
    "load_skill_vocabulary",
    "parse_resume_file",
    "parse_resume_text",
    "resume_skill_pool",
    "split_resume",
    "strip_volatile_content",
]


# ===========================================================================
# 数据模型
# ===========================================================================
class EducationItem(BaseModel):
    """一条教育经历。"""

    school: str = ""
    major: str = ""
    degree: str = ""
    period: str = ""


class ProjectItem(BaseModel):
    """一个项目经历。"""

    #: 项目名（含一行里能认出的全部前缀信息，保证"原文一定在"）
    name: str = ""
    role: str = ""
    period: str = ""
    #: 项目描述的分行内容（每条是一行）
    bullets: list[str] = Field(default_factory=list)
    #: 从描述里识别出的技能（用于 split 打分）
    skills: list[str] = Field(default_factory=list)


class InternshipItem(BaseModel):
    """一段实习经历。"""

    company: str = ""
    role: str = ""
    period: str = ""
    bullets: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class ResumeContent(BaseModel):
    """简历的结构化内容（`resumes.content_json` 存的就是它的 JSON）。

    字段与设计文档 docs/data_model.md 里写的一致，不要随意改名 ——
    P3 的匹配、P5 的定制都按这些字段读。
    """

    basic: dict[str, str] = Field(default_factory=dict)
    education: list[EducationItem] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    internships: list[InternshipItem] = Field(default_factory=list)
    #: 原文兜底：规则解析漏掉的段落还能被 LLM 兜住（P3）
    raw_text: str = ""
    #: 分段结果：段落名 → 该段的原始行（便于排查"这段为什么没被认出来"）
    sections: dict[str, list[str]] = Field(default_factory=dict)
    #: 没认出来的连续文本块数量（用来提示用户"解析不完整"）
    unparsed_blocks: int = 0

    def skill_names(self) -> list[str]:
        return [s for s in self.skills]


class ResumeParseError(ValueError):
    """解析失败（文件读不了 / 里面没有任何文本）。"""


# ===========================================================================
# 技能词表
# ===========================================================================
#: 内置技能词表。
#:
#: ★ 为什么要有内置词表，而不是只靠 config.yaml 的 skill_aliases：
#:   config 里的别名表是"归一化"用的（把 大模型/LLM 归到一个名字），
#:   用户不可能把所有技能都列进去。没有内置词表，抽取就会漏掉一大半技能，
#:   `jh gap` 和 `jh resume split` 都会失真。
#:
#: ★ 名字用**技能本名**（Python / Vue），中文写法的同义词放在 config 的别名表里。
#:   这样展示出来的技能名是国际通用的写法，看简历的 HR/面试官一眼能认。
_BUILTIN_SKILLS: tuple[str, ...] = (
    # 语言
    "Python",
    "Java",
    "JavaScript",
    "TypeScript",
    "C++",
    "C#",
    "Go",
    "Rust",
    "PHP",
    "Ruby",
    "Kotlin",
    "Swift",
    "Dart",
    "SQL",
    "Shell",
    "Bash",
    # 前端
    "HTML",
    "CSS",
    "Sass",
    "Less",
    "Vue",
    "React",
    "Angular",
    "Svelte",
    "jQuery",
    "Webpack",
    "Vite",
    "Rollup",
    "Babel",
    "Element Plus",
    "Ant Design",
    "Tailwind",
    "ECharts",
    "Axios",
    "Pinia",
    "Vuex",
    "小程序",
    "UniApp",
    "Flutter",
    "响应式布局",
    "组件库",
    "前端工程化",
    "SSR",
    "Node.js",
    "Nginx",
    # 后端 / 数据库
    "FastAPI",
    "Django",
    "Flask",
    "Tornado",
    "Spring",
    "SpringBoot",
    "MyBatis",
    "MySQL",
    "PostgreSQL",
    "SQLite",
    "MongoDB",
    "Redis",
    "Elasticsearch",
    "消息队列",
    "RabbitMQ",
    "Kafka",
    "Celery",
    "gRPC",
    "RESTful",
    "GraphQL",
    "HTTP",
    "微服务",
    "并发",
    "多线程",
    "异步",
    "性能优化",
    "单元测试",
    "接口文档",
    # 数据 / 运维
    "Docker",
    "Kubernetes",
    "Jenkins",
    "Git",
    "GitHub",
    "GitLab",
    "Linux",
    "CI/CD",
    "Prometheus",
    "Grafana",
    "阿里云",
    "腾讯云",
    "AWS",
    "部署",
    # 大模型 / AI
    "LLM",
    "大模型",
    "RAG",
    "Prompt",
    "Agent",
    "LangChain",
    "LlamaIndex",
    "向量数据库",
    "Milvus",
    "FAISS",
    "Chroma",
    "Pinecone",
    "Embedding",
    "微调",
    "LoRA",
    "PEFT",
    "Transformer",
    "Attention",
    "PyTorch",
    "TensorFlow",
    "OpenAI",
    "DeepSeek",
    "Qwen",
    "Dify",
    "Coze",
    "MCP",
    "Function Calling",
    "多智能体",
    "工作流编排",
    "流式输出",
    "模型评测",
    "数据清洗",
    "爬虫",
    "Pandas",
    "NumPy",
    "Matplotlib",
    "Jupyter",
    "机器学习",
    "深度学习",
    "NLP",
    "CV",
    "OpenCV",
    "YOLO",
    "推荐系统",
    "知识图谱",
    "OCR",
    "语音识别",
    # 工程素养
    "数据结构",
    "算法",
    "设计模式",
    "代码评审",
    "需求分析",
    "项目管理",
    "敏捷开发",
)


def load_skill_vocabulary(settings: AppSettings | None = None) -> dict[str, list[str]]:
    """合并内置词表与 config 的别名表，返回 `规范名 → 该技能的所有写法`。

    合并规则：
        · 内置词表的每个技能名，写法集合初始为 `[技能名]`；
        · config 的 `skill_aliases` 里的每个 key 追加进写法集合
          （key 本身也可能是内置词表里没有的新技能，那就新建一条）。

    为什么返回的是"写法表"而不是"技能名列表"：
        抽取时要按**所有写法**去文本里找，但汇报给用户时只报**规范名**，
        这样 `大模型 / LLM / 大语言模型` 不会在统计里被算成三个技能。
    """
    settings = settings or get_settings()
    vocabulary: dict[str, list[str]] = {name: [name] for name in _BUILTIN_SKILLS}

    for canonical, aliases in settings.skill_aliases.items():
        bucket = vocabulary.setdefault(canonical, [])
        for alias in aliases:
            if alias and alias not in bucket:
                bucket.append(alias)

    # 学习白名单里的技能也要能被识别出来 —— 否则"我在学 LangChain"
    # 这句话都抽不出 LangChain，白名单机制就形同虚设。
    for name in settings.learning_whitelist:
        vocabulary.setdefault(name, [name])

    return vocabulary


def _has_cjk(text: str) -> bool:
    """文本里是否含中日韩汉字/假名（这类字符没有 ASCII 意义上的"词边界"）。

    ★ 为什么必须单独判断，而不能靠 `ch.isalnum()`：
      `"大".isalnum()` 在 Python 里是 **True**（汉字属于字母类）。
      于是"按 isalnum 判断要不要加 `\\b`"会把中文别名也套上 `\\b`，
      而 `\\b` 的边界定义基于 `\\w`，`\\w` **包含汉字** →
      `\\b大模型\\b` 要求它旁边是标点或空白，在"做大模型应用"里永远匹配不上。
      这是个静默失效的 bug（不报错、只是抽不到技能），必须专门测。
    """
    return any(
        "\u4e00" <= ch <= "\u9fff"  # CJK 统一表意文字
        or "\u3040" <= ch <= "\u30ff"  # 平假名 / 片假名
        or "\uac00" <= ch <= "\ud7af"  # 韩文
        for ch in text
    )


def _alias_pattern(alias: str) -> re.Pattern[str] | None:
    """为一个技能写法编译匹配模式。

    ★★ 这是整个 P1 最容易写错的地方（评审专门点出来的 bug）

        中文没有词边界，`re.search("Java", "JavaScript")` 会命中，
        `re.search("Go", "Google")` 也会命中 —— 于是简历里写了 JavaScript 的人，
        会被系统认为"会 Java"，匹配分数虚高。

    所以按写法分两类处理：

        · **纯 ASCII 写法**（Java / Go / Vue / Node.js）
          → **两侧都不用 `\\b`**，改用"只排除 ASCII 字母数字下划线"的环视
            （见下面那段注释：`\\b` 是 Unicode 语义，汉字属 `\\w`，
            于是 `\\bJava` 匹配不到"熟悉Java开发"）；
            以符号结尾的写法（`C++`）不需要右边界。

        · **含中文的写法**（大模型 / 响应式布局）
          → `\\b` 完全不可用（见 `_has_cjk` 的说明）；边界只排除 ASCII 字母数字，
            即"接触过大模型"能命中（"过"是合法边界），"大模型X"不会命中。

    大小写统一忽略（`Python` = `python`），因为简历里两种写法都常见。
    """
    alias = alias.strip()
    if not alias:
        return None

    escaped = re.escape(alias)

    if _has_cjk(alias):
        # 中文写法：绝不能用 \b，也不能用 \w 做边界。
        #
        # ★ 为什么不能写成 (?<!\w)…(?!\w)（踩过两次的坑）：
        #   Python 正则里的 `\w` 默认是 **Unicode 感知**的，**汉字属于 \w**！
        #   于是 `(?<!\w)大模型(?!\w)` 在 "接触过大模型" 里匹配不上 ——
        #   因为"过"被当成了单词字符，lookbehind 判定失败。
        #   正确做法：边界只排除 **ASCII 字母数字下划线**，
        #   这样"过"（非 ASCII）算合法边界，而 "大模型X"（X 是 ASCII 字母）仍被正确拒绝。
        return re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", re.IGNORECASE)

    # ASCII 写法：按两侧字符类型分别决定边界策略
    #
    # ★★ 两侧**都不能用 `\b`** —— 这是同一个坑的两半，曾经只修了右半边
    #    （P3 验证会话用「熟悉Java开发」抓出来的，见 docs/ISSUES.md #1）
    #
    #   Python 正则的 `\b` 是 **Unicode 语义**的：`\w` 包含汉字，
    #   所以"悉"与"J"之间**没有**词边界 → `\bJava` 匹配不到"熟悉Java开发"，
    #   而中文 JD 里"熟悉Java开发"恰恰是最自然的写法（带空格的"熟悉 Java 开发"反而少见）。
    #   后果不止技能维度：方向判定与 `jh gap` 都吃这同一份抽词结果。
    #
    #   正确做法（两侧对称）：边界只排除 **ASCII 字母数字下划线** ——
    #     · 左侧 `(?<![A-Za-z0-9_])`：汉字/标点后面算合法起点，"XJava" 仍被拒；
    #     · 右侧 `(?![A-Za-z_])`：**不含数字** —— `Vue` 必须命中 `Vue3`、
    #       `TypeScript` 必须命中 `TypeScript5`（版本号很常见）；
    #       字母必须排除，这才是 `Java` 不命中 `JavaScript` 的关键。
    #     · 以符号结尾的写法（`C++`、`C#`）不需要右边界 —— `C++11` 也该算命中。
    left = r"(?<![A-Za-z0-9_])" if (alias[0].isalnum() or alias[0] == "_") else ""
    is_word_tail = alias[-1].isalnum() or alias[-1] == "_"
    right = r"(?![A-Za-z_])" if is_word_tail else ""

    return re.compile(left + escaped + right, re.IGNORECASE)


def resume_skill_pool(content: ResumeContent) -> tuple[set[str], set[str]]:
    """取出简历的技能全集。

    Returns:
        (pool, explicit)
        pool     —— 出现在任何地方（技能栏 / 项目描述 / 实习描述）的技能
        explicit —— 只在"专业技能"栏主动列出的技能

    ★ 为什么**不**把 `raw_text` 也算进来：
      `raw_text` 是整份简历的原文兜底，包含"我在学 LangChain"这种句子。
      把它算进技能池，等于**声称自己会**一个还在学的技能 ——
      匹配分虚高，而这恰恰是这个工具最不该犯的错（骗自己）。

    ★ 为什么放在 resume_svc（P4 从 match_svc 搬过来）：
      匹配要用它、`jh gap` 要用它、**防编造校验（fidelity）也要用它** ——
      而后者不该依赖"匹配"那一层。放在这里，三方都只依赖简历模型本身。
    """
    explicit = set(content.skills)
    pool = set(explicit)
    for item in content.projects:
        pool.update(item.skills)
    for item in content.internships:
        pool.update(item.skills)
    return pool, explicit


def extract_skills(
    text: str,
    vocabulary: dict[str, list[str]] | None = None,
) -> list[str]:
    """从一段文本里抽出规范化后的技能名（去重、保持稳定顺序）。

    顺序策略：按**词表顺序**输出，而不是按出现顺序 ——
    这样同一份简历重复解析结果完全一致（content_hash 才稳定）。
    """
    vocabulary = vocabulary or load_skill_vocabulary()
    return [name for name, aliases in vocabulary.items() if _match_any(text, aliases)]


def _match_any(text: str, aliases: list[str]) -> bool:
    """文本里是否出现了该技能的任意一种写法。"""
    if not text:
        return False
    return any(pattern.search(text) for pattern in (_alias_pattern(a) for a in aliases) if pattern)


# ===========================================================================
# 文本工具
# ===========================================================================
_DATE_RE = re.compile(
    # ★★ 月份必须写成 `0?[1-9]|1[0-2]`，**并且后面紧跟 `(?!\d)`**。
    #    漏掉负向断言会连续踩两次坑（都是真实简历实测出来的）：
    #      · "2023.10" 里先匹配到 "2023.1" → 10 月被拆成 1 月；
    #      · "2026.08" 里先匹配到 "2026.0" → 整个时间段的解析全错，
    #        连"这一行算不算项目标题"的判断都跟着失效，**项目解析直接归零**。
    #    正确语义：月份要么不吃，要么整段吃完，绝不能吃掉一半。
    #
    # ★ 分隔符集合要收全。真实简历里出现过的：`2023.09 ～ 2027.06`（全角波浪号 U+FF5E）、
    #   `2023年9月—2027年6月`、`2023/09-2027/06`、`2023.09至今`。
    #   少一个字符，时间段就抽不出来，项目排序与"教育经历"解析会一起失效。
    r"(?P<y1>19\d{2}|20\d{2})\s*[年./\-]\s*(?P<m1>0?[1-9]|1[0-2])?(?!\d)"
    r"(?:\s*月)?"
    # 分隔符用 `*` 而不是 `+`：中文简历经常写 `2024.03至今`（**没有**短横线），
    # 用 `+` 的话"至今"这个信息会被整个丢掉，时间段会被当成"已结束"，
    # 于是"最近的经历排最前"这个排序规则直接失效。
    r"(?:\s*[-~～—–－至到]*\s*"
    r"(?:(?P<y2>19\d{2}|20\d{2})\s*[年./\-]\s*(?P<m2>0?[1-9]|1[0-2])?(?!\d)(?:\s*月)?"
    r"|(?P<ongoing>至今|现在|now|Now|NOW)))?"
)

#: 表格单元格分隔符（utils/docx.py 把同一行的多个单元格用 `" | "` 连接）
_CELL_SEP = " | "

#: 表头行里的常见词。整行只由这些词组成时，判定为表头而不是数据
#: （简历模板常把"学校 | 专业 | 时间"做成一行表头，不排除会被当成教育经历）。
_TABLE_HEADER_WORDS = frozenset(
    {
        "学校",
        "院校",
        "专业",
        "专业学位",
        "学位",
        "学历",
        "时间",
        "起止时间",
        "公司",
        "单位",
        "岗位",
        "职位",
        "部门",
        "项目",
        "项目名称",
        "项目描述",
        "工作内容",
        "描述",
        "职责",
        "名称",
        "内容",
        "备注",
        "序号",
    }
)


def _normalize_table_word(cell: str) -> str:
    """归一化表头单元格：去掉空格与分隔符，便于与词表比对。

    `专业 / 学位` → `专业学位`、`起止时间` → `起止时间`、`工作内容` → `工作内容`
    """
    return re.sub(r"[\s\u3000/／|、,，·]+", "", cell)


def _is_table_header(line: str) -> bool:
    """判断一行是不是"表头行"（如 `学校 | 专业 / 学位 | 时间`）。

    ★ 两个必须容忍的真实情况（都实测踩过）：
      ① **末尾空单元格**：Word 表格常带一个空的格式列，读出来是 `学校 | 专业 | 时间 |`
         —— 严格等值匹配会漏判，于是表头被当成一条教育经历，
         教育经历里第一条永远是"学校"。
      ② **单元格里带分隔符**：`专业 / 学位` 这种写法要归一化成 `专业学位` 再比对。
    """
    if _CELL_SEP not in line:
        return False
    meaningful = [_normalize_table_word(cell) for cell in line.split(_CELL_SEP) if cell.strip()]
    if not meaningful:
        return False
    return all(word in _TABLE_HEADER_WORDS for word in meaningful)


def _cell_texts(line: str) -> list[str]:
    """把一行拆成单元格文本（不是表格行时返回单元素列表）。"""
    if _CELL_SEP not in line:
        return [line]
    return [cell.strip() for cell in line.split(_CELL_SEP) if cell.strip()]


def _date_spans(text: str) -> list[tuple[int, int, str]]:
    """找出文本里所有时间段，返回 `[(start, end, 展示文本)]`（按位置排序）。

    ★ 为什么返回**位置**而不只是文本：调用方需要把时间段从原文里**抠掉**
      （比如"校园二手交易平台 2023.10 - 2024.01 后端负责人"里的时间），
      只知道文本的话，遇到重复出现就会抠错位置。
    """
    return [(m.start(), m.end(), m.group(0)) for m in _DATE_RE.finditer(text)]


def _extract_period(text: str) -> tuple[str, tuple[int, int] | None]:
    """抽**第一个**时间段。

    Returns:
        (展示用文本, 排序键)。排序键是 (年, 月)，"至今"用 (9999, 12) 保证排最前。
        认不出来就返回 ("", None) —— **不编造时间**。
    """
    spans = _date_spans(text)
    if not spans:
        return "", None
    _, _, display = spans[0]
    return display, _period_sort_key(display)


def _period_sort_key(display: str) -> tuple[int, int] | None:
    """把时间段展示文本换算成排序键 (结束年, 结束月)。"""
    match = _DATE_RE.search(display)
    if not match:
        return None
    if match.group("ongoing"):
        return (9999, 12)
    if match.group("y2"):
        return (int(match.group("y2")), int(match.group("m2") or 1))
    return (int(match.group("y1")), int(match.group("m1") or 1))


def education_end_year(period: str) -> int | None:
    """从一段教育经历的 period 里取**结束年份**（判"是不是应届生"用）。

    ★ 为什么把它做成公开函数（而不是让 P3 直接用 `_period_sort_key`）：
      "应届生判定"是 P3 匹配的**业务规则**，它依赖"教育经历的结束年份"这个
      业务含义；而 `_period_sort_key` 是排序用的内部工具。
      直接跨模块用私有函数，将来改排序逻辑会**静默改掉应届判定** ——
      这类耦合最难查。包一层之后，两边的意图都写在名字里了。

    ★ 为什么"至今"（9999）要特判成 None：
      教育经历写"2023.09 至今"说明还在读 —— 这当然是应届，
      但返回 9999 会让调用方算出"9999 年毕业"这种荒唐结论。
      由调用方按 None 处理成"判不出来"，比在这里猜一个年份诚实。
    """
    key = _period_sort_key(period or "")
    if key is None:
        return None
    year, _month = key
    return None if year >= 9999 else year


def _strip_periods(text: str, spans: list[tuple[int, int, str]]) -> str:
    """把给定位置的时间段从文本里去掉（用于得到干净的"名称"部分）。"""
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end, _ in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _merge_spans(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """合并重叠的时间段 span。

    ★ 为什么需要：正则会把 "2023.10 - 2024.01" 整个匹配成**一个** span，
      而 "2023.10" 这个前缀同时也能单独匹配。不去重的话，
      抠掉时间后会残留 "0 - 2024.01" 这种碎片（实际踩过）。
      规则：如果某个 span 完全落在另一个 span 内，就丢掉它。
    """
    if not spans:
        return []
    ordered = sorted(spans, key=lambda item: (item[0], -item[1]))
    merged: list[tuple[int, int, str]] = []
    for start, end, display in ordered:
        if merged and start >= merged[-1][0] and end <= merged[-1][1]:
            continue  # 被前一个 span 完全包含
        merged.append((start, end, display))
    return merged


_DEGREES = ("博士", "硕士", "研究生", "本科", "学士", "大专", "专科", "高中", "中专")

#: 手机号（中国大陆）。**只在内存里用于抽取，绝不写进日志/事件流**。
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
#: 邮箱：**用 ASCII 字符类收尾**，不能写 `[A-Za-z0-9.-]+\.[A-Za-z]{2,}` ——
#: 那样在"邮箱：a@b.com 城市：深圳"这种连排里会把后面的中文也吞进来
#: （实测：email 抽成了 `xiaoman.lin@example.com 城市：深圳`）。
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
#: 「字段标签：值」的抽取模式。
#:
#: ★ 为什么必须有这一套（真实简历逼出来的）
#:   中文简历模板常把多个字段挤在**同一行、连空格都没有**（下面用**虚构**数据举例）：
#:         `姓 名 ：林小满求职意向 ：Agent开发`
#:         `学 校 ： 虚构大学 手机号码 ：13800000000`
#:   所以字段值必须"从标签后面取，一直取到下一个标签或行尾"，
#:   而不是按空格/竖线切分（切不开，会得到一坨）。
#:   标签本身允许中间有空格（`姓 名`）、冒号允许全角半角。
#:
#: ⚠️ 这里的示例**必须是虚构数据**：本项目曾把真实简历的姓名/学校/手机号
#:   抄进注释当例子（P4 复查时发现并清理，见 docs/ISSUES.md #44）。
#:   仓库将来会公开，注释里的"示例"同样是泄露面。
_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "name": ("姓名", "名字"),
    "job_intention": ("求职意向", "应聘岗位", "目标岗位", "期望职位", "求职目标"),
    "school": ("学校", "院校", "毕业院校"),
    "phone": ("手机号码", "手机号", "手机", "联系电话", "电话"),
    "email": ("电子邮箱", "邮箱", "电子邮件", "邮件"),
    # ★ 地址的标签不能写裸的「城市」：字段值抽取是"取到下一个标签为止"，
    #   而 `… 邮箱：a@b.com 城市：深圳` 这样的连排里，
    #   裸「城市」会让"邮箱"字段把 `城市：深圳` 一起吞掉（实测踩过）。
    "address": ("现居地址", "现居住地", "所在城市", "现居城市", "现居"),
}

#: 构造"字段标签"的正则片段：允许标签字符之间有空白（`姓 名`）
_LABEL_GAP = r"[\s\u3000]{0,3}"


def _field_patterns() -> dict[str, re.Pattern[str]]:
    """为每个字段编译"标签 → 值"的抽取正则。

    值的取法：`：` 之后，一直取到**下一个字段标签**或行尾。
    这样 `姓 名 ：林小满求职意向 ：Agent开发` 能正确切成两个字段（虚构数据）。
    """
    all_labels: list[str] = []
    for labels in _FIELD_LABELS.values():
        for label in labels:
            all_labels.append(_LABEL_GAP.join(label))
    stop = "|".join(sorted(all_labels, key=len, reverse=True))

    patterns: dict[str, re.Pattern[str]] = {}
    for field, labels in _FIELD_LABELS.items():
        alternation = "|".join(_LABEL_GAP.join(label) for label in labels)
        patterns[field] = re.compile(
            # ★ 值的取法：`：` 之后取到"下一个字段标签"**或行尾**。
            #   行尾这条不能少 —— 真实简历里常见"学校：X 手机号码：Y"和
            #   "求职意向：Z"被排版拼成同一块，去掉行尾判定会让求职意向
            #   把后面整段（电话/邮箱/城市）一起吞掉（实测踩过）。
            rf"(?:{alternation})\s*[:：]\s*(?P<value>.*?)"
            # 值到"下一个字段标签"、"竖线"或"行尾"为止。
            #   · 行尾：必填项 —— 否则"求职意向：Z"会把后面整段吞掉；
            #   · 竖线：表格行摊平后同一行会有多个字段（`… | 电话：…`）。
            rf"(?=(?:{stop})\s*[:：]|[|\n]|$)",
            re.DOTALL,
        )
    return patterns


_FIELD_RE = _field_patterns()

_CITY_RE = re.compile(
    "(北京|上海|深圳|广州|杭州|成都|武汉|南京|西安|苏州|东莞|佛山|珠海|长沙|重庆|天津|厦门|合肥|郑州|青岛|大连|福州|济南)"
)


def _looks_like_heading(line: str) -> bool:
    """判断一行是不是"段标题"。

    两条判定路径（真实简历逼出来的）：
        ① **整行就是标题**（`项目经验`、`教育经历`）—— 长度受限、不含句读；
        ② **行首是标题词 + 后面直接跟内容**（`专业技能 RAG 问答模块：…`）——
           排版时标题和正文被放进同一个文本框是常事。
           这时仍然要认它是标题，否则那一整块内容会被丢进上一段里。

    ★ 为什么长度限制从 12 放宽到"要么短、要么以标题词开头"：
      原先写死 12 会漏掉路径②，导致整段技能内容归属错误。
    """
    cleaned = _clean_heading(line)
    if not cleaned:
        return False
    if len(cleaned) > 12 and not any(cleaned.startswith(name) for name in _SECTION_NAMES):
        return False
    # 标题不该有句号/逗号（有的话更像正文）
    if any(ch in cleaned for ch in "。，,.;；"):
        return False
    return any(
        cleaned == name or cleaned.startswith(name) or cleaned.endswith(name)
        for name in _SECTION_NAMES
    )


#: 段落名（按"越具体越靠前"排列，匹配时先命中先算）
_SECTION_NAMES: tuple[str, ...] = (
    "教育经历",
    "教育背景",
    "学历",
    "专业技能",
    "技能清单",
    "技能特长",
    "技能",
    "项目经验",
    "项目经历",
    "项目实践",
    "主要项目",
    "项目",
    # ★ 单字"经验"也要认：模板里"项目经验"常被拆开，
    #   残留的"经验"两个字会被当成一条项目（实测：项目列表里冒出一条"经验"）。
    "经验",
    "实习经历",
    "实习经验",
    "工作经历",
    "工作经验",
    "实践经历",
    "校园经历",
    "社团经历",
    "学生工作",
    "荣誉奖项",
    "获奖情况",
    "奖项",
    "自我评价",
    "个人评价",
    "自我介绍",
    "证书",
    "证书技能",
    "求职意向",
    "求职目标",
    "个人信息",
    "基本信息",
    "联系方式",
)

#: 段落名 → 内部段 key
_SECTION_KEY: dict[str, str] = {
    "教育经历": "education",
    "教育背景": "education",
    "学历": "education",
    "专业技能": "skills",
    "技能清单": "skills",
    "技能特长": "skills",
    "技能": "skills",
    "项目经验": "projects",
    "项目经历": "projects",
    "项目实践": "projects",
    "主要项目": "projects",
    "项目": "projects",
    "实习经历": "internships",
    "实习经验": "internships",
    "工作经历": "internships",
    "工作经验": "internships",
    "实践经历": "internships",
    "校园经历": "campus",
    "社团经历": "campus",
    "学生工作": "campus",
    "荣誉奖项": "awards",
    "获奖情况": "awards",
    "奖项": "awards",
    "经验": "projects",
    "自我评价": "summary",
    "个人评价": "summary",
    "自我介绍": "summary",
    "证书": "certificates",
    "证书技能": "certificates",
    "求职意向": "intention",
    "求职目标": "intention",
    "个人信息": "contact",
    "基本信息": "contact",
    "联系方式": "contact",
}


def _clean_heading(line: str) -> str:
    """去掉标题周围的装饰（`, 【】, :：, 空格, 表格竖线, 编号前缀`）。"""
    cleaned = line.strip().strip("|").strip()
    cleaned = cleaned.strip("【】[]（）()《》 ")
    cleaned = re.sub(r"^[0-9一二三四五六七八九十]+[、.．)）]\s*", "", cleaned)
    cleaned = cleaned.strip(":：-—· ")
    # 去掉 "教育经历 | 2020-2024" 这种同行的情况（取竖线左边）
    if "|" in cleaned:
        cleaned = cleaned.split("|", 1)[0].strip()
    return cleaned.strip()


def _section_of(line: str) -> str | None:
    """这一行属于哪个段（是标题行才返回 key，否则 None）。"""
    if not _looks_like_heading(line):
        return None
    cleaned = _clean_heading(line)
    # 从具体的名字里找（先长后短，避免"技能"抢了"专业技能"）
    for name in sorted(_SECTION_NAMES, key=len, reverse=True):
        if name in cleaned:
            return _SECTION_KEY[name]
    return None


#: 专业技能栏的"字段式"写法。真实简历里很常见，而且往往**没有**"专业技能"标题：
#:     `开发语言：熟练使用 Python、JavaScript…`
#:     `AI/Agent：熟悉 Agent 构成…`
#:     `全栈 / 服务：Node.js + Express…`
_SKILL_FIELD_RE = re.compile(
    r"^\s*(?:开发语言|编程语言|语言|技能|技术栈|AI\s*/\s*Agent|AI|Agent|"
    r"大模型|LLM|RAG|后端|前端|全栈|数据库|运维|工具|框架|其他)\s*(?:/\s*\S+)?\s*[:：]"
)


def _looks_like_skill_line(line: str) -> bool:
    """判断一行是不是"专业技能"栏的字段行（见 `_SKILL_FIELD_RE`）。

    ★ 为什么需要这个兜底：本项目的真实简历样本里，"专业技能"四个字和技能内容是
      被排版拆开的（标题在一个形状里、内容在另一个形状里），
      只靠标题分段会得到 **1 个技能**，而实际有 20+ 个。
      技能抽取是 P3 匹配打分的核心输入，漏掉等于这个工具白做。
    """
    if _SKILL_FIELD_RE.match(line):
        return True
    # 也认"熟练/掌握/熟悉 + 技术"这类没有字段名的写法
    return bool(re.match(r"^\s*(精通|熟练掌握|熟练|熟悉|掌握)\b", line)) and len(line) < 200


def _heading_remainder(line: str, heading_tail: str) -> str:
    """取"标题行里除标题之外的内容"。

    三种真实写法都要处理：
        `项目经验 | XX系统 2024.03-2024.09`  → 取竖线右侧
        `专业技能 RAG 问答模块：PDF 解析 → …`  → 取标题词右侧
        `教育经历`                            → 没有内容，返回 ""

    ★ 为什么不能只按 `|` 切（原先的写法）：真实简历里标题和正文之间
      **没有竖线**（它们在同一个文本框里，中间只有空格），
      只按竖线切会把整块正文当成标题丢掉 —— 实测导致技能从 20+ 个掉到 1 个。
    """
    if _CELL_SEP in line:
        tail_part = line.split(_CELL_SEP, 1)[1].strip()
        if tail_part and tail_part != heading_tail:
            return tail_part
        return ""

    body = line.strip()
    for name in sorted(_SECTION_NAMES, key=len, reverse=True):
        if body.startswith(name):
            # ★ 命中就必须**立刻返回**（哪怕剩余内容是空串）。
            #   踩过的坑：原写法是"剩余内容非空才 return"，于是 `项目经验` 会继续
            #   匹配到更短的 `经验`，把它当成"标题之外的残留内容"返回 →
            #   项目段里凭空多出一条名叫「经验」的空项目（实测）。
            return body[len(name) :].strip(" :：-—·|")
    return ""


#: `_section_owner` 的返回值：该行在第一个内容段标题**之前**，归属待定
_PENDING_HEAD = "__head__"

#: 内容段（"头部信息"不属于内容段）。
#: 用来判断"哪个标题是第一个内容段"，以及"前导块该归给谁"。
_CONTENT_SECTIONS = frozenset(
    {"education", "skills", "projects", "internships", "campus", "awards", "summary"}
)


def _section_owner(index: int, headings: list[tuple[int, str]]) -> str:
    """某个行号属于哪个段 —— 由"它之前最近的那个标题"决定。

    第一遍收集的 `headings` 是 `[(行号, 段key)]`（已按行号递增）。

    返回值：
        · 段 key（education / skills / projects / …）—— 正常归属；
        · `_PENDING_HEAD` —— 它之前**没有任何内容段标题**，
          需要在第二遍里交给"第一个内容段"（见调用处的说明）。
    """
    owner: str | None = None
    for position, key in headings:
        if position >= index:
            break
        owner = key
    if owner is None:
        return _PENDING_HEAD
    return owner


def _first_content_section(headings: list[tuple[int, str]]) -> str | None:
    """第一个**内容段**标题（跳过"个人信息/联系方式"这类头部段）。"""
    for _, key in headings:
        if key in _CONTENT_SECTIONS:
            return key
    return None


def _extract_degree(text: str) -> str:
    """从文本里认出学历（本科/硕士/…），认不出返回空串。"""
    for degree in _DEGREES:
        if degree in text:
            return degree
    return ""


def _strip_skills_notation(text: str) -> str:
    """去掉"精通/熟悉/了解"这类熟练度前缀 —— 它们对匹配没有信息量。"""
    return re.sub(r"(精通|熟练掌握|熟练|熟悉|了解|掌握|会用|使用过|具备)", "", text)


# ===========================================================================
# 解析
# ===========================================================================
def parse_resume_text(
    lines: list[str],
    *,
    settings: AppSettings | None = None,
    source_name: str = "",
) -> ResumeContent:
    """把摊平后的文本行解析成结构化简历。

    Args:
        lines: `utils.docx.read_document()` 给出的文本块（已按文档顺序）。
        settings: 配置（提供技能别名表）。
        source_name: 来源文件名（只用于日志，不参与 hash）。

    解析策略（按顺序）：
        ① 逐行判断是不是段标题 → 把行归入段；
        ② 每段用自己的规则抽字段；
        ③ 全文抽基础信息（手机号/邮箱/城市/求职意向）；
        ④ 识别不出的内容原样进 `raw_text`，并记 `unparsed_blocks`。
    """
    settings = settings or get_settings()
    vocabulary = load_skill_vocabulary(settings)

    # ---- ① 分段（★ **两遍扫描**，不能只看"遇到标题就切"）------------------
    #
    # ★ 为什么必须两遍（真实简历逼出来的）：
    #   简历模板常把标题与正文放在不同的形状/文本框里，读出来的**顺序不可靠** ——
    #   本项目的真实样本里，"教育经历"这个标题出现在它的内容**之后**。
    #   只做一遍"遇到标题才切换分段"的话，教育经历会被归进"项目经验"段，
    #   而"姓名/手机号"这些头部信息会被归到最后一段里 —— 结果就是
    #   教育经历解析成 0 条、项目数虚高。
    #
    #   两遍的做法：
    #     第一遍：找出所有标题行 → 得到各段在文档里的**位置区间**；
    #     第二遍：每行按位置找它所属的段（"前一个标题"决定归属）。
    #   这样即使标题出现在内容后面，内容也能被正确归属。
    headings: list[tuple[int, str]] = []
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or _is_table_header(line):
            continue
        key = _section_of(line)
        if key:
            headings.append((index, key))

    sections: dict[str, list[str]] = {}
    unparsed = 0
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or _is_table_header(line):
            continue

        key = _section_of(line)
        if key:
            # 标题行：把"标题之外的那部分内容"归入该段（如 `专业技能 RAG…`）
            tail = _clean_heading(line)
            remainder = _heading_remainder(line, tail)
            sections.setdefault(key, [])
            if remainder:
                # ★ 但残留内容可能**并不属于这个标题所在的段**（标题乱序的模板会串行）：
                #   `专业技能 RAG 问答模块：…` 里那一段其实是项目描述，
                #   硬塞进技能段会让技能抽取读到一整段项目文字。
                #   所以按"内容像什么"再判一次归属。
                if _looks_like_education_line(remainder):
                    sections.setdefault("education", []).append(remainder)
                elif _looks_like_skill_line(remainder):
                    sections.setdefault("skills", []).append(remainder)
                elif key == "skills":
                    # 技能标题后面跟的却是长描述 → 更可能是项目正文
                    sections.setdefault("projects", []).append(remainder)
                else:
                    sections[key].append(remainder)
            continue

        owner = _section_owner(index, headings)

        # ★★ 前导块（第一个内容段标题**之前**的所有内容）该归给谁？
        #
        #   模板常把"标题"和"正文"放进不同的形状，读出来的顺序就会变成
        #   「正文在前、标题在后」。本项目的真实简历样本就是这样：
        #   两个项目的全部内容在第 0~9 行，而"项目经验"这个标题在第 15 行。
        #
        #   如果把这些前导块当成"头部信息"，项目经历会全部丢失（实测为 0 个）。
        #   正确的处理是：交给**第一个内容段**，让该段的解析器去处理它们 ——
        #   项目解析器本来就是"看到带时间段的行就当新条目"，
        #   所以它能把混在一起的项目、技能行各归各位。
        if owner == _PENDING_HEAD:
            if _looks_like_contact_line(line):
                # 电话/邮箱/姓名行 → 属于头部信息，不能塞进内容段；
                # 否则"教育经历在最前"的简历里，姓名会被当成一条教育经历（实测踩过）
                owner = "contact"
            else:
                owner = _first_content_section(headings) or "header"

        # 兜底：没有显式技能段时，靠"字段式"行认出技能栏
        if "skills" not in {k for _, k in headings} and _looks_like_skill_line(line):
            owner = "skills"
        # 兜底：学历行被归进项目段时改归教育段（标题乱序的模板会造成这种情况）
        elif owner != "education" and _looks_like_education_line(line):
            owner = "education"
        # 兜底：文档页眉标题（"个人简历""个人简历 PERSONAL RESUME"）不是数据，丢掉 ——
        #   不丢的话它会被当成一条教育经历（实测：教育经历里多出一条"个人简历"）
        elif _is_resume_title_line(line):
            continue
        sections.setdefault(owner, []).append(line)

    # ---- ② 各段抽字段 ----------------------------------------------------
    header_lines = sections.get("header", []) + sections.get("contact", [])
    skills_lines = sections.get("skills", [])
    project_lines = sections.get("projects", [])
    internship_lines = sections.get("internships", [])
    education_lines = sections.get("education", [])

    full_text = "\n".join(lines)
    explicit_skills = _skills_from_lines(skills_lines, vocabulary)

    education = _parse_education(education_lines, vocabulary)
    projects, project_leftover = _parse_experience(project_lines, vocabulary, kind="project")
    internships, internship_leftover = _parse_experience(
        internship_lines, vocabulary, kind="internship"
    )
    unparsed += len(project_leftover) + len(internship_leftover)

    # 技能总表 = 专业技能栏主动列出的 + 项目/实习里出现过的（保持稳定顺序）
    mentioned = set()
    for item in [*projects, *internships]:
        mentioned.update(item.skills)
    ordered_skills = [name for name in vocabulary if name in explicit_skills or name in mentioned]

    basic = _parse_basic(header_lines, full_text, sections)

    content = ResumeContent(
        basic=basic,
        education=education,
        skills=ordered_skills,
        projects=projects,
        internships=internships,
        raw_text=full_text,
        sections={k: v for k, v in sections.items()},
        unparsed_blocks=unparsed,
    )
    logger.debug(
        "简历解析完成（%s）：教育 %d 条 / 项目 %d 个 / 实习 %d 段 / 技能 %d 个 / 未识别块 %d",
        source_name or "未命名",
        len(content.education),
        len(content.projects),
        len(content.internships),
        len(content.skills),
        content.unparsed_blocks,
    )
    return content


def _skills_from_lines(lines: list[str], vocabulary: dict[str, list[str]]) -> set[str]:
    """从"专业技能"栏抽技能（explicit 来源）。"""
    if not lines:
        return set()
    text = _strip_skills_notation("\n".join(lines))
    return set(extract_skills(text, vocabulary))


def _parse_basic(
    header_lines: list[str],
    full_text: str,
    sections: dict[str, list[str]],
) -> dict[str, str]:
    """抽基础信息。

    ★ 两层策略（真实简历逼出来的）：

        ① **按字段标签抽**（首选）：`姓名：X`、`手机号码 ：Y`、`邮箱：Z`…
           值从标签后取到"下一个标签或行尾"，所以
           `姓 名 ：林小满求职意向 ：Agent开发` 这种**没有空格和分隔符**的
           连排也能正确切出两个字段（虚构数据）。
        ② **按全文正则兜底**：手机号/邮箱直接全文搜（它们有固定格式，不需要标签）；
           姓名再从头部短行里猜一个。

    ★ 安全约定：手机号/邮箱解析出来后**只存进数据库**，
      绝不进日志、绝不进事件流 payload（P0 的脱敏 Filter 是最后一道兜底，
      但"根本不打"比"打了再脱敏"更稳）。
    """
    basic: dict[str, str] = {}

    # ---- ① 按字段标签抽（对整份文本做，因为模板可能把"个人信息"放在任何位置）----
    for field, pattern in _FIELD_RE.items():
        match = pattern.search(full_text)
        if not match:
            continue
        value = _clean_field_value(match.group("value"))
        if not value or len(value) > 80:
            continue
        basic[field] = value

    # ---- ② 格式化的字段用全文正则兜底（标签可能被排版拆坏）------------------
    if "phone" not in basic:
        phone = _PHONE_RE.search(full_text)
        if phone:
            basic["phone"] = phone.group(0)
    if "email" not in basic:
        email = _EMAIL_RE.search(full_text)
        if email:
            basic["email"] = email.group(0)
    # 城市：优先从"现居地址"里认，其次全文里找第一个匹配的城市
    if "city" not in basic or not _CITY_RE.search(basic.get("city", "")):
        address = basic.get("address", "")
        city = _CITY_RE.search(address) if address else None
        if city is None:
            city = _CITY_RE.search(full_text)
        if city:
            basic["city"] = city.group(1)

    # 姓名：标签抽不到时，从头部/联系方式行里猜（"林小满" 这种独占一行的写法）
    #
    # ★ 必须**同时看 header 与 contact 两段**：姓名与手机号常常挤在同一行
    #   （`林小满 | 13800000000`），而这一行会被判成"联系方式行"归到 contact 段；
    #   只看 header 的话姓名永远抽不出来（实测：fixture 的姓名一直是空的）。
    if not basic.get("name"):
        candidates = [*sections.get("header", []), *sections.get("contact", [])]
        basic_name = _guess_name(candidates)
        if basic_name:
            basic["name"] = basic_name

    # 求职意向：优先用"求职意向"段里的**第一行**，否则用标签抽出来的
    #
    # ★ 只取第一行（实测踩过）：模板常把"求职意向"和联系方式拼进同一个块，
    #   把整段一起当意向会让意向变成
    #   "AI Agent 应用开发 / 后端开发 电话：138… 邮箱：… 城市：深圳"。
    intention_lines = [line.strip() for line in sections.get("intention", []) if line.strip()]
    intention = intention_lines[0] if intention_lines else basic.get("job_intention", "")
    if intention:
        basic["job_intention"] = _clean_field_value(intention)

    return basic


def _guess_name(lines: list[str]) -> str:
    """从头部若干行里猜姓名（中文简历的姓名通常独占一行，或与联系方式同行）。

    判据刻意保守 —— 猜错姓名比猜不出更糟（用户会以为工具读错了人）：
        · 去掉手机号/邮箱后再看剩下的部分；
        · 只接受 2~4 个纯汉字（`林小满`、`王小明`）；
        · 拒绝带数字、标点、"简历/求职意向"等字段词的内容。
    """
    for line in lines[:6]:
        candidate = line.strip()
        if not candidate:
            continue
        # 先抠掉同一行里的联系方式与字段标签，再看剩下什么
        candidate = _PHONE_RE.sub(" ", candidate)
        candidate = _EMAIL_RE.sub(" ", candidate)
        candidate = re.sub(r"[|｜]+", " ", candidate)
        for pattern in _FIELD_RE.values():
            candidate = pattern.sub(" ", candidate)
        candidate = re.sub(
            r"(简历|个人简历|求职意向|应聘岗位|目标岗位|电话|邮箱|城市)", " ", candidate
        )
        for piece in candidate.split():
            token = piece.strip(" :：-—·,，。;；()（）")
            # 2~4 个纯汉字才算名字（"林小满" / "王小明"）；英文名单独放宽处理
            if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", token):
                return token
            if re.fullmatch(r"[A-Za-z]{2,12}(?:\s[A-Za-z]{2,12})?", token):
                return token
    return ""


def _clean_field_value(value: str) -> str:
    """清理字段值：去掉排版塞进来的空白、竖线、全角空格。

    ★ 归一化空白是必须的：真实简历里 `姓 名`、`黄 欢` 这种"字间加空格"
      是模板为了对齐而插入的，不清理会得到 `黄 欢` 这样带空格的姓名。
    """
    cleaned = value.replace("\u3000", " ")
    cleaned = re.sub(r"[|]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" :：-—·")


def _parse_education(lines: list[str], vocabulary: dict[str, list[str]]) -> list[EducationItem]:
    """解析教育经历：一行一条（中文简历基本都是一行写完一条）。

    要同时吃下两种真实写法：
        · **表格行**：`学校 | 软件工程 · 本科 | 2023.09 ～ 2027.06`
        · **散文行**：`南方某大学（虚构） 软件工程 本科 2020.09 - 2024.06`

    ★ 关键取舍：**有竖线时严格按列解析**，不再把整行按空格拆。
      踩过的坑：表格行里的 `2023.09` 和时间段被空格拆开后，学校名会变成
      `2023.09`、专业变成 `-`（实测：教育经历首条记录完全错位）。
    """
    items: list[EducationItem] = []
    for line in lines:
        cleaned = line.strip("|-· ").strip()
        if not cleaned:
            continue

        if _CELL_SEP in cleaned:
            cells = [cell.strip() for cell in cleaned.split(_CELL_SEP) if cell.strip()]
            period_cell = ""
            body_cells = list(cells)
            # 时间：从右往左找第一个含时间段的单元格
            for index in range(len(cells) - 1, -1, -1):
                found, _ = _extract_period(cells[index])
                if found:
                    period_cell = cells[index]
                    body_cells = cells[:index] + cells[index + 1 :]
                    break
            period, _ = _extract_period(period_cell or cleaned)
            body = " ".join(body_cells) or cleaned
        else:
            period, _ = _extract_period(cleaned)
            body = cleaned

        degree = _extract_degree(body) or _extract_degree(cleaned)
        body = body.replace(period, " ") if period else body
        for degree_word in _DEGREES:
            body = body.replace(degree_word, " ")
        body = re.sub(r"[|·/／]+", " ", body)
        parts = [p.strip() for p in body.split() if p.strip()]
        school = parts[0] if parts else cleaned
        major = parts[1] if len(parts) > 1 else ""
        items.append(EducationItem(school=school, major=major, degree=degree, period=period))
    return items


#: 项目/实习标题里常见的"角色词"，用于把标题拆成"名称 + 角色"
_ROLE_RE = re.compile(
    r"(前端开发|后端开发|全栈开发|算法工程师|开发工程师|测试工程师|"
    r"项目负责人|负责人|组长|成员|实习生|工程师|个人项目|团队项目|课程作业)"
)

#: "元信息"词：描述**怎么做的**，而不是项目本身的特征。
#: 用于判断括号内容是不是可以整块丢掉（`（个人项目 · AI 协同开发，2026.08）`）。
#:
#: ★ 顺序很重要：**长词必须排在短词前面**，替换时也要按长度降序 ——
#:   否则 `个人项目` 会先被 `个人` 吃掉两个字符，剩下"项目"，
#:   于是"括号里还有内容"的误判发生（实测踩过）。
_INFO_WORDS: tuple[str, ...] = (
    "AI 协同开发",
    "AI协同开发",
    "协同开发",
    "独立开发",
    "独立完成",
    "个人项目",
    "团队项目",
    "开源项目",
    "练手项目",
    "课程作业",
    "课程设计",
    "毕业设计",
    "个人",
    "团队",
)

#: 条目起始标记（真正的"新条目"信号）
_TITLE_PREFIX_RE = re.compile(
    # ★ 只认真正的"条目起始"标记。**不要把 `·` 放进来** ——
    #   中文简历里 `软件工程 · 本科`、`AgentFlow · 可视化平台` 这种用中点做分隔的写法极其常见，
    #   把 `·` 当前缀会把整行拆坏（实测：`软件工程 · 本科` 被拆成名字"本科"、
    #   项目标题 `AgentFlow · …` 被拆成"经验"）。只认短横线与实心圆点。
    r"^\s*(?:[0-9]+[、.．)）]|[一二三四五六七八九十]+[、.．)）]|[【\[•●○]|[-–—])\s*"
)

#: **纯 bullet 前缀**（`-` `•` `●` `*` 开头）—— 只可能是列表项，不可能是指标题。
#: 用它来区分"新条目的序号"（`1.` `一、`）与"条目的描述行"（`- xxx`）。
_BULLET_PREFIX_RE = re.compile(r"^\s*[-–—•●○*·]\s*")


def _split_title_line(raw: str) -> tuple[str, str, str, str]:
    """把一行"标题行"拆成 `(名称, 角色, 时间段, 描述)`。

    处理的语言现象（都来自真实中文简历）：
        `1. 智能简历匹配助手（AI Agent 方向） 2024.03 - 2024.09 个人项目`
            → 名称/角色/时间/描述
        `校园二手交易平台 2023.10 - 2024.01 后端负责人`
        `虚构科技 | 后端开发实习生 | 2024.07 - 2024.09`（表格行）

    实现要点：**先按位置抠掉时间段，而不是按文本替换**。
    按文本替换会在"2023.10 - 2024.01"这种范围上出错 ——
    `2023.10` 这个前缀同时也能被单独匹配，替换后残留 `0 - 2024.01`（实际踩过）。
    """
    cells = _cell_texts(raw) if _CELL_SEP in raw else [raw]
    spans = _merge_spans(_date_spans(raw))
    period = spans[0][2].strip() if spans else ""

    without_period = _strip_periods(raw, spans)
    cells = _cell_texts(without_period) if _CELL_SEP in without_period else [without_period]
    primary = _TITLE_PREFIX_RE.sub("", cells[0]).strip(" 　-—|")

    # ★ 把括号里的"元信息"取出来单独看（`（个人项目 · AI 协同开发，2026.08）`）。
    #   角色词往往只出现在括号里，而**名称必须保留括号本身** ——
    #   项目名里的括号是名字的一部分（"某平台（CLI 版）"），
    #   把括号内容从名称里挖掉会得到半截名字（实测踩过）。
    meta = " ".join(re.findall(r"[（(]([^）)]*)[）)]", primary))

    role = ""
    role_match = _ROLE_RE.search(primary) or (_ROLE_RE.search(meta) if meta else None)
    if role_match:
        role = role_match.group(1)

    # 名称：只去掉"角色词"这种纯属性标签，保留括号与名称主体
    #
    # ★ 括号里如果只剩"角色 + 时间"这类元信息，就整块去掉，
    #   否则会留下 `（个人项目 · AI 协同开发，）` 这种尾部悬空的全角括号
    #   （时间段已经被抠走，括号却还在 —— 实测就是这个残留）。
    #   括号里还有别的实质内容（如"（CLI 版）"）时整块保留，那是名字的一部分。
    #
    #   判据要**同时排除角色词与元信息词**：只排除角色词的话，
    #   `（个人项目 · AI 协同开发，2026.08）` 里的"AI 协同开发"会被当成
    #   "括号里有真内容"从而保留下来（实测踩过）。"协同开发"这类词描述的是
    #   开发方式而不是项目本身的特征，属于元信息。
    name = primary
    bracket_match = re.search(r"[（(]", name)
    if bracket_match:
        inner = name[bracket_match.start() :].strip("()（）")
        leftover_inner = inner
        # 按长度降序替换，避免短词先吃掉长词的前缀（见 _INFO_WORDS 的说明）
        for piece in sorted((role, *_INFO_WORDS), key=len, reverse=True):
            if piece:
                leftover_inner = leftover_inner.replace(piece, "")
        leftover_inner = re.sub(r"[\d\s·,，、;；:：|/\\-]+", "", leftover_inner)
        if len(leftover_inner) <= 2:
            name = name[: bracket_match.start()].strip()
        elif role and role in inner:
            # 括号里有实质内容，但角色词也在里面 → 只摘掉角色词，
            # 同时清掉因此留下的空括号（"…（个人项目）" → "…"）
            name = name.replace(role, " ")
            name = re.sub(r"[（(]\s*[）)]", " ", name)
    if role and role not in {"个人项目", "团队项目"}:
        name = name.replace(role, " ")
    name = name.replace("\u3000", " ")
    name = re.sub(r"[|]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" 　-—|")
    name = re.sub(r"[,，、;；·:：]+$", "", name).strip()

    # 描述 = 标题行里除"名称/角色/时间"之外剩下的部分
    description_parts = [part for part in cells[1:] if part.strip()]
    description = " ".join(description_parts).strip()
    if not description:
        candidate = primary
        for piece in (name, role):
            if piece:
                candidate = candidate.replace(piece, " ")
        description = candidate.strip(" 　-—|()（）")

    # ★ 丢掉"只剩元信息"的描述片段。
    #   实测：真实简历的第一条描述是 `· AI 协同开发，` —— 它是标题行括号里的
    #   "开发方式"说明，既不是项目内容，也不该占一条 bullet
    #   （展示出来会让用户以为解析错位了）。
    if description and _is_only_metadata(description):
        description = ""

    return name or primary or raw, role, period, description


def _is_only_metadata(text: str) -> bool:
    """判断一段文字是否"只有元信息"（开发方式 / 项目性质，不含实质内容）。"""
    residue = text
    for word in sorted(_INFO_WORDS, key=len, reverse=True):
        residue = residue.replace(word, "")
    residue = re.sub(r"[\s·•,，、;；:：|/\\()（）\[\]【】\-—]+", "", residue)
    return len(residue) <= 1


#: 学历行（教育经历）的特征：`学校 | 专业 · 学位 | 时间`
#: 用"学位词 + 时间段"两个信号一起判断，避免把普通项目误判成学历。
_DEGREE_MARKER_RE = re.compile("|".join(_DEGREES))


def _looks_like_contact_line(line: str) -> bool:
    """判断一行是不是"头部信息"（姓名 / 电话 / 邮箱 / 城市 / 文档标题）。

    ★ 为什么要单独判一次：`_PENDING_HEAD`（第一个内容段标题之前的内容）
      在"项目经历在最前"的模板里确实都是正文（要归给项目段），
      但在"教育经历在最前"的模板里就是姓名和联系方式 ——
      不分青红皂白归给第一个内容段，会让教育经历里冒出"林小满"这种假记录。

      判据用**强信号**（手机号/邮箱/字段标签/文档标题/**短中文名**），
      不用"短行"这种弱信号，避免把真正的项目标题误判成联系方式。
    """
    if _PHONE_RE.search(line) or _EMAIL_RE.search(line):
        return True
    if _is_resume_title_line(line):
        return True
    if any(pattern.search(line) for pattern in _FIELD_RE.values()):
        return True
    # 2~4 个纯汉字、无数字无标点 → 极可能是姓名独占一行（"林小满"）
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", line.strip()))


def _is_resume_title_line(line: str) -> bool:
    """判断一行是不是"文档页眉标题"（`个人简历` / `PERSONAL RESUME` / `简历`）。

    ★ 为什么必须显式排除：这类行会被两遍分段归到某个段里，
      然后被当成一条真实条目 —— 实测"教育经历"里多出一条名叫「个人简历」的记录，
      而且它还会进入 content_hash，让缓存莫名其妙地失效。
    """
    cleaned = re.sub(r"[\s\u3000|]+", "", line).upper()
    if not cleaned:
        return False
    return cleaned in {"个人简历", "简历", "PERSONALRESUME", "RESUME", "个人求职简历"}


def _looks_like_education_line(line: str) -> bool:
    """判断一行是不是"教育经历"（而不是项目/实习）。
    ★ 为什么需要它（真实简历逼出来的）：
      模板把标题和正文放在不同形状里之后，读出来的顺序可能是
      「项目正文 → 项目标题 → 教育正文 → 教育标题」。
      两遍分段只能按"前一个标题"归属，于是**教育经历会被归进项目段**，
      项目解析器拿到一行 `学校 | 专业 · 本科 | 2023.09 ～ 2027.06`
      会把它当成一个项目 —— 实测教育经历变成 0 条、项目里多出一条垃圾。

      判据（两个信号同时满足才算）：
        · 含学位词（本科/硕士/专科…）
        · 含时间段
      普通项目几乎不会同时满足这两条（项目写的是"2024.03 - 2024.09"但没有"本科"）。
    """
    return bool(_DEGREE_MARKER_RE.search(line)) and bool(_extract_period(line)[0])


def _new_item(kind: str) -> Any:
    """按类型新建一个空的条目对象。"""
    return ProjectItem() if kind == "project" else InternshipItem()


def _assign(item: Any, kind: str, name: str, role: str, period: str) -> None:
    """把解析出来的名称/角色/时间写进条目（项目用 name，实习用 company）。"""
    if kind == "project":
        item.name = name
        item.role = role
        item.period = period
    else:
        item.company = name
        item.role = role
        item.period = period


def _parse_experience(
    lines: list[str],
    vocabulary: dict[str, list[str]],
    *,
    kind: str,
) -> tuple[list[Any], list[str]]:
    """解析项目经历 / 实习经历。

    规则（中文简历的常见写法）：
        · 以序号（`1.` `一、`）或 `【】` 开头 = 新条目的标题行
        · 或者：**带时间段的行** = 新条目的标题行（"XX系统 2024.03-2024.09"）
        · 表格行：**第一列是标题、第二列是描述**（左栏项目名 / 右栏项目描述）
        · **以 bullet 标记（`-` `•`）开头的行 = 描述**，永远不是新条目 ★
        · 其他行 = 上一条目的描述
        · 完全识别不出来的行 → 返回在 leftover 里（上层计入 unparsed_blocks）

    ★ 为什么 bullet 行必须排除在"标题"之外（实测踩过）：
      真实简历里每条项目描述都以 `-` 开头（`-Node.js + Express + …`），
      而 `-` 同时在 `_TITLE_PREFIX_RE` 里被当成"条目起始标记"。
      结果是**每一条描述都变成一个空项目** —— 2 个项目变成 11 个，
      每个还只有名字没有内容。项目数虚高会直接把 P3 的匹配打分带偏。

    取舍说明：**不做"完美解析"**。认不出就老实说认不出，
    比编一个看似合理的结构化结果要好 —— 后者会让用户以为解析对了，
    然后在匹配/定制环节被错误数据一路带偏。
    """
    items: list[Any] = []
    leftover: list[str] = []
    current: Any | None = None

    for line in lines:
        raw = line.strip()
        if not raw:
            continue

        # bullet 行：属于当前条目的描述（当前条目不存在时才退化成"新条目"）
        if _BULLET_PREFIX_RE.match(raw) and current is not None:
            text = _BULLET_PREFIX_RE.sub("", raw).strip()
            if text:
                current.bullets.append(text)
            continue

        # 表格行：第一列标题 + 其余列描述。表格行天然是"新条目"。
        if _CELL_SEP in raw:
            cells = _cell_texts(raw)
            head = cells[0]
            description = " ".join(cells[1:]).strip()
            current = _new_item(kind)
            name, role, period, title_desc = _split_title_line(head)
            _assign(current, kind, name, role, period)
            for text in (description, title_desc):
                if text:
                    current.bullets.append(text)
            items.append(current)
            continue

        period, _ = _extract_period(raw)
        is_title = bool(_TITLE_PREFIX_RE.match(raw)) or bool(period)
        if is_title or current is None:
            current = _new_item(kind)
            name, role, period, title_desc = _split_title_line(raw)
            _assign(current, kind, name, role, period)
            if title_desc:
                current.bullets.append(title_desc)
            items.append(current)
            continue

        current.bullets.append(raw)

    # 技能标注 + 过滤空条目
    result: list[Any] = []
    for item in items:
        title = item.name if kind == "project" else item.company
        description = " ".join(item.bullets)
        item.skills = extract_skills(f"{title} {description}", vocabulary)
        if title or item.bullets:
            result.append(item)
        else:
            leftover.append(description)
    return result, leftover


# ===========================================================================
# content_hash
# ===========================================================================
#: 参与 hash 计算的字段（顺序固定）。
#: ★ 故意**不包含** raw_text：原文里的空白/排版变化不该让缓存失效；
#:   也**不包含** file_path / 导入时间：重新导入同一份简历不该让 LLM 缓存失效。
_HASH_FIELDS = ("basic", "education", "skills", "projects", "internships")


def compute_content_hash(content: ResumeContent) -> str:
    """算简历内容 hash（LLM 缓存 key 的组成部分）。

    实现要点：先做成"规范化 dict"再序列化 ——
        · `sort_keys=True` 保证字段顺序不影响结果；
        · `ensure_ascii=True` 让中英文都稳定（同一个内容在不同平台编码下 hash 一致）；
        · 去掉所有空白差异（简历重新导出常常只是换行变了）。

    ★ 为什么必须稳定：hash 变了 → LLM 缓存失效 → 每次导入简历都白花钱重算。
      所以这里有一条专门的测试：**同一内容解析两次，hash 必须相同**；
      改了技能/项目之后再解析，hash 必须不同。
    """
    payload = content.model_dump(include=set(_HASH_FIELDS))
    normalized = _normalize_for_hash(payload)
    serialized = to_json(normalized, indent=None)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalize_for_hash(value: Any) -> Any:
    """递归去掉空白差异、统一空值表示，让 hash 只对"实质内容"敏感。"""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, dict):
        return {
            key: _normalize_for_hash(item)
            for key, item in sorted(value.items())
            if item not in ("", [], {}, None)
        }
    if isinstance(value, list):
        return [_normalize_for_hash(item) for item in value if item not in ("", [], {}, None)]
    return value


def strip_volatile_content(text: str) -> str:
    """把文本里的手机号/邮箱打码。

    ★ 用途与边界（别误解它的作用）：
        它是给**排障与文档**用的辅助函数 —— 当你确实需要打印一段简历文本
        （例如排查"为什么这个字段没解析出来"）时，先过一遍它。
        真正的隐私保障靠 **`utils/logging.py` 的脱敏 Filter**（自动、不可能忘）
        加上"日志只打摘要、不打正文"这条约定；这个函数是**人工的第二道保险**。

    ★ 为什么它暂时没有调用点却保留：删掉它不会让代码更安全，
      反而会让人在需要打印时**直接打印原文**。保留，并在这里写清定位。
    """
    masked = _PHONE_RE.sub(lambda m: f"{m.group(0)[:3]}****{m.group(0)[-4:]}", text)
    return _EMAIL_RE.sub(lambda m: re.sub(r"(?<=.).(?=.*@)", "*", m.group(0)), masked)


# ===========================================================================
# 持久化
# ===========================================================================
def content_from_json(raw: str | None) -> ResumeContent:
    """把 `resumes.content_json` 读回成结构化对象（容忍脏数据）。"""
    data = from_json(raw, default=None)
    if not isinstance(data, dict):
        return ResumeContent()
    try:
        return ResumeContent.model_validate(data)
    except Exception:  # noqa: BLE001 - 老版本/手工改坏的数据不应该让命令崩
        logger.warning("简历 content_json 无法解析成当前结构，已按空简历处理")
        return ResumeContent()


def derive_direction(content: ResumeContent, settings: AppSettings | None = None) -> str:
    """根据简历内容猜一个方向（用于 `resume add` 不传 --direction 时的默认值）。

    规则：看哪个方向画像的 core/plus 技能命中最多。命中数相同时按画像顺序取第一个。
    **只作为默认值猜一次，不覆盖用户显式传入的方向。**
    """
    settings = settings or get_settings()
    profiles = load_direction_profiles(settings)
    content_text = " ".join(
        [
            content.basic.get("job_intention", ""),
            " ".join(content.skills),
            " ".join(item.name + " " + " ".join(item.bullets) for item in content.projects),
        ]
    )
    best_label = ""
    best_score = 0
    for profile in profiles.values():
        score = 0
        for skill in [*profile.core_skills, *profile.plus_skills]:
            if skill in content.skills:
                score += 1
        for keyword in profile.title_hints:
            if keyword and keyword in content_text:
                score += 1
        if score > best_score:
            best_score, best_label = score, profile.label
    return best_label


# ===========================================================================
# 方向画像 + split 建议
# ===========================================================================
class DirectionProfile(BaseModel):
    """一个方向看重什么（数据来自 config/direction_profiles.yaml）。"""

    key: str = ""
    label: str = ""
    aliases: list[str] = Field(default_factory=list)
    core_skills: list[str] = Field(default_factory=list)
    plus_skills: list[str] = Field(default_factory=list)
    weak_skills: list[str] = Field(default_factory=list)
    title_hints: list[str] = Field(default_factory=list)
    priority_keywords: list[str] = Field(default_factory=list)


class ProjectFit(BaseModel):
    """某个项目对某个方向的契合度（split 的核心打分结果）。"""

    name: str
    score: int
    matched_skills: list[str] = Field(default_factory=list)
    hit_keywords: list[str] = Field(default_factory=list)

    @property
    def reason(self) -> str:
        """一句人话解释"为什么排在这里"。"""
        bits: list[str] = []
        if self.matched_skills:
            bits.append("命中技能：" + "、".join(self.matched_skills[:4]))
        if self.hit_keywords:
            bits.append("说到点上：" + "、".join(self.hit_keywords[:3]))
        return "；".join(bits) if bits else "没有明显方向相关的内容"


class SplitSuggestion(BaseModel):
    """针对一个方向的拆分建议（`jh resume split` 的产物）。"""

    direction: str
    #: 简历自身的方向（可能为空）
    current_direction: str = ""
    #: 该方向最相关的前 3 个项目（建议前置）
    priority_projects: list[ProjectFit] = Field(default_factory=list)
    #: 该方向该弱化的项目（相关性最低）
    de_emphasis_projects: list[ProjectFit] = Field(default_factory=list)
    #: 该方向主打技能（简历里已有）
    headline_skills: list[str] = Field(default_factory=list)
    #: 与方向无关、可以少提的技能
    weak_skills: list[str] = Field(default_factory=list)
    #: 该方向核心技能里我**没有**的（缺口）
    gaps: list[str] = Field(default_factory=list)
    #: 该方向需要的加分技能里我有的
    plus_skills_owned: list[str] = Field(default_factory=list)
    #: 我的"在学习"白名单技能（可以提，但必须标注"在学习中"）
    learning_skills: list[str] = Field(default_factory=list)
    #: 建议的简历小节顺序（把哪个项目放最前面）
    suggested_order: list[str] = Field(default_factory=list)
    #: 综合提示
    notes: list[str] = Field(default_factory=list)


def load_direction_profiles(settings: AppSettings | None = None) -> dict[str, DirectionProfile]:
    """读 `config/direction_profiles.yaml`。

    文件缺失时返回空 dict（而不是抛错）—— `jh resume split` 会提示"没配方向画像"，
    其余命令完全不受影响。**配置文件缺了不该让工具瘫掉。**
    """
    settings = settings or get_settings()
    path = Path(__file__).resolve().parent.parent / "config" / "direction_profiles.yaml"
    if not path.is_file():
        logger.warning("找不到方向画像文件：%s", path)
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了只影响 split，不该炸整个 CLI
        logger.warning("方向画像文件读不了（%s），jh resume split 将不可用", exc)
        return {}

    profiles: dict[str, DirectionProfile] = {}
    for key, raw in (data.get("directions") or {}).items():
        profile = DirectionProfile(key=key, **{k: v for k, v in raw.items() if k != "key"})
        profile.key = key
        profiles[key] = profile
    return profiles


def find_profiles_for(
    wanted: list[str], profiles: dict[str, DirectionProfile]
) -> list[DirectionProfile]:
    """按用户给的--direction 列表找到对应画像。

    匹配顺序：label 精确 → key 精确 → aliases 包含 → label 部分匹配。
    找不到就忽略（并在 notes 里提示），**不报错** ——
    用户想拆一个我没预置的方向（比如"测试开发"）时，应该还能跑，只是建议会少。
    """
    found: list[DirectionProfile] = []
    for item in wanted:
        target = item.strip()
        if not target:
            continue
        for profile in profiles.values():
            if profile in found:
                continue
            if target in (profile.label, profile.key) or target in profile.aliases:
                found.append(profile)
                break
        else:
            for profile in profiles.values():
                if profile in found:
                    continue
                if target in profile.label or profile.label in target:
                    found.append(profile)
                    break
    return found


def _score_project(project: ProjectItem, profile: DirectionProfile) -> ProjectFit:
    """给一个项目打方向契合度。

    打分口径（简单、可解释、可调）：
        · 命中 core_skills 的每个技能 +3
        · 命中 plus_skills 的每个技能 +1
        · 命中 priority_keywords 的每个词 +2
        · 命中 weak_skills 的每个技能 -2（这个方向不看重的技术）

    ★ 为什么不用权重更复杂的公式：`split` 的输出是**给人看的建议**，
      "为什么它排第一"必须一眼能解释。可解释性比精度重要得多。
    """
    text = f"{project.name} {project.role} {' '.join(project.bullets)}"
    matched_core = [s for s in profile.core_skills if s in project.skills]
    matched_plus = [s for s in profile.plus_skills if s in project.skills]
    hit_keywords = [k for k in profile.priority_keywords if k and k in text]
    matched_weak = [s for s in profile.weak_skills if s in project.skills]

    score = 3 * len(matched_core) + 1 * len(matched_plus) + 2 * len(hit_keywords)
    score -= 2 * len(matched_weak)
    return ProjectFit(
        name=project.name or "（未命名项目）",
        score=score,
        matched_skills=[*matched_core, *matched_plus],
        hit_keywords=hit_keywords,
    )


def analyze_split(
    content: ResumeContent,
    *,
    current_direction: str = "",
    wanted: list[str] | None = None,
    settings: AppSettings | None = None,
    top_n: int = 3,
) -> list[SplitSuggestion]:
    """为每个方向生成一份拆分建议。

    ★ 只给建议，不改任何内容、不写任何文件。

    Args:
        wanted: 想拆成的方向（用户显式传入）。为空时自动挑：
                简历当前方向 + 命中数最高的另外一个方向 ——
                因为用户的真实需求是"一份简历拆成两个方向"，
                只给一个方向的建议等于没帮他拆。
    """
    settings = settings or get_settings()
    profiles = load_direction_profiles(settings)
    if not profiles:
        return []

    if not wanted:
        wanted = _auto_pick_directions(content, profiles, current_direction, settings)

    selected = find_profiles_for(wanted, profiles)
    if not selected:
        return []

    learning = set(settings.learning_whitelist)
    resume_skills = set(content.skills)
    items = content.projects or []
    internship_items = content.internships or []

    suggestions: list[SplitSuggestion] = []
    for profile in selected:
        fits = [_score_project(p, profile) for p in items]
        # 实习经历也用同一套打分（项目为空的简历只能靠实习）
        fits += [_score_project(_internship_as_project(i), profile) for i in internship_items]
        fits.sort(key=lambda fit: (-fit.score, fit.name))

        priority = [fit for fit in fits if fit.score > 0][:top_n]
        weak = [fit for fit in reversed(fits) if fit.score <= 0][:2]

        core_owned = [s for s in profile.core_skills if s in resume_skills]
        plus_owned = [s for s in profile.plus_skills if s in resume_skills]
        gaps = [s for s in profile.core_skills if s not in resume_skills]
        weak_owned = [s for s in profile.weak_skills if s in resume_skills]
        learning_owned = [s for s in resume_skills if s in learning]

        suggestion = SplitSuggestion(
            direction=profile.label,
            current_direction=current_direction,
            priority_projects=priority,
            de_emphasis_projects=weak,
            headline_skills=core_owned,
            weak_skills=weak_owned,
            gaps=gaps,
            plus_skills_owned=plus_owned,
            learning_skills=learning_owned,
            suggested_order=[fit.name for fit in priority] + [fit.name for fit in weak],
            notes=_direction_notes(profile, content, core_owned, gaps, priority),
        )
        suggestions.append(suggestion)

    return suggestions


def _internship_as_project(item: InternshipItem) -> ProjectItem:
    """把实习经历转成 ProjectItem 以便复用同一套打分逻辑。"""
    return ProjectItem(
        name=f"[实习] {item.company}",
        role=item.role,
        period=item.period,
        bullets=item.bullets,
        skills=item.skills,
    )


def _auto_pick_directions(
    content: ResumeContent,
    profiles: dict[str, DirectionProfile],
    current_direction: str,
    settings: AppSettings,
) -> list[str]:
    """自动挑两个方向：当前方向 + 契合度最高的另一个。

    ★ 为什么默认给两个：用户的痛点原话是"只有 1 份、未分方向"，
      需求是"帮我拆"。只给一个方向的建议等于没拆。
      给两个正好对应"拆成「AI Agent 方向 / 前端方向」"这个真实场景。
    """
    scores: list[tuple[int, str]] = []
    resume_skills = set(content.skills)
    for profile in profiles.values():
        score = sum(1 for s in [*profile.core_skills, *profile.plus_skills] if s in resume_skills)
        scores.append((score, profile.label))
    scores.sort(key=lambda pair: (-pair[0], pair[1]))

    picked: list[str] = []
    if current_direction:
        picked.append(current_direction)
    for _, label in scores:
        if label not in picked:
            picked.append(label)
        if len(picked) >= 2:
            break
    return picked


def _direction_notes(
    profile: DirectionProfile,
    content: ResumeContent,
    core_owned: list[str],
    gaps: list[str],
    priority: list[ProjectFit],
) -> list[str]:
    """给这个方向补几句"人话结论"。"""
    notes: list[str] = []

    resume_direction = content.basic.get("job_intention", "")
    if resume_direction and profile.label in resume_direction:
        notes.append(f"简历的求职意向已经写了「{resume_direction}」，方向是自洽的。")
    elif resume_direction:
        notes.append(
            f"简历的求职意向写的是「{resume_direction}」，但这次要投「{profile.label}」"
            "—— 建议按下面这版**另存一份**，不要把两个方向混在一份里。"
        )

    if not priority:
        notes.append(
            "⚠️ 没有项目与这个方向明显相关。要么是解析没认出项目（看 raw_text），"
            "要么是这份简历确实不该投这个方向。"
        )
    elif len(priority) < 3:
        notes.append(f"只有 {len(priority)} 个项目与这个方向相关，建议再补一个小项目。")

    if gaps:
        notes.append("缺口（简历里完全没有）：" + "、".join(gaps[:6]))
    if core_owned:
        notes.append("主打（简历里已有、且这个方向最看重）：" + "、".join(core_owned[:6]))

    return notes


# ===========================================================================
# 落库（add / list / split 的业务入口）
# ===========================================================================
@dataclass
class AddResumeResult:
    """`jh resume add` 的结果（给 CLI 展示用）。"""

    resume_id: int
    name: str
    direction: str
    content_hash: str
    created: bool
    #: 解析出来的统计，用来在终端给一句"解析到什么程度"的可信反馈
    counts: dict[str, int]
    #: 被默认设成 is_default 了吗
    became_default: bool
    #: 解析不完整的提示（没识别出的文本块数量 > 0 时给）
    warnings: list[str]


@dataclass
class ResumeSummary:
    """`jh resume list` 的一行。"""

    id: int
    name: str
    direction: str
    is_default: bool
    content_hash: str
    file_path: str
    projects: int
    skills: int
    created_at: str


def parse_resume_file(
    path: str | Path,
    *,
    settings: AppSettings | None = None,
) -> ResumeContent:
    """读文件 + 解析，一步到位（add 与测试都用它）。"""
    from jobhunter.utils.docx import read_document

    settings = settings or get_settings()
    target = Path(path)
    read_result = read_document(target)
    if not read_result.lines:
        raise ResumeParseError(
            f"没能从 {target.name} 里读到任何文字。\n"
            "常见原因：① 简历是纯图片/扫描件；② 内容全在图片或 SmartArt 里。\n"
            "建议：把内容复制成 .txt 再导入，或换成文字版 docx。"
        )
    return parse_resume_text(read_result.lines, settings=settings, source_name=target.name)


def add_resume(
    session: Session,
    path: str | Path,
    *,
    name: str | None = None,
    direction: str | None = None,
    make_default: bool = False,
    settings: AppSettings | None = None,
) -> AddResumeResult:
    """导入一份简历：读文件 → 解析 → 算 hash → 入库（幂等）。

    ★ 幂等语义（和 P2 的岗位去重是同一条纪律）：
        同一份简历内容重复导入 → **不新建记录、不重复占 id**，
        只更新 name/direction/file_path 这些"描述性字段"，
        并返回 `created=False` 告诉用户"这份已经导过了"。

      判断依据是 `content_hash`。注意 hash **不含** file_path ——
      同一份简历换个目录再导入，仍然应该被认成同一份。

    Args:
        make_default: 设为默认简历。库里还没有任何简历时，**第一份自动成为默认**
                      （否则匹配时挑不到简历，用户还得再敲一条命令）。
    """
    settings = settings or get_settings()
    target = Path(path)
    content = parse_resume_file(target, settings=settings)
    content_hash = compute_content_hash(content)

    resolved_direction = direction or derive_direction(content, settings)
    resolved_name = name or target.stem

    existing = session.scalars(select(Resume).where(Resume.content_hash == content_hash)).first()

    warnings: list[str] = []
    if content.unparsed_blocks:
        warnings.append(
            f"有 {content.unparsed_blocks} 处内容没能结构化（原文已完整保留在 raw_text，"
            "P3 的 LLM 兜底会用到）"
        )
    if not content.projects:
        warnings.append(
            "没有解析出项目经历 —— 检查一下简历里是否用了「项目经验」这类小标题；"
            "没有小标题的话，规则解析认不出项目段"
        )
    if not content.skills:
        warnings.append("没有解析出任何技能，请检查「专业技能」小标题是否规范")

    counts = {
        "教育经历": len(content.education),
        "技能": len(content.skills),
        "项目": len(content.projects),
        "实习": len(content.internships),
        "未识别块": content.unparsed_blocks,
    }

    if existing is not None:
        # 命中已有记录：只更新描述性字段
        if name:
            existing.name = name
        if direction:
            existing.direction = direction
        existing.file_path = str(target)
        if make_default:
            _clear_default(session)
            existing.is_default = True
        session.flush()
        logger.info("简历内容已存在（id=%s），更新描述字段", existing.id)
        return AddResumeResult(
            resume_id=existing.id,
            name=existing.name or resolved_name,
            direction=existing.direction or resolved_direction,
            content_hash=content_hash,
            created=False,
            counts=counts,
            became_default=bool(existing.is_default),
            warnings=warnings,
        )

    has_any = session.scalars(select(Resume).limit(1)).first() is not None
    became_default = make_default or not has_any
    if became_default:
        _clear_default(session)

    resume = Resume(
        name=resolved_name,
        direction=resolved_direction,
        file_path=str(target),
        content_json=to_json(content.model_dump(), indent=None),
        content_hash=content_hash,
        is_default=became_default,
    )
    session.add(resume)
    session.flush()

    # ★ 日志只打摘要与计数，绝不打简历原文（P0 的脱敏 Filter 是兜底，不是许可）
    logger.info(
        "简历已导入：id=%s 方向=%s 项目=%d 技能=%d",
        resume.id,
        resolved_direction or "未识别",
        len(content.projects),
        len(content.skills),
    )

    return AddResumeResult(
        resume_id=resume.id,
        name=resolved_name,
        direction=resolved_direction,
        content_hash=content_hash,
        created=True,
        counts=counts,
        became_default=became_default,
        warnings=warnings,
    )


def _clear_default(session: Session) -> None:
    """把现有的默认简历取消掉（保证"默认"最多只有一份）。"""
    for resume in session.scalars(select(Resume).where(Resume.is_default.is_(True))):
        resume.is_default = False


def list_resumes(session: Session) -> list[ResumeSummary]:
    """列出所有简历（按 id 升序，稳定好读）。"""
    from jobhunter.utils.time import fmt_datetime_local

    rows: list[ResumeSummary] = []
    for resume in session.scalars(select(Resume).order_by(Resume.id)):
        content = content_from_json(resume.content_json)
        rows.append(
            ResumeSummary(
                id=resume.id,
                name=resume.name or "（未命名）",
                direction=resume.direction or "（未识别）",
                is_default=resume.is_default,
                content_hash=(resume.content_hash or "")[:12],
                file_path=resume.file_path or "",
                projects=len(content.projects),
                skills=len(content.skills),
                created_at=fmt_datetime_local(resume.created_at),
            )
        )
    return rows


def get_resume(session: Session, resume_id: int) -> Resume | None:
    """按 id 取简历（找不到返回 None，让 CLI 决定怎么提示）。"""
    return session.get(Resume, resume_id)


def split_resume(
    session: Session,
    resume_id: int,
    *,
    directions: list[str] | None = None,
    top_n: int = 3,
    settings: AppSettings | None = None,
) -> tuple[ResumeSummary, list[SplitSuggestion]]:
    """`jh resume split` 的业务入口：读简历 → 生成多方向拆分建议。

    ★ 只读不写：**不改简历、不写文件、不落库**。
      真正的改写是 P5 的 tailor（且必须过 FidelityGuard）。
    """
    settings = settings or get_settings()
    resume = get_resume(session, resume_id)
    if resume is None:
        raise LookupError(f"找不到 id={resume_id} 的简历。用 `jh resume list` 看有哪些。")

    content = content_from_json(resume.content_json)
    summaries = {row.id: row for row in list_resumes(session)}
    summary = summaries[resume_id]

    suggestions = analyze_split(
        content,
        current_direction=resume.direction or "",
        wanted=directions,
        settings=settings,
        top_n=top_n,
    )
    return summary, suggestions
