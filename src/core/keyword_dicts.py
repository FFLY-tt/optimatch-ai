"""
简历/JD 结构化解析用的关键词词典（中英双语）。

这是初始版本，覆盖常见表述，跑几批真实简历数据之后大概率需要扩充——
新增关键词直接往对应的 set/dict 里加就行，不用改调用方代码。
"""

import re


# ---------- 学历关键词（按等级从低到高排列，数字越大学历越高） ----------
# 用于：1) 从简历 Region A 抠出候选人最高学历；2) 从 JD 里抠出学历门槛；
#       3) 匹配打分时比较两者高低。
DEGREE_LEVELS: dict[str, int] = {
    # 大专/专科
    "associate degree": 1, "associate's degree": 1, "大专": 1, "专科": 1,
    # 本科/学士
    "bachelor": 2, "bachelor's": 2, "bachelors": 2, "b.s.": 2, "b.s": 2, "bs": 2,
    "b.a.": 2, "b.eng": 2, "beng": 2, "本科": 2, "学士": 2,
    # 硕士
    "master": 3, "master's": 3, "masters": 3, "m.s.": 3, "m.s": 3, "ms": 3,
    "msc": 3, "m.sc": 3, "meng": 3, "m.eng": 3, "mba": 3, "硕士": 3, "研究生": 3,
    # 博士
    "phd": 4, "ph.d": 4, "ph.d.": 4, "doctorate": 4, "doctoral": 4, "博士": 4,
}

# 按关键词长度从长到短排序，避免 "master" 先于 "master's" 被匹配截断
# 学历关键词必须按整词/短语边界匹配，不能用裸子串 `in` 判断——
# 实测坐实过一次："ms"（硕士）当子串会命中 "syste**ms**" 里的 "ms"。
# 边界用非字母数字字符判断（关键词本身可能含 "." "'" 这类符号，
# 所以只在两端各判断一次"不是字母/数字"，而不是用 \b，\b 对 "b.s." 这种
# 带标点的关键词不好用）。
_DEGREE_PATTERNS = [
    (re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])"), level)
    for kw, level in sorted(DEGREE_LEVELS.items(), key=lambda kv: len(kv[0]), reverse=True)
]


def extract_degree_level(text: str) -> int | None:
    """
    从一段文本里抠出提到的最高学历等级（数字越大越高），没提到返回 None。
    """
    low = text.lower()
    best = None
    for pattern, level in _DEGREE_PATTERNS:
        if pattern.search(low):
            if best is None or level > best:
                best = level
    return best


# ---------- 技术栈关键词（不分等级，命中即算，用于 Region A 关键词集合 + JD 技术栈重合度打分） ----------
TECH_STACK_KEYWORDS: set[str] = {
    # 编程语言
    "python", "java", "javascript", "typescript", "c++", "c#", "golang", "go",
    "rust", "ruby", "php", "swift", "kotlin", "scala", "r", "sql", "matlab",
    # 前端框架
    "react", "vue", "angular", "next.js", "nuxt", "svelte",
    # 后端框架
    "django", "flask", "fastapi", "spring", "spring boot", "express", "node.js",
    ".net", "asp.net", "rails", "laravel",
    # 云平台
    "aws", "azure", "gcp", "google cloud", "阿里云", "腾讯云", "华为云", "alibaba cloud",
    # 数据库
    "mysql", "postgresql", "mongodb", "redis", "elasticsearch", "oracle",
    "sqlite", "dynamodb", "cassandra",
    # 数据/AI
    "pytorch", "tensorflow", "scikit-learn", "sklearn", "pandas", "numpy",
    "spark", "hadoop", "langchain", "langgraph", "llm", "nlp", "cv",
    "machine learning", "deep learning", "机器学习", "深度学习",
    # 大数据 / 流处理（实测一份真实大数据工程师简历后补充：
    # flink/kafka 这类流处理组件、snowflake/dbt/airflow 这类现代数据栈、
    # clickhouse 这类 OLAP 数据库，之前的词典完全没覆盖）
    "flink", "kafka", "snowflake", "dbt", "airflow", "clickhouse",
    "data lakehouse", "etl", "elt", "olap",
    # DevOps / 基础设施
    "docker", "kubernetes", "k8s", "ci/cd", "jenkins", "terraform", "ansible",
    "git", "linux", "shell",
    # 后端框架（同上，spring cloud 之前漏了）
    "spring cloud",
}


def extract_tech_stack(text: str) -> set[str]:
    """从一段文本里抠出命中的技术栈关键词集合（大小写不敏感，整词/短语边界匹配）。"""
    low = text.lower()
    hits = set()
    for kw in TECH_STACK_KEYWORDS:
        pattern = r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])"
        if re.search(pattern, low):
            hits.add(kw)
    return hits


# ---------- 简历板块标题 → 区域归类（中英双语） ----------
# Region A：个人信息类（结构化字段，不做向量化）
# Region B：工作经验类（句子级切分 + tags 绑定，做向量化）
SECTION_TO_REGION: dict[str, str] = {
    # Region A
    "summary": "A", "objective": "A", "profile": "A", "about": "A",
    "个人简介": "A", "自我评价": "A", "求职意向": "A", "个人信息": "A",
    "education": "A", "教育经历": "A", "教育背景": "A",
    "skills": "A", "technical skills": "A", "core competencies": "A",
    "技能": "A", "专业技能": "A", "技术栈": "A",
    "certifications": "A", "awards": "A", "publications": "A",
    "证书": "A", "获奖": "A", "荣誉": "A", "论文": "A",
    "contact": "A", "header": "A",
    # Region B
    "experience": "B", "work experience": "B", "professional experience": "B",
    "employment history": "B", "工作经历": "B", "工作经验": "B",
    "projects": "B", "academic & research projects": "B", "project experience": "B",
    # 裸的 "project"（不带 s/experience）覆盖 "personal project"/"key project" 这类
    # 单数写法——实测坐实过一次真实 bug：某份简历板块标题就写的 "PERSONAL PROJECT"
    # （单数），之前的关键词表只有复数/带 experience 的变体，匹配不上，整段内容被
    # classify_section 兜底成 Region A，一条 chunk 都切不出来。
    "project": "B",
    "项目经验": "B", "项目经历": "B", "项目": "B",
    "internship": "B", "internships": "B", "实习经历": "B", "实习经验": "B",
}


def classify_section(header_text: str) -> str:
    """
    给一个板块标题文本（可能带 Markdown # 符号、大小写不一）判断归属区域。
    找不到匹配的关键词，默认归到 Region A（结构化字段兜底，不强行拆句子）。
    """
    normalized = header_text.strip().lstrip("#").strip().lower()
    for keyword, region in SECTION_TO_REGION.items():
        if keyword in normalized:
            return region
    return "A"


# ---------- 工作年限提取（用于 JD 门槛 + 未来简历总经验年限计算） ----------
_YEARS_PATTERN = re.compile(
    r"(\d+)\s*\+?\s*(?:-\s*(\d+)\s*)?\+?\s*(?:years?|yrs?|年)",
    re.IGNORECASE,
)


def extract_years_required(text: str) -> int | None:
    """
    从 JD 文本里抠出最低经验年限要求（"3+ years"/"3-5 years"/"3年以上经验" 这种）。
    取匹配到的第一个数字（范围表达式取下限），找不到返回 None。
    """
    match = _YEARS_PATTERN.search(text)
    if not match:
        return None
    return int(match.group(1))
