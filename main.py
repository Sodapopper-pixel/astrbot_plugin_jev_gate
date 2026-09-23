"""Jev 主动回复门控插件 (v0.3.0)

职责边界: 本插件只做**判断与触发**, 不组装 LLM 上下文、不直接发送消息。

  Layer 0  代码规则: 会话白名单 / @bot 检测 / 日评估上限
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

本插件的 on_group_message 是一个**生成器 handler**: 不提名时零 yield
直接返回, 提名时 yield 一次 request_llm。绝不 set_result、不 stop_event、
不调 context.send_message()——后者绕过主线全部后处理(分段/落库/占位符/
出站守卫), 是 2026-09-23 生产实锤的 bypass, 不再使用。

dry_run=true(默认): 提名只记账(提名原因 + 会怎么触发), 不 yield。

数据落盘: <plugin_data_dir>/ledger_YYYYMMDD.jsonl
"""
from __future__ import annotations

import asyncio
import datetime
import json
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
from astrbot.api.message_components import At, Plain
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
    # 暗指点名提名线: 提名后走主线问一嘴, 由 bot 自己在上下文里判断是不是说自己
    "mention": 0.60,
    # reply_value 提名线 + good_moment 护栏
    "reply_value": 1.50,
    "good_moment": 0.45,
    # 以下仅记录, 不参与提名(实测 AND-gate 全过会过严)
    "answerable": 0.60,
    "unanswered": 0.60,
    "persona_fit": 0.55,
}

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

    async def initialize(self) -> None:
        """启动时把关键开关打成一行日志——"装了没反应"第一个要看的就是它。"""
        try:
            sessions = [str(s) for s in self._cfg("enabled_sessions", []) or []]
            logger.info(
                "[JevGate] 已加载 | 会话: %s | dry_run: %s | api_key: %s | 模型: %s",
                ", ".join(sessions) if sessions else "（空，不会工作）",
                self._cfg("dry_run", True),
                "已配置" if (self._cfg("api_key", "") or "") else "未配置",
                self._cfg("model", "jev-1.13.0"),
            )
            if not sessions:
                logger.warning("[JevGate] enabled_sessions 为空，插件不会评估任何消息")
            if not (self._cfg("persona", "") or "").strip():
                logger.warning(
                    "[JevGate] 未配置 persona，正在用内置占位人设——"
                    "建议填写你自己 bot 的人设（名字/称呼/性格/常聊话题），否则提名会偏多"
                )
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
            try:
                th[k] = float(v)
            except (TypeError, ValueError):
                logger.warning(f"[JevGate] 阈值 {k}={v!r} 不是数字，忽略")
        return th

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
        """提名条件硬依赖这几个问题 ID, 缺了就会永远不提名——提前喊一声。"""
        missing = [q for q in ("is_mentioned", "is_good_moment") if q not in self._questions_for("0")]
        if missing:
            logger.warning(
                f"[JevGate] 配置的 questions 里缺少 {missing}，"
                "提名条件依赖它们，插件将不会触发（见 README「自定义问题」）"
            )

    def _session_enabled(self, umo: str) -> bool:
        """会话白名单。支持 \"*\" 通配所有会话, 方便先 dry-run 观察。"""
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
        body = json.dumps({
            "model": self._cfg("model", "jev-1.13.0"),
            "state": state,
            "questions": questions,
        }).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(4):
            req = urllib.request.Request(
                TYPESAFE_URL,
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
    def _nominate(nouls: dict[str, Any], value: float | None, th: dict[str, float]) -> tuple[bool, str]:
        """返回 (是否提名, 原因)。只保留评估实测有效的两条路径。"""
        m = float(nouls.get("is_mentioned") or 0.0)
        g = float(nouls.get("is_good_moment") or 0.0)
        if m >= th["mention"]:
            return True, f"possible_mention({m:.2f})"
        if (value or 0.0) >= th["reply_value"] and g > th["good_moment"]:
            return True, f"value({value:.2f})+moment({g:.2f})"
        return False, ""

    # ─── 入站: 判断; 提名时 yield 触发 ─────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=500)
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
        other_bots = {str(b) for b in self._cfg("other_bot_ids", []) or []}
        if sender_id in other_bots and not self._cfg("evaluate_other_bots", False):
            return
        sender_name = event.get_sender_name() or sender_id
        text = self._flatten(event)
        if self._is_at_bot(event, self_id):
            # 点名走 AstrBot 正常回复管线, 不需要 Jev (评估结论: @ 检测代码规则即可)
            self._append_ledger({
                "ts": time.time(), "kind": "mention_code_skip", "session": umo,
                "sender": sender_name, "content": text,
            })
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
            if st["daily"].get(today, 0) >= int(self._cfg("max_daily_evaluations", 400)):
                return
            st["daily"][today] = st["daily"].get(today, 0) + 1

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
        nominated, reason = self._nominate(nouls, value, self._thresholds())

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
        async with st["lock"]:
            cooldown = int(self._cfg("cooldown_minutes", 30)) * 60
            if time.time() - st["last_nominate"] < cooldown:
                record["trigger"] = {"choice": "skip", "detail": "cooldown"}
                self._append_ledger(record)
                return
            if st["nominated"].get(today, 0) >= int(self._cfg("daily_limit", 5)):
                record["trigger"] = {"choice": "skip", "detail": "daily_limit"}
                self._append_ledger(record)
                return
            st["nominated"][today] = st["nominated"].get(today, 0) + 1
            st["last_nominate"] = time.time()

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
        """只处理本插件触发的那一次回复; 整条恰好是拒答 token 才清空。

        [PASS] 是本插件 prompt 里的契约; [不回复] 是环境既有占位符约定
        (sleep_mode no_reply_placeholder, 主线 99999 守卫也会匹配)。
        精确相等才算数——模糊启发式("含不/拒就杀")会误伤正常短回复。
        """
        if not event.get_extra(_JEVGATE_TRIGGER_MARK):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return
        text = "".join(
            getattr(c, "text", "") or "" for c in chain
        ).strip()
        if text in _REFUSAL_TOKENS:
            chain.clear()
            logger.info("[JevGate] 触发回复命中拒答约定，已清空")

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
