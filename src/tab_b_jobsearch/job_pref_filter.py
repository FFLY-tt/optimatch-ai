"""
职位"地点 / 签证要求"是否符合求职偏好 —— 结果侧校验。

现状（改这个文件之前）：搜索链路只在拼查询关键词时带了地点词（"Canada"/"Remote"），
指望搜索引擎自己给对结果，结果侧从来没有人再校验一遍。于是经常混进：
- 地点在别的国家、又不支持远程的岗位
- 正文明确写 "no visa sponsorship" / "must be authorized to work in the US" 之类，
  而用户需要签证担保的岗位

这个模块做"基本规则判断"（不上 LLM，够用就行）：
- 岗位有明确地点、且跟偏好地区都不沾边、又不是 remote 的 -> 排除
- 正文出现明确排斥性签证表述、且用户需要担保的 -> 排除
判不准（拿不到地点、没有排斥性表述）就放行，宁可漏一条也不误杀。
"""

import re
from dataclasses import dataclass, field


@dataclass
class SearchPrefs:
    allowed_regions: list[str] = field(default_factory=list)   # 小写地区词，如 ["canada"]
    open_to_remote: bool = True
    needs_sponsorship: bool = False

    @property
    def configured(self) -> bool:
        """有没有可用来做地点判断的偏好——两者都没有就别做地点过滤。"""
        return bool(self.allowed_regions) or self.needs_sponsorship


# 地区别名 / 常见城市省州展开——job location 写 "Toronto, ON" 也能对上偏好 "canada"。
_REGION_EXPANSIONS: dict[str, set[str]] = {
    "canada": {
        "canada", "canadian", "toronto", "ottawa", "waterloo", "kitchener", "mississauga",
        "hamilton", "london, on", "vancouver", "victoria", "burnaby", "british columbia",
        "montreal", "montréal", "quebec", "québec", "calgary", "edmonton", "alberta",
        "winnipeg", "manitoba", "halifax", "nova scotia", "saskatoon", "regina",
    },
    "united states": {
        "united states", "u.s.a", "u.s.", "usa", "america", "new york", "brooklyn",
        "san francisco", "bay area", "seattle", "austin", "boston", "chicago", "denver",
        "atlanta", "los angeles", "san diego", "portland", "miami", "washington, dc",
    },
    "united kingdom": {"united kingdom", "england", "scotland", "wales", "london", "manchester",
                       "edinburgh", "bristol", "cambridge", "u.k."},
    "european union": {"european union", "eu", "germany", "berlin", "munich", "france", "paris",
                       "netherlands", "amsterdam", "spain", "portugal", "ireland", "dublin", "poland"},
}
_REGION_ALIASES = {
    "us": "united states", "u.s.": "united states", "usa": "united states", "america": "united states",
    "uk": "united kingdom", "eu": "european union", "europe": "european union",
}

_REMOTE_TOKEN_RE = re.compile(r"\b(fully[\s-]*remote|100%\s*remote|remote[\s-]*(first|friendly|ok|anywhere)?|"
                              r"work[\s-]*from[\s-]*home|distributed team)\b", re.IGNORECASE)
_ONSITE_ONLY_RE = re.compile(
    r"\b(no\s+remote|not?\s+remote|on[\s-]*site\s+only|in[\s-]*office\s+(only|required)|"
    r"must\s+be\s+(on[\s-]*site|in\s+the\s+office)|relocation\s+required)\b", re.IGNORECASE)

# 明确排斥性签证表述
_VISA_EXCLUDE_RE = re.compile(
    # <否定/no> + 最多 3 个词 + sponsor —— 覆盖 "no sponsorship" / "does not offer visa
    # sponsorship" / "not able to provide visa sponsorship" / "unable to sponsor" 等
    r"\b(?:no|not\s+able\s+to|unable\s+to|cannot|can'?t|does\s+not|do\s+not|will\s+not|won'?t|"
    r"not\s+in\s+a\s+position\s+to|are\s+not\s+offering)\s+(?:[a-z]+\s+){0,5}sponsor"
    r"|\bsponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided|possible|an\s+option)\b"
    r"|\bwithout\s+(?:visa\s+|the\s+need\s+for\s+)?sponsorship\b"
    r"|\b(?:visa\s+)?sponsorship\s+is\s+not\s+available\b"
    r"|\bw2\s+only\b|\bno\s+c2c\b"
    r"|\b(?:u\.?s\.?\s+)?citizens?\s+only\b|\bmust\s+be\s+a\s+(?:u\.?s\.?|us|american)\s+citizen\b"
    r"|\bsecurity\s+clearance\s+(?:is\s+)?required\b",
    re.IGNORECASE,
)
# "must be authorized / have the right to work in ..." —— 对"需要签证担保"的人来说，
# 不管后面写的是哪个国家都算排斥（"already authorized" 本身就意味着"不给你办"）。
_MUST_BE_AUTHORIZED_RE = re.compile(
    r"(?:must\s+(?:already\s+)?(?:be|have)|require[sd]?|need\s+to\s+(?:be|have)|"
    r"you\s+(?:must|should)\s+(?:be|have)|candidates?\s+must\s+(?:be|have))"
    r"[^.\n]{0,40}?"
    r"(?:legally\s+)?(?:authoriz\w+|authoris\w+|right|eligibilit\w+|eligible|permit(?:ted)?|permission)"
    r"\s+to\s+work",
    re.IGNORECASE,
)

_LOCATION_LINE_RE = re.compile(
    r"(?:^|\n)\s*(?:job\s+)?location[s]?\s*[:：]\s*([^\n|•]{2,80})", re.IGNORECASE)
_BASED_IN_RE = re.compile(r"\bbased\s+in\s+([A-Za-z .,'-]{2,40})", re.IGNORECASE)


def build_prefs(
    target_region: str = "",
    needs_sponsorship: bool | None = None,
    profile_location: str = "",
    profile_work_authorization: str = "",
    profile_willing_to_remote: bool | None = None,
) -> SearchPrefs:
    """
    把"搜索请求里的偏好 + applicant_profile.json 里的偏好"合并成一份 SearchPrefs。
    请求里显式传的优先；没传的用 profile 里的补。
    """
    regions: list[str] = []
    open_to_remote = False

    for chunk in (target_region or "").replace("/", ",").replace("|", ",").split(","):
        tok = chunk.strip().lower()
        if not tok:
            continue
        if "remote" in tok:
            open_to_remote = True
            tok = tok.replace("remote", "").strip()
        if tok:
            regions.append(_REGION_ALIASES.get(tok, tok))

    if profile_location:
        # "Toronto, ON, Canada" -> 末段国家
        parts = [p.strip().lower() for p in profile_location.split(",") if p.strip()]
        if parts:
            regions.append(_REGION_ALIASES.get(parts[-1], parts[-1]))

    if profile_willing_to_remote is not None:
        open_to_remote = open_to_remote or profile_willing_to_remote

    if needs_sponsorship is None:
        needs_sponsorship = (profile_work_authorization or "").lower() in ("needs_sponsorship", "sponsorship", "other")

    # 去重、保序
    seen = set()
    regions = [r for r in regions if r and not (r in seen or seen.add(r))]
    return SearchPrefs(allowed_regions=regions, open_to_remote=open_to_remote, needs_sponsorship=bool(needs_sponsorship))


def _accept_substrings(regions: list[str]) -> set[str]:
    out: set[str] = set()
    for r in regions:
        r = _REGION_ALIASES.get(r, r)
        out.add(r)
        out |= _REGION_EXPANSIONS.get(r, set())
    return out


def _region_ok(text: str, accept: set[str]) -> bool:
    low = text.lower()
    return any(a in low for a in accept)


def _extract_job_location(title: str, content: str, explicit_location: str) -> str:
    if explicit_location and explicit_location.strip():
        return explicit_location.strip()
    head = (content or "")[:800]
    m = _LOCATION_LINE_RE.search(head) or _LOCATION_LINE_RE.search(title or "")
    if m:
        return m.group(1).strip()
    m = _BASED_IN_RE.search(head)
    if m:
        return m.group(1).strip()
    return ""


def check_job(
    title: str,
    content: str,
    explicit_location: str = "",
    remote_flag: bool | None = None,
    prefs: SearchPrefs | None = None,
) -> tuple[bool, str]:
    """
    返回 (是否保留, 若剔除给出原因)。判不准一律保留。
    """
    if prefs is None or not prefs.configured:
        return True, ""

    text = f"{title}\n{content}"
    low = text.lower()
    is_remote = bool(remote_flag) or bool(_REMOTE_TOKEN_RE.search(low))

    # ---- 签证 ----
    if prefs.needs_sponsorship:
        if _VISA_EXCLUDE_RE.search(low) or _MUST_BE_AUTHORIZED_RE.search(text):
            return False, "正文明确写了不提供签证担保 / 要求已有当地工作许可，而你需要签证担保"

    # ---- 地点 ----
    if prefs.allowed_regions:
        job_loc = _extract_job_location(title, content, explicit_location)
        if job_loc:
            loc_low = job_loc.lower()
            if "remote" in loc_low or "anywhere" in loc_low:
                is_remote = True
            accept = _accept_substrings(prefs.allowed_regions)
            if not is_remote and not _region_ok(job_loc, accept):
                return False, f"地点「{job_loc}」不在你的偏好地区（{', '.join(prefs.allowed_regions)}），也不是远程岗位"

        # 偏好本身就是"只要远程"（没给具体地区），但岗位明确不支持远程
        if prefs.open_to_remote and not prefs.allowed_regions and _ONSITE_ONLY_RE.search(low) and not is_remote:
            return False, "岗位明确要求坐班 / 不支持远程"

    return True, ""
