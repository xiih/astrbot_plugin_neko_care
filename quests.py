"""每日心愿系统。

每天按 runtime_config 的 ``daily_wishes.refresh_at_hour`` 刷新，为已有猫娘的
用户从 ``daily_wishes.templates`` 模板池中随机生成 1 个心愿。玩家完成对应养成
行为后，由 main.py 的 ``_after_care_action`` 钩子调用
``DailyWishService.progress_daily_wish(uid, type, amount, target_name)`` 累加进度；达成时在同一事务内
发放宝石、亲密度和成长奖励。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from .catgirl_schema import calc_stage
from .storage import JsonStore


DAILY_WISH_TEMPLATES: Tuple[Dict[str, Any], ...] = (
    {
        "id": "wish_feed",
        "name": "想吃草莓奶油蛋糕",
        "type": "feed",
        "target_name": "草莓奶油蛋糕",
        "target": 1,
        "text": "她盯着甜点橱窗看了好久，今天想吃一份草莓奶油蛋糕。",
        "reward_min": 45,
        "reward_max": 75,
        "intimacy_min": 2,
        "intimacy_max": 5,
        "growth_min": 1,
        "growth_max": 3,
        "min_stage": 0,
    },
    {
        "id": "wish_interact",
        "name": "想被你撸猫",
        "type": "interact",
        "target_name": "撸猫",
        "target": 1,
        "text": "她靠近你的手边蹭了蹭，今天想让你认真撸撸她。",
        "reward_min": 35,
        "reward_max": 65,
        "intimacy_min": 3,
        "intimacy_max": 6,
        "growth_min": 1,
        "growth_max": 3,
        "min_stage": 0,
    },
    {
        "id": "wish_cat_work",
        "name": "想去猫咖服务员",
        "type": "cat_work",
        "target_name": "猫咖服务员",
        "target": 1,
        "text": "她整理好围裙，今天想去猫咖服务员的岗位帮你赚一点钱。",
        "reward_min": 55,
        "reward_max": 95,
        "intimacy_min": 2,
        "intimacy_max": 5,
        "growth_min": 3,
        "growth_max": 6,
        "min_stage": 0,
    },
    {
        "id": "wish_buy_gift",
        "name": "想收到小鱼干礼盒",
        "type": "buy_gift",
        "target_name": "小鱼干礼盒",
        "target": 1,
        "text": "她假装路过礼物架，其实眼神一直停在小鱼干礼盒上。",
        "reward_min": 30,
        "reward_max": 60,
        "intimacy_min": 4,
        "intimacy_max": 8,
        "growth_min": 1,
        "growth_max": 2,
        "min_stage": 0,
    },
    {
        "id": "wish_care",
        "name": "想做按摩护理",
        "type": "care",
        "target_name": "按摩护理",
        "target": 1,
        "text": "她捏了捏肩膀，今天想做一次按摩护理放松一下。",
        "reward_min": 40,
        "reward_max": 80,
        "intimacy_min": 3,
        "intimacy_max": 7,
        "growth_min": 1,
        "growth_max": 3,
        "min_stage": 0,
    },
)


class DailyWishService:
    def __init__(
        self,
        store: JsonStore,
        runtime_config_provider: Callable[[], Dict[str, Any]] | None = None,
        economy=None,
        catgirl=None,
        events=None,
    ) -> None:
        self.store = store
        self.runtime_config_provider = runtime_config_provider
        self.economy = economy
        self.catgirl = catgirl
        self.events = events

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

    def _daily_wish_rules(self) -> Dict[str, Any]:
        data = self._runtime().get("daily_wishes", {})
        return data if isinstance(data, dict) else {}

    def _daily_wish_enabled(self) -> bool:
        return bool(self._daily_wish_rules().get("enabled", True))

    def _refresh_at_hour(self) -> int:
        try:
            rules = self._daily_wish_rules()
            legacy_quests = self._runtime().get("quests", {})
            if not isinstance(legacy_quests, dict):
                legacy_quests = {}
            value = rules.get("refresh_at_hour", legacy_quests.get("refresh_at_hour", 4))
            return max(0, min(23, int(value or 0)))
        except Exception:
            return 4

    def _wish_day_str(self) -> str:
        now = datetime.now()
        refresh_at_hour = self._refresh_at_hour()
        if refresh_at_hour > 0 and now.hour < refresh_at_hour:
            now = now - timedelta(days=1)
        return now.strftime("%Y-%m-%d")

    @staticmethod
    def _cat_stage(cat: Any) -> int:
        if not isinstance(cat, dict):
            return 0
        try:
            if "stage" not in cat:
                return max(0, int(calc_stage(cat.get("growth", 0), cat.get("intimacy", 0))))
            return max(0, int(cat.get("stage", 0) or 0))
        except Exception:
            return 0

    @classmethod
    def _template_allowed_for_stage(cls, template: Dict[str, Any], stage: int) -> bool:
        try:
            min_stage = max(0, int(template.get("min_stage", 0) or 0))
        except Exception:
            min_stage = 0
        return stage >= min_stage

    # ---- 每日心愿 ------------------------------------------------------------

    def _coin_name(self) -> str:
        try:
            if self.economy is not None:
                return self.economy._coin_name()
        except Exception:
            pass
        return "宝石"

    def _active_cat_snapshot(self, uid: str) -> Optional[Dict[str, Any]]:
        cat = None
        if self.catgirl is not None and hasattr(self.catgirl, "_get"):
            try:
                cat = self.catgirl._get(uid)
            except Exception:
                cat = None
        if cat is None:
            cat = self.store.get("catgirls", uid, default=None)
        if isinstance(cat, dict) and cat.get("name"):
            return cat
        return None

    def _daily_wish_templates(self, stage: int) -> List[Dict[str, Any]]:
        rules = self._daily_wish_rules()
        if not bool(rules.get("enabled", True)):
            return []
        rows = rules.get("templates")
        if not isinstance(rows, list):
            rows = list(DAILY_WISH_TEMPLATES)
        return [
            dict(row)
            for row in rows
            if isinstance(row, dict)
            and row.get("enabled", True)
            and self._template_allowed_for_stage(row, stage)
        ]

    @staticmethod
    def _daily_wish_entry_from_template(t: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(t.get("id", "") or ""),
            "name": str(t.get("name", "") or "今日心愿"),
            "type": str(t.get("type", "") or ""),
            "target_name": str(t.get("target_name", "") or ""),
            "target": max(1, int(t.get("target", 1) or 1)),
            "text": str(t.get("text", "") or ""),
            "reward_min": int(t.get("reward_min", 0) or 0),
            "reward_max": int(t.get("reward_max", 0) or 0),
            "intimacy_min": int(t.get("intimacy_min", 0) or 0),
            "intimacy_max": int(t.get("intimacy_max", 0) or 0),
            "growth_min": int(t.get("growth_min", 0) or 0),
            "growth_max": int(t.get("growth_max", 0) or 0),
            "min_stage": int(t.get("min_stage", 0) or 0),
            "progress": 0,
            "claimed": False,
        }

    def _refresh_daily_wish_if_stale(self, uid: str, force: bool = False) -> bool:
        today = self._wish_day_str()
        current = self.store.get("daily_wishes", uid, default=None)
        if (
            isinstance(current, dict)
            and str(current.get("today", "")) == today
            and isinstance(current.get("entry"), dict)
            and not force
        ):
            return True

        cat = self._active_cat_snapshot(uid)
        if not cat:
            return False
        templates = self._daily_wish_templates(self._cat_stage(cat))
        if not templates:
            return False
        entry = self._daily_wish_entry_from_template(random.choice(templates))

        def op(root):
            cats = root.setdefault("catgirls", {})
            active_cat = cats.get(uid)
            if not isinstance(active_cat, dict) or not active_cat.get("name"):
                return False
            daily_wishes = root.setdefault("daily_wishes", {})
            user = daily_wishes.setdefault(uid, {})
            if (
                isinstance(user, dict)
                and str(user.get("today", "")) == today
                and isinstance(user.get("entry"), dict)
                and not force
            ):
                return True
            user["today"] = today
            user["entry"] = entry
            return True

        return bool(self.store.update(op))

    @staticmethod
    def _norm_target(value: Any) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _target_matches(cls, expected: Any, actual: Any) -> bool:
        expected_text = cls._norm_target(expected)
        if not expected_text:
            return True
        actual_text = cls._norm_target(actual)
        if not actual_text:
            return False
        return expected_text == actual_text or expected_text in actual_text or actual_text in expected_text

    def progress_daily_wish(self, uid: str, wtype: str, amount: int = 1, target_name: str | None = None) -> None:
        if not wtype or amount <= 0:
            return
        if not self._refresh_daily_wish_if_stale(uid):
            return

        def op(root):
            daily_wishes = root.setdefault("daily_wishes", {})
            user = daily_wishes.setdefault(uid, {})
            entry = user.get("entry")
            if not isinstance(entry, dict):
                return False
            if entry.get("claimed") or str(entry.get("type", "")) != wtype:
                return False
            if not self._target_matches(entry.get("target_name", ""), target_name):
                return False

            catgirls = root.setdefault("catgirls", {})
            cat = catgirls.get(uid) if isinstance(catgirls.get(uid), dict) else None
            if not isinstance(cat, dict) or not cat.get("name"):
                return False
            if not self._template_allowed_for_stage(entry, self._cat_stage(cat)):
                return False

            target = max(1, int(entry.get("target", 1) or 1))
            cur = int(entry.get("progress", 0) or 0)
            if cur >= target:
                return False
            new_progress = min(target, cur + amount)
            entry["progress"] = new_progress
            if new_progress < target:
                return True

            coin = random.randint(
                int(entry.get("reward_min", 0)),
                max(int(entry.get("reward_min", 0)), int(entry.get("reward_max", 0))),
            )
            intimacy_add = random.randint(
                int(entry.get("intimacy_min", 0)),
                max(int(entry.get("intimacy_min", 0)), int(entry.get("intimacy_max", 0))),
            )
            growth_add = random.randint(
                int(entry.get("growth_min", 0)),
                max(int(entry.get("growth_min", 0)), int(entry.get("growth_max", 0))),
            )
            wallet = root.setdefault("wallet", {})
            if coin:
                wallet[uid] = int(wallet.get(uid, 0)) + coin
            cat["intimacy"] = int(cat.get("intimacy", 0)) + intimacy_add
            cat["growth"] = int(cat.get("growth", 0)) + growth_add
            stage_msg = ""
            if self.catgirl is not None and hasattr(self.catgirl, "_advance_stage"):
                try:
                    cat, stage_msg = self.catgirl._advance_stage(cat)
                except Exception:
                    stage_msg = ""
            catgirls[uid] = cat
            entry["claimed"] = True
            entry["reward_coin"] = coin
            entry["reward_intimacy"] = intimacy_add
            entry["reward_growth"] = growth_add
            entry["stage_msg"] = str(stage_msg or "")
            return True

        self.store.update(op)

    def get_daily_wish(self, uid: str) -> Tuple[bool, str, Optional[Any]]:
        if not self._daily_wish_enabled():
            return False, "每日心愿未启用。", None
        cat = self._active_cat_snapshot(uid)
        if not cat:
            msg = "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。"
            return False, msg, None
        if not self._daily_wish_templates(self._cat_stage(cat)):
            return False, "今天没有可用的每日心愿模板。", None
        if not self._refresh_daily_wish_if_stale(uid):
            msg = "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。"
            return False, msg, None

        data = self.store.get("daily_wishes", uid, default=None) or {}
        entry = data.get("entry") if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            return False, "今天还没有生成心愿喔～稍后再看看。", None

        coin_name = self._coin_name()
        name = str(entry.get("name", "今日心愿") or "今日心愿")
        text = str(entry.get("text", "") or "")
        target_name = str(entry.get("target_name", "") or "").strip()
        progress = int(entry.get("progress", 0) or 0)
        target = max(1, int(entry.get("target", 1) or 1))
        claimed = bool(entry.get("claimed"))
        status = "已完成" if claimed else f"进度 {min(progress, target)}/{target}"
        target_line = f"目标：{target_name}" if target_name else ""
        reward_line = (
            f"奖励：{int(entry.get('reward_min', 0))}-{int(entry.get('reward_max', 0))} {coin_name}"
            f"，亲密 {int(entry.get('intimacy_min', 0))}-{int(entry.get('intimacy_max', 0))}"
            f"，成长 {int(entry.get('growth_min', 0))}-{int(entry.get('growth_max', 0))}"
        )
        if claimed:
            reward_line = (
                f"已发放：{int(entry.get('reward_coin', 0))} {coin_name}"
                f"，亲密 +{int(entry.get('reward_intimacy', 0))}"
                f"，成长 +{int(entry.get('reward_growth', 0))}"
            )

        target_msg = f"\n{target_line}" if target_line else ""
        msg = f"今日心愿：{name}{target_msg}\n{text}\n状态：{status}\n{reward_line}"
        stage_msg = str(entry.get("stage_msg", "") or "").strip()
        if stage_msg:
            msg += f"\n{stage_msg}"

        card = None
        try:
            if self.catgirl is not None and hasattr(self.catgirl, "draw_info_card"):
                card = self.catgirl.draw_info_card(
                    "猫娘心愿",
                    subtitle=name,
                    lines=[target_line, text, reward_line, stage_msg],
                    metrics=[
                        ("状态", status),
                        ("进度", f"{min(progress, target)}/{target}"),
                        ("类型", str(entry.get("type", ""))),
                        ("刷新", f"{self._refresh_at_hour()}:00"),
                    ],
                    footer="完成对应养成行为后，奖励会自动发放。",
                    tag=f"daily_wish_{uid}",
                )
        except Exception:
            card = None
        return True, msg, card

    def status_brief(self, uid: str) -> Dict[str, Any]:
        """返回状态卡可嵌入的每日心愿摘要，不额外渲染心愿卡。"""
        if not self._daily_wish_enabled():
            return {}
        cat = self._active_cat_snapshot(uid)
        if not cat:
            return {}
        if not self._daily_wish_templates(self._cat_stage(cat)):
            return {"lines": ["今日心愿：暂无可用模板"], "metric": ("心愿进度", "-")}
        if not self._refresh_daily_wish_if_stale(uid):
            return {}

        data = self.store.get("daily_wishes", uid, default=None) or {}
        entry = data.get("entry") if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            return {}

        name = str(entry.get("name", "今日心愿") or "今日心愿")
        text = str(entry.get("text", "") or "").strip()
        target_name = str(entry.get("target_name", "") or "").strip()
        progress = int(entry.get("progress", 0) or 0)
        target = max(1, int(entry.get("target", 1) or 1))
        done_progress = min(progress, target)
        claimed = bool(entry.get("claimed"))
        status = "已完成" if claimed else f"{done_progress}/{target}"
        metric_value = f"{target}/{target}" if claimed else f"{done_progress}/{target}"
        target_suffix = f"｜目标：{target_name}" if target_name else ""
        lines = [f"今日心愿：{name}（{status}）{target_suffix}"]
        if text:
            lines.append(text)
        if claimed:
            lines.append(
                f"心愿奖励已发放：{int(entry.get('reward_coin', 0))} {self._coin_name()}"
                f"，亲密 +{int(entry.get('reward_intimacy', 0))}"
                f"，成长 +{int(entry.get('reward_growth', 0))}"
            )
        return {"lines": lines, "metric": ("心愿进度", metric_value)}
