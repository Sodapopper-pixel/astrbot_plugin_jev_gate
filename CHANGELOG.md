# 更新日志

本项目遵循语义化版本。`0.x` 期间次要版本可能包含配置格式变更，升级前请留意对应条目。

## 0.3.4 — 2026-09-25

**修复 `followup_window_minutes=0` 反被长冷却拦死 + 冷却默认关 + 门控命中日志**

- **`followup_window_minutes=0` 的语义从"窗口 0 秒"改为"不限窗口"**：旧代码 `now - last_dispatch_ts < window_min * 60` 在窗口为 0 时永假 → `is_followup` 永假 → 用户本想关掉窗口限制，反被 30 分钟长冷却拦死（生产实测：设 0 后仍然撞冷却墙）。现在 `window_min <= 0` 直接判定为"接着聊"走短冷却；schema 的 `minimum` 也从 1 放开到 0
- **`cooldown_enabled` 默认值 `true` → `false`**：连续对话场景下全局冷却误伤太多（实测 137 秒间隔的合法后续对话被 30 分钟墙拦下，而 Jev 提名分 0.82 已达标）。默认关后提名通过 `daily_limit`（默认 5 次/会话/天）就直接触发；想要频率约束再手动打开，@点名回复不受影响
- **每次门控命中都打 info 日志，带配置字段与实测值**（日志里搜 `[JevGate] 跳过`）：cooldown 行给出 `reason`/`followup`/`elapsed_s`/`cooldown_s` 与四个冷却配置现值；daily_limit 行给出 `used`/上限/日期；directed_skip 行给出 `aimed_at`；日评估上限行给出上限与已用数（每会话每天只喊一次——命中后每条消息都走这个分支，不节流会刷屏）。账本 `trigger=skip` 同步补 `elapsed_s`/`cooldown_s`/`used`
- 测试：`jeval/test_v031_fixes.py` 新增 B6（窗口 0 = 不限窗口）/ B7（缺省键时默认关）/ E 节（四类门控日志的字段与值断言）

## 0.3.3 — 2026-09-24

**新增 `nomination_rule`：提名规则可配置，自定义问题从此能参与提名**
- `questions` 里加的新问题此前只进账本、不影响行为——内置提名条件写死在 `_nominate` 里，只读 `is_mentioned`/`reply_value`/`is_good_moment` 三个键
- 新增 `nomination_rule`（text JSON）布尔表达式：`{"any":[...]}` / `{"all":[...]}` / `{"<问题ID>":{">=":数字}}` 三种节点可任意嵌套，运算符白名单 `>= > <= < ==`（字典分发，不用 `eval`）
- 示例（与内置规则等价）：`{"any":[{"is_mentioned":{">=":0.6}},{"all":[{"reply_value":{">=":1.5}},{"is_good_moment":{">":0.45}}]}]}`
- 三层兜底：留空 = 内置规则（老用户零感知）；坏 JSON / 结构不合法 = 告警并回退内置规则，绝不静默变成永不提名；规则引用 `questions` 里不存在的问题 ID = 该条件不成立 + 启动时告警
- 命中的子句会写进账本 `reason`（如 `is_urgent(0.75>=0.7)`），可回查是哪条放行的
- `_check_question_ids` 改为仅在使用内置规则时要求 `is_mentioned`/`is_good_moment`
- 测试：`jeval/test_nomination_rule.py` 40+ 断言覆盖等价性、自定义问题端到端触发、8 类坏规则回退、运算符全覆盖

## 0.3.2 — 2026-09-24

**修复主动触发不带群聊历史（本轮最重要）**
- `on_group_message` 的 priority 从 `500` 改为 `-2`。AstrBot 的 handler 排序是**按 priority 降序**（`star_handler.py`: `sort(key=lambda h: -priority)`，数字越大越先执行），旧值 500 让本插件跑在了内置 astrbot star 的 `on_message`(priority=0) **前面**——那个 handler 负责把消息写进群上下文缓冲并打 `_group_context_record_id` 标记，`on_req_llm` 靠它决定注入群历史。标记不存在 → 早退 → 主动触发的请求完全没有群聊上下文
- 实证：对话数据里 @ 触发的轮次带着 `You are in a group chat...BEGIN CONTEXT` 块（实测一条 2722 字符），主动触发的轮次（`terk: 弥音可爱`、`CX2118: 有没有人帮忙采集...`）只有 `sender: text`；日志里主动触发消息的 handler 链中没有 `plugin -> astrbot - on_message`，它要等回复发出后才补跑
- 连带损伤一并修复：未消费的群上下文缓冲会攒到下次 @ 回复时一次性吐出（实测跨 4.5 小时的陈旧消息）
- 副作用（正向）：sleep_mode 的入站守卫(priority=1)现在跑在本插件前面，休眠群直接跳过评估，不再白烧 Jev 调用

**配置项调整**
- 新增 `jev_provider_url`（默认官方端点 `https://api.typesafe.ai/v1/systemone`）：可填自建代理/中转渠道，须兼容同一接口格式；留空回退官方
- `model` 默认值 `jev-1.13.0` → `jev-latest`（跟随官方最新版）。注意 alias 会漂移而阈值按版本调，质量变化时先钉版本号再对比账本
- 会话名单拆成 `session_mode`（whitelist/blacklist，默认 whitelist）+ `enabled_sessions`（白名单）+ `disabled_sessions`（黑名单）。老配置只有 `enabled_sessions`，默认走白名单，行为不变
- **`thresholds` 键位与问题 ID 对齐**：`mention`→`is_mentioned`、`good_moment`→`is_good_moment`、`answerable`→`is_answerable`、`unanswered`→`is_unanswered`、`persona_fit`→`is_persona_fit`。旧简写键仍被接受（自动改名并告警），老配置不炸；现在自定义了新问题就能直接给它配阈值
- `other_bot_ids` 描述改为「防止 bot 之间刷屏」，明确其作用是提醒 Jev 注意这些账号是机器人；`evaluate_other_bots` 描述改为「直接无视其他 bot 的发言（不评估）」

**文档**
- 插件 AGENTS.md / README 加入 Jev 实践参考：TypeSafe 官方 Agent Skill [`typesafe-ai/skills`](https://github.com/typesafe-ai/skills)（贡献者改判定逻辑前先读）
- 同步纠正 AstrBot handler priority 语义：**降序，数字越大越先执行**（此前工作区文档写的"越小越先"是错的，已修正）

## 0.3.1 — 2026-09-24

生产踩坑修复（五项都由账本 + backend.log + 数据库实证定位）。

**冷却改为「对话感知」**
- 新增 `cooldown_enabled`（默认 true）开关，`cooldown_minutes=0` 同样等效关闭——连续对话场景下 30 分钟全局静默确实没必要
- 新增 `followup_window_minutes`（默认 10）+ `followup_cooldown_seconds`（默认 0）：bot 刚回复过的发送者，在窗口内继续说话视为「接着聊」，直接放行不再被长冷却拦；**其他人插话仍走长冷却**，防刷屏语义不变
- 实证场景：用户「弥音在不」触发回复，137 秒后「帮我看看grok4.7的情况」Jev 提名 0.82 达标却被 cooldown skip——本版本该句可正常触发

**@指向别处不抢答（规则重设计）**
- 新增 `skip_directed_messages`（默认 true）：消息 **@ 了任何其他账号**（不只其他 bot，含 @全体）、或**引用了其他 bot 的消息**时，视为「别人点的对话」，不评估不触发（账本记 `directed_skip`）。关掉后交还 Jev 正常评估
- 实证：167 条 @他人的消息 Jev 的 is_mentioned 全部 ≤0.5——它分得清「不是说我」，但 value 提名路径压根不看指向，60+ 条被提名、5 条真实 dispatch（含 @普通群友问技术问题被抢答）。这活只能代码规则干
- 修复 `other_bot_ids` 的标注立意从未生效的问题：其他 bot 的发言以前在进缓冲前就 return 了，`is_bot` 标注是死代码；现在照常进缓冲并标注，Jev 看全场时知道「这条是别的 bot 说的」

**拒答不再隐形消耗额度 + [PASS] 防泄露**
- bot 输出 `[PASS]`/`[不回复]` 拒答时，退还本次触发的冷却与日限（此前拒答也照扣 30 分钟冷却）
- 拒答守卫从「仅本插件触发轮」扩展到所有 LLM 回复：普通回复（如 @bot 点名）模型偶发直接输出 `[PASS]` 时同样清空，不再发到群里
- 守卫仍只认整条精确相等（容忍首尾空白与句读），`[PASS]，喵` 这类正常短回复不误伤

**[PASS] 历史可见性可配（`pass_history_mode`）**
- `keep`（默认）：拒答照常落库，模型之后能看见自己曾经 `[PASS]` 过（群消息历史 7 条 + 会话历史实证存在）
- `hide`：守卫清空时把刚落库的拒答记录从群消息历史（`platform_message_history`）和会话历史（`conversations`）里删掉，模型完全不知道自己曾经拒答过
- 落库发生在 `on_llm_response`（persist_llm_response）和 agent 子阶段（_save_to_history），都早于本守卫，所以只能事后删；只删最新一条命中 token 的记录。livingmemory 自建的 messages 表不在可达范围，不处理

**拒答宽松匹配可配（`refusal_fuzzy_match`，默认 false）**
- 开启后本插件触发轮改为包含匹配：整条里出现 `[PASS]`/`[pass]`/`[不回复]` 就算拒答。普通回复始终精确匹配，避免误伤

## 0.3.0 — 2026-09-23

首个公开发布版本（AstrBot 插件市场）。

**配置**
- `questions` 改为 JSON 文本字段：可以自由修改判断文案，也可以**新增自己的判断问题**（旧写法把它当结构化对象，多出来的键会在配置加载时被静默裁掉）
- `enabled_sessions` 支持 `"*"` 通配所有会话，便于先用 dry-run 观察
- 默认值全部通用化：内置人设、判断问题、触发提醒不再包含任何特定 bot 的名字
- 新增启动自检日志：一行打印「会话 / dry_run / api_key / 模型」，并在会话为空、未配置人设、问题 ID 缺失时告警

**工程**
- `questions`/`thresholds` 的读取改为容错解析：坏 JSON、非数字阈值只告警并回退默认，不再影响插件加载
- 明确最低版本 `astrbot_version: >=4.25.0`（依赖 `TextPart.mark_as_temp`）
- 补齐面向插件市场的信息：`display_name`/`short_desc`/`repo`/`tags`/`support_platforms`/`social_link`、256×256 `logo.png`、MIT `LICENSE`

## 0.2.1 — 2026-09-23

- **修复主动回复不进对话数据**：触发的 ProviderRequest 必须携带 `conversation=`。
  主线只在 `req.conversation` 存在时才注入人格/skills、填对话上下文、把本轮写回对话历史；
  不带则「消息发出去了，但对话数据里查不到」。

## 0.2.0 — 2026-09-23

- **重构为「只判断 + 触发」**：移除插件内的回复链构造与直发逻辑。
  `context.send_message()` 会绕过主线全部后处理（分段、群历史落库、占位符匹配、出站守卫），
  实测出现过「回复末尾带句号、对话数据查不到该回复」。改为在管线内
  `yield event.request_llm(...)`，其余交给 AstrBot 主线。
- 触发提醒改走 `extra_user_content_parts` + `mark_as_temp`，只对本次请求可见、不污染对话历史。

## 0.1.x — 2026-09-21

- 初版：Jev 打分 + 阈值提名 + 冷却/日限 + 账本落盘 + dry-run。
- 修复：账本在发送决策之后落盘、`[At:...]` 占位符解析、拒答标记按环境约定匹配、休眠期不出站。
