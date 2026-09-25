# Jev 主动回复门控 (astrbot_plugin_jev_gate)

让 bot 更灵活地“插话”——用 TypeSafe [Jev](https://typesafe.ai) 模型给每条群消息打分，只在真的值得接话时才触发一次主动回复。

- **便宜**：单条消息约 `$0.0001`，比逐条问大模型便宜三个数量级
- **可控**：人设、判断问题、提名规则、阈值全可配
- **不越权**：只做"判断 + 触发"，人格/对话历史/分段/落库全部交给 AstrBot 主线，主动回复和普通回复走同一条管线
- 内置 dry-run 只审计模式：只打分写账本，确认提名质量后再开启实发
- 冷却时间（默认关，可开）、每日触发上限、每日评估上限三重护栏
- 每次被门控拦下都打 info 日志，带上配置字段与实测值，不用翻账本
- 触发时会把提名原因告知 bot，可自行拒绝（输出 [PASS]，拒答不消耗冷却和日限）
- 使用需要自备 TypeSafe API Key（https://console.typesafe.ai/keys）。

> **注意**：本插件只定义了"哪条消息值得接"，不生成回复内容。回复内容是主线 LLM 结合人设、对话历史、群历史自己写的；判断质量很大程度取决于你填的 `persona`。

---

## 它解决什么问题

群里一天几百条消息，真正值得 bot 主动接话的可能只有个位数。

- 全交给大模型判断：每条都要带上下文跑一次，贵且慢
- 用正则/关键词：不懂语义，"有人夸这个游戏吗"和"这个游戏做得一坨"分不开

Jev 是 System One 模型：不生成文字，只回答你定义好的结构化问题并给出概率（比如"这条消息是否在点名 bot" = 0.92）。它又快又便宜，正适合做高频门控，把值不值得接的判断前置，再让主线 LLM 只处理真正值得的那几条。

## 工作原理

```
真实群消息（priority=-2 旁路钩子，只观察不拦截）
  │
  ├─ Layer 0 代码规则（零成本零延迟）
  │    会话名单（白名单/黑名单，见 session_mode）/ @bot 检测（@了就走正常回复管线，不问 Jev）
  │    @指向别处不抢答（@其他人/引用其他bot/@全体，可关）
  │    每日评估上限
  │
  └─ Layer 1 Jev 提名（约 $0.0001/条）
       输入：人设 + 最近 N 条群聊 + 这条消息
       6 个问题：5 个 noul（0-1 概率）+ 1 个 reply_value（0-3 分级）
       内置提名条件（nomination_rule 留空时，阈值取 thresholds）：
         is_mentioned ≥ 0.6
         或 reply_value ≥ 1.5 且 is_good_moment > 0.45
       填了 nomination_rule 就按自定义布尔表达式求值
         │
         ▼
  Layer 2 触发（通过每日上限，和可选的冷却之后）
       yield event.request_llm(prompt=这条消息, conversation=该会话当前对话)
         │
         ▼
       主线正常链路：人格 / 对话历史 / 群历史 / 分段 / 落库 / 占位符 / 出站守卫
       触发提醒通过 extra_user_content_parts + mark_as_temp 下发，只对本次请求可见、不写进历史
       bot 若判断不值得回，输出 [PASS] 即可放弃（出站守卫会清空这条消息，并退还冷却/日限）
```

`dry_run` 开启时提名只写账本，不触发。

## 安装

1. 安装插件，二选一：
   - **插件市场**：AstrBot WebUI → 插件 → 插件市场，搜索 **Jev 主动回复门控**（上架后可用）
   - **从仓库装**：WebUI → 插件 → 安装插件 → 填本仓库地址 `https://github.com/Sodapopper-pixel/astrbot_plugin_jev_gate`（依赖 git，需要装 git 客户端）
2. 在 [console.typesafe.ai/keys](https://console.typesafe.ai/keys) 申请 TypeSafe API Key
3. 重载插件

要求 AstrBot `>= 4.25.0`（依赖 `mark_as_temp` 让触发提醒不写进对话历史）。

## 快速上手

1. **填会话**：默认是白名单模式，`enabled_sessions` 填要监控的会话 UMO，例如 `QQ:GroupMessage:123456789`。
   多个会话写成列表；填 `"*"` 表示所有会话（先配合 dry-run 观察很省事）。
   只想屏蔽个别群、其余全开：把 `session_mode` 改成 `blacklist`，在 `disabled_sessions` 里填要排除的会话。
   - UMO 在 AstrBot 日志里能直接看到，也可以从 WebUI 对话列表 / `data/config` 里的会话标识确认
2. **填 Key 和人设**：`api_key`；`persona` 写清 bot 在群里的**名字/常见称呼、性格、常聊话题**——
   Jev 靠这段判断"这条消息是不是在说它"，写得越具体越准（详见下面的「人设怎么写」）
3. **先开 `dry_run: true` 跑 1-3 天**，看账本里提名了哪些消息、理由是什么
4. 觉得靠谱了，把 `dry_run` 改成 `false`，bot 就会开始按提名主动接话

日志里会有一行启动自检，确认配置是否到位：

```
[JevGate] 已加载 | 会话模式: whitelist | 白名单: QQ:GroupMessage:123456789 | 黑名单: （空） | dry_run: True | api_key: 已配置 | 模型: jev-latest | Jev: https://api.typesafe.ai/v1/systemone
```

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `session_mode` | `whitelist` | 会话名单模式：`whitelist` 只跑白名单里的会话；`blacklist` 跑除黑名单外的所有会话 |
| `enabled_sessions` | `[]` | 白名单：启用的会话 (UMO)，支持 `"*"` |
| `disabled_sessions` | `[]` | 黑名单：不评估的会话 (UMO)，仅 blacklist 模式生效，`"*"` = 全禁 |
| `api_key` | 空 | TypeSafe API Key（明文存在插件配置里，只在本机使用） |
| `model` | `jev-latest` | Jev 模型 ID，默认跟随官方最新版。alias 会漂移而阈值按版本调，质量变化时先钉版本号（如 `jev-1.13.0`）再对比账本 |
| `jev_provider_url` | 官方端点 | Jev API 地址，可填自建代理/中转（须兼容 `/v1/systemone`），留空回退官方 |
| `persona` | 空 | bot 人设，发给 Jev 当背景。留空用内置占位文案 |
| `questions` | 见下 | 评估问题定义（JSON）。可改文案、可加新问题 |
| `nomination_rule` | 空（内置规则） | 提名规则（JSON 布尔表达式）。**questions 里加的新问题要靠它才能参与提名**，详见下面「自定义提名规则」 |
| `thresholds` | 见下 | 提名阈值（键=问题 ID）。只驱动内置提名规则；填了 `nomination_rule` 后数字写在规则里 |
| `context_window` | `15` | 评估时附带的历史消息条数，越大越准也越贵 |
| `other_bot_ids` | `[]` | **防止 bot 之间刷屏**：群里其他机器人的账号。作用是提醒 Jev 注意这些账号是机器人——上下文里它们的发言标注为「机器人」，引用它们的消息也不会抢答 |
| `evaluate_other_bots` | `false` | **直接无视**其他 bot 的发言：不评估、不触发，但上下文里仍标注为机器人供 Jev 看全场。`true` = 照常评估（可能接 bot 的话，慎用） |
| `skip_directed_messages` | `true` | @ 指向别人的消息不接话：消息 @ 了任何其他账号（含 @全体）、或引用了其他 bot 的消息时跳过。关掉则交还 Jev 评估（可能抢答） |
| `cooldown_minutes` | `30` | 两次主动触发的最小间隔（对"接着聊"无效，见下一行）。仅在 `cooldown_enabled=true` 时生效 |
| `cooldown_enabled` | `false` | 冷却总开关。**默认关**：提名通过日限就直接触发，连续对话不会再被冷却墙拦。开启后仅约束主动触发频率，@点名回复不受影响；真正的上限是 `daily_limit`（默认 5 次/会话/天）。`cooldown_minutes=0` 等效关闭 |
| `followup_window_minutes` | `10` | 连续对话判定窗口：bot 刚回复过的发送者，在此窗口内继续说话算"接着聊"，走 `followup_cooldown_seconds` 短冷却。**`0` = 不限窗口**（同一发送者永远算接着聊，不是"窗口 0 秒"——后者会反被长冷却拦死） |
| `followup_cooldown_seconds` | `0` | "接着聊"时的短冷却。`0` = 窗口内同一发送者的提名直接放行；设为 60 之类可防回复还在生成时用户连发导致连回两条 |
| `daily_limit` | `5` | 每个会话每天最多触发几次（拒答不计数，会退还） |
| `max_daily_evaluations` | `300` | 每个会话每天最多评估几条（纯成本护栏，约 $0.03/天） |
| `dry_run` | `false` | 只审计模式：开启后只打分写账本、不触发回复。建议先用它观察提名质量，再关闭实发 |
| `trigger_notice` | 见下 | 触发时随请求发给 bot 的提醒，占位符 `{reason}` |
| `pass_history_mode` | `keep` | 拒答 `[PASS]` 是否留在历史里。`keep`：照常落库，模型之后能看见自己曾经拒答过；`hide`：守卫清空时把刚落库的拒答记录从群消息历史和会话历史里删掉 |
| `refusal_fuzzy_match` | `false` | 拒答宽松匹配。开启后本插件触发轮改为包含匹配（整条里出现 token 就算拒答）；普通回复始终精确匹配 |
| `jev_timeout_seconds` | `25` | 单次评估超时，超时放弃本条 |

### 人设怎么写

人设是判断质量的最大变量。建议写全三件事：

1. **它叫什么**：名字、群里常用称呼/昵称、QQ 号（用 `{self_id}` 占位符自动填）
2. **它是什么**：性格、说话风格、擅长什么
3. **它不接什么**：明确写出与它无关的话题类型（例如"炒股、八卦、复读不属于它的接话范围"）

只写第 1、2 条也能跑，但 Jev 缺少边界判断，容易把"没人回答的问题"都当成值得接的话，提名会偏多。留空人设时插件会在启动日志里给出警告。

### 自定义判断问题

`questions` 是一段 JSON，键是问题 ID，值是 `{type, instructions, criteria}`：

```json
{
  "is_mentioned": {
    "type": "noul",
    "instructions": "目标消息是否在点名 QQ {self_id} 或直接对它说话？",
    "criteria": {
      "true": "叫了它的名字或群里常用的称呼，或话头明显是对它说的",
      "false": "没有针对它：@其他机器人、@其他群友、群内普通对话都不算"
    }
  },
  "reply_value": {
    "type": "score",
    "instructions": "综合来看，机器人主动回复这条消息的价值有多大？",
    "criteria": ["不该接：表情、复读", "可接可不接", "值得接", "很适合接：点名搭话"]
  }
}
```

- `type`：`noul` = 是否判断，返回 0-1 概率；`score` = 分级判断，`criteria` 是有序数组（从低到高）
- `{self_id}` 会替换成当前 bot 账号
- 可以**新增**问题（例如 `"is_ask_for_help"`），新问题的结果会一并写进账本，方便你先观察再纳入提名条件
- 改动问题等于改动判定语义，改完建议重新 dry-run 一批数据对比
- **新问题默认只进账本、不参与提名**——要让它影响是否触发，见下面「自定义提名规则」

### 自定义提名规则

`questions` 里加的新问题，它的值会写进账本，但**默认不参与提名**：内置提名条件是写死的两条路径，只读 `is_mentioned`/`reply_value`/`is_good_moment`。想让新问题（或任何组合）真正影响是否触发，就填 `nomination_rule`——一段 JSON 布尔表达式。

三种节点，可任意嵌套：

| 节点 | 含义 |
|------|------|
| `{"any": [子句, ...]}` | 任一子句成立即提名 |
| `{"all": [子句, ...]}` | 全部子句成立才提名 |
| `{"<问题ID>": {"<运算符>": 数字}}` | 拿该问题的值和数字比较 |

运算符：`>=` `>` `<=` `<` `==`。

**与内置规则等价的写法：**

```json
{
  "any": [
    {"is_mentioned": {">=": 0.6}},
    {"all": [
      {"reply_value": {">=": 1.5}},
      {"is_good_moment": {">": 0.45}}
    ]}
  ]
}
```

**让自定义问题参与提名的例子**（`questions` 里加了 `is_ask_for_help`）：

```json
{
  "any": [
    {"is_ask_for_help": {">=": 0.7}},
    {"is_mentioned": {">=": 0.6}}
  ]
}
```

- 问题 ID 可以是 `questions` 里任意 `noul` 问题或 `reply_value`
- 留空 = 用内置规则（阈值读 `thresholds`）；填了规则后数字写在规则里，`thresholds` 不再参与
- 规则引用了一个 `questions` 里不存在的问题 ID：该条件永远不成立，启动时会告警
- **写错不会静默失效**：坏 JSON 或结构不合法会自动回退内置规则并在日志告警；回退后行为和你没填规则时完全一致
- 命中的原因会写进账本 `reason` 字段（如 `is_ask_for_help(0.82>=0.7)`），方便回查是哪条子句放行的

## 账本与调优

账本位于 `data/plugin_data/jev_gate/ledger_YYYYMMDD.jsonl`，每条评估一行 JSON：

| 字段 | 含义 |
|---|---|
| `kind` | `evaluated`（评估过）/ `mention_code_skip`（@bot，代码层跳过）/ `directed_skip`（@指向别处，跳过）/ `error` |
| `nouls` | 各 noul 问题的原始概率 |
| `reply_value` | 分值 / 各级概率 / 置信度 |
| `nominated` / `reason` | 是否提名、按哪条规则提名 |
| `trigger` | `dry_run`（只记录）/ `skip`（冷却或日限，附 `elapsed_s`/`cooldown_s`/`used` 实测值）/ `dispatch`（已触发） |
| `usage` / `latency_s` / `model` | token 用量、耗时、模型版本 |

调优建议：

- **提名太多**：先看是不是人设太泛（补"不接什么"），再考虑把 `thresholds.reply_value` 从 1.5 提到 1.8-2.0
- **提名太少**：先确认 `is_mentioned` 在真实点名消息上的概率（通常 >0.9），若正常就适当下调 `thresholds.is_mentioned` 或 `thresholds.reply_value`（填了 `nomination_rule` 时改规则里的数字）
- **反复接同一话题**：把 `cooldown_enabled` 打开再调大 `cooldown_minutes`（注意它管不了"接着聊"——那种场景本来就该接）
- **喊完名字接着聊却被冷场**：默认冷却就是关的，不该出现；若你开了冷却，确认 `followup_window_minutes` 够长（默认 10 分钟），或直接设 `0` = 不限窗口
- **成本**：`usage` 字段可以直接累加算出真实花费；`max_daily_evaluations` 是硬上限

### 门控命中日志

每次被门控拦下都会打一条 **info** 日志（AstrBot 日志里搜 `[JevGate] 跳过`），带上**命中哪个门控、相关配置字段与实测值**，不用翻账本也知道为什么没回：

```
[JevGate] 跳过触发(cooldown): 空气: 这玩意咋整 | reason=value(2.00)+moment(0.60) followup=False elapsed_s=137 < cooldown_s=1800 | cooldown_enabled=True cooldown_minutes=30 followup_window_minutes=10 followup_cooldown_seconds=0
[JevGate] 跳过触发(daily_limit): 空气: 弥音在不 | reason=possible_mention(0.97) used=5 >= daily_limit=5 | date=2026-09-25
[JevGate] 跳过评估(directed_skip): 裁缝: [At:2384303423]看看这个 | aimed_at=2384303423 skip_directed_messages=True
[JevGate] 本会话今日评估已达上限, 停止评估: QQ:GroupMessage:756741478 | max_daily_evaluations=300 used=300 date=2026-09-25
```

- `cooldown` 行给出 `elapsed_s`（距上次触发过了多久）和实际生效的 `cooldown_s`，以及四个冷却配置的当前值——`followup=True` 说明走了连续对话短冷却
- `daily_limit` 行给出当日已用次数和上限
- 日评估上限命中后每条消息都会走到那个分支，所以它的日志**每个会话每天只打一次**
- @bot 点名和其他 bot 发言属于预期行为，只写账本不打日志，避免刷屏

## 常见问题

**装了没反应？**
依次检查：`enabled_sessions` 是否填对（UMO 必须完全一致）→ `api_key` 是否填了 → 插件是否已重载 → 看 `ledger_*.jsonl` 里有没有 `evaluated` 记录。启动日志那行自检会直接告诉你缺哪项。

**dry_run 关了还是没主动发言？**
先在日志里搜 `[JevGate] 跳过`——冷却、日限、@指向别处、日评估上限每次命中都会打 info 日志并带上配置值与实测值。也可以看账本里 `nominated: true` 的条目有没有 `trigger: skip`（`detail: cooldown` 带 `followup` 标记区分长短冷却，`daily_limit` 是日限）。另外 qq_official 平台的群主动消息会被平台拒绝（见下）。

**主动回复没进对话历史 / 结尾带句号？**
本插件不会直发消息，触发一律走主线管线，所以分段、落库、占位符都由 AstrBot 负责。如果你看到没走管线的回复，说明配置里还有别的插件在用 `context.send_message()` 直发。

**bot 拒绝了怎么办？**
这是设计的一部分：触发提醒会告诉 bot 提名原因，bot 结合完整上下文后可以输出 `[PASS]` 放弃（消息会被出站守卫清空，冷却和日限也会退还）。插件只是"提醒它有这么个机会"，最终决定权仍在 bot。拒答记录默认照常写进历史（模型之后能看见自己曾经拒答过）；不想要这个效果就把 `pass_history_mode` 设成 `hide`。

**能换别的判断模型吗？**
目前只支持 TypeSafe Jev 的接口格式（`/v1/systemone`）。`model` 字段可以换成别的 Jev 版本，但阈值可能需要重新调。

## 已知限制

1. **实发仅限 aiocqhttp / OneBot 平台的群**：qq_official 平台的群主动消息会被平台拒绝（错误码 40034105），频道另有每日额度限制，需要改走被动回复路径（未实现）。
2. **评估在消息管线内同步进行**：Jev 实测中位 1-2 秒，长尾偶发十几秒，`jev_timeout_seconds` 到点放弃并记 `error`；@bot 的消息在代码层提前返回，不受影响。
3. **热重载会清空内存状态**：消息缓冲、冷却时间、每日计数都在实例内存里（账本不受影响）。
4. **判定质量与数据强相关**：默认阈值来自 369 条真实中文群聊消息的离线评估。换群、换人设、换语言建议先 dry-run 跑几天看账本再调阈值。
5. **模型对 CJK 文本的准确率官方标注偏低**：机械性判断（是否点名、是否是问题）实测可用，但语义更细的问题建议先用自己的数据 dry-run 验证。

## 成本

单次评估的输入约为人设 + 15 条上下文，实测约 `$0.0001/条`。按每天 300 条评估算，一个月约 `$0.9`。`max_daily_evaluations` 限制单会话每日上限，避免异常刷屏导致失控。

## 开发与测试

```bash
# 语法/导入检查（用 AstrBot 自带 python，能验证真实依赖可导入）
D:/AstrBot/backend/python/python.exe -m py_compile main.py

# 插件在 AstrBot 内热重载：WebUI → 插件 → 重载
```

离线测试（mock astrbot，不打真实 API，位于仓库外的 `jeval/` 目录）：

```bash
cd jeval
D:/AstrBot/backend/python/python.exe test_plugin_harness.py      # 全链路：配置/白名单/上下文/提名/冷却/守卫
D:/AstrBot/backend/python/python.exe test_v031_fixes.py          # 修复项：@指向/冷却/拒答守卫/历史清理/门控日志
D:/AstrBot/backend/python/python.exe test_nomination_rule.py     # 提名规则引擎：等价性/回退/运算符

# test_plugin_harness 会打真实 Jev API，需带 key：
TYPESAFE_API_KEY=xxx D:/AstrBot/backend/python/python.exe test_plugin_harness.py
```

插件结构与数据流（供二次开发参考）：

```
main.py
├── on_group_message               生成器 handler（priority=-2）：判断；提名时 yield request_llm
├── _call_jev / _call_jev_sync     Jev HTTP 调用（线程池 + 超时 + 429/529 退避重试，URL 可配）
├── _nominate                      提名：rule=None 走内置两条路径，否则按 nomination_rule 求值
├── _validate_rule / _eval_rule    规则校验与求值（运算符白名单，不用 eval）
├── _current_conversation          取/建会话当前对话（不带 conversation 主线不落库）
├── on_proactive_refusal_guard     出站守卫：拒答 token 清空 + 退还冷却/日限 + 可选清理历史
└── _build_state                   拼 Jev 的 state（人设 + 上下文 + 目标消息）
```

## 许可证

MIT

## 相关资源

- **Jev 实践参考**：TypeSafe 官方 Agent Skill [`typesafe-ai/skills`](https://github.com/typesafe-ai/skills)（MIT）——Noul/Choice/Score 原语、state/instructions/criteria 规范写法、并行提问与阈值设计模式。本插件的 `questions` 设计遵循它；安装：`npx skills add typesafe-ai/skills --skill typesafe-ai`
- 官方文档（唯一事实源）：https://docs.typesafe.ai
