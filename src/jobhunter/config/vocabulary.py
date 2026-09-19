"""内置技能词表（纯数据）。

★ 为什么它放在 config 层而不是 services/resume_svc.py（ISSUES #43）：

  配置校验要用到这份词表 —— `learning_whitelist` 里写一个词表里没有的词，
  说明**写错了**（多半是拼写错误），而错的那份白名单永远不会命中任何东西，
  用户只会觉得「这个功能没效果」。

  校验必须发生在 settings 加载时（和其它配置校验同一时机），于是出现方向问题：
  services 依赖 config，config 反过来 import services 就成环。
  它本来就是一张**静态数据表**（不依赖任何运行时状态），放进 config 是正确方向。

★ 词条是**技能本名**（Python / Vue），中文同义词放 config.yaml 的 `skill_aliases`。
  `resume_svc._BUILTIN_SKILLS` 保留同名别名，历史引用（含文档）不受影响。
"""

from __future__ import annotations

__all__ = ["BUILTIN_SKILLS"]


#: 内置技能词表。
#:
#: ★ 为什么要有内置词表，而不是只靠 config.yaml 的 skill_aliases：
#:   config 里的别名表是"归一化"用的（把 大模型/LLM 归到一个名字），
#:   用户不可能把所有技能都列进去。没有内置词表，抽取就会漏掉一大半技能，
#:   `jh gap` 和 `jh resume split` 都会失真。
#:
#: ★ 名字用**技能本名**（Python / Vue），中文写法的同义词放在 config 的别名表里。
#:   这样展示出来的技能名是国际通用的写法，看简历的 HR/面试官一眼能认。
BUILTIN_SKILLS: tuple[str, ...] = (
    # 语言
    "Python",
    "Java",
    "JavaScript",
    "TypeScript",
    "C++",
    "C#",
    "Go",
    "Rust",
    "PHP",
    "Ruby",
    "Kotlin",
    "Swift",
    "Dart",
    "SQL",
    "Shell",
    "Bash",
    # 前端
    "HTML",
    "CSS",
    "Sass",
    "Less",
    "Vue",
    "React",
    "Angular",
    "Svelte",
    "jQuery",
    "Webpack",
    "Vite",
    "Rollup",
    "Babel",
    "Element Plus",
    "Ant Design",
    "Tailwind",
    "ECharts",
    "Axios",
    "Pinia",
    "Vuex",
    "小程序",
    "UniApp",
    "Flutter",
    "响应式布局",
    "组件库",
    "前端工程化",
    "SSR",
    "Node.js",
    "Nginx",
    # 后端 / 数据库
    "FastAPI",
    "Django",
    "Flask",
    "Tornado",
    "Spring",
    "SpringBoot",
    "MyBatis",
    "MySQL",
    "PostgreSQL",
    "SQLite",
    "MongoDB",
    "Redis",
    "Elasticsearch",
    "消息队列",
    "RabbitMQ",
    "Kafka",
    "Celery",
    "gRPC",
    "RESTful",
    "GraphQL",
    "HTTP",
    "微服务",
    "并发",
    "多线程",
    "异步",
    "性能优化",
    "单元测试",
    "接口文档",
    # 数据 / 运维
    "Docker",
    "Kubernetes",
    "Jenkins",
    "Git",
    "GitHub",
    "GitLab",
    "Linux",
    "CI/CD",
    "Prometheus",
    "Grafana",
    "阿里云",
    "腾讯云",
    "AWS",
    "部署",
    # 大模型 / AI
    "LLM",
    "大模型",
    "RAG",
    "Prompt",
    "Agent",
    "LangChain",
    "LlamaIndex",
    "向量数据库",
    "Milvus",
    "FAISS",
    "Chroma",
    "Pinecone",
    "Embedding",
    "微调",
    "LoRA",
    "PEFT",
    "Transformer",
    "Attention",
    "PyTorch",
    "TensorFlow",
    "OpenAI",
    "DeepSeek",
    "Qwen",
    "Dify",
    "Coze",
    "MCP",
    "Function Calling",
    "多智能体",
    "工作流编排",
    "流式输出",
    "模型评测",
    "数据清洗",
    "爬虫",
    "Pandas",
    "NumPy",
    "Matplotlib",
    "Jupyter",
    "机器学习",
    "深度学习",
    "NLP",
    "CV",
    "OpenCV",
    "YOLO",
    "推荐系统",
    "知识图谱",
    "OCR",
    "语音识别",
    # 工程素养
    "数据结构",
    "算法",
    "设计模式",
    "代码评审",
    "需求分析",
    "项目管理",
    "敏捷开发",
)
