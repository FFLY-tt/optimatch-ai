"""
求职者个人信息配置。
自动投递要往表单里填姓名/邮箱/电话/工作授权这些字段，这些信息不应该
硬编码在代码里，也不该提交到 git（属于 PII）——统一放一个本地 JSON，
参考 applicant_profile.example.json 的格式自己复制一份改名成
applicant_profile.json（已在 .gitignore 里忽略）。
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
PROFILE_PATH = os.path.join(DATA_DIR, "applicant_profile.json")
EXAMPLE_PATH = os.path.join(DATA_DIR, "applicant_profile.example.json")


class ProfileNotConfigured(Exception):
    pass


@dataclass
class ApplicantProfile:
    first_name: str
    last_name: str
    email: str
    phone: str
    location: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    github_url: str = ""
    resume_path: str = ""  # 默认简历文件的绝对路径，投递时可以按职位单独覆盖
    years_experience: Optional[int] = None
    desired_salary: str = ""
    work_authorization: str = "authorized"  # authorized / needs_sponsorship / other
    willing_to_relocate: bool = False
    willing_to_remote: bool = True
    willing_to_travel: bool = False
    cover_letter_template: str = ""  # 可以用 {company} / {role} 占位符
    # 申请表常见但之前没覆盖的字段
    current_company: str = ""       # 当前/最近一份工作的公司名
    notice_period: str = ""         # 离职通知期 / 最快到岗时间，比如 "2 weeks" / "Immediately"
    highest_education: str = ""     # 最高学历，比如 "Master's Degree" / "Bachelor's Degree"
    school: str = ""                # 毕业院校名
    # EEO/人口统计类问题在美国属于自愿披露，默认统一给"不透露"这种安全答案，
    # 用户如果想真实填写，自己改这几个字段。
    eeo_gender: str = "Decline to answer"
    eeo_race: str = "Decline to answer"
    eeo_veteran: str = "I am not a protected veteran"
    eeo_disability: str = "I do not want to answer"
    # 高频出现但没法用规则覆盖的问题，用户自己攒的问答对照表，
    # key 是问题的关键词（小写、去标点），value 是答案。命中优先级高于 LLM 兜底生成。
    extra_answers: dict = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def load_profile() -> ApplicantProfile:
    if not os.path.exists(PROFILE_PATH):
        raise ProfileNotConfigured(
            f"没有找到 {PROFILE_PATH}。"
            f"先复制一份 data/applicant_profile.example.json 改名成 "
            f"data/applicant_profile.json，把里面的姓名/邮箱/电话等信息填成你自己的，再重试。"
        )
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    known_fields = {f.name for f in ApplicantProfile.__dataclass_fields__.values()}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    profile = ApplicantProfile(**filtered)

    missing = [name for name in ("first_name", "last_name", "email", "phone") if not getattr(profile, name)]
    if missing:
        raise ProfileNotConfigured(
            f"data/applicant_profile.json 里这几个必填字段还是空的: {missing}，先填好再重试。"
        )
    return profile
