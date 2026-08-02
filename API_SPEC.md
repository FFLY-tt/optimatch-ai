# OptiMatch AI — API 接口规范

本文档是前后端的契约。后端所有接口按这份文档实现，前端按这份文档对接。
**接口一旦在这里定下来，不能随便改字段名/结构**——如果确实需要改，先改这份文档，
再改代码，不能反过来。

Base URL（本地开发）：`http://127.0.0.1:8000`

---

## 通用约定

- 所有请求/响应 body 格式：`application/json`
- 所有面向用户展示的文本字段（生成内容、错误提示）：英文
- 所有接口如果失败，返回标准错误格式：
```json
{ "detail": "错误信息" }
```
- 状态字段统一用这四种值：`new` / `viewed` / `contacted` / `ignored`

---

## 一、简历相关接口（Tab B）

### 1.1 上传简历
```
POST /api/upload-resume
Content-Type: multipart/form-data
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| file | File (PDF) | 是 | 简历 PDF 文件 |

响应：
```json
{
  "success": true,
  "parsed_sections": ["HEADER", "EDUCATION", "ACADEMIC & RESEARCH PROJECTS"],
  "total_chunks": 7,
  "preview_text": "Fangyu Lin\nMEng in Software Engineering..."
}
```

### 1.2 生成定制简历
```
POST /api/tailor-resume
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| job_description | string | 是 | 职位描述全文 |
| user_notes | string | 否 | 用户补充说明 |
| top_k | int | 否，默认 3 | 检索多少条相关简历片段 |

响应：
```json
{
  "tailored_resume": "...",
  "passed_review": true,
  "issue": "None",
  "attempts": 1,
  "matched_sections": ["ACADEMIC & RESEARCH PROJECTS", "TECHNICAL SKILLS"]
}
```

### 1.3 自动搜索职位
```
POST /api/search-jobs
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| target_role | string | 是 | 目标职位方向 |
| target_region | string | 否，默认 "Canada Remote" | 目标地区 |
| max_results | int | 否，默认 15 | 最多返回多少条 |

响应：
```json
{
  "jobs": [
    {"id": "hn_48835373", "source": "hackernews", "title": "...", "url": "...", "posted_at": "...", "status": "new"}
  ],
  "total": 12
}
```

### 1.4 导出 Word 简历
```
POST /api/export-resume-docx
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| final_content | string | 是 | 用户复审/修改后的最终简历文字 |
| candidate_name | string | 是 | 用于文件命名和文档标题 |

响应：
```json
{ "success": true, "download_url": "/files/Fangyu_Lin_Resume_Tailored.docx" }
```

---

## 二、商机相关接口（Tab A）

### 2.1 提交业务信息
```
POST /api/setup-business-profile
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| business_description | string | 是 | 业务/产品描述，≤300字 |
| target_customer | string | 是 | 目标客户画像，≤150字 |
| website_url | string | 否 | 官网链接，系统自动抓取补充上下文 |

响应：
```json
{ "success": true, "total_chunks": 3 }
```

后端行为：把业务描述（+抓取的官网正文，如果有）切块、向量化，存入 Chroma
的独立 collection `"business_profile"`（和简历的 `"resume"` collection 分开）。

---

### 2.2 推荐线索类别（意图路由，新增）

```
POST /api/suggest-lead-categories
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| business_description | string | 是 | 用于推断相关线索类别 |
| max_categories | int | 否，默认 3 | 推荐几个类别 |

响应：
```json
{
  "categories": [
    {"id": "distributor", "label": "Local Distributors / Wholesalers", "suggested": true},
    {"id": "affiliate_kol", "label": "Affiliate Creators / KOLs", "suggested": true},
    {"id": "ecommerce_seller", "label": "E-commerce Sellers Expanding Product Lines", "suggested": false},
    {"id": "retail_boutique", "label": "Retail Stores / Boutiques", "suggested": false},
    {"id": "competitor_gap", "label": "Competitor Pain Points (Switching Opportunities)", "suggested": true},
    {"id": "media_review", "label": "Media / Bloggers Seeking Products to Feature", "suggested": false}
  ]
}
```

**设计原则**：不直接静默执行搜索。返回全部 6 个类别，AI 推荐的打上 `suggested: true`，
前端用 checkbox 展示、默认勾选推荐项，用户可以自己增减，确认后再调用 2.3 真正执行搜索。
这样保留用户的知情权和调整空间，不是纯黑箱自动化。

**6 类线索定义**：
| 类别 id | 说明 |
|---|---|
| distributor | 本地经销商/批发商，主动找新供应商合作 |
| affiliate_kol | 达人/KOL 自荐"open to collabs"，不是品牌方发的联盟计划页 |
| ecommerce_seller | 已运营的 Amazon/Shopify 卖家，想拓展新品类 |
| retail_boutique | 线下精品店/零售商，想上架新品牌 |
| competitor_gap | 有人抱怨竞品/找替代方案，可主动推荐自己产品 |
| media_review | 博主/媒体主动征集产品做测评/清单文章 |

---

### 2.3 搜索商机（按类别，用户确认后触发）

```
POST /api/search-opportunities
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| categories | list[string] | 是 | 用户确认后的类别列表（来自 2.2 的 checkbox 选择） |
| max_results_per_category | int | 否，默认 5 | 每个类别最多返回多少条 |

响应：
```json
{
  "opportunities": [
    {
      "id": "tavily_a1b2c3",
      "source": "tavily",
      "title": "Planning to open a pet food manufacturing business",
      "url": "https://www.reddit.com/r/Entrepreneur/comments/...",
      "posted_at": "2026-07-09T10:00:00Z",
      "status": "new",
      "category": "ecommerce_seller"
    }
  ],
  "total": 8
}
```

后端行为：对每个用户确认的类别，分别用该类别的行业知识指导 LLM 规划搜索查询、
执行 Tavily 搜索、评估结果是否充分（必要时补搜一轮），多个类别**并发**执行
（线程池，避免前端等待时间随类别数量线性增长）。返回结果带 `category` 字段，
前端可以按类别分组/筛选展示（建议界面上做成 6 个可切换的筛选标签）。

---

### 2.4 生成开发信

```
POST /api/generate-outreach
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| opportunity_content | string | 是 | 目标商机的原文内容（从 2.3 返回结果里选中的一条） |
| user_notes | string | 否 | 用户补充说明 |

响应（结构和简历生成对称，方便前端复用同一套展示逻辑）：
```json
{
  "outreach_message": "...",
  "passed_review": true,
  "issue": "None",
  "attempts": 1,
  "matched_sections": ["BUSINESS_DESCRIPTION"]
}
```

反思标准（和简历那边不同，专门针对开发信场景）：忠实性、切中痛点、自然度。

---

## 三、通用状态管理接口

### 3.1 更新记录状态（求职/商机通用）
```
POST /api/update-status
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| record_id | string | 是 | 对应记录的 id |
| status | string | 是 | "viewed" / "contacted" / "ignored" |

响应：
```json
{ "success": true }
```

---

## 四、系统接口

### 4.1 健康检查
```
GET /api/health
```
响应：
```json
{ "status": "ok" }
```

---

## 五、字段命名规范

- 时间字段统一用 `posted_at`，ISO 8601 格式
- id 字段格式：`{来源}_{原始id或哈希}`，例如 `hn_48835373`、`tavily_a1b2c3`
- 布尔字段用动词过去分词/形容词开头，例如 `passed_review`、`suggested`
- 所有列表类响应，统一包一层 `{"xxx": [...], "total": N}`（`/api/suggest-lead-categories` 例外，
  因为它返回的是固定 6 个类别选项，不是可变数量的搜索结果列表，不需要 `total` 字段）

---

## 六、当前实现状态

| 接口 | 状态 |
|---|---|
| POST /api/tailor-resume | ✅ 已实现 |
| GET /api/health | ✅ 已实现 |
| POST /api/upload-resume | ✅ 已实现 |
| POST /api/search-jobs | ✅ 已实现（底层用 search_agent.py 多角度规划） |
| POST /api/export-resume-docx | ✅ 已实现 |
| POST /api/update-status | ✅ 已实现 |
| POST /api/setup-business-profile | ✅ 已实现 |
| POST /api/suggest-lead-categories | ✅ 已实现（意图路由，返回 checkbox 选项） |
| POST /api/search-opportunities | ✅ 已实现（按类别 + 并发搜索） |
| POST /api/generate-outreach | ✅ 已实现 |