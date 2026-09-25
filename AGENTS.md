# AGENTS — astrbot_plugin_jev_gate

Jev 主动回复门控插件（v0.3.4 判断+触发，已发布到 AstrBot 插件市场）。Jev 给群聊消息打分，提名值得主动回复的消息后**在管线内 yield request_llm 触发**——插件不组装 LLM 上下文、不直发消息。

- 仓库：https://github.com/Sodapopper-pixel/astrbot_plugin_jev_gate
- **Jev 实践参考（贡献前先读）**：TypeSafe 官方 Agent Skill `typesafe-ai/skills` — https://github.com/typesafe-ai/skills
  - 安装：`npx skills add typesafe-ai/skills --skill typesafe-ai`（Claude Code 用 `claude plugin marketplace add typesafe-ai/skills`）
  - 本插件的 questions 设计（Noul/Score 原语、state/instructions/criteria 写法、并行提问、0.3-0.7 中间值不触发）都遵循它的规范；改判定逻辑前对照它的 "Design the judgments" 一节
  - 它把 docs.typesafe.ai 当唯一事实源（纯索引型 skill），API 细节以官方文档实时为准，别信二手转述
- 作者：terk ｜ 许可：MIT ｜ `astrbot_version: ">=4.25.0"`（依赖 `TextPart.mark_as_temp`）

## 架构要点（v0.3.4）

- **一层钩子**：`@filter.event_message_type(GROUP_MESSAGE, priority=-2)` 的 **生成器 handler**（`on_group_message`）。**priority=-2 是负载性的**：AstrBot handler 按 priority **降序**执行（`star_handler.py` 的 `sort(key=lambda h: -priority)`），-2 保证排在内置 star 的 `on_message`(0, 群上下文捕获) 和 chat_plus(-1) 之后——改成非负会让主动触发丢掉群聊历史，详见下方踩坑。

  不提名时**零 yield 直接返回**（经 `call_handler` 透传，消息后续命运不受影响）；提名通过冷却/日限后 `yield event.request_llm(...)` **一次**——返回值 ProviderRequest 由 `ProcessStage` 交给 Agent 子阶段执行，人格/对话历史/群历史/分段/落库/占位符/出站守卫全部走主线。
- **绝不 set_result / stop_event / `context.send_message()`**：后者绕过主线全部后处理（分段、群历史落库、占位符匹配、出站守卫），2026-09-23 生产实锤（回复末尾带"。"、对话数据查不到）。
- **⚠️ `conversation=` 不是可选项（2026-09-23 生产实锤）**：主线只在 `req.conversation` 存在时才 (a) 注入人格/skills（`_ensure_persona_and_skills` 开头 `if not req.conversation: return`，astr_main_agent.py:536）、(b) 给 `req.contexts` 填对话历史（:1436-1442）、(c) 把本轮写回对话历史（`_save_to_history` 同款早退，internal.py:459-461）。**症状**：回复能正常发出（走管线能分段），但无 persona、上下文为空、`conversations.updated_at` 永远停在触发前——"日志里发了、对话数据里没有"。取不到就 `new_conversation` 新建（与主线 `_get_session_conv` 同口径），新建失败才跳过并记 `trigger=skip/no_conversation`。group_chat_plus main.py:10989 有同一踩坑注释。
- **触发提醒不落库**：`TextPart(提醒).mark_as_temp()` 挂 `req.extra_user_content_parts`——本次请求可见，落库时被 `dump_messages_with_checkpoints` 按 `part._no_save` 过滤（message.py:354），prompt 保持干净的目标消息文本。老版本无 TextPart 时退化为拼进 prompt（会落库）——这也是 `astrbot_version >= 4.25` 的来源。
- **拒答守卫**：`@filter.on_decorating_result(priority=99990)`，整条命中拒答 token（`[PASS]`/`[pass]`/`[不回复]`，容忍首尾空白与句读）才 clear（精确白名单，`[PASS]，喵`/`[笑死]` 不误伤）。**覆盖所有 LLM 回复**（`__is_llm_reply`），不只本插件触发轮——普通 @bot 回复模型偶发直接输出 `[PASS]` 也清（2026-09-24 生产实锤泄露两连发）。拒答即无真实回复：守卫里 `_refund_trigger` 退还冷却与日限快照（`prev_last_nominate`/`prev_dispatch_*`），"触发了但没说话"不再隐形消耗。取不到 conversation 的 no_conversation 分支同样退还。`refusal_fuzzy_match`（默认 false）开启后触发轮改包含匹配。`pass_history_mode`（默认 keep）设 `hide` 时守卫顺带删掉刚落库的拒答记录（群消息历史 `delete_by_id` + 会话历史弹尾条）。这是第二道——sleep_mode 的 99999 守卫按 `no_reply_placeholder` 子串匹配也会清，且先于本守卫执行。
- **[PASS] 落库早于守卫（2026-09-24 实证）**：`persist_llm_response`（on_llm_response）把 bot 消息写进 `platform_message_history`，agent 子阶段 `_save_to_history` 写进 `conversations`，两者都早于 result_decorate——所以守卫清空消息链**挡不住落库**，群历史里躺着 7 条 `[PASS]`、模型后续上下文真能看见自己曾经拒答过。要"完全不知道"就得 `pass_history_mode=hide` 事后删。另注意 persona 的 `<no_reply>` 段只教了 `[不回复]`，`[PASS]` 是 jev_gate 私有契约，模型会混用两个 token。
- **Layer 0 代码规则**：会话白名单（支持 `"*"` 通配）/ @bot 检测（`_is_at_bot` 命中直接跳过，@ 检测代码规则 100% 可靠，评估实测 Jev 有一条直接 @ 只给 0.69）/ **@指向别处不抢答**（`_directed_elsewhere` + `skip_directed_messages` 开关，默认开：消息 @ 了任何其他账号含 @全体、或引用了其他 bot 的消息 → 跳过记 `directed_skip`；引用普通群友不带@不算，留给 Jev）/ 日评估上限 / 冷却 / 日触发上限。
- **⚠️ handler priority 是降序（数字越大越先执行）**：`star_handler.py` 的 `StarHandlerRegistry.append` 用 `sort(key=lambda h: -priority)`。**工作区 AGENTS.md 里"priority 数字越小越先执行"的正文是错的**（它自己的优先级表格反而符合降序）。本插件 `on_group_message` 用 **-2** 而不是 500，就是为了排在内置 astrbot star 的 `on_message`(priority=0) 之后——见下条。
- **主动触发必须晚于群上下文捕获（2026-09-24 生产实锤）**：内置 star 的 `on_message`(priority=0) 调 `group_chat_context.handle_message(event)`，把本消息写进群上下文缓冲并打 `_group_context_record_id`/`_group_context_raw_idx` 标记；`decorate_llm_req` → `on_req_llm` 靠这两个标记决定注入 `raw_list[:prompt_idx]` 作为群历史。**旧版 priority=500 跑在 on_message 前面**，yield 出的请求标记还不存在 → `on_req_llm` 开头 `if not isinstance(record_id, str) and (prompt_idx < 0): return` 直接早退 → 主动触发的请求**完全没有群聊历史**。日志实锤：主动触发消息的 handler 链里没有 `plugin -> astrbot - on_message`，它要等回复发出后才补跑（`StarRequestSubStage` 的 handler 循环在 yield 后会被 ProcessStage 内联的 agent 子阶段打断，回复完才继续）。连带损伤：未消费的缓冲攒到下次 @ 回复一次性吐出（实测一条 2722 字符、跨 4.5 小时的陈旧上下文块）。修法=priority 改 -2。副作用（正向）：sleep_mode 入站守卫(1)现在跑在前面，休眠群不再浪费 Jev 调用。
- **为什么指向规则必须代码层**：167 条 @他人的实测消息 Jev 的 is_mentioned 全部 ≤0.5（它分得清"不是说我"），但 **value 提名路径压根不看指向**——60+ 条 @他人的消息被提名、5 条真实 dispatch（含 @普通群友问技术问题被抢答）。指望调阈值/改 criteria 堵不住这条路。
- **other_bot_ids 的两个用途**（v0.3.1 修复了第一个从未生效的问题）：① 上下文标注——其他 bot 的发言进缓冲时打 `is_bot`，Jev 渲染时显示 `name(机器人)`（旧版在进缓冲前就 return，标注是死代码）；② 引用判定——引用它们的消息算 bot 间对话不抢答。是否评估它们发的消息由 `evaluate_other_bots` 决定（默认 false，但**仍进缓冲**）。
- **冷却语义（v0.3.4 改）**：`cooldown_enabled` **默认 false**（v0.3.1 曾是 true，生产实测"喊完名字接着聊被 30 分钟墙拦死"），`cooldown_minutes=0` 等效关闭；开启时**同一发送者**在 `followup_window_minutes`（默认 10min）内接着说话走 `followup_cooldown_seconds`（默认 0=直接放行）短冷却，其他人插话仍走长冷却。**⚠️ `followup_window_minutes=0` 的含义是"不限窗口"（同一发送者永远算接着聊），不是"窗口 0 秒"**——后者 `now - last_dispatch_ts < 0` 永假 → `is_followup` 永假 → 本想放宽限制反被长冷却拦死（2026-09-25 用户实测踩坑，故 v0.3.4 改成 `window_min <= 0 or ...`，schema `minimum` 也从 1 放开到 0）。触发时**乐观**记录 `last_dispatch_ts/sender`（不必等回复发出），拒答时守卫回滚快照。
- **门控命中日志（v0.3.4 新增）**：每次被门控拦下都打 info（搜 `[JevGate] 跳过`），带字段与实测值：cooldown（`reason` + `followup` + `elapsed_s`/`cooldown_s` + 四个冷却配置现值）、daily_limit（`used`/`daily_limit`/`date`）、directed_skip（`aimed_at`/`skip_directed_messages`）、日评估上限（`max_daily_evaluations`/`used`/`date`，**每会话每天只喊一次**——命中后每条消息都走这个分支，不节流会刷屏）。@bot 点名（`mention_code_skip`）和其他 bot 发言属于预期行为，只写账本不打日志。harness 用 `H.reset_logs()` / `H.logs()` 断言这些内容。
- **提名条件**：留空 `nomination_rule` 时走内置两条路径（`_nominate`，评估实测）：`is_mentioned >= is_mentioned` 或 `reply_value >= reply_value 且 is_good_moment > is_good_moment`，阈值取 `thresholds`。`is_unanswered` 对非问题消息是噪音（全量 mean 0.63），只记账不参与提名。
- **自定义提名规则**（v0.3.3 `nomination_rule`，text JSON）：`{"any":[子句...]}` / `{"all":[子句...]}` / `{"<问题ID>":{">=":数字}}` 三种节点，运算符白名单 `>= > <= < ==`（`_RULE_OPS`，**不用 eval**）。**这是 questions 自定义问题能参与提名的唯一途径**——否则新问题的值只进账本。三层兜底：留空=内置规则、坏 JSON/结构非法=告警回退内置（绝不静默不提名）、规则引用 questions 里没有的问题 ID=该条件不成立+启动时告警。原因串由实际命中的子句生成（如 `is_urgent(0.75>=0.7)`），落账本可追溯。按原始字符串缓存，配置没变不重复解析。
- **Jev 调用超时**：`_call_jev` 外层 `asyncio.wait_for(jev_timeout_seconds=25)`。Jev 实测 p50 1.2s / p90 7s / 极端 191s（重试睡 1+2+4+8s + 4×60s timeout）。调用本身在生成器 handler 内同步 await（评估完才决定 yield 与否），@bot 消息在代码层提前返回、不受影响。
- **v0.1.x 的终审层/直发/`_is_refusal`/`_build_reply_chain`/合成事件/后台任务/`speech log 读取`/`llm_context_window`/`_is_group_sleeping` 全部已删除**——`[At:...]` 由主线发送链解析，休眠由 sleep_mode 入站(1)+出站(99999)覆盖，拒答由 99990+99999 两守卫覆盖。"影子实现"是 09-23 两轮排查的结论。

## 关键状态

- `self._sessions[umo]`：`buffer`(deque, maxlen = context_window+5，供 Jev 看全场) + asyncio.Lock（**只保护计数器**，Jev HTTP 不持锁）+ daily/nominated 计数 + last_nominate + `last_dispatch_ts/sender`（对话连续性：bot 上次触发/回复的发送者，触发时乐观写、拒答回滚）+ `prev_*` 快照（拒答退还用）。**热重载丢缓冲与计数**（账本不丢）。
- `self._persona_cache[self_id]` / `_questions_for`：`{self_id}` 占位符按事件替换（一个实例服务多平台）。
- 账本：`StarTools.get_data_dir("jev_gate")/ledger_YYYYMMDD.jsonl`（即 `data/plugin_data/jev_gate/`）。`kind`：`mention_code_skip` / `directed_skip`（带 `aimed_at`）/ `evaluated` / `error`；`trigger` 字段：`dry_run` / `skip(cooldown|daily_limit|no_conversation)` / `dispatch`（带 `conversation` 前缀便于和 DB 对账）。cooldown skip 带 `followup: true/false` 区分长短冷却 + `elapsed_s`/`cooldown_s` 实测值，daily_limit skip 带 `used`。**日志与账本是两套可见性**：门控命中必有 info 日志（字段+值），@bot 点名/其他 bot 发言只进账本。
- `initialize()`：启动自检，打一行「会话/dry_run/api_key/model」状态，并在 `enabled_sessions` 为空、`persona` 未配置、`questions` 缺关键 ID 时告警。"装了没反应"先看这行。

## 配置层（v0.3.0 改版，面向市场用户）

- **`questions` 是 `text`（JSON 字符串）而不是 object/dict**：`text` 是**叶子值**，配置完整性检查（`check_config_integrity`）不会把它递归裁键 —— 所以用户**能自行新增问题 ID**；而 `object`/`dict` 会被 refer 结构逐键比对，多余键在加载时被静默删除（实测）。代码侧 `_json_cfg` 同时接受 dict（老配置/手改配置文件）与 JSON 字符串，坏 JSON 只告警并回退默认。
- **`thresholds` 的键 = Jev 问题 ID**（v0.3.2 对齐，旧简写 mention/good_moment/answerable/unanswered/persona_fit 自动改名并告警）：键与 `questions` 一一对应，自定义了新问题就能给它加阈值。提名只用三个：`is_mentioned`、`reply_value`、`is_good_moment`，其余仅记账。
- **`thresholds` 是 `object` + `items`**：键集固定正是想要的（UI 里逐项数字输入），缺项由 items 的 `default` 补齐；代码侧再做一层 `float()` 容错。
- **`dict` 类型不要用**：v4.27.0 才加入 `DEFAULT_VALUE_MAP`（4.26 及以前会直接 `TypeError: 不受支持的配置类型`）；`object` 在 4.17 就有。schema 里 `object` **必须带 `items`**，否则 `KeyError: 'items'` 让插件加载即崩。
- **默认值通用化**：内置 persona 是「一个技术性占位人设」，不含任何具体 bot 的名字/人设；`questions`/`trigger_notice` 同样去掉环境特有称呼。**通用 persona 会让 Jev 缺少边界、提名偏多**（实测：同一批闲聊消息，具体人设 0 提名、通用人设 2-3 提名）——所以启动日志会对未配置 persona 告警，README 把「人设怎么写」列为判断质量的第一要素。

## 踩坑记录

- **`[PASS]` 泄露到群里（2026-09-24 生产实锤）**：用户 @bot 的消息模型直接回复了 `[PASS]`（08:26 连发两条），旧守卫只处理带触发标记的轮次所以没拦。根因侧写：persona 的 `<no_reply>` 段只教了 `[不回复]`，`[PASS]` 是 jev_gate 私有契约——模型混用两个 token。修复=守卫扩到所有 LLM 回复。教训：**任何"模型输出约定 token"都必须有覆盖全部回复路径的出站守卫**，不能只守自己触发的那一条。
- **@他人的消息被真实 dispatch（2026-09-24 账本实锤）**：`other_bot_ids` 为空时，群里其他 bot（织羽酱/九曜）的消息按人类评估，reply_value 路径提名并触发——既浪费冷却/日限，又让 bot 去接其他 bot 的话。同批数据里还有 @普通群友问技术问题被抢答的 dispatch。167 条 @他人消息的 is_mentioned 全部 ≤0.5 证明 Jev 分得清指向，是 value 路径不看指向。群里其他 bot 的 QQ 号可从日志 `sender=AIxxx(QQ号)` 或消息 `[At:QQ号]` 反查；引用消息的作者读 `Reply.sender_id`（`aiocqhttp_platform_adapter.py` 构造引用段时填）。
- **冷却拦得住"喊名字接着聊"（2026-09-24 账本实锤，v0.3.4 起默认关）**：「弥音在不」dispatch 后 137 秒的「帮我看看grok4.7的情况」提名 0.82 达标却被 30 分钟冷却 skip。全局冷却对"正在进行的对话"是误伤——v0.3.1 起同发送者窗口内放行，v0.3.4 起干脆默认关闭。**判定层没问题先查账本的 `trigger.detail` 或日志的 `[JevGate] 跳过`，别去调阈值**。
- **thresholds 键集硬编码 vs questions 可自由扩展的矛盾（v0.3.3 解法）**：`check_config_integrity`（astrbot_config.py:167）每次加载都按 schema 逐键比对，`conf` 有而 `refer`（schema）没有的键**直接删掉**（实测日志只有一行 INFO `Config key removed: thresholds.xxx`）——所以 `object`+`items` 的键集被 schema 钉死，用户无法给自定义问题配阈值；而 `text` 是不透明叶子值，里面写什么都不管。曾想加 `custom_thresholds`(text) 合并，但单独做没意义：**阈值只有三个键被 `_nominate` 读，自定义问题的阈值是死配置**。最终解法是让提名规则本身可配置（`nomination_rule`），数字直接写在规则里，单一事实源。
- **Jev 的 state 不是单条消息**：`recent_conversation`（context_window 条，排除目标消息本身）+ `bot_persona` + `target_message`。is_unanswered 的前提来自这个窗口，窗口外的更早回答看不见。
- **@检测初版把"@其他机器人"误判成点名**（0.94）：必须在 persona 里钉死 QQ 号+名字、criteria 里写"@其他机器人不算"。多 bot 群必配 `other_bot_ids`。
- **暗指名（"昨天哪个AI在刷屏"）不能自动回**：Jev 给 0.4-0.6 中间值，误发=群里社死。提名后走主线问一嘴，bot 在自有上下文里判断是不是说自己。
- **人格解析必须取 per-UMO 配置（2026-09-22 生产排查实锤）**：`resolve_selected_persona` 第三级回退读 `provider_settings.default_personality`，而它**按 UMO 路由**——本机 `umop_config_routing` 把 `QQ:*:*` 路由到「群聊bot」档案；`cmd_config.json` 顶层的 `default_personality` 对 QQ 会话**不生效**。v0.2 起人格由主线装配（前提是 conversation 挂上了），插件不碰。
- **`provider_ltm_settings.group_message_history_enable` 关闭时主线无群上下文**：降级靠对话历史，设计内，不是 bug。
- **model 默认 jev-latest**（v0.3.2 起跟随官方最新）：alias 会随官方更新漂移，而阈值是按具体版本调的——提名质量变化时先钉版本号（jev-1.13.0）再对比账本；账本记 `model` 字段便于回溯。
- **`persona` 是 text 型且 schema 默认 `""`，`_cfg(key, default)` 对空字符串不兜底**：必须 `or DEFAULT_PERSONA`（harness 断言看住）。
- StarTools 导入：`astrbot.core.star.star_tools`，老版本回退 `astrbot.api.star`；`get_data_dir(plugin_name)` 返回 `data/plugin_data/{plugin_name}`。
- **导入路径以真实安装为准，别信 mock**：`MessageChain` 在 `astrbot.api.event`，**不在** `astrbot.api.message_components`。自测 stub 骗过 py_compile，最终用 bundled python 真实导入核验。
- **加载链路验证姿势**：`importlib.util.spec_from_file_location('data.plugins.<name>.main', <main.py 路径>)` + `exec_module`，在 bundled python（`D:\AstrBot\backend\python\python.exe`）下跑，等于模拟 star_manager 的加载。
- **mock 桩要与真实事件读面对齐**：harness 曾因 MockEvent 缺 `get_platform_id`/`session_id` 而让插件走进 no_conversation/异常分支——桩缺字段会伪装成"插件有 bug"，先补桩再怀疑代码。
- **测试断言不要绑人设**：harness 的提名断言一度依赖内置 persona，换成通用人设就红。正确做法：fixture 用明确人设（并写明 bot 在群里的称呼），并用**点名消息**（mention 概率稳定 >0.9）作为提名驱动；判断质量交给离线评估，不在管线 harness 里断言。

## 配置速查

`session_mode`(whitelist|blacklist) / `enabled_sessions`(白名单，UMO 列表或 `"*"`) / `disabled_sessions`(黑名单) / `api_key` / `model`(默认 jev-latest) / `jev_provider_url`(默认官方) / `persona`(text) / `questions`(text=JSON) / `nomination_rule`(text=JSON，空=内置) / `thresholds`(object+items) / `context_window`(15) / `other_bot_ids`(标注+引用判定) / `evaluate_other_bots`(false) / `skip_directed_messages`(true) / `cooldown_minutes`(30) / `cooldown_enabled`(**false**，v0.3.4 起默认关) / `followup_window_minutes`(10，**0=不限窗口**) / `followup_cooldown_seconds`(0) / `daily_limit`(5) / `max_daily_evaluations`(300) / `dry_run`(false) / `jev_timeout_seconds`(25) / `trigger_notice`（仅 `{reason}` 占位符）/ `pass_history_mode`(keep|hide) / `refusal_fuzzy_match`(false)

## 发布要点（AstrBot 插件市场）

- 上架入口：https://cloud.astrbot.app/publish（需 AstrBot Cloud 账号），仓库必须是公开 GitHub。
- `metadata.yaml` 必填 `name/desc/version/author`（`_PluginUpdater.validate_plugin_metadata` 校验：非空字符串）；`name` 还要是合法 Python 标识符且裸目录名一致。
- 可选但已填：`display_name` / `short_desc` / `repo` / `astrbot_version`（PEP 440，**不带 v 前缀**）/ `support_platforms`（取值必须是 `ADAPTER_NAME_2_TYPE` 的键）/ `tags` / `social_link`。
- `logo.png`：1:1，官方建议 256×256（本仓库已生成）。
- 仓库 zip **≤ 16MB**，CI 会拒；别提交 `__pycache__`、`data/`、日志（`.gitignore` 已覆盖）。
- 无需 `requirements.txt`：本插件只用标准库 + AstrBot 自带 API。

判定质量与调参依据：369 条真实中文群聊消息的离线评估（Jev 直调 + 提名回放），加上此后数天的 dry-run 账本。换群/换人设/换语言都建议先 dry-run 采样再调阈值——单条评估约 $0.0001，采样成本可以忽略。
