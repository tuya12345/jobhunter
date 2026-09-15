"""单元测试：全部确定性、不联网、毫秒级。

P0 的 5 个文件，每个都在守一个"踩过坑才会提前做"的底座决策：

    test_smoke.py             建库/建表/迁移可逆/init 幂等/外键真的生效
    test_logging_redaction.py 日志不泄露手机号、邮箱、简历原文
    test_io_encoding.py       强制 UTF-8（Windows cp936 的坑）
    test_time_utc.py          UTC aware 存 / Asia/Shanghai 展示
    test_cli_skeleton.py      19 个命令形状正确、占位命令不假装成功
"""
