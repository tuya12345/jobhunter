# 截图 OCR（shotocr）

本地离线的截图文字识别工具。截屏幕区域、剪贴板或图片，识别出文字，
可导出 `txt` / `json` / `markdown`。

**全程离线**，不联网、不上传图片。

针对"截图识别率不高"这个具体问题，内置了三件事：

1. **多档预处理**：放大、灰度、对比度增强、锐化、自适应二值化；
2. **低置信度自动重试**：平均置信度低于阈值时，自动换更强的预处理重跑，取最好的一次；
3. **分栏阅读顺序**：多栏排版（左右两栏的网页、IDE）不会被串成一行。

---

## 实测准确率

10 个场景 × 2 引擎的合成样本基准（**内容完全一致，只改渲染方式**）：

| 场景 | RapidOCR | PaddleOCR |
|---|---|---|
| 白底 / 深色 / 深色高亮 / 小字 / 彩底 / 低对比 / 模糊 | 0.0% | 0.0% |
| 噪点背景 | 1.5% | **0.0%** |
| **双栏排版** | **0.0%** | 0.0% |
| JPEG 压缩（quality=22） | 1.5% | 1.5% |

数字是字符错误率（CER），越小越好。

两个值得注意的点：

- **双栏排版原本 CER 54.3%** —— 6 个块内容全对，但左右栏被拼成一行。
  修好分栏检测后降到 0.0%。这个缺陷**只测单栏样本是发现不了的**。
- **jpeg 那 1.5% 不是真错**：是 `￥`(U+FFE5) 被认成 `¥`(U+00A5)，
  视觉和语义都相同。

> ⚠️ **样本是合成的，比真实截图干净**（没有真实界面元素干扰、没有复杂背景纹理）。
> **真实截图上的识别率没测过**，不要把上面的 0.0% 当成承诺。
> 完整数据与局限见 `docs\部署记录.md` §4.6。

自己复现：

```cmd
.venv\Scripts\python.exe tools\make_scenes.py
.venv\Scripts\python.exe tools\benchmark.py --engines rapid,paddle
```

### 真实截图验证（5 张不同版式）

用 5 张真实网页截图（均 1919 × 1079）实测，覆盖三种版式：

| 版式 | 预期栏数 | 实际 |
|---|---|---|
| 搜索页：左列表 + 右详情 | 2 | ✅ 2 |
| 详情页：正文 + 右侧卡片 | 2 | ✅ 2 |
| 详情页：纯长正文 | **1** | ✅ 1 |
| 详情页：正文 + 右侧卡片 | 2 | ✅ 2 |
| 详情页：正文 + 右侧卡片 | 2 | ✅ 2 |

**这批测试连续抓出两个真 bug**：

1. **分栏检测在真实页面上完全失效**（检测到 1 栏）——
   真实页面顶部有横跨全宽的标签栏 / 导航栏，且真实栏间距只有 7px
   （原阈值是 96px，差了 13 倍）；
2. **修好之后又矫枉过正** —— 把"同一行里的多个块"（标题 / 薪资 / 标签）
   误判成两栏，4 张图全被切成 4 栏。加了"**y 方向覆盖率**"判据才解决。

引擎耗时对比（同一张图）：

| 指标 | RapidOCR | PaddleOCR |
|---|---|---|
| 平均置信度 | 0.9641 | 0.9515 |
| **耗时** | **4.55 s** | **85.40 s** |

**真实大截图上两者的耗时差距被放大到约 19 倍**（合成图上是 4 倍）。
**RapidOCR 是实用选择**，除非你愿意为每张图等一分半钟。

**已知局限**：顶部横跨全宽的标签栏 / 导航栏会被拆成左右两半，
这部分顺序不理想；但**主体内容（左右分栏区）的顺序是正确的**。
详见 `docs\部署记录.md` §4.7。

---

## 快速开始

项目位置：`D:\P7code\shotocr`

### 方式一：激活虚拟环境（推荐）

```cmd
cd /d D:\P7code\shotocr
.venv\Scripts\activate

rem 识别屏幕区域（x,y,w,h）
shotocr --region 100,100,900,600

rem 识别剪贴板里的图片
shotocr --clipboard

rem 识别本地图片，并导出 txt / json / md
shotocr --file samples\demo.png --out-dir out
```

项目已经用 `pip install -e .` 装成**可编辑模式**，所以激活环境后
`shotocr` 命令直接可用，改了源码也不用重装。

### 方式二：不激活，直接用完整路径

```cmd
cd /d D:\P7code\shotocr
.venv\Scripts\shotocr.exe --clipboard
```

### 方式三：在 bash 里调用

```bash
cd /d/P7code/shotocr
./.venv/Scripts/shotocr.exe --clipboard
```

### 方式四：备用启动脚本

项目根目录还有一个 `shotocr.cmd`，它内部会设好 `PYTHONPATH` 再调用：

```cmd
cd /d D:\P7code\shotocr
shotocr.cmd --clipboard
```

> 它等价于「设好 `PYTHONPATH=src` 后跑 `python -m shotocr`」，
> 不需要激活环境。

> 四种方式等价。**推荐方式一或方式二**，因为它们不依赖 `.cmd` 脚本。

---

## 自检

怀疑环境坏了、或者改完代码想确认没弄坏，跑这个：

```cmd
cd /d D:\P7code\shotocr
.venv\Scripts\python.exe tools\selfcheck.py
```

它会检查依赖导入、引擎可用性、识别结果、坐标合理性、三种输出格式、
**分栏阅读顺序**，最后打印 `N/N 通过`，**全部通过时退出码为 0**。

当前实测：**14/14 通过**。

---

## 目录结构

```
D:\P7code\shotocr\
├── README.md                  ← 本文档，先看这个
├── .gitignore                 ← 排除 .venv / out / 真实截图
├── pyproject.toml
├── requirements.txt           ← 基础依赖（RapidOCR 轻量引擎）
├── requirements-paddle.txt    ← 高精度引擎依赖（PaddleOCR，可选）
├── shotocr.cmd                ← Windows 启动脚本
│
├── docs\                      ← 文档（索引见 docs\README.md）
│   ├── README.md              ← 文档索引：按需求查
│   ├── 调用手册.md             ← 全部参数、Python API、算法细节
│   └── 部署记录.md             ← 环境、版本、实测数据、踩过的坑
│
├── src\shotocr\               ← 源码
│   ├── capture.py             ← 截图（屏幕 / 区域 / 剪贴板 / 文件）
│   ├── preprocess.py          ← 图像预处理四档
│   ├── engines\               ← 引擎适配层
│   │   ├── base.py            ← 引擎协议
│   │   ├── rapid.py           ← RapidOCR
│   │   └── paddle.py          ← PaddleOCR
│   ├── pipeline.py            ← 主流程 + 低置信度重试
│   ├── schema.py              ← 统一结果结构 + 阅读顺序算法
│   ├── output.py              ← txt / json / md 输出
│   └── cli.py                 ← 命令行入口
│
├── tools\                     ← 工具脚本（索引见 tools\README.md）
│   ├── README.md              ← 工具索引：什么时候用哪个
│   ├── selfcheck.py           ← 自检（14 项，改完代码先跑这个）
│   ├── benchmark.py           ← 多场景准确率基准（算 CER）
│   ├── make_sample.py         ← 生成单张测试图
│   ├── make_scenes.py         ← 生成 10 个场景样本（深色/小字/双栏…）
│   └── put_image_to_clipboard.py  ← 把图片放进剪贴板（测剪贴板识别用）
│
├── samples\                   ← 样本（索引见 samples\README.md）
│   ├── README.md
│   ├── demo.png               ← 单张测试图
│   ├── scenes\                ← 10 个合成场景 + labels.json
│   └── real\                  ← 你的真实截图回归样本（目前为空）
│
├── results\                   ← 基准结果（benchmark.json）
├── out\                       ← 识别输出目录（可随时清空）
└── .venv\                     ← 虚拟环境（Python 3.13，约 987 MB）
```

**样本和结果分开放**：`samples\` 是只读素材，`results\` 是跑出来的产物 ——
删掉 `results\` 不影响复现。

**每个目录都有自己的 README**（`docs\` / `tools\` / `samples\`），
进去先看那个。

---

## 两个引擎怎么选

| | RapidOCR | PaddleOCR |
|---|---|---|
| 推理后端 | ONNX Runtime | PaddlePaddle |
| **实测总体积** | **292 MB** | **约 1.12 GB**（987 MB venv + 133 MB 模型） |
| **实测耗时**（同一张图，模型已加载） | **2.48 s** | 10.50 s |
| **实测精度** | 好 | 更好（6 行里 4 行更准） |
| 首次使用 | 装完即用 | 首次要下载 133 MB 模型 |
| 适用 | 日常截图、要打进 exe | 追求精度、不介意体积和耗时 |

实测对比的完整数据见 `docs\部署记录.md` §4.2。

```cmd
rem 默认 auto：装了 paddle 就用 paddle，没装就用 rapid
shotocr --clipboard

rem 强制指定
shotocr --clipboard --engine rapid
shotocr --clipboard --engine paddle

rem 看当前装了哪些引擎
shotocr --list-engines
```

装高精度引擎：

```cmd
cd /d D:\P7code\shotocr
.venv\Scripts\python.exe -m pip install -r requirements-paddle.txt -i https://mirrors.aliyun.com/pypi/simple/
```

> 注意：
> - 安装耗时较长（本机实测约 **25 分钟**），体积增加约 **700 MB**；
> - 首次运行会自动下载约 **133 MB** 模型，第一次识别明显慢；
> - paddlepaddle 3.3.1 默认配置有个 oneDNN 报错，**代码里已经绕过**
>   （`enable_mkldnn=False`），你不需要手动处理。详见 `docs\部署记录.md` §7.5；
> - 装完它会把 `numpy` 降级到 2.3.5。已验证 **RapidOCR 不受影响**（自检仍 12/12）。

---

## 常用命令

> 以下命令假设你已经在 `D:\P7code\shotocr` 目录、且已激活虚拟环境
> （见「快速开始」方式一）。没激活的话，把 `shotocr` 换成
> `.venv\Scripts\shotocr.exe` 就行。

```cmd
rem 全屏
shotocr --screen

rem 屏幕区域：x,y,w,h（左上角坐标 + 宽 + 高）
shotocr --region 0,0,1920,400

rem 剪贴板（微信/QQ 截图后直接识别）
shotocr --clipboard

rem 本地图片
shotocr --file D:\pics\screenshot.png

rem 导出三种格式，文件名自动带时间戳
shotocr --clipboard --out-dir out

rem 指定单个输出文件
shotocr --clipboard --md out\result.md

rem 打印文本块明细（含坐标与置信度，调参用）
shotocr --clipboard --show-blocks

rem 关掉自动重试（更快，但识别率可能下降）
shotocr --clipboard --no-retry

rem 调低重试阈值，更激进地重试
shotocr --clipboard --min-score 0.85

rem 关掉分栏检测（单栏文本被误判成两栏时才需要）
shotocr --file x.png --reading-order row
```

识别出来的文本走 **stdout**，可以直接重定向：

```cmd
shotocr --clipboard > result.txt
```

---

## 输出格式

**txt** —— 按阅读顺序排好的纯文本（按 y 分行、行内按 x 排序）。

**json** —— 完整结构化结果：

```json
{
  "source": "clipboard",
  "engine": "rapid",
  "elapsed_seconds": 0.83,
  "image_size": [1920, 1080],
  "stages": ["none"],
  "block_count": 23,
  "mean_score": 0.94,
  "min_score": 0.61,
  "text": "识别出的完整文本",
  "blocks": [
    { "text": "某一行文字", "score": 0.98, "box": [[x, y], [x, y], [x, y], [x, y]] }
  ],
  "meta": { "attempts": [], "retried": false, "preset": "none" }
}
```

**markdown** —— 人类可读的报告，含元信息、识别文本、低置信度块清单、重试记录。

---

## 在 Python 里调用

```python
import sys
sys.path.insert(0, r"D:\P7code\shotocr\src")

from shotocr import capture
from shotocr.engines import build_engine
from shotocr.pipeline import recognize_image

engine = build_engine("rapid")

# 截屏幕区域
image = capture.grab(capture.parse_region("100,100,900,600"))
# 或者读文件：image = capture.load_image(r"D:\pics\a.png")

result = recognize_image(capture.to_ndarray(image), engine)

print(result.text)          # 纯文本
print(result.mean_score)    # 平均置信度
for block in result.blocks: # 块级信息
    print(block.text, block.score, block.box)
```

---

## 常见问题

**Q：识别率不满意怎么办？**

按顺序试：

1. `--preset strong` 强制走增强预处理；
2. `--preset binary` 适合灰底、彩色背景上的文字；
3. `--min-score 0.9` 让它更积极重试；
4. `--engine paddle` 换高精度引擎；
5. `--show-blocks` 看哪些块置信度低，判断是"没识别出来"还是"识别错了"。

**Q：区域坐标对不上，截出来的位置偏了？**

程序已经声明了 DPI 感知，正常情况下不会偏。如果仍偏，检查是否用了
`--region` 的 `x,y,w,h` 语义（不是对角两点），以及显示器缩放比例。

**Q：剪贴板识别报"剪贴板里没有图片"？**

先截图或复制图片再调用。某些截图工具不把图片放进标准剪贴板格式。

**Q：终端里中文是乱码？**

程序已把 stdout 设为 utf-8。若仍乱码，执行 `chcp 65001` 切换代码页。

**Q：pip 装不上包？**

`pypi.org` 在本机代理下不通，用镜像源：

```cmd
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

---

## 相关文档

- [`docs\调用手册.md`](docs/调用手册.md) —— 全部参数、Python API、结果结构、扩展方式
- [`docs\部署记录.md`](docs/部署记录.md) —— 环境版本、部署过程、实测结果

---

## 后续可扩展方向

当前版本只做"截图 → 文字"。以下方向尚未实现，按需再加：

- **OCR 兜底到模拟器项目**：LeadHunter 的 `docs/emulator.md` §6 记着 OCR 兜底
  待办（当时卡在 `rapidocr-onnxruntime` 会让 exe 增大数百 MB）。
  本项目已给出实测数据：纯 OCR 依赖约 **250 MB**，其中 `opencv-python` 独占
  **113 MB**，而**模型本身只有 32 MB** —— 瓶颈是图像处理库，不是模型。
  详见 `docs\部署记录.md` §5.3。
- **更深的版面分析**：当前只做**列级**分栏判断（能保证列内顺序正确），
  不做表格结构还原、段落层级、公式识别。需要这些时接 OpenOCR 或
  PaddleOCR 的 PP-Structure。
- **语义理解**：需要判断"这段文字属于哪个按钮/字段"时，接本地视觉模型（如 Qwen3-VL）。
- **常驻热键**：做全局快捷键（如 Alt+Q 框选识别），需要额外的 GUI 依赖。
- **打包成 exe**：PyInstaller 打包时注意 `rapidocr` 的模型文件属于包数据，
  静态分析看不到，必须在 spec 里显式声明。
