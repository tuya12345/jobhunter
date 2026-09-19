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

__all__ = [
    "AppSettings",
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


class RateLimitSettings(BaseModel):
    """频率克制（合规红线之一）。

    随机延时 + 翻页上限 + 出错即停 —— 不做自动搜索，但即使抓详情也要有节奏，
    避免给目标站点造成压力，也避免被风控标记。
    """

    min_delay_seconds: float = Field(default=2.0, ge=0.0)
    max_delay_seconds: float = Field(default=5.0, ge=0.0)
    max_pages: int = Field(default=3, ge=1, le=50)

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
    # 设了它之后，`jh resume add` 不传路径就用这份；`jh resume split` 不传 id
    # 也用这份。**这是本机个人配置，绝不要提交进仓库**（简历文件名往往带真名）。
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

        # 禁用词只做去空，不强制非空（用户可以一个都不填）
        self.greeting_banned_words = [w for w in self.greeting_banned_words if w.strip()]
        return self

    # ------------------------------------------------------------------
    # 便捷访问
    # ------------------------------------------------------------------
    @property
    def paths(self) -> PathSet:
        """解析好的目录集合。显式配置的目录优先，否则用默认布局。

        项目根 = config.yaml 所在目录（便于把整个项目搬走）；
        没指定 config.yaml 时退回默认的项目根探测。
        """
        base = default_paths(self.config_file.parent if self.config_file else None)
        return PathSet(
            root=base.root,
            data_dir=self.data_dir or base.data_dir,
            output_dir=self.output_dir or base.output_dir,
            state_dir=self.state_dir or base.state_dir,
        )

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
