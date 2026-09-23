# Jev 主动回复门控 (astrbot_plugin_jev_gate)

让 bot 学会**什么时候不该说话**——用 TypeSafe [Jev](https://typesafe.ai) 模型给每条群消息打分，只在真的值得接话时才触发一次主动回复。

- **便宜**：单条消息约 `$0.0001`，比逐条问大模型便宜三个数量级
- **可控**：人设、判断问题、提名阈值全可配，默认 dry-run 只记录不发言
- **不越权**：只做"判断 + 触发"，人格/对话历史/分段/落库全部交给 AstrBot 主线，主动回复和普通回复走同一条管线

> **注意**：本插件只定义了"哪条消息值得接"，不生成回复内容。回复内容是主线 LLM 结合人设、对话历史、群历史自己写的；判断质量很大程度取决于你填的 `persona`。

---

## 它解决什么问题

群里一天几百条消息，真正值得 bot 主动接话的可能只有个位数。

- 全交给大模型判断：每条都要带上下文跑一次，贵且慢
- 用正则/关键词：不懂语义，"有人夸这个游戏吗"和"这个游戏真烂"分不开

Jev 是 System One 模型：不生成文字，只回答你定义好的结构化问题并给出概率（比如"这条消息是否在点名 bot" = 0.92）。它又快又便宜，正适合做高频门控，把值不值得接的判断前置，再让主线 LLM 只处理真正值得的那几条。

## 工作原理

```
真实群消息（priority=500 旁路钩子，只观察不拦截）
  │
  ├─ Layer 0 代码规则（零成本零延迟）
  │    会话白名单 / @bot 检测（@了就走正常回复管线，不问 Jev）
  │    每日评估上限
  │
  └─ Layer 1 Jev 提名（约 $0.0001/条）
       输入：人设 + 最近 N 条群聊 + 这条消息
       6 个问题：5 个 noul（0-1 概率）+ 1 个 reply_value（0-3 分级）
       提名条件：is_mentioned ≥ 0.6
              或 reply_value ≥ 1.5 且 is_good_moment > 0.45
         │
         ▼
  Layer 2 触发（通过冷却与每日上限后）
       yield event.request_llm(prompt=这条消息, conversation=该会话当前对话)
         │
         ▼
       主线正常链路：人格 / 对话历史 / 群历史 / 分段 / 落库 / 占位符 / 出站守卫
       触发提醒通过 extra_user_content_parts + mark_as_temp 下发，只对本次请求可见、不写进历史
       bot 若判断不值得回，输出 [PASS] 即可放弃（出站守卫会清空这条消息）
```

`dry_run`（默认开启）下提名只写账本，不触发。

## 安装

1. 在 AstrBot 插件市场搜索 **Jev 主动回复门控** 安装（或在 WebUI 插件页上传本仓库 zip）
2. 在 [console.typesafe.ai/keys](https://console.typesafe.ai/keys) 申请 TypeSafe API Key
3. 重载插件

要求 AstrBot `>= 4.25.0`（依赖 `mark_as_temp` 让触发提醒不写进对话历史）。

## 快速上手

1. **填会话**：`enabled_sessions` 填要监控的会话 UMO，例如 `QQ:GroupMessage:123456789`。
   多个会话写成列表；填 `"*"` 表示所有会话（先配合 dry-run 观察很省事）。
   - UMO 在 AstrBot 日志里能直接看到，也可以从 WebUI 对话列表 / `data/config` 里的会话标识确认
2. **填 Key 和人设**：`api_key`；`persona` 写清 bot 在群里的**名字/常见称呼、性格、常聊话题**——
   Jev 靠这段判断"这条消息是不是在说它"，写得越具体越准（详见下面的「人设怎么写」）
3. **保持 `dry_run: true` 跑 1-3 天**，看账本里提名了哪些消息、理由是什么
4. 觉得靠谱了，把 `dry_run` 改成 `false`，bot 就会开始按提名主动接话

日志里会有一行启动自检，确认配置是否到位：

```
[JevGate] 已加载 | 会话: QQ:GroupMessage:123456789 | dry_run: True | api_key: 已配置 | 模型: jev-1.13.0
```

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled_sessions` | `[]` | 生效会话 UMO 列表，支持 `"*"` 通配。**留空则完全不工作** |
| `api_key` | 空 | TypeSafe API Key（明文存在插件配置里，只在本机使用） |
| `model` | `jev-1.13.0` | Jev 模型 ID。建议钉版本号：阈值是按具体版本调的 |
| `persona` | 空 | bot 人设，发给 Jev 当背景。留空用内置占位文案 |
| `questions` | 见下 | 评估问题定义（JSON）。可改文案、可加新问题 |
| `thresholds` | 见下 | 提名阈值，逐项可调 |
| `context_window` | `15` | 评估时附带的历史消息条数，越大越准也越贵 |
| `other_bot_ids` | `[]` | 群里其他机器人的账号，用于在上下文里标注"这条是别的 bot 说的" |
| `evaluate_other_bots` | `false` | 是否也评估其他机器人发的消息 |
| `cooldown_minutes` | `30` | 两次主动触发的最小间隔 |
| `daily_limit` | `5` | 每个会话每天最多触发几次 |
| `max_daily_evaluations` | `300` | 每个会话每天最多评估几条（纯成本护栏，约 $0.03/天） |
| `dry_run` | `true` | 只打分记录、不触发 |
| `trigger_notice` | 见下 | 触发时随请求发给 bot 的提醒，占位符 `{reason}` |
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
- 提名条件硬依赖 `is_mentioned`、`is_good_moment`、`reply_value` 三个 ID；缺了插件会在日志里警告并且不会触发
- 改动问题等于改动判定语义，改完建议重新 dry-run 一批数据对比

## 账本与调优

账本位于 `data/plugin_data/jev_gate/ledger_YYYYMMDD.jsonl`，每条评估一行 JSON：

| 字段 | 含义 |
|---|---|
| `kind` | `evaluated`（评估过）/ `mention_code_skip`（@bot，代码层跳过）/ `error` |
| `nouls` | 各 noul 问题的原始概率 |
| `reply_value` | 分值 / 各级概率 / 置信度 |
| `nominated` / `reason` | 是否提名、按哪条规则提名 |
| `trigger` | `dry_run`（只记录）/ `skip`（冷却或日限）/ `dispatch`（已触发） |
| `usage` / `latency_s` / `model` | token 用量、耗时、模型版本 |

调优建议：

- **提名太多**：先看是不是人设太泛（补"不接什么"），再考虑把 `thresholds.reply_value` 从 1.5 提到 1.8-2.0
- **提名太少**：先确认 `is_mentioned` 在真实点名消息上的概率（通常 >0.9），若正常就适当下调 `mention` 或 `reply_value`
- **反复接同一话题**：调大 `cooldown_minutes`
- **成本**：`usage` 字段可以直接累加算出真实花费；`max_daily_evaluations` 是硬上限

## 常见问题

**装了没反应？**
依次检查：`enabled_sessions` 是否填对（UMO 必须完全一致）→ `api_key` 是否填了 → 插件是否已重载 → 看 `ledger_*.jsonl` 里有没有 `evaluated` 记录。启动日志那行自检会直接告诉你缺哪项。

**dry_run 关了还是没主动发言？**
看账本里 `nominated: true` 的条目有没有 `trigger: skip`——冷却时间和 `daily_limit` 都会拦下触发。另外 qq_official 平台的群主动消息会被平台拒绝（见下）。

**主动回复没进对话历史 / 结尾带句号？**
本插件不会直发消息，触发一律走主线管线，所以分段、落库、占位符都由 AstrBot 负责。如果你看到没走管线的回复，说明配置里还有别的插件在用 `context.send_message()` 直发。

**bot 拒绝了怎么办？**
这是设计的一部分：触发提醒会告诉 bot 提名原因，bot 结合完整上下文后可以输出 `[PASS]` 放弃（消息会被出站守卫清空）。插件只是"提醒它有这么个机会"，最终决定权仍在 bot。

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
# 语法/导入检查
python -m py_compile main.py

# 插件在 AstrBot 内热重载：WebUI → 插件 → 重载
```

插件结构与数据流（供二次开发参考）：

```
main.py
├── on_group_message               生成器 handler：判断；提名时 yield request_llm
├── _call_jev / _call_jev_sync     Jev HTTP 调用（线程池 + 超时 + 429/529 退避重试）
├── _nominate                      提名条件（mention 或 value+moment）
├── _current_conversation          取/建会话当前对话（不带 conversation 主线不落库）
├── on_proactive_refusal_guard     出站守卫：整条恰好是拒答 token 才清空
└── _build_state                   拼 Jev 的 state（人设 + 上下文 + 目标消息）
```

## 许可证

MIT
