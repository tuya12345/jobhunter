# 工具脚本

| 脚本 | 干什么 | 什么时候用 |
|---|---|---|
| `selfcheck.py` | 自检：依赖 / 引擎 / 识别 / 坐标 / 输出 / **分栏** | **改完代码第一件事** |
| `benchmark.py` | 多场景准确率基准（算 CER） | 改过预处理或分栏逻辑后，确认没退化 |
| `make_sample.py` | 生成单张测试图 → `samples\demo.png` | 想快速验证环境是否正常 |
| `make_scenes.py` | 生成 10 个场景 + 标准答案 → `samples\scenes\` | 想跑准确率基准 |
| `put_image_to_clipboard.py` | 把图片放进剪贴板（位图格式） | 测 `--clipboard` 的位图分支 |

---

## 常用组合

```cmd
cd /d D:\P7code\shotocr

rem 改完代码，先跑这个（约 15 秒）
.venv\Scripts\python.exe tools\selfcheck.py

rem 改了预处理 / 分栏逻辑，再跑基准（约 1 分钟）
.venv\Scripts\python.exe tools\benchmark.py --engines rapid

rem 想连 paddle 一起测（慢很多，每张约 6 秒）
.venv\Scripts\python.exe tools\benchmark.py --engines rapid,paddle --presets none

rem 跑你自己放的真实截图回归样本
.venv\Scripts\python.exe tools\benchmark.py --scenes real
```

---

## 两个生成脚本的区别

- **`make_sample.py`**：一张图，内容是中英数字混排的 6 行，用来验证"能不能识别"；
- **`make_scenes.py`**：10 张图，**文字内容完全相同、只改渲染方式**，
  用来量化"哪种情况会掉链子"。

做对照实验要用后者 —— **内容一致才能确定差异来自渲染方式**，
而不是来自文字本身。

---

## `benchmark.py` 的场景集

| `--scenes` | 目录 | 说明 |
|---|---|---|
| `synthetic`（默认） | `samples\scenes\` | 10 个合成场景 |
| `real` | `samples\real\` | 你自己的真实截图，见 `samples\real\README.md` |
| `both` | 两个都跑 | 场景名会带上目录前缀区分 |

也可以直接给一个目录路径。结果写到 `results\benchmark.json`。

**没有 `labels.json` 的目录会被跳过**，并明确打印原因；
一个可用场景集都没有时退出码为 **2**（不会静默返回成功）。

---

## `selfcheck.py` 覆盖什么

14 项：依赖导入 / 引擎可用性 / 测试图 / 识别结果 / 内容断言 /
坐标合理性 / 三种输出格式 / **分栏阅读顺序**。

全部通过时退出码 **0**，可以直接用在脚本里判断。

> 分栏那两项是回归守卫，对应"多栏内容被串成一行"这个 bug ——
> 它曾经在真实截图上失效过（详见 `docs\部署记录.md` §4.7）。
> 这两项做过变异测试：把分栏逻辑改坏后，自检会变成 13/14、退出码 1。

---

## 加新脚本时的约定

1. **路径用 `ROOT = Path(__file__).resolve().parent.parent` 推导**，
   不要写死 `D:\P7code\...`（换机器就废）；
2. 生成样本的脚本，输出到 `samples\`；跑测试的脚本，结果写到 `results\`；
3. 脚本要能**单独跑通**，不要依赖"先跑另一个脚本"（缺输入时明确报错并给提示）；
4. 判断成功用**退出码**，不要只靠打印（脚本会被别的脚本调用）。
