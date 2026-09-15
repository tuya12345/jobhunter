"""config：配置层。

  settings.py   pydantic-settings：config.yaml + .env + 环境变量(JH_*) 三层合并 + 校验  [P0]
  paths.py      统一的目录解析（data / output / state），方便测试注入临时目录      [P0]

优先级（后者覆盖前者）：代码默认值 → config.yaml → .env → 环境变量

分工原则：
  · config.yaml  = **个人求职偏好**（关键词/城市/分数线/权重/禁用词）——不含隐私，可提交；
  · .env         = **密钥**（DEEPSEEK_API_KEY…）——永不提交；
  · pydantic 校验 = 把"配置写错"变成**启动时就报错**，而不是跑到一半出现莫名其妙的分数。
    例：三个维度权重加起来必须等于 100，否则 rule_score 会整体偏移，
    这种 bug 不校验的话极难发现。
"""
