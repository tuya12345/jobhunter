"""配置：`config.yaml`（个人偏好）+ `.env`（密钥）+ 环境变量（覆盖）三层合并。

╔══════════════════════════════════════════════════════════════════════════╗
║ 设计要点                                                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

1. **优先级**：显式传参 → 环境变量 `JH_*` → `.env` → config.yaml → 代码默认值

2. **为什么用 pydantic 校验配置**
   配置写错的代价极高且隐蔽。举两个真实例子：
     · `match_weights` 三项加起来是 95 → 所有岗位的 rule_score 都偏低 5%，
       你只会觉得"这工具打分怎么这么严"，永远想不到是配置问题；
     · `threshold: 200` → 待投清单永远是空的，以为是抓取坏了。
   所以这些都用校验器变成**启动时的一句明确报错**。

3. **密钥只从 .env / 环境变量来**，绝不写进 config.yaml（那个文件会提交进 Git）。

4. **可测试性**：`get_settings()` 带 lru_cache 做单例；测试通过
   `JH_CONFIG` + `JH_DATA_DIR` 环境变量做隔离，`tests/conftest.py` 里还有一条硬断言，
   保证测试绝不会解到真实的 data/jobhunter.db。

★ 踩过的坑（写下来免得再犯）：`_yaml_file` **不能**声明成 pydantic 的私有属性
  （`_yaml_file: Path | None = None`）。pydantic v2 会把下划线开头的注解包成
  `ModelPrivateAttr`，于是 `getattr(AppSettings, "_yaml_file")` 拿到的不是 Path 而是
  ModelPrivateAttr，`Path(...)` 直接抛 TypeError。
  正确做法是把路径**当参数传进来**（见 `_YamlSource`），不依赖类属性。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, ValidationError, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from jobhunter.config.paths import PathSet, default_paths
from jobhunter.config.vocabulary import BUILTIN_SKILLS

__all__ = [
    "AppSettings",
    "BrowserSettings",
    "GreetingSettings",
    "MatchWeights",
    "RateLimitSettings",
    "TailorSettings",
    "get_settings",
    "load_settings",
    "reset_settings",
]

# 环境变量名：指定 config.yaml 的位置（CLI 的 --config 也写这个变量）
ENV_CONFIG = "JH_CONFIG"

#: 单条开场白禁用词的长度上限（ISSUES #43）。
#:
#: ★ 为什么要有上限：禁用词走的是**朴素子串匹配**（`if word in text`）。
#:   写进去一整个句子，就等于"开场白里不能出现这句话的任何一个片段"，
#:   正常文案会被大面积误伤（`docs/config.md` §6 记着一次同类事故）。
#:   12 字足够覆盖真实套话（"感谢您的阅读"是 6 字），再长就该考虑换个判定方式了。
MAX_BANNED_WORD_CHARS = 12


class _YamlSource(YamlConfigSettingsSource):
    """从指定路径读 config.yaml。

    为什么不用 pydantic-settings 默认的"读 settings_cls.model_config['yaml_file']"：
    路径是**运行时**决定的（`--config` / 环境变量 / 当前目录 / 项目根），
    写死在模型配置里就没法按需切换。这里直接构造时把路径传进来，最直接。
    """

    def __init__(self, settings_cls: type[BaseSettings], yaml_file: Path | None) -> None:
        # ★ 必须把 yaml_file 传给父类构造器：ConfigFileSourceMixin 就是用它来定位文件的
        #   （踩过的坑：在 super().__init__() 之后赋值 self.yaml_file_path 是无效的）
        super().__init__(settings_cls, yaml_file=yaml_file, yaml_file_encoding="utf-8")


# ---------------------------------------------------------------------------
# 子配置
# ---------------------------------------------------------------------------
class MatchWeights(BaseModel):
    """三维度权重（百分比，合计必须 100）。

    为什么是这三个维度（而不是"技能"一个维度）：
        · 岗位方向：AI Agent 岗和前端岗的简历根本不该互投 → 方向错了，分再高也白投；
        · 硬条件：应届生遇到"3 年经验"通常仍可投，所以是**降分不是淘汰**；
        · 技能关键词：最能体现"简历与 JD 是否匹配"的量化维度。
    调权重的方法：先跑 `jh match explain <job_id>` 看分维度明细，再回来改这里。
    """

    direction: int = Field(default=30, ge=0, le=100)
    hard: int = Field(default=30, ge=0, le=100)
    skill: int = Field(default=40, ge=0, le=100)

    @model_validator(mode="after")
    def _check_sum(self) -> MatchWeights:
        total = self.direction + self.hard + self.skill
        if total != 100:
            raise ValueError(
                f"match_weights 三项之和必须等于 100，当前是 {total} "
                f"(direction={self.direction}, hard={self.hard}, skill={self.skill})。"
                "否则所有岗位的 rule_score 会整体偏移，且极难排查。"
            )
        return self


class GreetingSettings(BaseModel):
    """开场白的字数约束。

    为什么要两个版本：BOSS 的开场白输入框有字数限制，
    80-150 字的完整版在很多岗位发不出去 → 必须同时产出 ≤60 字的精简版。
    """

    full_min_chars: int = Field(default=80, ge=1)
    full_max_chars: int = Field(default=150, ge=1)
    short_max_chars: int = Field(default=60, ge=1)
    github_url: str = ""

    @model_validator(mode="after")
    def _check_order(self) -> GreetingSettings:
        if self.full_min_chars > self.full_max_chars:
            raise ValueError(
                f"greeting.full_min_chars({self.full_min_chars}) 不能大于 "
                f"full_max_chars({self.full_max_chars})"
            )
        if self.short_max_chars >= self.full_max_chars:
            raise ValueError(
                f"greeting.short_max_chars({self.short_max_chars}) 应小于 "
                f"full_max_chars({self.full_max_chars})，否则精简版没有意义"
            )
        return self


class TailorSettings(BaseModel):
    """AI 定制简历（P5）的硬约束。

    ★ 为什么长度上限要有"绝对余量"（`length_floor_extra`）：
      计划书写的是"不超过原文 150%"，但原句只有 4 个字时 150% 就是 6 个字 ——
      任何通顺的改写都会超，这条检查会退化成"短句一律不许改"。
      所以真正的上限是 `max(原文 × ratio, 原文 + floor_extra)`。
      误报会让检查被人关掉，宁可留一点余量。

    ★ `max_suggestions` 是**给模型的提示词参数**（不是代码截断）：
      一次改 20 处以上，我就没耐心逐条审了；而"逐条审核"正是这个功能的安全边界。
    """

    max_length_ratio: float = Field(default=1.5, ge=1.0, le=5.0)
    length_floor_extra: int = Field(default=30, ge=0, le=500)
    max_suggestions: int = Field(default=12, ge=1, le=100)
    #: `jh tailor brief` 重点讲几个项目 / 突出几个技能
    brief_projects: int = Field(default=3, ge=1, le=10)
    brief_skills: int = Field(default=2, ge=1, le=10)

    @model_validator(mode="after")
    def _check_ratio(self) -> TailorSettings:
        if self.max_length_ratio < 1.0:
            raise ValueError(
                f"tailor.max_length_ratio({self.max_length_ratio}) 不能小于 1.0："
                "改写至少要允许和原文一样长"
            )
        return self


class BrowserSettings(BaseModel):
    """浏览器抓取（ISSUES #65：`jh job add --cdp`）。

    ★ 为什么要有这一组配置：默认抓取会**新起一个 headless 浏览器**（没有登录态），
      于是需要登录的站点（BOSS）只会给登录页。`--cdp` 改成连到**用户自己的**
      浏览器实例去读页面 —— 而那需要三件事可配：端口、浏览器路径、专用配置目录。

    ★ `cdp_profile_dir` 默认是 `<state_dir>/cdp-profile`：
      那个目录里会有**真实登录态**（Cookie），所以它必须落在 `state/`（.gitignore 覆盖）。
      **不要**把它改到仓库里（`config.yaml` 同级的目录也不行）——有测试守这条。
    """

    #: CDP 调试端口。挑一个不常见的（9222 是很多人默认的，容易撞）
    cdp_port: int = Field(default=9333, ge=1024, le=65535)
    #: 浏览器可执行文件；不填则自动找 Edge / Chrome（找不到会给可操作提示）
    cdp_executable: str | None = None
    #: 专用配置目录；不填 → `<state_dir>/cdp-profile`
    cdp_profile_dir: Path | None = None


class OcrSettings(BaseModel):
    """截图 OCR（`jh job add --image`，2026-09-21 接入 shotocr）。

    ★ 为什么是一个**可选**能力：shotocr 是独立项目（`vendor/shotocr`），它的依赖
      很重（RapidOCR + opencv ≈ 250 MB，其中 opencv 独占 113 MB）。
      所以 jobhunter **不**把它变成硬依赖：`jh job add --image` 是**子进程调用**，
      `uv sync` 不装它时其余功能照常可用（见 `adapters/ocr.py`）。

    ★ `root` 默认自动探测 `<项目根>/vendor/shotocr`；把它挪到别处时在这里指路径。
    """

    #: shotocr 项目根目录（含 `src/shotocr` 与 `.venv`）；不填 → 自动探测 vendor/shotocr
    root: Path | None = None
    #: 单张图片的识别超时（秒）。OCR 要加载模型，第一次跑偏慢（实测 demo 图约 14 秒）
    timeout_seconds: int = Field(default=300, ge=10, le=3600)


class RateLimitSettings(BaseModel):
    """频率克制（合规红线之一）。

    随机延时 + 出错即停 —— 即使只抓详情也要有节奏，避免给目标站点造成压力，
    也避免被风控标记。

    ★ 2026-09-21 起**有自动搜索**了（`jh job crawl`，用户授权，见
      `docs/COMPLIANCE.md` §5.8），这条配置对"翻页之间"与"详情之间"**都**生效。

    ★ 这里**刻意没有** `max_pages`（ISSUES #18）：它曾经存在，但全仓没有任何读取点，
      而 `docs/COMPLIANCE.md` 却据此声称"抓取路径带翻页上限"——
      **合规文档声称一个不存在的控制，比一个没人读的配置项严重得多**。
      现在的页数由命令行 `--pages` 显式给定（`jh job crawl`），
      条数护栏是 `--limit`（硬上限 100），都不需要这个字段。
    """

    min_delay_seconds: float = Field(default=2.0, ge=0.0)
    max_delay_seconds: float = Field(default=5.0, ge=0.0)

    @model_validator(mode="after")
    def _check_range(self) -> RateLimitSettings:
        if self.min_delay_seconds > self.max_delay_seconds:
            raise ValueError(
                f"rate_limit.min_delay_seconds({self.min_delay_seconds}) 不能大于 "
                f"max_delay_seconds({self.max_delay_seconds})"
            )
        return self


# ---------------------------------------------------------------------------
# 主配置
# ---------------------------------------------------------------------------
class AppSettings(BaseSettings):
    """全项目唯一的配置对象。

    用法：
        from jobhunter.config.settings import get_settings
        settings = get_settings()
        settings.threshold          # 60
        settings.paths.db_path      # D:/.../data/jobhunter.db
    """

    model_config = SettingsConfigDict(
        env_prefix="JH_",  # 通用覆盖用 JH_ 前缀，如 JH_THRESHOLD=70
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env / yaml 里多写的键不要让程序崩
        case_sensitive=False,
    )

    # ---- 求职意图（给可选适配器用的"我想找什么"，不是自动搜索的开关）--------
    keywords: list[str] = Field(
        default_factory=lambda: ["AI Agent", "大模型应用", "Python 开发", "前端开发"]
    )
    cities: list[str] = Field(default_factory=lambda: ["深圳", "广州", "东莞"])
    job_type: Literal["intern", "fulltime", "both"] = "both"

    # ---- 打分与额度 ---------------------------------------------------------
    threshold: int = Field(default=60, ge=0, le=100, description="匹配分低于此值不进待投清单")
    daily_limit: int = Field(default=20, ge=1, le=200, description="每天最多投几条")
    llm_top_n: int = Field(default=30, ge=0, le=1000, description="只对规则分前 N 走 LLM 精评")

    match_weights: MatchWeights = Field(default_factory=MatchWeights)
    match_dimensions: list[str] = Field(
        default_factory=lambda: ["技能关键词", "岗位方向", "经验年限/是否应届"]
    )

    # ---- 技能归一化与白名单 -------------------------------------------------
    # 目标名 → 等价写法列表。解决 Java≠JavaScript、Go≠Google 的误命中。
    skill_aliases: dict[str, list[str]] = Field(default_factory=dict)
    # "我在学"白名单：允许在开场白/简历建议里引用，但必须标注"在学习中"。
    learning_whitelist: list[str] = Field(default_factory=list)

    # ---- 开场白 -------------------------------------------------------------
    greeting_banned_words: list[str] = Field(default_factory=list)
    greeting: GreetingSettings = Field(default_factory=GreetingSettings)

    # ---- 定制简历（P5）-----------------------------------------------------
    tailor: TailorSettings = Field(default_factory=TailorSettings)

    # ---- 频率克制 -----------------------------------------------------------
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)

    # ---- 浏览器抓取（可选，`jh job add --cdp` 用）---------------------------
    browser: BrowserSettings = Field(default_factory=BrowserSettings)

    # ---- 截图 OCR（可选，`jh job add --image` 用）---------------------------
    ocr: OcrSettings = Field(default_factory=OcrSettings)

    # ---- CLI 行为 -----------------------------------------------------------
    show_reminders: bool = True

    # ---- 密钥（只从 .env / 环境变量来）-------------------------------------
    # ★★ 为什么这三个字段要写 `validation_alias`（P3 验收时发现的 P0 老 bug）
    #
    #   上面 model_config 里有 `env_prefix="JH_"`，它对本类**所有**字段生效，
    #   于是 `deepseek_api_key` 实际去找的环境变量名是 `JH_DEEPSEEK_API_KEY`。
    #   但 `.env.example`（以及按它写的真实 `.env`）里写的是 `DEEPSEEK_API_KEY`
    #   —— 名字对不上，密钥**从来没有被读进来过**，而且完全静默：
    #   命令照跑，只是 LLM 层一直"未配置"，用户会以为是自己没充钱。
    #
    #   修法用 `AliasChoices` 同时接受三种写法（顺序即优先级）：
    #     · DEEPSEEK_API_KEY      —— .env.example 里的官方写法（保持不变）
    #     · JH_DEEPSEEK_API_KEY   —— 尊重 env_prefix 的通用写法
    #     · deepseek_api_key      —— 字段名本身（保证 yaml / 代码里构造仍可用）
    deepseek_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "DEEPSEEK_API_KEY", "JH_DEEPSEEK_API_KEY", "deepseek_api_key"
        ),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices(
            "DEEPSEEK_BASE_URL", "JH_DEEPSEEK_BASE_URL", "deepseek_base_url"
        ),
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "JH_DEEPSEEK_MODEL", "deepseek_model"),
    )

    # ---- LLM 行为 -----------------------------------------------------------
    # ★ 为什么要有 provider 开关（而不是"有 key 就走真模型"）：
    #   ① 测试必须能在**完全确定性**的条件下跑匹配（`JH_LLM_PROVIDER=mock`），
    #      否则 CI 会因网络抖动随机变红 → 最后大家把这些测试删掉 → 等于没测；
    #   ② `jh demo` 要用假数据跑通全流程，不能真的花钱。
    llm_provider: Literal["deepseek", "mock"] = "deepseek"
    # 并发度：DeepSeek 对并发比较宽松，但这里是**个人工具**，
    # 4 路已经能把 30 条岗位压到十几秒；再高收益极小，只会更容易触发限流。
    llm_concurrency: int = Field(default=4, ge=1, le=16)
    # 业务层重试次数（空 content / 坏 JSON）。默认只重试 1 次：
    # 空 content 是**偶发**的，重试一次基本就好；连续两次失败说明不是抖动，
    # 继续烧 token 不如直接降级到规则分（用户至少还能看到分数）。
    llm_max_retries: int = Field(default=1, ge=0, le=3)

    # ---- 本地路径（可选覆盖）-----------------------------------------------
    # 直接给 sqlite URL 时优先用它；否则按 paths 推导
    db_url: str | None = None
    data_dir: Path | None = None
    output_dir: Path | None = None
    state_dir: Path | None = None

    # ---- 由 CLI 注入的 config.yaml 位置（便于把项目整体搬走）---------------
    # 说明：这不是"用户配置"，而是"配置文件自己的位置"。
    # 之所以做成字段，是因为 settings_customise_sources 是**类方法**，
    # 拿不到实例参数；做成字段后它会随 __init__ 参数走 init 源，类方法里能读到。
    config_file: Path | None = None

    # ---- 默认简历路径 -------------------------------------------------------
    # `jh resume add` 不传路径时用它（"我就这一份简历"的常见情况）。
    # ★ `jh resume split` **不用它**：那个命令是按库里 `resumes.is_default` 挑简历的
    #   （P9 复查时发现注释与实现不一致，这里按**实际行为**改正）。
    # **这是本机个人配置，绝不要提交进仓库**（简历文件名往往带真名）。
    default_resume_path: Path | None = None

    # ------------------------------------------------------------------
    # source 顺序：显式 init > 环境变量 > .env > config.yaml > 默认值
    # ------------------------------------------------------------------
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 显式传进来的 config_file 优先；否则看环境变量；再否则按默认位置找
        explicit = None
        init_kwargs = getattr(init_settings, "init_kwargs", None)
        if isinstance(init_kwargs, dict):
            explicit = init_kwargs.get("config_file")

        yaml_path: Path | None = None
        if explicit:
            yaml_path = Path(explicit)
        else:
            import os

            from_env = os.environ.get(ENV_CONFIG)
            yaml_path = Path(from_env) if from_env else _load_default_yaml()

        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls, yaml_path),
            file_secret_settings,
        )

    # ------------------------------------------------------------------
    # 校验：把"配置写错"变成启动时的明确报错
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate(self) -> AppSettings:
        if not self.keywords:
            raise ValueError("keywords 不能为空：没有搜索意图就无法判断岗位方向是否匹配")
        if not self.cities:
            raise ValueError("cities 不能为空：城市是硬条件维度之一")

        if not 0 <= self.threshold <= 100:
            raise ValueError(f"threshold 必须在 0-100 之间，当前 {self.threshold}")

        # skill_aliases 的值必须是非空字符串列表（写错格式会导致归一化静默失效）
        for canonical, aliases in self.skill_aliases.items():
            if not canonical.strip():
                raise ValueError("skill_aliases 里出现了空白的目标名")
            if not aliases:
                raise ValueError(f"skill_aliases[{canonical!r}] 的别名列表不能为空")

        self._check_learning_whitelist()
        self._check_banned_words()
        return self

    # ------------------------------------------------------------------
    # ISSUES #43：这两项以前"写错了不报错"，后果都是**静默无效**
    # ------------------------------------------------------------------
    def _check_learning_whitelist(self) -> None:
        """学习白名单里的词必须是**词表里存在的技能**。

        ★ 挡的是什么：白名单的用途是"允许提、但必须标注『在学习』"。
          如果写成一个词表里没有的词（九成是拼错，比如 `LanchChain`），
          它既抽不出来、也不会有任何一份 JD 提到它 ——
          用户以为放开了某个技能，实际上这份白名单**永远是空的**，而且不报错。

        ★ 为什么"必须是词表里的词"而不是"随便写、我们自动收进词表"：
          后者正是改动前的行为（`resume_svc.load_skill_vocabulary` 会把白名单里的词
          `setdefault` 进词表），它让**拼写错误也变得合法**——错词只是安静地躺在词表里。
          现在改为：想加一个新技能，先在 `skill_aliases` 里把它声明成规范名
          （那样它本来就会进词表、也才能从 JD 里抽出来），再放进白名单。
          报错信息里直接给出这一步，用户不用去翻文档。
        """
        if not self.learning_whitelist:
            return
        known = set(BUILTIN_SKILLS) | set(self.skill_aliases)
        unknown = [w for w in self.learning_whitelist if w.strip() and w.strip() not in known]
        blank = [w for w in self.learning_whitelist if not w.strip()]
        if blank:
            raise ValueError(
                "learning_whitelist 里有空白项：期望每一项都是一个技能名，"
                "空白项永远匹配不上任何东西（多半是多写了一个 `-`）"
            )
        if unknown:
            raise ValueError(
                "learning_whitelist 里出现了词表里没有的技能："
                + "、".join(repr(w) for w in unknown)
                + "。\n"
                "  期望：只写内置技能词表（见 jobhunter/config/vocabulary.py）里已有的技能名，"
                "或先把它作为规范名写进 skill_aliases。\n"
                "  为什么必须这样：白名单里的错词永远不会命中任何 JD，"
                "「我在学 X」这条规则会静默失效 —— 你只会觉得这个功能没效果。"
            )

    def _check_banned_words(self) -> None:
        """开场白禁用词必须是**有意义的短词**（ISSUES #43）。

        ★ 以前这里只做 `[w for w in ... if w.strip()]` 静默去空，问题有两个：
          · 用户在列表里多写一个空行 / 一个 `-`，配置看起来"生效了"，其实被悄悄丢掉；
          · 超长句子当禁用词会**误伤**：禁用词是朴素子串匹配，
            写进去一整句话，等于要求开场白不能出现那句话里的任何一个字序组合
            （`docs/config.md` §6 记着上一次同类事故：太短的词导致每条文案都被判违规）。
          所以现在：空串/纯空白直接报错，并给一个长度上限。
        """
        for index, word in enumerate(self.greeting_banned_words):
            if not word.strip():
                raise ValueError(
                    f"greeting_banned_words 第 {index + 1} 项是空的（值 {word!r}）："
                    "空字符串会被当成『匹配一切』的禁用词，必须删掉这一项"
                )
            if word != word.strip():
                raise ValueError(
                    f"greeting_banned_words 第 {index + 1} 项首尾有空白（值 {word!r}）："
                    f"期望写成 {word.strip()!r}，否则匹配不上任何正常文案"
                )
            if len(word) > MAX_BANNED_WORD_CHARS:
                raise ValueError(
                    f"greeting_banned_words 第 {index + 1} 项太长"
                    f"（{len(word)} 字 > 上限 {MAX_BANNED_WORD_CHARS}）："
                    f"值 {word!r}。禁用词是朴素子串匹配，写一整句话会误伤大量正常文案 —— "
                    "请只留套话级词（如 贵司 / 三生有幸 / 冒昧打扰）"
                )

    # ------------------------------------------------------------------
    # 便捷访问
    # ------------------------------------------------------------------
    @property
    def paths(self) -> PathSet:
        """解析好的目录集合。显式配置的目录优先，否则用默认布局。

        项目根 = config.yaml 所在目录（便于把整个项目搬走）；
        没指定 config.yaml 时退回默认的项目根探测。

        ★★ 显式配置的**相对**路径按 `config.yaml` 所在目录解析（只读代码审查 JH-05）。

        这条 docstring 一直写着"项目根 = config.yaml 所在目录"，但实现只对
        **默认**目录用了那个 base：显式写 `data_dir: data` 时 `Path("data")` 原样留着，
        最后相对于**当前工作目录**打开 —— 于是同一份配置在不同目录下运行会打开
        **不同的库**（"我的数据丢了"），或者把简历/导出物写到用户没预期的位置。

        现在的口径（三处一致：`data_dir` / `output_dir` / `state_dir`）：
          · 绝对路径 → 原样使用；
          · 相对路径 → `config.yaml` 所在目录 / 相对路径；
          · 没配 → 默认布局（`<项目根>/data` 等）。

        ⚠️ 环境变量（`JH_DATA_DIR` 等）里的相对路径语义见 `docs/config.md` §2：
        它们与 config.yaml 无关，按**当前工作目录**解析（那是进程给的值）。
        """
        base = default_paths(self.config_file.parent if self.config_file else None)
        return PathSet(
            root=base.root,
            data_dir=self._resolve_dir(self.data_dir) or base.data_dir,
            output_dir=self._resolve_dir(self.output_dir) or base.output_dir,
            state_dir=self._resolve_dir(self.state_dir) or base.state_dir,
        )

    def _resolve_dir(self, value: Path | None) -> Path | None:
        """把配置里显式写的目录解析成绝对路径（相对路径挂到 config.yaml 旁边）。

        ★ 锚点为什么是 `config_file.parent` 而**不是** `find_project_root()`：
          "把整个项目搬走"这个用法要求配置与它指的数据目录一起搬 ——
          锚在配置文件旁边，搬完之后相对路径仍然指向搬过去的那份数据。
        """
        if value is None:
            return None
        if value.is_absolute():
            return value
        anchor = (
            Path(self.config_file).resolve().parent if self.config_file else default_paths().root
        )
        return anchor / value

    @property
    def database_url(self) -> str:
        """SQLAlchemy 连接串：显式 db_url 优先，否则按 paths 推导。"""
        if self.db_url:
            return self.db_url
        return self.paths.sqlite_url

    @property
    def llm_enabled(self) -> bool:
        """是否配好了大模型密钥。P3 的精评会用这个判断能不能走 LLM 层。"""
        return bool(self.deepseek_api_key.strip())

    def aliases_for(self, canonical: str) -> list[str]:
        """取某个技能的全部等价写法（含它自己），用于归一化匹配。"""
        extra = self.skill_aliases.get(canonical, [])
        return [canonical, *extra]


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
def _load_default_yaml() -> Path | None:
    """默认 config.yaml 位置：优先当前工作目录，其次项目根。

    为什么要两个候选：开发时在项目根跑（cwd 命中），
    装成工具后在别处跑（用 find_project_root 找到的那份）。
    """
    cwd_candidate = Path.cwd() / "config.yaml"
    if cwd_candidate.is_file():
        return cwd_candidate
    root_candidate = default_paths().root / "config.yaml"
    if root_candidate.is_file():
        return root_candidate
    return None


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """取全局配置单例（带缓存）。

    ★ 测试里必须改配置时，用 `get_settings.cache_clear()` 清缓存，
      或者用 tests/conftest.py 提供的 `settings` fixture（已处理好隔离）。
    ★ 环境变量 `JH_CONFIG` 可指定 config.yaml 路径（CLI 的 --config 也走这个变量）。
    """
    from_env = _env_config_path()
    try:
        return AppSettings(config_file=from_env)
    except ValidationError as exc:
        # 把 pydantic 的长篇报错压成一句人能看懂的话。
        # 这个函数在 CLI 启动时就会被调用，报错质量直接决定"配置写错"的体验。
        raise ValueError(f"配置有问题，请检查 config.yaml：\n{exc}") from exc


def _env_config_path() -> Path | None:
    """环境变量指定的 config.yaml（不存在就当作没指定，回落到默认位置）。"""
    import os

    raw = os.environ.get(ENV_CONFIG)
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def reset_settings() -> None:
    """清掉配置单例缓存（测试与 `--config` 切换后使用）。"""
    get_settings.cache_clear()


def load_settings(config_file: Path | str | None = None) -> AppSettings:
    """按指定文件加载配置（**不污染全局单例**）。

    与 `--config` 的区别（别搞混，两条路是分开的）：
        · `jh --config other.yaml` 走的是**环境变量**：`cli/app.py` 把路径写进
          `JH_CONFIG` 再 `reset_settings()`，这样所有命令拿到的都是同一份配置；
        · 这个函数不碰单例与环境变量，**直接构造一个独立的 AppSettings**，
          适合测试里要两个不同配置同时存在的场合。

    传 None 时退化成 `get_settings()`（即走单例/常规加载路径）。
    """
    if config_file is None:
        return get_settings()

    path = Path(config_file)
    if not path.is_file():
        raise FileNotFoundError(f"找不到配置文件：{path}")
    try:
        return AppSettings(config_file=path)
    except ValidationError as exc:
        raise ValueError(f"配置文件有问题：{path}\n{exc}") from exc


def settings_snapshot(settings: AppSettings) -> dict[str, Any]:
    """导出可打印的配置快照（**不含密钥**）。

    ★ 绝不包含 deepseek_api_key —— 这个函数就是"打印配置"这件事的安全出口。
      注意 `utils/logging.py` 的脱敏 Filter 只处理手机号/邮箱/身份证，
      **不认 API key**，所以"别把整个 settings 对象 dump 出去"必须靠这条纪律：
      要打印配置，就调这个函数（白名单字段），不要 `settings.model_dump()`。

    ★ 状态说明（免得下一个人以为它有隐藏调用方）：
      目前**没有调用点**。保留它是因为 P3 要调 `match_weights` / `threshold`，
      那时"为什么这个岗位只打了 52 分"的第一步就是确认"当前到底用的什么权重"；
      到那一刻它自然会接上 `jh match explain` 或 `jh config`。
      在那之前它也不是白占地方 —— 它把"哪些字段可以安全打印"写成了代码。
    """
    return {
        "keywords": settings.keywords,
        "cities": settings.cities,
        "job_type": settings.job_type,
        "threshold": settings.threshold,
        "daily_limit": settings.daily_limit,
        "llm_top_n": settings.llm_top_n,
        "match_weights": settings.match_weights.model_dump(),
        "greeting_chars": (
            settings.greeting.full_min_chars,
            settings.greeting.full_max_chars,
            settings.greeting.short_max_chars,
        ),
        "tailor_limits": {
            "max_length_ratio": settings.tailor.max_length_ratio,
            "max_suggestions": settings.tailor.max_suggestions,
        },
        "llm_enabled": settings.llm_enabled,
        "config_file": str(settings.config_file) if settings.config_file else None,
    }
