"""随机事件引擎——纯计算，不读写存档。

调用方在自己 ``store.update(op)`` 内的合适位置调用 ``EventService.roll(...)``
拿到 ``(event_dict, deltas_dict)`` 或 ``None``，然后由调用方在**同一事务**内应用
delta 到 wallet / catgirls 字段并写入 ``event_log`` / ``event_state``。

这样跨字段（原收益 + 事件 delta + 事件日志）在同一事务内完成，避免并发不一致。
本 service 自身不持锁、不写盘，仅做概率判定 / 性格过滤 / 每日上限 / 今日去重。
"""
from __future__ import annotations

import random
from typing import Any, Callable, Dict, List, Optional, Tuple

from .catgirl_schema import PERSONALITIES


# 与 runtime_config.EVENT_ANCHORS / PERSONALITY_EVENT_ANCHORS 保持一致
EVENT_ANCHORS = ("sign", "daily_work", "feed", "cat_work_finish", "interact")
PERSONALITY_EVENT_ANCHORS = ("interact", "feed", "cat_work_finish")


EventRoll = Optional[Tuple[Dict[str, Any], Dict[str, int]]]


class EventService:
    def __init__(self, runtime_config_provider: Callable[[], Dict[str, Any]] | None = None) -> None:
        self.runtime_config_provider = runtime_config_provider

    # ---- 配置读取 ----------------------------------------------------------

    def _runtime(self) -> Dict[str, Any]:
        if callable(self.runtime_config_provider):
            try:
                data = self.runtime_config_provider()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _rules(self, section: str = "events") -> Dict[str, Any]:
        data = self._runtime().get(section, {})
        return data if isinstance(data, dict) else {}

    # ---- 派生数据 ----------------------------------------------------------

    def _enabled_items(self, rows: List[Dict[str, Any]], anchor: str) -> List[Dict[str, Any]]:
        result = []
        if not isinstance(rows, list):
            return result
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not row.get("enabled", True):
                continue
            if str(row.get("anchor") or "") != anchor:
                continue
            result.append(row)
        return result

    def _deltas_from(self, ev: Dict[str, Any]) -> Dict[str, int]:
        coin_min = int(ev.get("coin_min", 0))
        coin_max = max(coin_min, int(ev.get("coin_max", coin_min)))
        coin = random.randint(coin_min, coin_max) if coin_min or coin_max else 0
        return {
            "coin": coin,
            "mood": int(ev.get("mood", 0) or 0),
            "energy": int(ev.get("energy", 0) or 0),
            "intimacy": int(ev.get("intimacy", 0) or 0),
            "growth": int(ev.get("growth", 0) or 0),
        }

    @staticmethod
    def _stage_filter(rows: List[Dict[str, Any]], cat: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if cat is None:
            return rows
        try:
            stage = int(cat.get("stage", 0) or 0)
        except Exception:
            stage = 0
        return [row for row in rows if stage >= int(row.get("min_stage", 0) or 0)]

    # ---- 主入口：通用事件 + 性格事件 ----------------------------------------

    def roll(
        self,
        anchor: str,
        cat: Optional[Dict[str, Any]],
        today_anchor_count: int,
        triggered_today_ids: List[str],
    ) -> EventRoll:
        """按锚点随机抽一个事件。

        - ``anchor`` 必须在 EVENT_ANCHORS 内（性格事件锚点会顺带二次 roll）。
        - ``cat`` 为猫娘对象，参与性格事件过滤；为 None 时跳过性格事件。
        - ``today_anchor_count`` 是该锚点今日已触发次数，超过配置上限则不再触发。
        - ``triggered_today_ids`` 是今日已触发的事件 id 列表，用于去重。
        返回 ``(event_dict, deltas_dict)`` 或 ``None``。
        """
        rules = self._rules()
        # 主人行为锚点（sign/daily_work）不涉及性格；确保只在受性格影响的锚点上做二次 roll
        allow_personality = cat is not None and anchor in PERSONALITY_EVENT_ANCHORS

        # 1) 通用事件池
        general = self._stage_filter(self._enabled_items(rules.get("items", []), anchor), cat)
        roll = self._pick(general, today_anchor_count, triggered_today_ids, rules, "daily_limit_per_anchor")
        if roll is not None:
            return roll

        # 2) 性格事件池（独立二次 roll）
        if allow_personality:
            personality = str(cat.get("personality", "") or "")
            if personality in PERSONALITIES:
                personality_items = [
                    row
                    for row in self._stage_filter(self._enabled_items(rules.get("personality_events", []), anchor), cat)
                    if str(row.get("personality", "") or "") == personality
                ]
                # 性格事件走独立的每日上限计数——这里简化为复用锚点计数加一个软上限
                roll = self._pick(
                    personality_items,
                    today_anchor_count,
                    triggered_today_ids,
                    rules,
                    "daily_limit_per_personality",
                )
                if roll is not None:
                    return roll
        return None

    def _pick(
        self,
        candidates: List[Dict[str, Any]],
        today_anchor_count: int,
        triggered_today_ids: List[str],
        rules: Dict[str, Any],
        limit_key: str,
    ) -> EventRoll:
        if not candidates:
            return None
        # 每日上限：该锚点今天已经触发达上限则不再触发（0 表示不限）
        limit = int(rules.get(limit_key, 0) or 0)
        if limit > 0 and today_anchor_count >= limit:
            return None
        # 候选中先按概率筛选，再排除今日已触发去重
        triggered = set(triggered_today_ids or [])
        weighted: List[Tuple[float, Dict[str, Any]]] = []
        for ev in candidates:
            try:
                prob = float(ev.get("prob", 0) or 0)
            except (TypeError, ValueError):
                prob = 0.0
            if prob <= 0:
                continue
            ev_id = str(ev.get("id", "") or "")
            if ev_id and ev_id in triggered:
                continue
            # 阶段过滤交给调用方在 op 内做（cat.stage），这里只做概率
            weighted.append((prob, ev))
        if not weighted:
            return None
        # 加权随机：仅在命中 prob 的候选中再均匀选取一条
        hits = [ev for prob, ev in weighted if random.random() < prob]
        if not hits:
            return None
        chosen = random.choice(hits)
        return chosen, self._deltas_from(chosen)

    # ---- 打工风险结算（80/15/5 分布） ---------------------------------------

    def roll_work_finish(self) -> Tuple[str, Dict[str, Any]]:
        """返回 (mode, payload)。mode ∈ normal/surprise/accident。payload 含倍率/delta/text。"""
        rules = self._rules().get("work_finish", {})
        if not isinstance(rules, dict):
            rules = {}
        try:
            normal_p = float(rules.get("normal_prob", 0.80) or 0)
            surprise_p = float(rules.get("surprise_prob", 0.15) or 0)
            accident_p = float(rules.get("accident_prob", 0.05) or 0)
        except (TypeError, ValueError):
            normal_p, surprise_p, accident_p = 0.80, 0.15, 0.05
        total = normal_p + surprise_p + accident_p
        if total <= 0:
            normal_p, surprise_p, accident_p = 0.80, 0.15, 0.05
            total = 1.0
        r = random.random() * (total if total > 0 else 1.0)
        if r < normal_p:
            return "normal", {"coin_multiplier": 1.0, "mood": 0, "intimacy": 0, "growth": 0, "text": ""}
        if r < normal_p + surprise_p:
            sub = rules.get("surprise", {}) if isinstance(rules.get("surprise"), dict) else {}
            return "surprise", {
                "coin_multiplier": self._f(sub.get("coin_multiplier"), 1.0),
                "mood": int(sub.get("mood", 0) or 0),
                "intimacy": int(sub.get("intimacy", 0) or 0),
                "growth": int(sub.get("growth", 0) or 0),
                "text": self._pick_text(sub),
            }
        sub = rules.get("accident", {}) if isinstance(rules.get("accident"), dict) else {}
        return "accident", {
            "coin_multiplier": self._f(sub.get("coin_multiplier"), 1.0),
            "mood": int(sub.get("mood", -10) or 0),
            "intimacy": int(sub.get("intimacy", 0) or 0),
            "growth": int(sub.get("growth", 0) or 0),
            "text": self._pick_text(sub),
        }

    @staticmethod
    def _pick_text(sub: Dict[str, Any]) -> str:
        rows = sub.get("texts")
        if isinstance(rows, list):
            texts = [str(row or "").strip() for row in rows if str(row or "").strip()]
            if texts:
                return random.choice(texts)
        return str(sub.get("text", "") or "")

    @staticmethod
    def _f(value: Any, default: float) -> float:
        try:
            return float(value or default)
        except (TypeError, ValueError):
            return default

    # ---- 辅助：把事件文本拼成展示行 ----------------------------------------

    @staticmethod
    def format_event_line(text: str) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        return f"\n✦ 今日奇遇：{text}"

    # ---- 事件日志读写辅助（供 sign/economy/catgirl service 复用） -----------
    # 这些方法读取 store 已有数据用于 roll 前的"今日已触发计数/去重"，
    # 以及在 op 内同事务写入 event_log/event_state。它们不自己持锁，依赖
    # 调用方的事务边界（store.get 在 RLock 下读、op 在 store.update RLock 下写）。

    @staticmethod
    def today_anchor_count(store, uid: str, today: str, anchor: str) -> int:
        try:
            state = store.get("event_state", uid, default=None)
            if not isinstance(state, dict):
                return 0
            if str(state.get("today", "")) != today:
                return 0
            counters = state.get("counters", {})
            return int(counters.get(anchor, 0)) if isinstance(counters, dict) else 0
        except Exception:
            return 0

    @staticmethod
    def today_triggered_ids(store, uid: str, today: str) -> List[str]:
        try:
            log = store.get("event_log", uid, default=None)
            if not isinstance(log, dict):
                return []
            if str(log.get("today", "")) != today:
                return []
            events = log.get("today_events", [])
            if not isinstance(events, list):
                return []
            return [str(row.get("event_id", "")) for row in events if isinstance(row, dict)]
        except Exception:
            return []

    @staticmethod
    def record_event(root: Dict[str, Any], uid: str, today: str, anchor: str, event_id: str, text: str) -> None:
        log = root.setdefault("event_log", {})
        user_log = log.setdefault(uid, {})
        if str(user_log.get("today", "")) != today:
            user_log["today"] = today
            user_log["today_events"] = []
        events_list = user_log.setdefault("today_events", [])
        events_list.append({"anchor": anchor, "event_id": event_id, "text": str(text or "")})
        state = root.setdefault("event_state", {})
        user_state = state.setdefault(uid, {})
        if str(user_state.get("today", "")) != today:
            user_state["today"] = today
            user_state["counters"] = {}
        counters = user_state.setdefault("counters", {})
        counters[anchor] = int(counters.get(anchor, 0)) + 1
