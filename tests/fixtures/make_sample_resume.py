"""生成虚构的简历 fixture（`tests/fixtures/sample_resume.docx`）。

★ 为什么用脚本生成、而不是提交一个手搓的二进制 docx

    1. **可审计**：docx 是 zip 二进制，放进仓库后没人能 review 里面到底写了什么。
       而"测试数据"恰恰是最需要被 review 的东西之一 ——
       **一旦有人把真实简历误存成 fixture 提交，就是一次隐私泄露事故。**
       用脚本生成，内容全在代码里，一眼能看完。
    2. **可重生成**：想加一个测试场景（比如"没有小标题的简历"），改脚本重跑即可。
    3. **可校验**：本脚本里所有个人信息的每一个字符都是编的。

★ 这个 fixture 刻意做得"难一点"，覆盖 P1 解析的真实难点

    · **两栏表格排版**：左栏项目名、右栏项目描述（中文简历最常见）
    · **文本框**：把联系方式放在文本框里（python-docx 默认读不到 → 测我们的兜底）
    · **段落 + 表格混排**：项目经历用段落、教育经历用表格
    · **技能栏带熟练度前缀**："精通 Python、熟悉 Vue3" → 测前缀剥离
    · **词边界陷阱**：技能栏写 `JavaScript` 但不能被识别成 `Java`；
      实习经历里写 `Google` 但不能被识别成 `Go`
    · **时间段多种写法**：`2024.03 - 2024.09`、`2023/09 - 至今`

★★ 隐私纪律（重要）
    这里的所有身份信息都是**编造的**，且刻意用一眼可辨的假值：
        姓名：张三丰 → 改用「林小满」（虚构）
        手机：13800000000（这是"明显假"的号码，不是某个真人的号）
        邮箱：xiaoman.lin@example.com（example.com 是 RFC 2606 保留域名，永不指向真人）
    运行时会校验这一点（见 `assert_fictional()`）。
"""

from __future__ import annotations

import re
from pathlib import Path

from jobhunter.utils.io import ensure_dir

__all__ = ["FICTIONAL_IDENTITY", "assert_fictional", "build_sample_resume", "main"]

#: 虚构身份信息（全仓库唯一来源，改这里就改了 fixture）
FICTIONAL_IDENTITY: dict[str, str] = {
    "name": "林小满",
    "phone": "13800000000",
    "email": "xiaoman.lin@example.com",
    "city": "深圳",
    "school": "南方某大学（虚构）",
    "major": "软件工程",
    "intention": "AI Agent 应用开发 / 后端开发",
}

#: 明显假的手机号：**`1[3-9]` 之后除了一个可选的 `8`，其余全是 0**（如 13800000000）。
#: 真实号码不可能长这样，任何人扫一眼就知道不是真号。
#: （`8?` 这一段是必要的：138 里的 `8` 是前导号段的一部分，不是"连号"的一部分。）
_FICTIONAL_PHONE_RE = re.compile(r"^1[3-9]8?0+$")
#: RFC 2606 保留域名 —— 永远不会指向真实邮箱服务
_SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "test.invalid")


def assert_fictional(identity: dict[str, str] | None = None) -> None:
    """★ 隐私自检：fixture 里的身份信息必须是"明显假的"。

    这是最后一道闸门。如果将来有人把这里改成真实信息，测试会直接失败 ——
    因为"测试数据"是最容易被顺手写成真实数据的地方（复制粘贴自己的简历最快）。
    """
    data = identity or FICTIONAL_IDENTITY

    phone = data.get("phone", "")
    assert _FICTIONAL_PHONE_RE.match(phone), (
        f"fixture 里的手机号 {phone!r} 看起来不够假 —— "
        "测试数据必须用一眼可辨的假号码（如 13800000000）"
    )

    email = data.get("email", "")
    domain = email.rsplit("@", 1)[-1].lower()
    assert domain in _SAFE_EMAIL_DOMAINS, (
        f"fixture 里的邮箱域名 {domain!r} 不在保留域名白名单里 —— "
        "测试数据必须用 example.com 这类永不指向真人的域名"
    )


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def build_sample_resume(path: str | Path) -> Path:
    """生成虚构简历 docx，返回文件路径。

    文档结构（刻意覆盖真实排版）：

        [文本框] 林小满 · 13800000000 · xiaoman.lin@example.com · 深圳
        林小满
        求职意向：AI Agent 应用开发 / 后端开发
        教育经历（表格三列：学校 | 专业/学位 | 时间）
        专业技能
        项目经验（段落式，3 个项目）
        实习经历（表格两栏：公司+岗位 | 工作内容）
        荣誉奖项
    """
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt

    assert_fictional()
    target = Path(path)
    ensure_dir(target.parent)

    document = docx.Document()
    style = document.styles["Normal"]
    style.font.name = "微软雅黑"
    # 中文字体必须显式设置 eastasia，否则中文会掉回默认字体（在别的机器上排版会变）
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    style.font.size = Pt(10.5)

    def heading(text: str) -> None:
        paragraph = document.add_paragraph()
        run = paragraph.add_run(text)
        run.bold = True
        run.font.size = Pt(12)

    # ---- ① 文本框：联系方式（python-docx 默认读不到，测兜底逻辑）----------
    _add_textbox(document, " | ".join([FICTIONAL_IDENTITY["name"], FICTIONAL_IDENTITY["phone"]]))

    # ---- ② 顶部信息 ------------------------------------------------------
    name_para = document.add_paragraph()
    name_run = name_para.add_run(FICTIONAL_IDENTITY["name"])
    name_run.bold = True
    name_run.font.size = Pt(16)
    name_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.add_paragraph(f"求职意向：{FICTIONAL_IDENTITY['intention']}")
    document.add_paragraph(
        f"电话：{FICTIONAL_IDENTITY['phone']}　邮箱：{FICTIONAL_IDENTITY['email']}"
        f"　城市：{FICTIONAL_IDENTITY['city']}"
    )

    # ---- ③ 教育经历（表格）----------------------------------------------
    heading("教育经历")
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    header = table.rows[0].cells
    header[0].text = "学校"
    header[1].text = "专业 / 学位"
    header[2].text = "时间"
    row = table.add_row().cells
    row[0].text = FICTIONAL_IDENTITY["school"]
    row[1].text = f"{FICTIONAL_IDENTITY['major']} / 本科"
    row[2].text = "2023.09 - 2027.06"
    row = table.add_row().cells
    row[0].text = "虚构市第一中学"
    row[1].text = "理科"
    row[2].text = "2020.09 - 2023.06"

    # ---- ④ 专业技能（带熟练度前缀，测前缀剥离）--------------------------
    heading("专业技能")
    document.add_paragraph(
        "精通 Python、熟练掌握 FastAPI 与 SQLAlchemy；熟悉 Vue3 + TypeScript 前端开发；"
        "了解 Docker、Redis、MySQL；使用过 LangChain 做过 RAG 检索增强的 demo。"
    )
    document.add_paragraph("JavaScript 与 CSS 基础扎实（写过组件库）；了解 Git 与 Linux 常用命令。")

    # ---- ⑤ 项目经验（段落式，标题行带编号与时间段）---------------------
    heading("项目经验")
    document.add_paragraph("1. 智能简历匹配助手（AI Agent 方向）　2024.03 - 2024.09　个人项目")
    document.add_paragraph("· 用 Python + FastAPI 搭后端，接入大模型做 JD 与简历的关键词抽取；")
    document.add_paragraph(
        "· 用 LangChain 编排 Agent 工作流，结合向量数据库做 RAG 检索，"
        "把 JD 解析结果落库复用，减少重复的模型调用；"
    )
    document.add_paragraph("· 用 SQLite + SQLAlchemy 存储，写了 30 多个单元测试。")

    document.add_paragraph("2. 校园二手交易平台　2023.10 - 2024.01　后端负责人")
    document.add_paragraph("· 用 Python + Flask 实现商品与订单接口，MySQL 建表并做了索引优化；")
    document.add_paragraph("· 用 Redis 做热点数据缓存，接口平均响应从 300ms 降到 90ms。")

    document.add_paragraph("3. 数据看板前端　2023.05 - 2023.08　前端开发")
    document.add_paragraph("· 用 Vue3 + TypeScript + ECharts 做可视化看板，封装了 12 个通用组件；")
    document.add_paragraph("· 用 Vite 做构建，首屏体积从 1.8MB 优化到 900KB。")

    # ---- ⑥ 实习经历（表格两栏，测单元格合并/去重）-----------------------
    heading("实习经历")
    intern_table = document.add_table(rows=0, cols=2)
    intern_table.style = "Table Grid"
    cells = intern_table.add_row().cells
    cells[0].text = "虚构科技有限公司 | 后端开发实习生 | 2024.07 - 2024.09"
    cells[1].text = (
        "参与内部工单系统的接口开发（Python + FastAPI）；"
        "用 Docker 把服务容器化并写了部署脚本；"
        "在 Google 风格代码评审里过了一轮重构，学会写清晰的接口文档。"
    )
    cells = intern_table.add_row().cells
    cells[0].text = "某某网络工作室 | 前端实习生 | 2023.07 - 2023.09"
    cells[1].text = "用 Vue3 重写后台管理页面；配合设计师做了响应式布局适配。"

    # ---- ⑦ 荣誉奖项（测"认得出但不是核心段"的处理）---------------------
    heading("荣誉奖项")
    document.add_paragraph("· 全国大学生软件设计大赛 省级二等奖（2024）")
    document.add_paragraph("· 校级一等奖学金（2024 - 2025 学年）")

    document.save(str(target))
    return target


def _add_textbox(document: object, text: str) -> None:
    """往文档最前面插一个文本框（模拟"联系方式放在文本框里"的模板）。

    ★ 为什么用 VML（`w:pict`）这种"老"写法而不是 DrawingML（`mc:AlternateContent`）：
      · 只需要 `w` 和 `v` 两个命名空间，XML 短、不易写错
        （DrawingML 还要 `mc`/`wp`/`a`/`pic`，`nsdecls()` 少给一个就直接 KeyError）；
      · **关键**：它同样会被解析成 `w:txbxContent` ——
        而 `w:txbxContent` 正是 python-docx 的 `paragraphs`/`tables` **读不到**的东西。
        也就是说，P1 要测的那条兜底路径（`utils/docx.py::_extract_textboxes`）
        用 VML 触发是等价的。
    """
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    xml = (
        f"<w:p {nsdecls('w')}>"
        f"<w:r><w:pict>"
        f'<v:shape xmlns:v="urn:schemas-microsoft-com:vml" '
        f'style="width:300pt;height:20pt" stroked="f">'
        f"<v:textbox><w:txbxContent>"
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        f"</w:txbxContent></v:textbox>"
        f"</v:shape></w:pict></w:r></w:p>"
    )
    paragraph = parse_xml(xml)
    body = document.element.body  # type: ignore[attr-defined]
    body.insert(0, paragraph)


def main() -> None:
    """命令行入口：`python tests/fixtures/make_sample_resume.py [输出路径]`。"""
    import sys

    default = Path(__file__).resolve().parent / "sample_resume.docx"
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    written = build_sample_resume(target)
    print(f"已生成虚构简历 fixture：{written}")
    print(f"  身份信息（全部虚构）：{FICTIONAL_IDENTITY}")


if __name__ == "__main__":
    main()
