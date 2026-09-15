"""JobHunter 测试包。

分层约定（pyproject 里注册的 markers，CI 默认排除后两者）：

    默认（无 marker）   确定性测试：不联网、不需要模拟器。CI 只跑这些。
    @pytest.mark.live       需要真实网络 / 真实大模型调用 → 只在 nightly 跑。
    @pytest.mark.emulator   需要安卓模拟器 + uiautomator2 → P7 手动跑。

★ 隔离铁律：任何测试都不许碰真实的 data/jobhunter.db。
  conftest.py 里对这一条有硬性断言（解到真实库路径就直接 fail），
  因为"测试把真实投递记录洗掉"是这类本地工具最不可接受的意外。
"""
