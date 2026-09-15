"""utils：无业务依赖的通用工具层。

为什么单独一层：这些工具（编码、时间、脱敏）的正确性是**全项目的地基**，
但它们不该知道任何业务概念。放进 utils 之后：
  · 任何层都能引用（cli / services / storage 都用得上）；
  · 测试可以单独、快速地验证它们（见 tests/unit/）；
  · 关键约定（如"必须显式 UTF-8"）只有一处实现，不会各处漏改。

P0 阶段包含：
    io.py      强制 UTF-8 的文件/JSON 读写（Windows 默认 cp936 的坑）
    time.py    统一 UTC aware 存储 + Asia/Shanghai 展示
    logging.py 日志脱敏 Filter（手机号/邮箱打码，绝不打印简历原文）
"""
