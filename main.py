"""Jev 主动回复门控插件 (v0.3.0)

职责边界: 本插件只做**判断与触发**, 不组装 LLM 上下文、不直接发送消息。

  Layer 0  代码规则: 会话白名单 / @bot 检测 / @指向别处不抢答 / 日评估上限
  Layer 1  Jev 提名:  对非点名消息打分 (多个 Noul 概率 + reply_value),
                      按阈值产出"值得主动回复"的提名及原因
  Layer 2  触发:      提名通过冷却/日限后, yield event.request_llm(
                      prompt=目标消息, conversation=该会话当前对话)。
                      **conversation 必须挂**——主线只有在 req.conversation
                      存在时才注入人格/skills、填对话上下文、并把本轮写回
                      对话历史; 不带 = 无persona 且永不落库(生产实锤)。
                      触发提醒走 extra_user_content_parts + mark_as_temp,
                      只对本次请求可见、不污染历史。
                      人格/对话历史/群历史/分段/落库/占位符/出站守卫
                      全部走主线正常链路。不想回的信号是 prompt 内约定的
                      [PASS], 由本插件 99990 守卫或主线出站守卫清空。

@指向规则(skip_directed_messages): 消息 @ 了任何其他账号(含@全体)、或引用
了其他 bot 的消息时, 视为"别人点的对话", 不评估不触发——Jev 的 is_mentioned
分得清"不是说我", 但 value 提名路径不看指向, 所以这活只能代码规则干。

冷却**默认关闭**(cooldown_enabled=false, cooldown_minutes=0 等效); 开启时
同一发送者在 followup 窗口内接着聊走 followup_cooldown_seconds 短冷却(默认
0 = 直接放行), 不影响对其他人插话的长冷却。⚠️ followup_window_minutes=0 的
含义是"**不限窗口**"(同一发送者永远算接着聊), 不是"窗口 0 秒"——后者会让
is_followup 永假, 本想放宽限制反被 30 分钟长冷却拦死(2026-09-25 实测踩坑)。
bot 输出 [PASS] 拒答时, 冷却与日限会被退还(99990 守卫里回退), "触发了但
没说话"不再隐形消耗额度。

拒答历史(pass_history_mode): keep(默认)= [PASS] 照常落库, 模型之后能看见
自己曾经拒答过; hide = 守卫清空时把刚落库的拒答记录从群消息历史和会话历史
里删掉, 模型完全不知道自己"曾经"[PASS]过。匹配精度由 refusal_fuzzy_match
控制(默认精确相等, 开启后触发轮包含匹配即算拒答)。

提名规则(nomination_rule): 留空 = 内置两条路径(阈值取 thresholds); 填了
JSON 布尔表达式就按规则求值, questions 里自定义的问题也能参与提名——
{"any":[{"is_mentioned":{">=":0.6}},{"all":[...]}]}。坏 JSON/结构不合法都
回退内置规则并告警, 绝不静默不提名。规则引用的问题 ID 会与 questions 交叉
检查, 缺一个喊一次。

本插件的 on_group_message 是一个**生成器 handler**: 不提名时零 yield
直接返回, 提名时 yield 一次 request_llm。绝不 set_result、不 stop_event、
不调 context.send_message()——后者绕过主线全部后处理(分段/落库/占位符/
出站守卫), 是 2026-09-23 生产实锤的 bypass, 不再使用。

⚠️ handler priority 是**降序**执行(数字越大越先), 本插件用 -2 确保排在
内置 astrbot star 的 on_message(priority=0, 群上下文捕获)之后——否则
yield 出的请求拿不到群历史标记, 主动触发就没有群聊上下文。详见
on_group_message 上方的长注释。

dry_run=true(默认): 提名只记账(提名原因 + 会怎么触发), 不 yield。

数据落盘: <plugin_data_dir>/ledger_YYYYMMDD.jsonl
"""
from __future__ import annotations

import asyncio
import datetime
import json
import operator
import re
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star

try:  # star_tools 在 core 下; 老版本可能在 api 下, 两者都试
    from astrbot.core.star.star_tools import StarTools
except Exception:  # pragma: no cover
    try:
        from astrbot.api.star import StarTools
    except Exception:
        StarTools = None  # type: ignore[assignment]

try:  # TextPart: 触发提醒走 extra_user_content_parts(mark_as_temp 不落库)
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore[assignment]

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
# jev_provider_url 的默认值: 官方端点。可换成自建代理/中转渠道(兼容同一
# /v1/systemone 接口格式), 留空则回退官方。
DEFAULT_JEV_URL = TYPESAFE_URL
DEFAULT_JEV_MODEL = "jev-latest"

# ─── 默认配置 ────────────────────────────────────────────────────────────────

DEFAULT_PERSONA = (
    "这是一个群聊机器人，QQ 号 {self_id}。\n"
    "它平时很少主动发言，只有被点名搭话、或话题明确落在它自己的兴趣范围内时才接话；"
    "群友之间的日常闲聊（行情、八卦、复读、与它无关的话题）都不属于它的接话范围。"
)

# 问题定义与 2026-09-21 dry-run 评估保持一致; {self_id} 在调用时替换
DEFAULT_QUESTIONS: dict[str, Any] = {
    "is_mentioned": {
        "type": "noul",
        "instructions": "目标消息是否在点名 QQ {self_id} 或直接对它说话？",
        "criteria": {
            "true": "叫了它的名字、群里常用的称呼或昵称，或话头明显是对它说的——包括暗指和含沙射影",
            "false": "没有针对它：@其他机器人、@其他群友、或群内普通对话都不算",
        },
    },
    "is_answerable": {
        "type": "noul",
        "instructions": "目标消息是否提出了一个可以回答的问题或请求？",
        "criteria": {
            "true": "在提问、求助、征求意见，或抛出了一个可以接的话头",
            "false": "陈述、自言自语、表情、复读、纯吐槽无问号",
        },
    },
    "is_unanswered": {
        "type": "noul",
        "instructions": "目标消息提出的问题，在它之前的对话里还没有人回答过？",
        "criteria": {
            "true": "之前的消息没有人回答这个问题",
            "false": "前面已经有人回答或解决了",
        },
    },
    "is_good_moment": {
        "type": "noul",
        "instructions": "此刻机器人接话是否自然得体？",
        "criteria": {
            "true": "对话节奏允许加入，接话不会打断别人正在进行的话题",
            "false": "群里正专注聊别的事，或两人对话正浓，插话会尴尬",
        },
    },
    "is_persona_fit": {
        "type": "noul",
        "instructions": "这条消息的话题是否符合机器人的人设，它接话会不会有趣？",
        "criteria": {
            "true": "话题与机器人的人设、兴趣或常聊领域相关，或它能给出符合人设的有趣反应",
            "false": "话题与人设无关，接话会莫名其妙",
        },
    },
    "reply_value": {
        "type": "score",
        "instructions": "综合来看，机器人主动回复这条消息的价值有多大？",
        "criteria": [
            "不该接：表情、复读、与人设完全无关的闲聊",
            "可接可不接：有点意思但错过也无所谓",
            "值得接：符合人设的问题或话题，且还没人回答",
            "很适合接：点名搭话，或话题正中人设且气氛合适",
        ],
    },
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    # 键 = Jev 问题 ID(与 questions 配置一一对应), 不接受简写;
    # 旧版的 mention/good_moment 简写在 _thresholds() 里自动改名并告警
    "is_mentioned": 0.60,  # 暗指点名提名线: 提名后走主线问一嘴, 由 bot 自己判断是不是说自己
    "reply_value": 1.50,   # 综合分提名线
    "is_good_moment": 0.45,  # 护栏(须大于此值)
    # 以下仅记录, 不参与提名(实测 AND-gate 全过会过严)
    "is_answerable": 0.60,
    "is_unanswered": 0.60,
    "is_persona_fit": 0.55,
}

# 旧版阈值简写 → 问题 ID。保留映射只为让老配置不炸, 加载时会告警提示改名
_LEGACY_THRESHOLD_KEYS: dict[str, str] = {
    "mention": "is_mentioned",
    "good_moment": "is_good_moment",
}

# ─── 提名规则(nomination_rule) ────────────────────────────────────────────
# 空配置 = 用内置规则(即 _nominate 里的两条路径, 阈值取 thresholds)。
# 自定义规则是纯 JSON 的布尔表达式, 让 questions 里加的新问题也能参与提名:
#
#   {"any": [                                   # any = 或, 任一子句成立即提名
#     {"is_mentioned": {">=": 0.6}},
#     {"all": [                                 # all = 且, 全部子句成立才通过
#       {"reply_value": {">=": 1.5}},
#       {"is_good_moment": {">": 0.45}}
#     ]}
#   ]}
#
# 叶子子句 = {"<问题ID>": {"<运算符>": <数字>}}; 问题 ID 可以是 questions 里
# 任意 noul 问题或 reply_value。运算符白名单, 不做 eval。
_RULE_OPS: dict[str, Any] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
}
_RULE_COMBINATORS = ("any", "all")

# 触发提醒: 追加在提示末尾的 <system> 段。人格/对话历史/群历史全部由主线装配,
# 这里只给触发原因 + [PASS] 约定(不想回只输出 [PASS], 走主线出站守卫匹配清空)。
# {reason} = Jev 提名原因。
DEFAULT_TRIGGER_NOTICE = (
    "<system>\n"
    "上面是一条没有 @ 你的群消息。门控模型评估认为它可能值得你接话，参考原因：{reason}。\n"
    "如果以你对群里氛围和话题的了解，判断不值得回应，只输出 [PASS]；\n"
    "否则就用你平时在群里的方式直接回应。\n"
    "</system>"
)

_JEVGATE_TRIGGER_MARK = "__jevgate_trigger__"

# 拒答精确匹配: 只认这两个(本插件契约 + 环境既有占位符约定 sleep_mode
# no_reply_placeholder)。主线出站守卫做匹配, 这里只列白名单, 不做模糊解析。
_REFUSAL_TOKENS = frozenset({"[PASS]", "[pass]", "[不回复]"})

_SKIP_TEXT_RE = re.compile(r"^\s*$")


def _now_str(ts: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(ts or time.time()).strftime("%m-%d %H:%M")


def _validate_rule(node: Any) -> tuple[bool, str]:
    """递归校验提名规则。返回 (是否合法, 错误描述)。"""
    if not isinstance(node, dict) or len(node) != 1:
        return False, "每个节点必须是且只能是 {\"any\": [...]}/{\"all\": [...]}/{\"<问题ID>\": {运算符: 数字}}"
    (key, val), = node.items()
    if key in _RULE_COMBINATORS:
        if not isinstance(val, list) or not val:
            return False, f"{key} 需要非空子句列表"
        for clause in val:
            ok, err = _validate_rule(clause)
            if not ok:
                return False, err
        return True, ""
    if not isinstance(val, dict) or len(val) != 1:
        return False, f"问题 {key!r} 的条件必须是且只能是一个 {{运算符: 数字}}"
    (op, num), = val.items()
    if op not in _RULE_OPS:
        return False, f"不支持的运算符 {op!r}（可用: {', '.join(_RULE_OPS)}）"
    if isinstance(num, bool) or not isinstance(num, (int, float)):
        return False, f"问题 {key!r} 的比较值必须是数字，得到 {num!r}"
    return True, ""


def _rule_question_ids(node: Any, out: set[str] | None = None) -> set[str]:
    """收集规则里引用到的所有问题 ID(用于和 questions 配置交叉检查)。"""
    if out is None:
        out = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _RULE_COMBINATORS and isinstance(v, list):
                for c in v:
                    _rule_question_ids(c, out)
            else:
                out.add(k)
    return out


def _eval_rule(node: Any, values: dict[str, float]) -> tuple[bool, str]:
    """求值提名规则。返回 (是否成立, 命中的原因串)。"""
    (key, val), = node.items()
    if key == "any":
        for clause in val:
            ok, why = _eval_rule(clause, values)
            if ok:
                return True, why
        return False, ""
    if key == "all":
        whys: list[str] = []
        for clause in val:
            ok, why = _eval_rule(clause, values)
            if not ok:
                return False, ""
            whys.append(why)
        return True, " & ".join(whys)
    # 叶子子句: 问题 ID 没被 Jev 返回(用户从 questions 里删了) → 条件不成立
    (op, num), = val.items()
    got = values.get(key)
    if got is None:
        return False, ""
    if _RULE_OPS[op](float(got), float(num)):
        return True, f"{key}({float(got):.2f}{op}{num})"
    return False, ""


class JevGatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        # umo -> {"buffer": deque, "lock": asyncio.Lock, "daily": {date: n},
        #         "nominated": {date: n}, "last_nominate": ts}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._data_dir: Path | None = None
        self._persona_cache: dict[str, str] = {}
        self._warned_no_key = False
        # (原始配置字符串, 解析后的规则|None) —— 配置没变就不重复解析/告警
        self._rule_cache: tuple[str, dict | None] | None = None

    async def initialize(self) -> None:
        """启动时把关键开关打成一行日志——"装了没反应"第一个要看的就是它。"""
        try:
            mode = str(self._cfg("session_mode", "whitelist") or "whitelist").strip().lower()
            enabled = [str(s) for s in self._cfg("enabled_sessions", []) or []]
            disabled = [str(s) for s in self._cfg("disabled_sessions", []) or []]
            logger.info(
                "[JevGate] 已加载 | 会话模式: %s | 白名单: %s | 黑名单: %s | "
                "dry_run: %s | api_key: %s | 模型: %s | Jev: %s",
                mode,
                ", ".join(enabled) if enabled else "（空）",
                ", ".join(disabled) if disabled else "（空）",
                self._cfg("dry_run", True),
                "已配置" if (self._cfg("api_key", "") or "") else "未配置",
                self._cfg("model", DEFAULT_JEV_MODEL),
                (str(self._cfg("jev_provider_url", "") or "").strip() or DEFAULT_JEV_URL),
            )
            if mode != "blacklist" and not enabled:
                logger.warning(
                    "[JevGate] 白名单模式但 enabled_sessions 为空，插件不会评估任何消息"
                )
            if not (self._cfg("persona", "") or "").strip():
                logger.warning(
                    "[JevGate] 未配置 persona，正在用内置占位人设——"
                    "建议填写你自己 bot 的人设（名字/称呼/性格/常聊话题），否则提名会偏多"
                )
            rule_raw = str(self._cfg("nomination_rule", "") or "").strip()
            if rule_raw:
                logger.info("[JevGate] 使用自定义提名规则: %s", rule_raw[:120])
            self._check_question_ids()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[JevGate] 启动自检失败（不影响加载）: {e}")

    # ─── 配置辅助 ──────────────────────────────────────────────────────────

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def _json_cfg(self, key: str, default: dict[str, Any]) -> dict[str, Any]:
        """读一个 JSON 配置项。兼容三种存法: JSON 字符串(schema 的 text 类型) /
        dict(老配置或手改配置文件) / 空值。解析失败只警告并回退默认, 不抛。
        """
        raw = self._cfg(key, None)
        if isinstance(raw, dict):
            return raw or default
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning(f"[JevGate] 配置 {key} 不是合法 JSON，已回退默认值: {e}")
                return default
            if isinstance(parsed, dict) and parsed:
                return parsed
            logger.warning(f"[JevGate] 配置 {key} 解析结果不是非空对象，已回退默认值")
        return default

    def _thresholds(self) -> dict[str, float]:
        th = dict(DEFAULT_THRESHOLDS)
        for k, v in self._json_cfg("thresholds", {}).items():
            key = _LEGACY_THRESHOLD_KEYS.get(k, k)
            if key != k:
                logger.warning(
                    f"[JevGate] 阈值键 {k!r} 已更名为 {key!r}(与问题 ID 对齐)，请更新配置"
                )
            try:
                th[key] = float(v)
            except (TypeError, ValueError):
                logger.warning(f"[JevGate] 阈值 {k}={v!r} 不是数字，忽略")
        return th

    def _nomination_rule(self) -> dict | None:
        """读 nomination_rule 配置。None = 用内置规则。

        留空/坏 JSON/结构不合法都回退内置规则并告警——绝不静默变成"永不提名"。
        按原始字符串缓存, 配置没变就不重复解析和告警。
        """
        raw = str(self._cfg("nomination_rule", "") or "").strip()
        if not raw:
            self._rule_cache = ("", None)
            return None
        if self._rule_cache is not None and self._rule_cache[0] == raw:
            return self._rule_cache[1]
        rule: dict | None = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"[JevGate] nomination_rule 不是合法 JSON，回退内置规则: {e}")
        else:
            ok, err = _validate_rule(parsed)
            if ok:
                rule = parsed
                # 规则引用了 questions 里没有的问题 → 那些条件永远不成立, 提前喊
                missing = _rule_question_ids(rule) - set(self._questions_for("0"))
                if missing:
                    logger.warning(
                        f"[JevGate] nomination_rule 引用了 questions 里不存在的问题 "
                        f"{sorted(missing)}，这些条件永远不成立"
                    )
            else:
                logger.warning(f"[JevGate] nomination_rule 不合法({err})，回退内置规则")
        self._rule_cache = (raw, rule)
        return rule

    def _persona_for(self, self_id: str) -> str:
        if self_id not in self._persona_cache:
            # schema 里 persona 默认是 "", 不能只靠 _cfg 的默认参数兜底
            self._persona_cache[self_id] = (
                self._cfg("persona", "") or DEFAULT_PERSONA
            ).replace("{self_id}", self_id)
        return self._persona_cache[self_id]

    def _questions_for(self, self_id: str) -> dict[str, Any]:
        raw = json.dumps(self._json_cfg("questions", DEFAULT_QUESTIONS))
        return json.loads(raw.replace("{self_id}", self_id))

    def _check_question_ids(self) -> None:
        """内置规则硬依赖这几个问题 ID, 缺了就会永远不提名——提前喊一声。

        自定义 nomination_rule 时这些 ID 不再必需(规则引用什么就要求什么),
        所以只在用内置规则时检查。
        """
        if self._nomination_rule() is not None:
            return
        missing = [q for q in ("is_mentioned", "is_good_moment") if q not in self._questions_for("0")]
        if missing:
            logger.warning(
                f"[JevGate] 配置的 questions 里缺少 {missing}，"
                "提名条件依赖它们，插件将不会触发（见 README「自定义问题」）"
            )

    def _session_enabled(self, umo: str) -> bool:
        """会话开关。`session_mode`:
        - `whitelist`(默认): 只跑 enabled_sessions 里的会话(支持 "*" 通配);
        - `blacklist`: 跑除 disabled_sessions 外的所有会话("*" = 全禁)。
        """
        mode = str(self._cfg("session_mode", "whitelist") or "whitelist").strip().lower()
        if mode == "blacklist":
            disabled = {str(s).strip() for s in self._cfg("disabled_sessions", []) or []}
            return not ("*" in disabled or umo in disabled)
        sessions = {str(s).strip() for s in self._cfg("enabled_sessions", []) or []}
        return "*" in sessions or umo in sessions

    def _trigger_notice(self, reason: str) -> str:
        return str(
            self._cfg("trigger_notice", DEFAULT_TRIGGER_NOTICE)
        ).replace("{reason}", reason)

    # ─── 数据落盘 ──────────────────────────────────────────────────────────

    def _get_data_dir(self) -> Path:
        if self._data_dir is None:
            if StarTools is None:
                raise RuntimeError("StarTools 不可用, 无法获取插件数据目录")
            data_dir = Path(StarTools.get_data_dir("jev_gate"))
            data_dir.mkdir(parents=True, exist_ok=True)
            self._data_dir = data_dir
        return self._data_dir

    def _append_ledger(self, record: dict[str, Any]) -> None:
        try:
            path = self._get_data_dir() / f"ledger_{datetime.date.today():%Y%m%d}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[JevGate] 账本写入失败: {e}")

    # ─── 消息处理 ──────────────────────────────────────────────────────────

    def _flatten(self, event: AstrMessageEvent) -> str:
        """把消息组件拉平成文本, 与 collector 导出的格式保持一致。"""
        parts: list[str] = []
        for comp in event.get_messages():
            if isinstance(comp, Plain):
                parts.append(comp.text or "")
            elif isinstance(comp, At):
                parts.append(f"[At:{comp.qq}]")
            else:
                parts.append(f"[{type(comp).__name__}]")
        text = "".join(parts).strip()
        if not text:
            text = (event.get_message_str() or "").strip()
        return text

    def _is_at_bot(self, event: AstrMessageEvent, self_id: str) -> bool:
        for comp in event.get_messages():
            if isinstance(comp, At) and str(comp.qq) == self_id:
                return True
        return False

    @staticmethod
    def _at_targets(event: AstrMessageEvent) -> set[str]:
        """消息里 @ 到的所有账号(含 @全体)。"""
        targets: set[str] = set()
        for comp in event.get_messages():
            if isinstance(comp, At):
                targets.add(str(comp.qq))
        return targets

    @staticmethod
    def _reply_target(event: AstrMessageEvent) -> str:
        """消息引用的那条消息的作者账号; 取不到返回空串。

        Reply 组件的 sender_id 由平台适配器解析引用消息时填入
        (aiocqhttp_platform_adapter.py); 引用解析被关时组件可能不带该字段。
        """
        try:
            for comp in event.get_messages():
                if isinstance(comp, Reply):
                    return str(getattr(comp, "sender_id", "") or "")
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _directed_elsewhere(
        self,
        event: AstrMessageEvent,
        self_id: str,
        other_bots: set[str],
    ) -> str:
        """消息明确指向"别处"时返回被指向的账号, 否则空串。

        两条规则(都由 skip_directed_messages 开关控制):
        1. @ 了任何其他账号(含 @全体): 明说了这话是冲谁去的, 不该由本 bot
           抢答。实测 167 条 @他人的消息 Jev 的 is_mentioned 全部 ≤0.5——它
           分得清"不是说我", 但 value 提名路径压根不看指向, 所以这活只能
           代码规则干。
        2. 引用了其他 bot 的消息: bot 之间的对话串, 插进去就是两个 AI 对答。
           引用普通群友(不带@)不算——那通常只是借楼补充, 留给 Jev 判断。
        """
        targets = self._at_targets(event)
        if "all" in targets:
            return "all"
        for t in targets:
            if t != self_id:
                return t
        reply_to = self._reply_target(event)
        if reply_to and reply_to in other_bots and reply_to != self_id:
            return reply_to
        return ""

    def _session(self, umo: str) -> dict[str, Any]:
        st = self._sessions.get(umo)
        if st is None:
            window = int(self._cfg("context_window", 15)) + 5
            st = {
                "buffer": deque(maxlen=window),
                "lock": asyncio.Lock(),
                "daily": {},
                "nominated": {},
                "last_nominate": 0.0,
                # 连续对话识别: bot 上次真实回复的发送者与时间。同发送者在
                # followup 窗口内继续说话属于"接着聊", 不该被长冷却拦住
                "last_dispatch_ts": 0.0,
                "last_dispatch_sender": "",
                # 拒答退还: 触发前的 last_nominate / 连续性标记快照
                "prev_last_nominate": 0.0,
                "prev_dispatch_ts": 0.0,
                "prev_dispatch_sender": "",
                "trigger_sender": "",
                # 日评估上限命中告警只喊一次(按会话+日期去重)
                "eval_cap_logged": "",
            }
            self._sessions[umo] = st
        return st

    def _today(self) -> str:
        return datetime.date.today().isoformat()

    # ─── Jev API ───────────────────────────────────────────────────────────

    def _call_jev_sync(
        self, state: dict[str, Any], questions: dict[str, Any]
    ) -> dict[str, Any]:
        api_key = self._cfg("api_key", "") or ""
        if not api_key:
            if not self._warned_no_key:
                logger.warning("[JevGate] 未配置 api_key, 跳过评估")
                self._warned_no_key = True
            raise RuntimeError("no api key")
        # jev_provider_url 留空回退官方端点; 可填自建代理/中转(接口格式须兼容)
        url = (str(self._cfg("jev_provider_url", "") or "").strip() or DEFAULT_JEV_URL)
        body = json.dumps({
            "model": self._cfg("model", DEFAULT_JEV_MODEL) or DEFAULT_JEV_MODEL,
            "state": state,
            "questions": questions,
        }).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(4):
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 529):
                    time.sleep(2 ** attempt)
                    last_err = e
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                time.sleep(2 ** attempt)
                last_err = e
        raise RuntimeError(f"Jev API 重试后仍失败: {last_err}")

    async def _call_jev(
        self, state: dict[str, Any], questions: dict[str, Any]
    ) -> dict[str, Any]:
        # 阻塞 HTTP 放到线程里, 不卡事件循环; 外层再套超时, 防止管线任务悬挂
        timeout = float(self._cfg("jev_timeout_seconds", 25))
        return await asyncio.wait_for(
            asyncio.to_thread(self._call_jev_sync, state, questions),
            timeout=timeout,
        )

    # ─── 门控逻辑 ──────────────────────────────────────────────────────────

    @staticmethod
    def _nominate(
        nouls: dict[str, Any],
        value: float | None,
        th: dict[str, float],
        rule: dict | None = None,
    ) -> tuple[bool, str]:
        """返回 (是否提名, 原因)。

        `rule` 为 None 时走内置两条路径(阈值取自 th); 给了 rule 就按自定义
        布尔表达式求值, 原因串由实际命中的子句生成(便于回账本追溯)。
        """
        if rule is not None:
            values: dict[str, float] = dict(nouls)
            if value is not None:
                values["reply_value"] = float(value)
            return _eval_rule(rule, values)
        m = float(nouls.get("is_mentioned") or 0.0)
        g = float(nouls.get("is_good_moment") or 0.0)
        if m >= th["is_mentioned"]:
            return True, f"possible_mention({m:.2f})"
        if (value or 0.0) >= th["reply_value"] and g > th["is_good_moment"]:
            return True, f"value({value:.2f})+moment({g:.2f})"
        return False, ""

    # ─── 入站: 判断; 提名时 yield 触发 ─────────────────────────────────────

    # ⚠️ priority 必须是负数——AstrBot 的 handler 排序是**按 priority 降序**
    # (star_handler.py: `sort(key=lambda h: -priority)`), 数字越大越先执行。
    # 本插件必须在内置 astrbot star 的 on_message(priority=0) **之后**运行:
    # 那个 handler 调用 group_chat_context.handle_message 把本消息写进群上下文
    # 缓冲并打下 _group_context_record_id 标记, on_req_llm 靠这个标记决定注入
    # 哪些群历史。若本插件先跑并 yield, 标记还不存在 → on_req_llm 直接早退 →
    # 主动触发的请求不带群聊历史(2026-09-24 生产实锤), 且未消费的缓冲会攒到
    # 下次 @ 回复时一次性吐出几小时的陈旧上下文。
    # -2: 排在所有 0 优先级与 chat_plus(-1) 之后; 没必要更低——event_message_type
    # 里目前最低只到 -1, 再低只会排在可能 stop_event 的处理器后面。
    # 副作用(正向): sleep_mode 的入站守卫(priority=1)现在跑在本插件前面,
    # 休眠群直接跳过评估, 不再浪费 Jev 调用。
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-2)
    async def on_group_message(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[Any, None]:
        """生成器 handler: 判断后决定 yield 与否。

        不提名时零 yield 直接返回(经 call_handler 透传, 消息后续命运不受影响)。
        提名时 yield event.request_llm(...) 一次——返回值 ProviderRequest 由
        ProcessStage 交给 Agent 子阶段执行, 全程走主线链路。
        绝不 set_result / stop_event / send_message。
        """
        umo = event.unified_msg_origin
        if not self._session_enabled(umo):
            return

        self_id = str(event.get_self_id() or "")
        sender_id = str(event.get_sender_id() or "")
        if not self_id:
            return
        if sender_id == self_id:
            return
        sender_name = event.get_sender_name() or sender_id
        text = self._flatten(event)
        other_bots = {str(b) for b in self._cfg("other_bot_ids", []) or []}
        if sender_id in other_bots and not self._cfg("evaluate_other_bots", False):
            # 其他 bot 的发言默认不评估, 但仍进上下文缓冲并标注 is_bot——
            # 这是 other_bot_ids 的设计立意: 让 Jev 看全场时知道"这条是别的
            # bot 说的"(渲染成 name(机器人))。旧版在这里直接 return, 标注从未
            # 真正生效过。
            st = self._session(umo)
            st["buffer"].append({
                "ts": time.time(), "time": _now_str(), "name": sender_name,
                "content": text or "[非文本]",
                "is_self": False, "is_bot": True,
            })
            return
        if self._is_at_bot(event, self_id):
            # 点名走 AstrBot 正常回复管线, 不需要 Jev (评估结论: @ 检测代码规则即可)
            self._append_ledger({
                "ts": time.time(), "kind": "mention_code_skip", "session": umo,
                "sender": sender_name, "content": text,
            })
            return
        # 指向别处(@其他人/引用其他bot/@全体): 这是"别人点的对话", 不抢答。
        # 仍进上下文缓冲, Jev 看全场时需要知道这条是对别人说的。
        aimed_at = self._directed_elsewhere(event, self_id, other_bots)
        if aimed_at and self._cfg("skip_directed_messages", True):
            st = self._session(umo)
            st["buffer"].append({
                "ts": time.time(), "time": _now_str(), "name": sender_name,
                "content": text or "[非文本]",
                "is_self": False, "is_bot": sender_id in other_bots,
            })
            self._append_ledger({
                "ts": time.time(), "kind": "directed_skip", "session": umo,
                "sender": sender_name, "content": text, "aimed_at": aimed_at,
            })
            logger.info(
                f"[JevGate] 跳过评估(directed_skip): {sender_name}: "
                f"{text[:40]} | aimed_at={aimed_at} "
                f"skip_directed_messages=True"
            )
            return
        if _SKIP_TEXT_RE.match(text):
            return

        st = self._session(umo)

        # 上下文缓冲: 进评估的消息都进 (含其他 bot, 供 Jev 看全场)
        st["buffer"].append({
            "ts": time.time(), "time": _now_str(), "name": sender_name,
            "content": text or "[非文本]",
            "is_self": False, "is_bot": sender_id in other_bots,
        })

        # ── Layer 0: 日评估上限(计数器只加锁操作, Jev 调用本身不持锁) ──
        today = self._today()
        async with st["lock"]:
            eval_cap = int(self._cfg("max_daily_evaluations", 400))
            used = st["daily"].get(today, 0)
            if used >= eval_cap:
                # 命中后每条消息都会走到这, 日志按"会话+日期"只喊一次
                if st.get("eval_cap_logged") != today:
                    st["eval_cap_logged"] = today
                    logger.info(
                        f"[JevGate] 本会话今日评估已达上限, 停止评估: {umo} | "
                        f"max_daily_evaluations={eval_cap} used={used} "
                        f"date={today}"
                    )
                return
            st["daily"][today] = used + 1

        # ── Layer 1: Jev 提名(上下文排除目标消息本身, 与评估数据形态一致) ──
        ts = time.time()
        msg_id = str(event.message_obj.message_id) if event.message_obj else ""
        context = list(st["buffer"])[:-1][-int(self._cfg("context_window", 15)):]
        state = self._build_state(self._persona_for(self_id), context, sender_name, text)
        try:
            t0 = time.monotonic()
            resp = await self._call_jev(state, self._questions_for(self_id))
            latency = time.monotonic() - t0
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("[JevGate] Jev 调用超时, 本次放弃触发")
            self._append_ledger({
                "ts": ts, "kind": "error", "session": umo, "sender": sender_name,
                "content": text, "detail": "jev_timeout",
            })
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[JevGate] Jev 调用失败: {e}")
            self._append_ledger({
                "ts": ts, "kind": "error", "session": umo, "sender": sender_name,
                "content": text, "detail": str(e),
            })
            return

        answers = resp.get("answers", {})
        nouls = {k: v.get("noul") for k, v in answers.items() if v.get("type") == "noul"}
        rv = answers.get("reply_value", {})
        value = rv.get("score")
        rule = self._nomination_rule()
        nominated, reason = self._nominate(nouls, value, self._thresholds(), rule)

        record: dict[str, Any] = {
            "ts": ts, "kind": "evaluated", "session": umo,
            "msg_id": msg_id, "sender": sender_name, "content": text,
            "context_size": len(context),
            "nouls": nouls,
            "reply_value": {
                "score": value,
                "probabilities": rv.get("probabilities"),
                "confidence": rv.get("confidence"),
            },
            "model": resp.get("model"),
            "usage": resp.get("usage"),
            "latency_s": round(latency, 2),
            "nominated": nominated,
            "reason": reason,
        }

        if not nominated:
            self._append_ledger(record)
            return

        # ── 冷却 / 每日触发上限 (只约束"真的要行动"的路径) ──
        # cooldown_enabled=false(默认) 或 cooldown_minutes=0 时完全关闭冷却;
        # 否则同一发送者在 followup 窗口内接着聊走短冷却(默认 0 = 直接放行),
        # 其他人插话仍走长冷却。日限永远是硬上限。
        async with st["lock"]:
            if self._cfg("cooldown_enabled", False):
                cd_minutes = int(self._cfg("cooldown_minutes", 30))
                if cd_minutes > 0:
                    now = time.time()
                    window_min = int(self._cfg("followup_window_minutes", 10))
                    followup_s = int(self._cfg("followup_cooldown_seconds", 0))
                    # window_min<=0 = 不限窗口: 同一发送者永远算"接着聊"。
                    # 曾经的坑: 0 被当成"窗口 0 秒" → is_followup 永假 →
                    # 用户本想关掉窗口限制, 反被 30 分钟长冷却拦死。
                    is_followup = sender_id == st.get("last_dispatch_sender") and (
                        window_min <= 0
                        or now - st.get("last_dispatch_ts", 0.0) < window_min * 60
                    )
                    cooldown = followup_s if is_followup else cd_minutes * 60
                    elapsed = now - st["last_nominate"]
                    if elapsed < cooldown:
                        record["trigger"] = {
                            "choice": "skip", "detail": "cooldown",
                            "followup": is_followup,
                            "elapsed_s": round(elapsed, 1),
                            "cooldown_s": cooldown,
                        }
                        self._append_ledger(record)
                        logger.info(
                            f"[JevGate] 跳过触发(cooldown): {sender_name}: "
                            f"{text[:40]} | reason={reason} "
                            f"followup={is_followup} "
                            f"elapsed_s={elapsed:.0f} < cooldown_s={cooldown:.0f} "
                            f"| cooldown_enabled=True cooldown_minutes={cd_minutes} "
                            f"followup_window_minutes={window_min} "
                            f"followup_cooldown_seconds={followup_s}"
                        )
                        return
            day_limit = int(self._cfg("daily_limit", 5))
            used_today = st["nominated"].get(today, 0)
            if used_today >= day_limit:
                record["trigger"] = {
                    "choice": "skip", "detail": "daily_limit",
                    "used": used_today,
                }
                self._append_ledger(record)
                logger.info(
                    f"[JevGate] 跳过触发(daily_limit): {sender_name}: "
                    f"{text[:40]} | reason={reason} "
                    f"used={used_today} >= daily_limit={day_limit} | date={today}"
                )
                return
            st["nominated"][today] = st["nominated"].get(today, 0) + 1
            # prev_* 快照供拒答退还: bot 输出 [PASS] 或取不到 conversation 时
            # 回到触发前的状态(冷却/日限/连续性标记全部回滚)
            st["prev_last_nominate"] = st["last_nominate"]
            st["prev_dispatch_ts"] = st.get("last_dispatch_ts", 0.0)
            st["prev_dispatch_sender"] = st.get("last_dispatch_sender", "")
            st["last_nominate"] = time.time()
            # 乐观标记: 触发即视为"bot 即将回复这个发送者", 连续对话判定不必
            # 等回复真正发出(回复生成要几秒到十几秒, 期间用户可能就接着说了)
            st["last_dispatch_ts"] = time.time()
            st["last_dispatch_sender"] = sender_id
            st["trigger_sender"] = sender_id

        if self._cfg("dry_run", True):
            record["trigger"] = {"choice": "dry_run", "detail": "logged_only"}
            self._append_ledger(record)
            logger.info(
                f"[JevGate] dry-run 提名({reason}): {sender_name}: {text[:40]}"
                " | 只记录不触发"
            )
            return

        # ── Layer 2: 触发。必须挂 conversation + 打标记, 剩下的交给主线。
        # conversation 不是可选项: 主线只在 req.conversation 存在时才
        #   (a) 注入人格/skills —— `_ensure_persona_and_skills` 开头
        #       `if not req.conversation: return` (astr_main_agent.py:536)
        #   (b) 给 req.contexts 填对话历史 (astr_main_agent.py:1436-1442)
        #   (c) 把本轮写回对话历史 —— `_save_to_history` 同款早退
        #     (internal.py:459-461)
        # 不带 conversation 的后果 2026-09-23 生产实锤: 回复能发出, 但无persona、
        # 上下文为空、且永不落库(对话数据 updated_at 停在触发前)。group_chat_plus
        # main.py:10989 有同一踩坑注释。取不到就新建(与主线 _get_session_conv 同口径)。
        conv, cid = await self._current_conversation(umo, event)
        if conv is None:
            record["trigger"] = {"choice": "skip", "detail": "no_conversation"}
            self._append_ledger(record)
            logger.warning(f"[JevGate] {umo} 无可用对话且新建失败, 跳过触发")
            # 没有真正发出请求, 退还刚才扣掉的冷却/额度
            self._refund_trigger(umo)
            return

        notice = self._trigger_notice(reason)
        req = event.request_llm(
            prompt=f"{sender_name}: {text}",
            session_id=event.session_id,
            conversation=conv,
        )
        if TextPart is not None:
            # 触发提醒只对本次请求可见, mark_as_temp 让落库时被
            # dump_messages_with_checkpoints 过滤掉, 不污染对话历史
            req.extra_user_content_parts.append(
                TextPart(text=notice).mark_as_temp()
            )
        else:  # pragma: no cover  老版本无 TextPart 时退化为拼进 prompt(会落库)
            req.prompt = f"{req.prompt}\n\n{notice}"

        event.set_extra(_JEVGATE_TRIGGER_MARK, {"reason": reason})
        record["trigger"] = {
            "choice": "dispatch", "detail": "yield_request_llm",
            "conversation": cid[:8],
        }
        self._append_ledger(record)
        yield req

    async def _current_conversation(self, umo: str, event: AstrMessageEvent):
        """取该 umo 的当前对话; 没有则新建。返回 (Conversation|None, cid)。

        与主线 `_get_session_conv`(astr_main_agent.py:273-285) 同口径:
        先查当前 cid, 没有就 new_conversation。**不要省这一步**——见调用点注释。
        """
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return None, ""
        try:
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                cid = await conv_mgr.new_conversation(umo, event.get_platform_id())
            if not cid:
                return None, ""
            conv = await conv_mgr.get_conversation(umo, cid)
            return (conv, str(cid)) if conv is not None else (None, str(cid))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[JevGate] 对话读取/新建失败: {e}")
            return None, ""

    # ─── 拒答守卫: 管线原生匹配, 精确白名单 ────────────────────────────────

    @filter.on_decorating_result(priority=99990)
    async def on_proactive_refusal_guard(self, event: AstrMessageEvent) -> None:
        """整条回复命中拒答约定时清空, 并按配置处理历史与额度。

        三个作用:
        1. 本插件触发轮: [PASS] 是 prompt 里的约定, 兜底防止泄露到群里;
        2. 普通回复(如 @bot 点名): 模型偶发直接输出 [PASS]/[不回复] 时
           (实测发生过), 同样清空——这类 token 一旦发到群里就是社死,
           宁可漏发也不该出现;
        3. pass_history_mode="hide" 时, 把刚落库的拒答记录从群消息历史和
           会话历史里删掉, 让模型完全不知道自己"曾经"[PASS]过。

        匹配精度由 refusal_fuzzy_match 控制: 关闭(默认)时整条精确相等才算数
        (容忍首尾空白与句读), "[PASS]，喵" 这类正常短回复不误伤; 开启后
        本插件触发轮改为包含匹配——整条里出现 token 就算拒答。
        """
        triggered = bool(event.get_extra(_JEVGATE_TRIGGER_MARK))
        if not triggered and not getattr(event, "__is_llm_reply", False):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return
        text = "".join(
            getattr(c, "text", "") or "" for c in chain
        ).strip()
        stripped = text.strip("。.!！~～ \t\"'“”‘’")
        is_refusal = (
            stripped in _REFUSAL_TOKENS
            or (triggered and self._cfg("refusal_fuzzy_match", False)
                and any(tok in text for tok in _REFUSAL_TOKENS))
        )
        if not is_refusal:
            # 正常回复: 记录"bot 刚回过这个发送者", 供连续对话短冷却识别
            self._note_reply(event)
            return
        chain.clear()
        logger.info(
            "[JevGate] 回复命中拒答约定，已清空"
            f"（{'本插件触发轮' if triggered else '普通 LLM 回复'}）"
        )
        if triggered:
            # 拒答 = 没有真实回复: 退还冷却与当日额度, 避免被"触发了但没说话"
            # 隐形消耗(冷却照扣 30 分钟、日限照扣一次)
            self._refund_trigger(event.unified_msg_origin)
        if self._cfg("pass_history_mode", "keep") == "hide":
            await self._purge_refusal_from_history(event, stripped)

    def _note_reply(self, event: AstrMessageEvent) -> None:
        """bot 真实回复后记录对话连续性: 同一发送者短时间内继续说话, 视为"接着聊"。

        @bot 点名触发的普通回复也记——用户被 bot 回复后接着聊, 同样不该被
        长冷却拦住。
        """
        try:
            umo = event.unified_msg_origin
            if not self._session_enabled(umo):
                return
            st = self._session(umo)
            sender_id = str(event.get_sender_id() or "")
            if sender_id:
                st["last_dispatch_ts"] = time.time()
                st["last_dispatch_sender"] = sender_id
        except Exception:  # noqa: BLE001
            pass

    def _refund_trigger(self, umo: str) -> None:
        st = self._sessions.get(umo)
        if st is None:
            return
        today = self._today()
        if st["nominated"].get(today, 0) > 0:
            st["nominated"][today] -= 1
        # 冷却窗口与连续性标记都回到本次触发之前
        st["last_nominate"] = st.get("prev_last_nominate", 0.0)
        st["last_dispatch_ts"] = st.get("prev_dispatch_ts", 0.0)
        st["last_dispatch_sender"] = st.get("prev_dispatch_sender", "")

    @staticmethod
    def _is_refusal_record(msg: Any) -> bool:
        """判断一条会话历史消息是否是拒答(role=assistant 且文本命中 token)。"""
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            return False
        content = msg.get("content")
        if isinstance(content, list):
            text = "".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            )
        else:
            text = str(content or "")
        return text.strip().strip("。.!！~～ \t\"'“”‘’") in _REFUSAL_TOKENS

    async def _purge_refusal_from_history(self, event: AstrMessageEvent, token: str) -> None:
        """pass_history_mode="hide": 把刚落库的拒答记录从历史里删掉。

        落库发生在 on_llm_response(persist_llm_response → 群消息历史)和 agent
        子阶段(_save_to_history → 会话历史), 都早于本守卫, 所以只能事后删。
        只删"最新一条、文本命中拒答 token"的记录, 更早的历史不碰。
        livingmemory 自建的 messages 表(记忆抽取用)不在可达范围, 不做处理。
        """
        umo = event.unified_msg_origin
        # 1) 群消息历史 platform_message_history: 群历史开启时主线在这里落 bot 消息
        mgr = getattr(self.context, "message_history_manager", None)
        if mgr is not None:
            try:
                records = await mgr.get(
                    event.get_platform_id(), umo, page=1, page_size=20
                )
                for rec in reversed(list(records or [])):
                    content = getattr(rec, "content", None)
                    parts = content.get("message", []) if isinstance(content, dict) else []
                    text = "".join(
                        str(p.get("text", "")) for p in parts if isinstance(p, dict)
                    )
                    if text.strip().strip("。.!！~～ \t\"'“”‘’") in _REFUSAL_TOKENS:
                        rid = getattr(rec, "id", None)
                        if rid is not None:
                            await mgr.delete_by_id(rid)
                            logger.info(f"[JevGate] 已从群消息历史删除拒答记录 id={rid}")
                        break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[JevGate] 清理群消息历史失败: {e}")
        # 2) 会话历史 conversations: 删掉末尾的 assistant 拒答消息
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is not None:
            try:
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if cid:
                    conv = await conv_mgr.get_conversation(umo, cid)
                    if conv is not None:
                        history = json.loads(getattr(conv, "history", "[]") or "[]")
                        if history and self._is_refusal_record(history[-1]):
                            history.pop()
                            await conv_mgr.update_conversation(umo, cid, history=history)
                            logger.info("[JevGate] 已从会话历史删除拒答记录")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[JevGate] 清理会话历史失败: {e}")

    @staticmethod
    def _build_state(
        persona: str, context: list[dict[str, Any]], sender: str, content: str
    ) -> dict[str, Any]:
        lines = []
        for entry in context:
            name = entry["name"]
            if entry.get("is_self"):
                name += "(我)"
            elif entry.get("is_bot"):
                name += "(机器人)"
            lines.append(f"[{entry['time']}] {name}: {entry['content']}")
        return {
            "bot_persona": persona,
            "recent_conversation": lines,
            "target_message": {"sender": sender, "content": content},
        }
