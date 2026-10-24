# 更新日志

本项目遵循语义化版本。`0.x` 期间次要版本可能包含配置格式变更，升级前请留意对应条目。

## 0.3.8 — 2026-10-24

**修复：更换中转渠道后 Jev 调用全部 403（Cloudflare 1010 封 Python-urllib 默认 UA）**

生产实锤（2026-09-29 19:05，`jev_provider_url` 换到 `https://api.commandcode.ai/provider/v1/systemone` 后）：所有评估报 `HTTP Error 403: Forbidden`，但同一 key 用 curl 直连同一端点返回 200——请求根本没到鉴权层。实测插件同款 urllib 请求：响应头 `Server: cloudflare`、响应体 `error code: 1010`，即 CF 按**浏览器签名**封禁了 `Python-urllib/3.x` 默认 UA。

- **修复**：`_call_jev_sync` 请求头补自定义 `User-Agent`（`astrbot-plugin-jev_gate/0.3.8`）。任何套 Cloudflare 的官方/中转端点都需要；官方 `api.typesafe.ai` 此前未封纯属侥幸
- 排查时顺带确认：commandcode 的 403 在其文档里另有"套餐无 API 权限（Go 计划）"一种成因，且错误体会直接写明 `upgrade_required`——如果换 UA 后仍 403，读响应体对号入座
- 官方端点双 UA 实测（api.typesafe.ai + `/v1/systemone`）：自定义 UA 与 Python-urllib 默认 UA 均 200，`jev-latest` 当前解析为 jev-1.13.0——**自定义 UA 对官方端点无害**

**修复：schema 默认问题集缺 `is_unfinished`（与代码 `DEFAULT_QUESTIONS` 不一致）**

- `_conf_schema.json` 的 `questions` 默认 JSON 字符串漏了防抖专用问题 `is_unfinished`（v0.3.6 引入防抖时只加进了代码兜底 `DEFAULT_QUESTIONS`），新装用户开启防抖即告警并退化为无防抖。已补齐，两处定义保持一致
- ⚠️ 已装用户的 `questions` 是 text 叶子值，**schema 默认值变更不会迁移进已有配置**——升级到 0.3.8 后若要开防抖，需在配置里手动给 `questions` 补 `is_unfinished`（防抖开启而缺它时日志有告警提示）

**补齐：真实触发（dispatch）现在也打 info 日志**

- 此前评估结果的日志覆盖缺一角：跳过（cooldown/daily_limit）、dry-run 提名、防抖重评否决、no_conversation 都有日志，唯独**真的 yield `request_llm` 的那条路完全静默**——只进账本 `trigger.choice=dispatch`，控制台上 bot 突然开口看不出是门控触发的。根因是 v0.3.4 的「门控命中日志」把"命中"实现成了"被拦下"，与"被触发时输出 info"的设计意图错开。现在触发时打 `[JevGate] 触发主动回复: sender: text | reason=… in_dialogue=… debounce=… model=… latency_s=… conversation=…`，字段与账本 dispatch 行一一对应

**新增：合并转发展开后再交给 Jev 评估（思路来自社区 PR #1，本地重实现）**

- `_flatten` 对合并转发只留 `[Forward]` 占位，Jev 看不到内容，转发几乎拿不到提名（PR 作者实测 reply_value 0.31）。现在 `_expand_forwards` 在 @bot/指向别处早退之后调用 OneBot `get_forward_msg`（复用主线引用解析的 `OneBotClient`/`OneBotPayloadParser`，内部已做各实现参数名兼容）展开一层聊天记录，评估/缓冲/账本/触发提示看到同一份展开文本
- 与 PR 实现的两点差异：① **取回+解析整体进 try**——`parse_get_forward_payload` 对意外 payload 抛错不再炸掉整条 handler，只退回占位符；② **展开字数上限挂 `max_message_chars`×4**（关闭时 2000），不旁路长度护栏口径，避免多条转发攒同一个窗口把 state 推向 32k 硬限（v0.3.6 缓冲投毒的同款风险面）。行上限固定 20，取回超时 5 秒
- 非 OneBot 平台、调用失败、超时或解析为空时保留 `[Forward]` 占位，行为与旧版一致；无新增配置项

## 0.3.7 — 2026-09-27

**修复戳一戳误触发主动回复 + 新增 bot 自戳评估（`poke_self_evaluation`，默认关）**

生产实锤（2026-09-27 11:28，群 756741478）：群友 A 戳了群友 B（不是 bot），bot 主动回了「干嘛又戳弥音啦！」。完整链路：aiocqhttp 适配器把 `notify/poke` notice 转成普通群消息事件（`message=[Poke(id=target)]`、`message_str=""`，**不区分 target 是谁**）→ WakingCheckStage 因插件 handler 匹配而放行（`is_wake=True`）→ 本插件把 `_flatten` 出的 `[Poke]` 送进 Jev 评估，戳人者正处于活跃对话（`in_dialogue=true`）中打分过线（reply_value 1.54 + is_good_moment 0.56）→ 提名 → `request_llm`。追加进进行中 agent run 的 follow-up 文本 `[ComponentType.Poke]` 还会丢失被戳者信息（outline 渲染兜底分支不带 id），LLM 只能结合上文脑补，回复语义与真实被戳者都对不上。同日 12:02 的同类事件因 sleep_mode 休眠拦截未复现——非休眠 + 戳人者活跃对话 + Jev 给分过线才触发。

- **戳一戳不再送评估（行为修复，无开关）**：`on_group_message` 新增 Poke 组件检测。含 Poke 的消息照常进评估上下文缓冲（Jev 看全场时仍知道发生过戳一戳），但不再送 Jev 评估、不占日评估额度，账本记 `notice_skip`（`detail=poke_other` / `poke_self`）。戳别人（target≠bot）的事件任何时候都不评估
- **bot 自戳评估（`poke_self_evaluation`，默认 false）**：开启后，戳 bot 自己的事件经 `_render_poke` 渲染成语义文本（`[戳一戳] 戳了戳的尾巴，并被反过来哈气`——取 OneBot notice 的 `action`/`suffix` 字段，取不到退回 `[Poke]`）替换 `[Poke]` 走完整评估/提名/触发链，冷却/日限/防抖/[PASS] 终审与普通消息同权；关闭时与戳别人同等处理（只进缓冲）

## 0.3.6 — 2026-09-26

**长消息护栏 + 碎片消息合并 + 触发前防抖（Jev 判断"话没说完"才等）**

- **长度护栏**（`max_message_chars`，默认 500，0 = 不截断）：限制的真实口径经三次 API 实测确认为 64k tokens/请求（state+最长问题 32k）——超限返回 400 `max_tokens_exceeded`，插件跳过该消息，但此前**超长条目会留在上下文缓冲里，连带同窗口后续最多 15 条消息的评估一起失败**（缓冲投毒）。现在超长消息在 `_flatten` 单点截断（评估上下文/触发提示/账本同一份，带 `[截断,原N字]` 标记）；完整原文不受影响——群历史由主线独立落库，bot 回复照样看得到全文
- **缓冲合并**（`merge_window_seconds` 默认 20 秒 + `merge_max_count` 默认 10 条）：同一发送者窗口内的连续消息在发给 Jev 的上下文里合并成一条（`msg1 / msg2 / msg3`），QQ 碎片化打字不再被切成零散小句，有限的 context_window 装下更多有效内容。纯视图变换：缓冲本体保持单条、账本按单条记、合并时浅拷贝不动原始数据
- **触发前防抖**（`debounce_enabled`，默认关）：解决"burst 第一句就抢答"。新问题 `is_unfinished`（并行提问，零额外成本）判断「话是不是没说完」：概率低于 `debounce_confidence_floor`（0.5）→ 立即触发；越像没说完 → 线性映射到 `[debounce_min_wait_seconds, debounce_max_wait_seconds]`（5/20 秒）等越久（**用 noul 概率做映射变量**，不用单独的 confidence 字段）。等待期同发送者的新消息被吸收（`debounce_absorbed`，不再单独评估、不占日评估额度），安静期满合并重评一遍完整提名：仍提名 → 继续等（最多 3 轮）；否决 → 放弃触发并退还日限额度（`debounce_rejected`）
  - 延迟只落在少数被提名的消息上，普通消息零影响；@bot 点名路径完全绕开
  - 实现纪律：等待在生成器 handler 内分片 `asyncio.sleep`，不持锁、**不起后台任务**（热重载安全）；防抖标记带自愈上限，生成器异常退出也不会把人永久静音
  - 防抖开启但 `questions` 缺 `is_unfinished` 时启动告警并退化为无防抖
- 测试：`jeval/test_v031_fixes.py` 新增 H 节 24 断言（截断/合并窗口与上限/滑动断链/防抖立即触发/吸收/合并重评/否决退款/轮数上限/缺问题退化）

## 0.3.5 — 2026-09-25

**接续对话识别：bot 回复进入评估上下文 + 接续提名放宽线**

生产实锤（2026-09-25）：bot 18 秒前刚回复"呜！断零食也不干！"，用户接"那拔网线"——省略主语的接话字面无指向，is_mentioned 0.40 / reply_value 1.40 双双够不着内置两条提名线（0.6 / 1.5），落选冷场。此前几条能触发全靠"你/弥音"字面指向。

- **bot 侧上下文（数据层，治本）**：新增 `after_message_sent` 钩子，把 bot 真实发出的 LLM 回复回写会话缓冲（`is_self: True`，state 里渲染为 `bot(我): …`）。此前评估上下文只有群友消息——bot 自己的入站消息在 handler 开头就 return、主动回复也不经过 `event_message_type`，`_build_state` 的 `is_self` 渲染一直是死代码，Jev 判断"是不是接续对话"时根本看不见 bot 刚说过什么。拒答被清空的轮次不会回写（RespondStage 对空链早退，不触发该钩子）
- **接续提名路径（提名层，立竿见影）**：bot 刚回复过的同一发送者（乐观标记，拒答回滚）在 `followup_window_minutes` 窗口内接着说话时，追加内置第三条提名路径 `reply_value >= thresholds.continuation_reply_value`（新阈值键，默认 **1.2**；设 3.1 以上等效关闭）。兜底链不变：daily_limit 照计数、LLM 终审可 `[PASS]`、拒答退还
- **`nomination_rule` 注入伪值 `continuation`**（对话中 = 1.0，否则 0.0）：自定义规则可写 `{"all":[{"continuation":{">=":0.5}},{"reply_value":{">=":1.2}}]}`；伪值已从 questions 交叉检查中排除，不会触发"问题不存在"误告警
- **自持 LLM 回复标记**：新增 `on_llm_response` 钩子自打 `__jevgate_llm_reply__`。此前"守卫覆盖所有 LLM 回复"依赖 fold_fan_formatter/splitter 等环境插件代打 `__is_llm_reply`，裸装市场用户没有它们时静默失效；现在两套标记并存，不再有外部依赖
- 账本 `evaluated` 行新增 `in_dialogue` 字段，接续提名的 `reason` 前缀 `continuation:`，方便统计该路径实际效果
- 测试：`jeval/test_v031_fixes.py` 新增 F 节（接续提名 8 例：窗口/换人/过期/窗口 0 不限/伪值/退还破坏连续性）+ G 节（缓冲回写与自持标记 8 例）

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
