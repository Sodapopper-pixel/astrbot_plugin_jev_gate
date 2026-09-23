# AGENTS — astrbot_plugin_jev_gate

Jev 主动回复门控插件（v0.3.0 判断+触发，已发布到 AstrBot 插件市场）。Jev 给群聊消息打分，提名值得主动回复的消息后**在管线内 yield request_llm 触发**——插件不组装 LLM 上下文、不直发消息。

- 仓库：https://github.com/Sodapopper-pixel/astrbot_plugin_jev_gate
- 作者：terk ｜ 许可：MIT ｜ `astrbot_version: ">=4.25.0"`（依赖 `TextPart.mark_as_temp`）

## 架构要点（v0.3.0）

- **一层钩子**：`@filter.event_message_type(GROUP_MESSAGE, priority=500)` 的 **生成器 handler**（`on_group_message`）。不提名时**零 yield 直接返回**（经 `call_handler` 透传，消息后续命运不受影响）；提名通过冷却/日限后 `yield event.request_llm(...)` **一次**——返回值 ProviderRequest 由 `ProcessStage` 交给 Agent 子阶段执行，人格/对话历史/群历史/分段/落库/占位符/出站守卫全部走主线。
- **绝不 set_result / stop_event / `context.send_message()`**：后者绕过主线全部后处理（分段、群历史落库、占位符匹配、出站守卫），2026-09-23 生产实锤（回复末尾带"。"、对话数据查不到）。
- **⚠️ `conversation=` 不是可选项（2026-09-23 生产实锤）**：主线只在 `req.conversation` 存在时才 (a) 注入人格/skills（`_ensure_persona_and_skills` 开头 `if not req.conversation: return`，astr_main_agent.py:536）、(b) 给 `req.contexts` 填对话历史（:1436-1442）、(c) 把本轮写回对话历史（`_save_to_history` 同款早退，internal.py:459-461）。**症状**：回复能正常发出（走管线能分段），但无 persona、上下文为空、`conversations.updated_at` 永远停在触发前——"日志里发了、对话数据里没有"。取不到就 `new_conversation` 新建（与主线 `_get_session_conv` 同口径），新建失败才跳过并记 `trigger=skip/no_conversation`。group_chat_plus main.py:10989 有同一踩坑注释。
- **触发提醒不落库**：`TextPart(提醒).mark_as_temp()` 挂 `req.extra_user_content_parts`——本次请求可见，落库时被 `dump_messages_with_checkpoints` 按 `part._no_save` 过滤（message.py:354），prompt 保持干净的目标消息文本。老版本无 TextPart 时退化为拼进 prompt（会落库）——这也是 `astrbot_version >= 4.25` 的来源。
- **拒答守卫**：`@filter.on_decorating_result(priority=99990)`，只处理带 `_JEVGATE_TRIGGER_MARK` 的触发轮；整条恰好等于 `[PASS]`/`[pass]`/`[不回复]` 才 clear（精确白名单，`[笑死]` 不误伤）。这是第二道——sleep_mode 的 99999 守卫按 `no_reply_placeholder` 也会匹配。
- **Layer 0 代码规则**：会话白名单（支持 `"*"` 通配）/ @bot 检测（`_is_at_bot` 命中直接跳过，@ 检测代码规则 100% 可靠，评估实测 Jev 有一条直接 @ 只给 0.69）/ 日评估上限 / 冷却 / 日触发上限。
- **提名条件**（`_nominate`，评估实测）：`is_mentioned >= 0.6` 或 `reply_value >= 1.5 且 good_moment > 0.45`。`is_unanswered` 对非问题消息是噪音（全量 mean 0.63），只记账不参与提名。
- **Jev 调用超时**：`_call_jev` 外层 `asyncio.wait_for(jev_timeout_seconds=25)`。Jev 实测 p50 1.2s / p90 7s / 极端 191s（重试睡 1+2+4+8s + 4×60s timeout）。调用本身在生成器 handler 内同步 await（评估完才决定 yield 与否），@bot 消息在代码层提前返回、不受影响。
- **v0.1.x 的终审层/直发/`_is_refusal`/`_build_reply_chain`/合成事件/后台任务/`speech log 读取`/`llm_context_window`/`_is_group_sleeping` 全部已删除**——`[At:...]` 由主线发送链解析，休眠由 sleep_mode 入站(1)+出站(99999)覆盖，拒答由 99990+99999 两守卫覆盖。"影子实现"是 09-23 两轮排查的结论。

## 关键状态

- `self._sessions[umo]`：`buffer`(deque, maxlen = context_window+5，供 Jev 看全场) + asyncio.Lock（**只保护计数器**，Jev HTTP 不持锁）+ daily/nominated 计数 + last_nominate。**热重载丢缓冲与计数**（账本不丢）。
- `self._persona_cache[self_id]` / `_questions_for`：`{self_id}` 占位符按事件替换（一个实例服务多平台）。
- 账本：`StarTools.get_data_dir("jev_gate")/ledger_YYYYMMDD.jsonl`（即 `data/plugin_data/jev_gate/`）。`trigger` 字段：`dry_run` / `skip(cooldown|daily_limit|no_conversation)` / `dispatch`（带 `conversation` 前缀便于和 DB 对账）。
- `initialize()`：启动自检，打一行「会话/dry_run/api_key/model」状态，并在 `enabled_sessions` 为空、`persona` 未配置、`questions` 缺关键 ID 时告警。"装了没反应"先看这行。

## 配置层（v0.3.0 改版，面向市场用户）

- **`questions` 是 `text`（JSON 字符串）而不是 object/dict**：`text` 是**叶子值**，配置完整性检查（`check_config_integrity`）不会把它递归裁键 —— 所以用户**能自行新增问题 ID**；而 `object`/`dict` 会被 refer 结构逐键比对，多余键在加载时被静默删除（实测）。代码侧 `_json_cfg` 同时接受 dict（老配置/手改配置文件）与 JSON 字符串，坏 JSON 只告警并回退默认。
- **`thresholds` 是 `object` + `items`**：键集固定正是想要的（UI 里逐项数字输入），缺项由 items 的 `default` 补齐；代码侧再做一层 `float()` 容错。
- **`dict` 类型不要用**：v4.27.0 才加入 `DEFAULT_VALUE_MAP`（4.26 及以前会直接 `TypeError: 不受支持的配置类型`）；`object` 在 4.17 就有。schema 里 `object` **必须带 `items`**，否则 `KeyError: 'items'` 让插件加载即崩。
- **默认值通用化**：内置 persona 是「一个技术性占位人设」，不含任何具体 bot 的名字/人设；`questions`/`trigger_notice` 同样去掉环境特有称呼。**通用 persona 会让 Jev 缺少边界、提名偏多**（实测：同一批闲聊消息，具体人设 0 提名、通用人设 2-3 提名）——所以启动日志会对未配置 persona 告警，README 把「人设怎么写」列为判断质量的第一要素。

## 踩坑记录

- **Jev 的 state 不是单条消息**：`recent_conversation`（context_window 条，排除目标消息本身）+ `bot_persona` + `target_message`。is_unanswered 的前提来自这个窗口，窗口外的更早回答看不见。
- **@检测初版把"@其他机器人"误判成点名**（0.94）：必须在 persona 里钉死 QQ 号+名字、criteria 里写"@其他机器人不算"。多 bot 群必配 `other_bot_ids`。
- **暗指名（"昨天哪个AI在刷屏"）不能自动回**：Jev 给 0.4-0.6 中间值，误发=群里社死。提名后走主线问一嘴，bot 在自有上下文里判断是不是说自己。
- **人格解析必须取 per-UMO 配置（2026-09-22 生产排查实锤）**：`resolve_selected_persona` 第三级回退读 `provider_settings.default_personality`，而它**按 UMO 路由**——本机 `umop_config_routing` 把 `QQ:*:*` 路由到「群聊bot」档案；`cmd_config.json` 顶层的 `default_personality` 对 QQ 会话**不生效**。v0.2 起人格由主线装配（前提是 conversation 挂上了），插件不碰。
- **`provider_ltm_settings.group_message_history_enable` 关闭时主线无群上下文**：降级靠对话历史，设计内，不是 bug。
- **model 钉版本号**（jev-1.13.0）不用 jev-latest：阈值按版本调，alias 会漂移；账本记 `model` 字段。
- **`persona` 是 text 型且 schema 默认 `""`，`_cfg(key, default)` 对空字符串不兜底**：必须 `or DEFAULT_PERSONA`（harness 断言看住）。
- StarTools 导入：`astrbot.core.star.star_tools`，老版本回退 `astrbot.api.star`；`get_data_dir(plugin_name)` 返回 `data/plugin_data/{plugin_name}`。
- **导入路径以真实安装为准，别信 mock**：`MessageChain` 在 `astrbot.api.event`，**不在** `astrbot.api.message_components`。自测 stub 骗过 py_compile，最终用 bundled python 真实导入核验。
- **加载链路验证姿势**：`importlib.util.spec_from_file_location('data.plugins.<name>.main', <main.py 路径>)` + `exec_module`，在 bundled python（`D:\AstrBot\backend\python\python.exe`）下跑，等于模拟 star_manager 的加载。
- **mock 桩要与真实事件读面对齐**：harness 曾因 MockEvent 缺 `get_platform_id`/`session_id` 而让插件走进 no_conversation/异常分支——桩缺字段会伪装成"插件有 bug"，先补桩再怀疑代码。
- **测试断言不要绑人设**：harness 的提名断言一度依赖内置 persona，换成通用人设就红。正确做法：fixture 用明确人设（并写明 bot 在群里的称呼），并用**点名消息**（mention 概率稳定 >0.9）作为提名驱动；判断质量交给离线评估，不在管线 harness 里断言。

## 配置速查

`enabled_sessions`(UMO 列表或 `"*"`，必填) / `api_key` / `model` / `persona`(text) / `questions`(text=JSON) / `thresholds`(object+items) / `context_window`(15) / `other_bot_ids` / `evaluate_other_bots`(false) / `cooldown_minutes`(30) / `daily_limit`(5) / `max_daily_evaluations`(300) / `dry_run`(true) / `jev_timeout_seconds`(25) / `trigger_notice`（仅 `{reason}` 占位符）

## 发布要点（AstrBot 插件市场）

- 上架入口：https://cloud.astrbot.app/publish（需 AstrBot Cloud 账号），仓库必须是公开 GitHub。
- `metadata.yaml` 必填 `name/desc/version/author`（`_PluginUpdater.validate_plugin_metadata` 校验：非空字符串）；`name` 还要是合法 Python 标识符且裸目录名一致。
- 可选但已填：`display_name` / `short_desc` / `repo` / `astrbot_version`（PEP 440，**不带 v 前缀**）/ `support_platforms`（取值必须是 `ADAPTER_NAME_2_TYPE` 的键）/ `tags` / `social_link`。
- `logo.png`：1:1，官方建议 256×256（本仓库已生成）。
- 仓库 zip **≤ 16MB**，CI 会拒；别提交 `__pycache__`、`data/`、日志（`.gitignore` 已覆盖）。
- 无需 `requirements.txt`：本插件只用标准库 + AstrBot 自带 API。

判定质量与调参依据：369 条真实中文群聊消息的离线评估（Jev 直调 + 提名回放），加上此后数天的 dry-run 账本。换群/换人设/换语言都建议先 dry-run 采样再调阈值——单条评估约 $0.0001，采样成本可以忽略。
