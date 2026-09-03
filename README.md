# optimatch-ai
## 自动投递（实验性功能）

在"生成定制简历"之后新加的一步：自动打开职位申请页、填表单，停在提交前一步，
等你在终端/前端里确认了才真正点提交。目前支持 LinkedIn Easy Apply、Indeed
一键申请，以及通用 ATS 表单（Greenhouse/Lever/Workday 等公司官网投递页）兜底。

### ⚠️ 使用前必读

- **LinkedIn 用户协议明确禁止自动化操作账号**，这个功能有触发账号风控/封号的
  风险。用不用、投几个、多大频率投，自己权衡；出问题跟这份代码没关系。
- 默认设计成"自动填表，最后一步人工确认才提交"，不是全自动无人值守投递——
  这是刻意的安全边界，别改成自动点提交。
- 投递前务必自己看一眼截图/浏览器窗口，确认信息填得没问题（尤其是薪资/工作
  授权这类字段——这些不会用 AI 瞎编，但规则匹配也可能出错）。

### 首次配置

1. `pip install -r requirements.txt`（会装 playwright）
2. `python -m playwright install chromium`（装一次浏览器内核）
3. 复制 `data/applicant_profile.example.json` 成 `data/applicant_profile.json`，
   填成你自己的姓名/邮箱/电话/简历路径等信息（这个文件已在 .gitignore 里，
   不会被提交）。
4. 手动跑一次验证脚本：
   ```
   python -m scripts.test_apply_dry_run "<职位链接>"
   ```
   第一次跑会弹出一个浏览器窗口。如果是 LinkedIn/Indeed 且停在登录页，
   手动登录一次即可，登录态存在 `data/apply_browser_profile/`，以后不用重复登录。

### API

- `POST /api/apply/start` — 打开职位页、自动填表，返回填写报告 + 截图，不提交。
- `GET /api/apply/screenshot/{session_id}` — 拿填表后的截图。
- `POST /api/apply/confirm` — 用户确认后真正点提交，同时把这条职位的状态标成 `applied`。
- `POST /api/apply/cancel` — 放弃这次投递，不提交任何内容。
