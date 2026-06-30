# Labour OS MVP

一个使用 Flask + SQLite 构建的本地劳务全生命周期操作系统。默认首页为 **My Work Queue（我的待办队列）**，集中处理最紧急的业务事项。

## 个人操作模式

- One-line Query：输入姓名、合同号、名额号或事件关键词，直接返回当前状态、下一步建议及是否需要处理。
- 单一待办队列：合同、证件、名额、风险和入境任务统一按紧急程度排序。
- 自动提醒：页面通过 SSE 接收服务端推送，无需进入风险页或任务页手动查看。
- 快捷执行：可在队列中直接创建续约任务、替补流程或提醒任务。

## 生产启动

macOS / Linux 一键启动：

```bash
cd labor-management-mvp
./start-production.sh
```

`start-production.sh` 是唯一允许的服务启动入口。禁止直接执行 `app.py`，禁止使用 Flask 开发服务器。

浏览器打开 <http://127.0.0.1:5001>。数据库 `labor.db` 会在首次运行时自动创建。首次登录账号为 `admin` / `admin123`，上线前请通过环境变量修改密码。

页面必须通过上述 HTTP 地址访问；不要使用 `file://` 或直接双击 `templates/index.html`。

健康检查：<http://127.0.0.1:5001/health>

## 自动化验收

```bash
./test.sh
```

测试从空数据库开始，完整覆盖人员、名额、合同、审批流程、资料上传与查询、生命周期、风险和入境任务。

## 项目结构

```text
labor-management-mvp/
├── app.py                 # Gunicorn app:app 对象；禁止直接运行
├── app/
│   ├── __init__.py        # Application Factory
│   ├── routes/            # 登录与导入控制器（无 SQL）
│   ├── models/            # SQLite 连接与 Repository
│   ├── services/          # 登录、Excel 导入等业务逻辑
│   └── utils/             # 权限与统一响应
├── config/                # 开发/生产配置
├── requirements.txt      # Python 依赖
├── start-production.sh    # 唯一生产启动入口
├── test.sh                # 一键验收
├── README.md              # 使用说明
├── labor.db               # 首次运行后自动生成
├── logs/                  # Gunicorn、应用日志和 PID
├── design/
│   └── concept.png        # 页面设计概念图
├── static/
│   ├── app.js             # 弹窗与视图切换交互
│   └── style.css          # 响应式样式
├── templates/             # Jinja2 页面（含登录页）
└── tests/
    └── test_mvp.py        # 完整业务闭环测试
```

## 生产部署

```bash
cd /Users/fanzuijikeai/Documents/1project/labor-management-mvp
export LABOUR_OS_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export LABOUR_OS_ADMIN_USERNAME="admin"
export LABOUR_OS_ADMIN_PASSWORD="请替换为强密码"
./start-production.sh
```

创建只读操作员或管理员：

```bash
flask --app app create-user --username operator --role user
flask --app app create-user --username manager --role admin
```

生产导入接口均需管理员登录：`POST /imports/person`、`POST /imports/quota`、`POST /imports/contract`。上传字段为 `file`，可选 `mapping` JSON；模板入口为 `GET /imports/template/person|quota|contract`，导入日志为 `GET /imports/logs`。

## 功能

- Personal Operator Mode：默认首页为 My Work Queue，全部信息改为操作视角
- Priority Ranking：风险权重（高300/中200/低100）叠加90天窗口内的时间紧急度，结果实时排序
- 合同建议：90天内到期合同自动建议“续约 / 替补”，可创建 Contract Renewal Task
- 证件建议：有明确到期日的证件风险自动建议启动更新，并可创建 Reminder Task
- 名额建议：仅在 Risk 表存在名额风险时建议释放或替补，可创建 Quota Replacement Flow
- 入境建议：根据 Event 的30/45/60天节点创建 Reminder Task
- 动作幂等：同一合同、风险或事件重复执行不会重复创建任务或进行中流程
- 原驾驶舱聚合数据继续作为待办计算来源，不再以汇报分类展示
- 入境节点：直接根据 Event 表最新入境日期推导30、45、60天跟进节点，不复制任务数据
- 证件预警：仅统计 Risk 表中带有明确到期日且进入90天窗口的数据；缺少日期时明确提示，不做推算
- 驾驶舱跳转：每个聚合区块均可进入对应合同、名额、事件或风险业务页面
- 跨模块搜索：统一查询 Person、Contract、Quota 和 Event
- 业务工作台：首页集中显示流程、风险、任务、资料和常用办理入口
- 全局搜索：输入人名、合同编号或名额编号，统一返回关联业务数据
- 姓名快捷查询：直接汇总人员的合同、当前名额与风险，无需逐页查找
- 自动更新：页面每30秒重新计算风险和任务到期状态，无需手工刷新
- 两步内办理：新增人员、合同、名额和事件均可从首页一步打开表单
- Web 管理界面：人员列表、合同详情、名额使用历史、风险仪表盘和任务提醒页
- Excel 批量导入：支持 `.xlsx` 批量导入人员与配额，整批校验失败时自动回滚
- 合同详情：查看周期、剩余天数、人员证件、名额、事件、流程、资料和风险，并直接更新状态
- 名额详情：查看 SWD/LD 使用记录、累计时长、剩余额度，并直接进行人员替补
- 风险与任务操作：风险可标记已解决，任务可在待办/进行中/已完成/逾期之间更新
- 合同：合同编号、人员、公司、开始/结束时间和六阶段状态
- 合同业务时间：合同创建时不填写开始日期；“入境”事件的 `event_date` 自动写入 `arrival_date`，合同开始日等于该日期，合同结束日自动取其后24个月；`created_at` 仅记录建档时间
- 合同查询：搜索合同/人员/公司，计算剩余天数，按状态筛选
- 人员：姓名、身份证后四位、港澳通行证后四位
- 配额：仅类型与公司名必填；批文号、序号、使用人及批文有效期均可后补
- 名额类型与额度：SWD 仅计算当前使用人；LD 累计所有使用记录；总额度均为24个月
- 替补记录：更换使用人时自动结束旧记录并保留完整历史
- 续期评估：关联人员与名额，自动计算提前3个月启动日、结束前1个月截止日和续期状态
- Document Vault：资料强制绑定人员、名额、流程；支持上传、OCR状态与全文查询
- Labour Workflow System：四步审批链与完整状态流转记录
- Lifecycle Engine：自动生成续期、提交和到期节点
- Risk Engine：自动识别合同、证件、资料和名额风险
- Task Reminder System：入境后自动生成7天、30天任务并支持状态更新

## Document Vault OCR

- TXT 文件使用内置文本提取，无需额外依赖。
- 图片在系统安装 Tesseract 时自动执行中英文 OCR。
- PDF 在系统安装 `pdftotext` 时自动提取文字。
- 缺少本地 OCR 引擎时文件仍会安全入库并标记“待OCR”，也可在上传时人工补录OCR文字。
- 默认单文件上限16MB，允许 TXT、PNG、JPG、TIFF、PDF。
- 事件：登记、签证、入境、离境、其他状态及备注
- 搜索：按姓名、证件后四位、名额编号、公司或事件内容搜索
- AI 问答：用自然语言查询名额、人员和最近事件，返回结构化状态与风险判断
- 数据校验：新建人员仅校验姓名与性别；新建配额仅校验类型与公司，同一人员不能同时占用多个配额

## AI 问答说明

当前版本使用规则识别 + 参数化 SQL + 规则总结，不需要外部 API Key。查询会优先匹配 `contracts`，再关联 `people` 与 `events`；旧有人员即使尚无合同也仍可查询。前端调用 `POST /api/ai/ask`，请求格式为：

```json
{"question": "C2026-001什么时候到期？"}
```

后续接入真实大模型时，可保留 SQL 查询结果，只替换 `answer_question` 的总结层。

## 名额额度规则

- `SWD`：同一时间一名使用人；替补后旧记录结束；已使用时长只取当前活动记录。
- `LD`：使用人可连续替补；已使用时长为该名额全部历史记录之和。
- 两类名额总额度均为24个月，剩余额度最低为0。
- 每条使用记录按完整日历月计算；完整月之外只要还有天数，就向上增加1个月。例如1月15日至2月15日为1个月，至2月16日为2个月。
- 旧数据库会无损迁移配额字段与使用记录；历史编号保留，新配额允许未编号的半数据状态。

## 续期评估规则

- 保存字段：`person_id`、`quota_id`、`current_contract_end_date`、`renewal_start_date`、`passport_expiry_check`、`id_card_expiry_check`、`submission_deadline`、`status`。
- `renewal_start_date` 自动取当前合同结束日前3个月。
- `submission_deadline` 自动取当前合同结束日前1个月。
- 任一证件检查“不通过”，或合同已经结束，状态为“不可续”。
- 两项检查均“通过”且未过提交截止日，状态为“可续”。
- 存在“待检查”或已错过提交截止日但合同尚未结束，状态为“风险”。
