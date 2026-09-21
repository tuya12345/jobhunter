# 样本目录

识别测试用的图片都在这里，分三类：

| 位置 | 是什么 | 怎么生成 |
|---|---|---|
| `demo.png` | 单张测试图（900×347，中英数字混排 6 行） | `tools\make_sample.py` |
| `scenes\` | 10 个场景样本 + 标准答案 `labels.json` | `tools\make_scenes.py` |
| `real\` | 真实截图回归样本（**目前为空**） | 你自己放，见 `real\README.md` |

---

## 为什么合成样本和真实样本都要有

**合成样本**（`demo.png` / `scenes\`）的好处是**内容完全一致、可复现**，
适合做对照实验 —— 改一个参数跑一遍，差异一定来自这个参数。

但它们的**覆盖面有限**：字体渲染单一、没有真实界面元素干扰。
实测教训：只靠合成样本，漏掉了两个真 bug ——

- **双栏阅读顺序**：多栏内容被串成一行（只测单栏样本发现不了）；
- **分栏过度切分**：修好上一个之后又切碎了，4 张真实图全错。

**两个 bug 都是拿真实截图测才暴露的。** 所以两类样本都要有。

---

## 合成样本

```cmd
rem 生成单张测试图 -> samples\demo.png
.venv\Scripts\python.exe tools\make_sample.py

rem 生成 10 个场景 + 标准答案 -> samples\scenes\
.venv\Scripts\python.exe tools\make_scenes.py
```

`scenes\` 里 10 个场景的**文字内容完全相同**（同一段 5 行文本），只改渲染方式：

| 文件 | 模拟的情况 |
|---|---|
| `light.png` | 白底黑字（基准） |
| `dark.png` | 深色主题（IDE / 终端） |
| `dark_code.png` | 深色 + 语法高亮（多色文字） |
| `small.png` | 小字号（13px） |
| `blue_bg.png` | 彩色背景 + 白字 |
| `low_contrast.png` | 浅灰字 + 白底 |
| `blur.png` | 高斯模糊 |
| `jpeg.png` | JPEG 压缩伪影（quality=22） |
| `noisy_bg.png` | 噪点背景 |
| `two_column.png` | **左右双栏**（回归重点） |

`labels.json` 是每个场景的标准答案，`tools\benchmark.py` 用它算字符错误率（CER）。

> `labels.json` 里 `two_column.png` 的答案按**左栏三行 + 右栏三行**写，
> 这是"正确的阅读顺序"。如果识别结果按行把左右栏拼在一起，CER 会立刻飙升 ——
> 这就是它作为回归样本的价值。

---

## 真实样本（`real\`）

放你自己的真实截图，用于回归测试。详细说明见 [`real\README.md`](real/README.md)。

⚠️ **注意隐私**：真实截图里可能有个人信息（聊天记录、账号、岗位信息）。
`.gitignore` 已经排除 `samples/real/*`（只保留 `README.md`），
但**你自己也要留意别把这些图传到别处**。

---

## 基准结果在哪

在 `results\benchmark.json`，不在这个目录里。

样本（输入）和结果（输出）分开，是为了让 `samples\` 保持"只读素材"的性质 ——
删掉 `results\` 不影响复现。
