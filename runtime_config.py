import copy
import json
import re
import time
from pathlib import Path
from typing import Any, Dict


def default_runtime_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "economy": {
            "coin_name": "宝石",
            "sign_min_reward": 80,
            "sign_max_reward": 150,
            "daily_work_min_reward": 50,
            "daily_work_max_reward": 120,
            "daily_work_events": [
                "你在猫咖帮忙端了一天甜点。",
                "你帮老板整理仓库，累得耳朵都耷拉下来了。",
                "你接了一个临时外包，顺利完成。",
                "你在便利店值班，遇到了一群买关东煮的猫娘。",
                "你帮别人修好了坏掉的自动贩卖机。",
            ],
        },
        "wish": {
            "probability": 0.8,
            "pity": 3,
            "appearance_change_price": 1200,
        },
        "care": {
            "feed_satiety_limit": 85,
            "satiety_decay_minutes": 2880,
            "mood_decay_per_day": 3,
            "energy_recovery_per_day": 20,
            "health_hungry_decay_per_day": 5,
            "health_low_mood_decay_per_day": 2,
            "health_recovery_per_day": 1,
            "health_hungry_satiety_threshold": 20,
            "health_low_mood_threshold": 30,
            "runaway_after_zero_hours": 24,
            "interaction_daily_limit": 5,
            "interaction_cooldown_seconds": 300,
            "interaction_energy_cost": 6,
            "interaction_soft_limit_extra": 3,
            "interaction_heavy_limit_extra": 7,
            "interaction_soft_limit_multiplier": 0.6,
            "interaction_heavy_limit_multiplier": 0.3,
            "interaction_minimal_limit_multiplier": 0.1,
            "interaction_good_mood_threshold": 80,
            "interaction_low_mood_threshold": 50,
            "interaction_bad_mood_threshold": 30,
            "interaction_high_mood_multiplier": 1.15,
            "interaction_low_mood_multiplier": 0.75,
            "interaction_bad_mood_multiplier": 0.5,
            "feed_healthy_threshold": 70,
            "feed_low_health_threshold": 40,
            "feed_bad_health_threshold": 20,
            "feed_low_health_multiplier": 0.85,
            "feed_bad_health_multiplier": 0.65,
            "feed_critical_health_multiplier": 0.45,
            "work_stable_energy_threshold": 50,
            "work_high_energy_threshold": 80,
            "work_stable_energy_reward_multiplier": 1.05,
            "work_high_energy_reward_multiplier": 1.15,
            "work_min_health": 40,
            "interact_min_health": 25,
            "work_min_satiety": 25,
            "work_min_mood": 35,
        },
        "personalities": {
            "effects": [
                {
                    "name": "害羞",
                    "satiety_decay_multiplier": 1,
                    "mood_decay_multiplier": 1,
                    "energy_recovery_multiplier": 1,
                    "health_recovery_multiplier": 1,
                    "feed_satiety_multiplier": 1,
                    "feed_mood_multiplier": 1,
                    "feed_growth_multiplier": 1,
                    "feed_intimacy_multiplier": 1,
                    "work_reward_multiplier": 1,
                    "work_energy_cost_multiplier": 1,
                    "work_growth_multiplier": 1,
                    "work_intimacy_multiplier": 1,
                    "interaction_mood_multiplier": 1,
                    "interaction_growth_multiplier": 1,
                    "interaction_intimacy_multiplier": 1.15,
                    "interaction_energy_cost_multiplier": 1,
                    "enabled": True,
                },
                {
                    "name": "活泼",
                    "satiety_decay_multiplier": 1.05,
                    "mood_decay_multiplier": 1,
                    "energy_recovery_multiplier": 1.12,
                    "health_recovery_multiplier": 1,
                    "feed_satiety_multiplier": 1,
                    "feed_mood_multiplier": 1.08,
                    "feed_growth_multiplier": 1,
                    "feed_intimacy_multiplier": 1,
                    "work_reward_multiplier": 1,
                    "work_energy_cost_multiplier": 1.08,
                    "work_growth_multiplier": 1.08,
                    "work_intimacy_multiplier": 1,
                    "interaction_mood_multiplier": 1.12,
                    "interaction_growth_multiplier": 1.12,
                    "interaction_intimacy_multiplier": 1,
                    "interaction_energy_cost_multiplier": 1.05,
                    "enabled": True,
                },
                {
                    "name": "傲娇",
                    "satiety_decay_multiplier": 1,
                    "mood_decay_multiplier": 1.08,
                    "energy_recovery_multiplier": 1,
                    "health_recovery_multiplier": 1,
                    "feed_satiety_multiplier": 1,
                    "feed_mood_multiplier": 0.95,
                    "feed_growth_multiplier": 1.05,
                    "feed_intimacy_multiplier": 0.95,
                    "work_reward_multiplier": 1.05,
                    "work_energy_cost_multiplier": 1,
                    "work_growth_multiplier": 1.05,
                    "work_intimacy_multiplier": 0.95,
                    "interaction_mood_multiplier": 0.95,
                    "interaction_growth_multiplier": 1.05,
                    "interaction_intimacy_multiplier": 0.95,
                    "interaction_energy_cost_multiplier": 1,
                    "enabled": True,
                },
                {
                    "name": "温柔",
                    "satiety_decay_multiplier": 1,
                    "mood_decay_multiplier": 0.95,
                    "energy_recovery_multiplier": 1,
                    "health_recovery_multiplier": 1.15,
                    "feed_satiety_multiplier": 1,
                    "feed_mood_multiplier": 1.05,
                    "feed_growth_multiplier": 1,
                    "feed_intimacy_multiplier": 1.05,
                    "work_reward_multiplier": 1,
                    "work_energy_cost_multiplier": 1,
                    "work_growth_multiplier": 1,
                    "work_intimacy_multiplier": 1.05,
                    "interaction_mood_multiplier": 1.08,
                    "interaction_growth_multiplier": 1,
                    "interaction_intimacy_multiplier": 1.08,
                    "interaction_energy_cost_multiplier": 1,
                    "enabled": True,
                },
                {
                    "name": "贪吃",
                    "satiety_decay_multiplier": 1.15,
                    "mood_decay_multiplier": 1,
                    "energy_recovery_multiplier": 1,
                    "health_recovery_multiplier": 1,
                    "feed_satiety_multiplier": 1.2,
                    "feed_mood_multiplier": 1.12,
                    "feed_growth_multiplier": 1,
                    "feed_intimacy_multiplier": 1,
                    "work_reward_multiplier": 1,
                    "work_energy_cost_multiplier": 1,
                    "work_growth_multiplier": 1,
                    "work_intimacy_multiplier": 1,
                    "interaction_mood_multiplier": 1,
                    "interaction_growth_multiplier": 1,
                    "interaction_intimacy_multiplier": 1,
                    "interaction_energy_cost_multiplier": 1,
                    "enabled": True,
                },
                {
                    "name": "慵懒",
                    "satiety_decay_multiplier": 0.95,
                    "mood_decay_multiplier": 0.92,
                    "energy_recovery_multiplier": 1.15,
                    "health_recovery_multiplier": 1,
                    "feed_satiety_multiplier": 1,
                    "feed_mood_multiplier": 1,
                    "feed_growth_multiplier": 0.95,
                    "feed_intimacy_multiplier": 1,
                    "work_reward_multiplier": 0.92,
                    "work_energy_cost_multiplier": 0.85,
                    "work_growth_multiplier": 0.95,
                    "work_intimacy_multiplier": 1,
                    "interaction_mood_multiplier": 1,
                    "interaction_growth_multiplier": 0.95,
                    "interaction_intimacy_multiplier": 1.05,
                    "interaction_energy_cost_multiplier": 0.9,
                    "enabled": True,
                },
                {
                    "name": "认真",
                    "satiety_decay_multiplier": 1,
                    "mood_decay_multiplier": 1,
                    "energy_recovery_multiplier": 0.98,
                    "health_recovery_multiplier": 1,
                    "feed_satiety_multiplier": 1,
                    "feed_mood_multiplier": 1,
                    "feed_growth_multiplier": 1.1,
                    "feed_intimacy_multiplier": 1,
                    "work_reward_multiplier": 1.15,
                    "work_energy_cost_multiplier": 1.05,
                    "work_growth_multiplier": 1.12,
                    "work_intimacy_multiplier": 1,
                    "interaction_mood_multiplier": 1,
                    "interaction_growth_multiplier": 1.08,
                    "interaction_intimacy_multiplier": 1,
                    "interaction_energy_cost_multiplier": 1,
                    "enabled": True,
                },
                {
                    "name": "黏人",
                    "satiety_decay_multiplier": 1,
                    "mood_decay_multiplier": 0.9,
                    "energy_recovery_multiplier": 1,
                    "health_recovery_multiplier": 1,
                    "feed_satiety_multiplier": 1,
                    "feed_mood_multiplier": 1.05,
                    "feed_growth_multiplier": 1,
                    "feed_intimacy_multiplier": 1.12,
                    "work_reward_multiplier": 1,
                    "work_energy_cost_multiplier": 1,
                    "work_growth_multiplier": 1,
                    "work_intimacy_multiplier": 1.1,
                    "interaction_mood_multiplier": 1.05,
                    "interaction_growth_multiplier": 1,
                    "interaction_intimacy_multiplier": 1.2,
                    "interaction_energy_cost_multiplier": 1,
                    "enabled": True,
                },
            ],
        },
        "feed": {
            "satiety_add_min": 20,
            "satiety_add_max": 35,
            "mood_add_min": 2,
            "mood_add_max": 8,
            "health_add_min": 0,
            "health_add_max": 3,
            "energy_add_min": 3,
            "energy_add_max": 8,
            "growth_add_min": 5,
            "growth_add_max": 12,
            "intimacy_add_min": 1,
            "intimacy_add_max": 4,
            "foods": [
                {"name": "草莓奶油蛋糕", "cost_min": 18, "cost_max": 38, "verb": "吃", "enabled": True},
                {"name": "热乎乎的蛋包饭", "cost_min": 15, "cost_max": 32, "verb": "吃", "enabled": True},
                {"name": "芝士焗饭", "cost_min": 20, "cost_max": 45, "verb": "吃", "enabled": True},
                {"name": "炸鸡块", "cost_min": 12, "cost_max": 28, "verb": "吃", "enabled": True},
                {"name": "牛奶布丁", "cost_min": 8, "cost_max": 18, "verb": "吃", "enabled": True},
                {"name": "珍珠奶茶", "cost_min": 10, "cost_max": 24, "verb": "喝", "enabled": True},
                {"name": "抹茶拿铁", "cost_min": 12, "cost_max": 26, "verb": "喝", "enabled": True},
                {"name": "可颂面包", "cost_min": 8, "cost_max": 20, "verb": "吃", "enabled": True},
                {"name": "巧克力曲奇", "cost_min": 6, "cost_max": 16, "verb": "吃", "enabled": True},
                {"name": "海鲜乌冬面", "cost_min": 25, "cost_max": 55, "verb": "吃", "enabled": True},
                {"name": "小鱼干便当", "cost_min": 16, "cost_max": 36, "verb": "吃", "enabled": True},
                {"name": "奶油蘑菇汤", "cost_min": 12, "cost_max": 30, "verb": "喝", "enabled": True},
            ],
        },
        "work": {
            "reward_stage_base": 0.8,
            "reward_stage_step": 0.12,
            "jobs": [
                {"id": "cat_cafe", "name": "猫咖服务员", "reward_min": 120, "reward_max": 220, "duration_minutes": 45, "energy_cost": 22, "satiety_cost": 8, "mood_cost": 2, "growth_min": 5, "growth_max": 10, "intimacy_min": 1, "intimacy_max": 3, "mood_reward": 1, "enabled": True},
                {"id": "dessert_shop", "name": "甜点店看板娘", "reward_min": 100, "reward_max": 200, "duration_minutes": 60, "energy_cost": 26, "satiety_cost": 10, "mood_cost": 3, "growth_min": 6, "growth_max": 12, "intimacy_min": 2, "intimacy_max": 4, "mood_reward": 1, "enabled": True},
                {"id": "night_store", "name": "便利店夜班", "reward_min": 160, "reward_max": 300, "duration_minutes": 120, "energy_cost": 42, "satiety_cost": 16, "mood_cost": 8, "growth_min": 10, "growth_max": 18, "intimacy_min": 2, "intimacy_max": 5, "mood_reward": 1, "enabled": True},
                {"id": "doujin_booth", "name": "同人展摊位助手", "reward_min": 180, "reward_max": 360, "duration_minutes": 180, "energy_cost": 55, "satiety_cost": 20, "mood_cost": 10, "growth_min": 14, "growth_max": 24, "intimacy_min": 3, "intimacy_max": 7, "mood_reward": 1, "enabled": True},
                {"id": "milk_tea", "name": "奶茶店试喝员", "reward_min": 90, "reward_max": 180, "duration_minutes": 30, "energy_cost": 16, "satiety_cost": 6, "mood_cost": 1, "growth_min": 4, "growth_max": 8, "intimacy_min": 1, "intimacy_max": 3, "mood_reward": 1, "enabled": True},
            ],
        },
        "interactions": {
            "effects": [
                {"command": "撸猫", "text": "你轻轻撸了撸她的头发，她舒服地眯起眼睛。", "mood_min": 6, "mood_max": 12, "intimacy_min": 2, "intimacy_max": 5, "growth_min": 2, "growth_max": 5, "energy_cost": 0, "min_stage": 0, "enabled": True},
                {"command": "逗猫", "text": "你拿出小玩具逗她，她开心地扑来扑去。", "mood_min": 8, "mood_max": 15, "intimacy_min": 2, "intimacy_max": 5, "growth_min": 3, "growth_max": 6, "energy_cost": 6, "min_stage": 0, "enabled": True},
                {"command": "摸猫", "text": "你摸了摸她的脑袋，她小声地喵了一下。", "mood_min": 4, "mood_max": 10, "intimacy_min": 3, "intimacy_max": 6, "growth_min": 2, "growth_max": 5, "energy_cost": 0, "min_stage": 0, "enabled": True},
                {"command": "rua猫", "text": "你把她 rua 成了一团软乎乎的猫猫球。", "mood_min": 5, "mood_max": 12, "intimacy_min": 3, "intimacy_max": 7, "growth_min": 2, "growth_max": 5, "energy_cost": 2, "min_stage": 0, "enabled": True},
                {"command": "陪猫娘", "text": "你陪她聊了一会儿，她看起来安心了很多。", "mood_min": 4, "mood_max": 8, "intimacy_min": 5, "intimacy_max": 10, "growth_min": 3, "growth_max": 7, "energy_cost": 0, "min_stage": 0, "enabled": True},
                {"command": "陪猫猫", "text": "你陪她窝在一起晒太阳，气氛软绵绵的。", "mood_min": 4, "mood_max": 8, "intimacy_min": 5, "intimacy_max": 10, "growth_min": 3, "growth_max": 7, "energy_cost": 0, "min_stage": 0, "enabled": True},
                {"command": "贴贴猫娘", "text": "你和她贴贴了一下，她脸红地别过头。", "mood_min": 4, "mood_max": 8, "intimacy_min": 6, "intimacy_max": 12, "growth_min": 4, "growth_max": 8, "energy_cost": 0, "min_stage": 2, "enabled": True},
                {"command": "贴贴猫猫", "text": "你和她贴贴了一下，她尾巴轻轻晃了晃。", "mood_min": 4, "mood_max": 8, "intimacy_min": 6, "intimacy_max": 12, "growth_min": 4, "growth_max": 8, "energy_cost": 0, "min_stage": 2, "enabled": True},
            ],
        },
    }


class NekoRuntimeConfig:
    def __init__(self, path: Path, legacy_config: Dict[str, Any] | None = None):
        self.path = Path(path)
        self.data = default_runtime_config()
        first_boot = not self.path.exists()
        self.load()
        if first_boot and legacy_config:
            self.data = self.normalize(self._with_legacy_config(self.data, legacy_config))
            self.save()

    def load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                self.data = self.normalize(raw)
            except Exception:
                self.data = default_runtime_config()
        else:
            self.data = default_runtime_config()
            self.save()
        return self.snapshot()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def snapshot(self) -> Dict[str, Any]:
        return copy.deepcopy(self.data)

    def replace(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.data = self.normalize(payload)
        self.save()
        return self.snapshot()

    def reset(self) -> Dict[str, Any]:
        self.data = default_runtime_config()
        self.save()
        return self.snapshot()

    def normalize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        base = default_runtime_config()
        src = payload if isinstance(payload, dict) else {}

        economy = src.get("economy") if isinstance(src.get("economy"), dict) else {}
        base["economy"].update({
            "coin_name": self._text(economy.get("coin_name"), "宝石", 16),
            "sign_min_reward": self._int(economy.get("sign_min_reward"), 80, 0, 1_000_000),
            "sign_max_reward": self._int(economy.get("sign_max_reward"), 150, 0, 1_000_000),
            "daily_work_min_reward": self._int(economy.get("daily_work_min_reward"), 50, 0, 1_000_000),
            "daily_work_max_reward": self._int(economy.get("daily_work_max_reward"), 120, 0, 1_000_000),
            "daily_work_events": self._text_list(economy.get("daily_work_events"), base["economy"]["daily_work_events"], 80, 20),
        })
        self._ensure_order(base["economy"], "sign_min_reward", "sign_max_reward")
        self._ensure_order(base["economy"], "daily_work_min_reward", "daily_work_max_reward")

        wish = src.get("wish") if isinstance(src.get("wish"), dict) else {}
        base["wish"].update({
            "probability": self._float(wish.get("probability"), 0.8, 0, 1),
            "pity": self._int(wish.get("pity"), 3, 1, 365),
            "appearance_change_price": self._int(wish.get("appearance_change_price"), 1200, 0, 1_000_000),
        })

        care = src.get("care") if isinstance(src.get("care"), dict) else {}
        for key, default, low, high in [
            ("feed_satiety_limit", 85, 0, 100),
            ("satiety_decay_minutes", 2880, 1, 100_000),
            ("mood_decay_per_day", 3, 0, 1000),
            ("energy_recovery_per_day", 20, 0, 1000),
            ("health_hungry_decay_per_day", 5, 0, 1000),
            ("health_low_mood_decay_per_day", 2, 0, 1000),
            ("health_recovery_per_day", 1, 0, 1000),
            ("health_hungry_satiety_threshold", 20, 0, 100),
            ("health_low_mood_threshold", 30, 0, 100),
            ("runaway_after_zero_hours", 24, 1, 10_000),
            ("interaction_daily_limit", 5, 0, 1000),
            ("interaction_cooldown_seconds", 300, 0, 86_400),
            ("interaction_energy_cost", 6, 0, 100),
            ("interaction_soft_limit_extra", 3, 0, 1000),
            ("interaction_heavy_limit_extra", 7, 0, 1000),
            ("interaction_soft_limit_multiplier", 0.6, 0, 10),
            ("interaction_heavy_limit_multiplier", 0.3, 0, 10),
            ("interaction_minimal_limit_multiplier", 0.1, 0, 10),
            ("interaction_good_mood_threshold", 80, 0, 100),
            ("interaction_low_mood_threshold", 50, 0, 100),
            ("interaction_bad_mood_threshold", 30, 0, 100),
            ("interaction_high_mood_multiplier", 1.15, 0, 10),
            ("interaction_low_mood_multiplier", 0.75, 0, 10),
            ("interaction_bad_mood_multiplier", 0.5, 0, 10),
            ("feed_healthy_threshold", 70, 0, 100),
            ("feed_low_health_threshold", 40, 0, 100),
            ("feed_bad_health_threshold", 20, 0, 100),
            ("feed_low_health_multiplier", 0.85, 0, 10),
            ("feed_bad_health_multiplier", 0.65, 0, 10),
            ("feed_critical_health_multiplier", 0.45, 0, 10),
            ("work_stable_energy_threshold", 50, 0, 100),
            ("work_high_energy_threshold", 80, 0, 100),
            ("work_stable_energy_reward_multiplier", 1.05, 0, 10),
            ("work_high_energy_reward_multiplier", 1.15, 0, 10),
            ("work_min_health", 40, 0, 100),
            ("interact_min_health", 25, 0, 100),
            ("work_min_satiety", 25, 0, 100),
            ("work_min_mood", 35, 0, 100),
        ]:
            base["care"][key] = self._float(care.get(key), default, low, high)
        base["care"]["interaction_daily_limit"] = int(base["care"]["interaction_daily_limit"])
        for key in ["interaction_cooldown_seconds", "interaction_energy_cost", "interaction_soft_limit_extra", "interaction_heavy_limit_extra"]:
            base["care"][key] = int(base["care"][key])
        if base["care"]["interaction_heavy_limit_extra"] < base["care"]["interaction_soft_limit_extra"]:
            base["care"]["interaction_heavy_limit_extra"] = base["care"]["interaction_soft_limit_extra"]
        if base["care"]["interaction_low_mood_threshold"] < base["care"]["interaction_bad_mood_threshold"]:
            base["care"]["interaction_low_mood_threshold"] = base["care"]["interaction_bad_mood_threshold"]
        if base["care"]["interaction_good_mood_threshold"] < base["care"]["interaction_low_mood_threshold"]:
            base["care"]["interaction_good_mood_threshold"] = base["care"]["interaction_low_mood_threshold"]
        if base["care"]["feed_low_health_threshold"] < base["care"]["feed_bad_health_threshold"]:
            base["care"]["feed_low_health_threshold"] = base["care"]["feed_bad_health_threshold"]
        if base["care"]["feed_healthy_threshold"] < base["care"]["feed_low_health_threshold"]:
            base["care"]["feed_healthy_threshold"] = base["care"]["feed_low_health_threshold"]
        if base["care"]["work_high_energy_threshold"] < base["care"]["work_stable_energy_threshold"]:
            base["care"]["work_high_energy_threshold"] = base["care"]["work_stable_energy_threshold"]

        feed = src.get("feed") if isinstance(src.get("feed"), dict) else {}
        for key, default, low, high in [
            ("satiety_add_min", 20, 0, 100),
            ("satiety_add_max", 35, 0, 100),
            ("mood_add_min", 2, 0, 100),
            ("mood_add_max", 8, 0, 100),
            ("health_add_min", 0, 0, 100),
            ("health_add_max", 3, 0, 100),
            ("energy_add_min", 3, 0, 100),
            ("energy_add_max", 8, 0, 100),
            ("growth_add_min", 5, 0, 10000),
            ("growth_add_max", 12, 0, 10000),
            ("intimacy_add_min", 1, 0, 10000),
            ("intimacy_add_max", 4, 0, 10000),
        ]:
            base["feed"][key] = self._int(feed.get(key), default, low, high)
        for a, b in [
            ("satiety_add_min", "satiety_add_max"),
            ("mood_add_min", "mood_add_max"),
            ("health_add_min", "health_add_max"),
            ("energy_add_min", "energy_add_max"),
            ("growth_add_min", "growth_add_max"),
            ("intimacy_add_min", "intimacy_add_max"),
        ]:
            self._ensure_order(base["feed"], a, b)
        base["feed"]["foods"] = self._foods(feed.get("foods"), base["feed"]["foods"])

        work = src.get("work") if isinstance(src.get("work"), dict) else {}
        for key, default, low, high in [
            ("reward_stage_base", 0.8, 0, 10),
            ("reward_stage_step", 0.12, 0, 10),
        ]:
            base["work"][key] = self._float(work.get(key), default, low, high)
        base["work"]["jobs"] = self._jobs(work.get("jobs"), base["work"]["jobs"])

        personalities = src.get("personalities") if isinstance(src.get("personalities"), dict) else {}
        base["personalities"]["effects"] = self._personalities(
            personalities.get("effects"),
            base["personalities"]["effects"],
        )

        interactions = src.get("interactions") if isinstance(src.get("interactions"), dict) else {}
        base["interactions"]["effects"] = self._interactions(interactions.get("effects"), base["interactions"]["effects"])
        return base

    def _with_legacy_config(self, config: Dict[str, Any], legacy_config: Dict[str, Any]) -> Dict[str, Any]:
        merged = copy.deepcopy(config)
        if not isinstance(legacy_config, dict):
            return merged

        economy = merged.setdefault("economy", {})
        legacy_map = {
            "coin_name": "coin_name",
            "sign_min_reward": "sign_min_reward",
            "sign_max_reward": "sign_max_reward",
            "work_min_reward": "daily_work_min_reward",
            "work_max_reward": "daily_work_max_reward",
        }
        for old_key, new_key in legacy_map.items():
            if old_key in legacy_config:
                economy[new_key] = legacy_config.get(old_key)

        wish = merged.setdefault("wish", {})
        if "catgirl_wish_probability" in legacy_config:
            wish["probability"] = legacy_config.get("catgirl_wish_probability")
        if "catgirl_wish_pity" in legacy_config:
            wish["pity"] = legacy_config.get("catgirl_wish_pity")
        if "appearance_change_price" in legacy_config:
            wish["appearance_change_price"] = legacy_config.get("appearance_change_price")

        return merged

    def _foods(self, rows, defaults):
        result = []
        for row in rows if isinstance(rows, list) else defaults:
            if not isinstance(row, dict):
                continue
            item = {
                "name": self._text(row.get("name"), "食物", 30),
                "cost_min": self._int(row.get("cost_min"), 1, 0, 1_000_000),
                "cost_max": self._int(row.get("cost_max"), 1, 0, 1_000_000),
                "verb": self._text(row.get("verb"), "吃", 8),
                "enabled": bool(row.get("enabled", True)),
            }
            self._ensure_order(item, "cost_min", "cost_max")
            result.append(item)
        return result or copy.deepcopy(defaults)

    def _jobs(self, rows, defaults):
        result = []
        seen = set()
        for row in rows if isinstance(rows, list) else defaults:
            if not isinstance(row, dict):
                continue
            name = self._text(row.get("name"), "打工地点", 40)
            job_id = self._id(row.get("id"), name, seen)
            item = {
                "id": job_id,
                "name": name,
                "reward_min": self._int(row.get("reward_min"), 1, 1, 1_000_000),
                "reward_max": self._int(row.get("reward_max"), 1, 1, 1_000_000),
                "duration_minutes": self._int(row.get("duration_minutes"), 30, 1, 100_000),
                "energy_cost": self._int(row.get("energy_cost"), 0, 0, 100),
                "satiety_cost": self._int(row.get("satiety_cost"), 0, 0, 100),
                "mood_cost": self._int(row.get("mood_cost"), 0, 0, 100),
                "growth_min": self._int(row.get("growth_min"), 0, 0, 100_000),
                "growth_max": self._int(row.get("growth_max"), 0, 0, 100_000),
                "intimacy_min": self._int(row.get("intimacy_min"), 0, 0, 100_000),
                "intimacy_max": self._int(row.get("intimacy_max"), 0, 0, 100_000),
                "mood_reward": self._float(row.get("mood_reward"), 1, 0, 100),
                "enabled": bool(row.get("enabled", True)),
            }
            for a, b in [("reward_min", "reward_max"), ("growth_min", "growth_max"), ("intimacy_min", "intimacy_max")]:
                self._ensure_order(item, a, b)
            result.append(item)
        return result or copy.deepcopy(defaults)

    def _interactions(self, rows, defaults):
        result = []
        seen = set()
        for row in rows if isinstance(rows, list) else defaults:
            if not isinstance(row, dict):
                continue
            command = self._text(row.get("command"), "互动", 20)
            if command in seen:
                continue
            seen.add(command)
            item = {
                "command": command,
                "text": self._text(row.get("text"), "你陪她玩了一会儿。", 120),
                "mood_min": self._int(row.get("mood_min"), 1, 0, 100),
                "mood_max": self._int(row.get("mood_max"), 1, 0, 100),
                "intimacy_min": self._int(row.get("intimacy_min"), 1, 0, 100_000),
                "intimacy_max": self._int(row.get("intimacy_max"), 1, 0, 100_000),
                "growth_min": self._int(row.get("growth_min"), 1, 0, 100_000),
                "growth_max": self._int(row.get("growth_max"), 1, 0, 100_000),
                "energy_cost": self._int(row.get("energy_cost"), 0, 0, 100),
                "min_stage": self._int(row.get("min_stage"), 0, 0, 6),
                "enabled": bool(row.get("enabled", True)),
            }
            for a, b in [("mood_min", "mood_max"), ("intimacy_min", "intimacy_max"), ("growth_min", "growth_max")]:
                self._ensure_order(item, a, b)
            result.append(item)
        return result or copy.deepcopy(defaults)

    def _personalities(self, rows, defaults):
        defaults_by_name = {row.get("name"): row for row in defaults if isinstance(row, dict)}
        rows_by_name = {}
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("name"):
                    rows_by_name[str(row.get("name")).strip()] = row

        result = []
        for name, default_row in defaults_by_name.items():
            row = rows_by_name.get(name, {})
            item = {"name": name}
            for key, default_value in default_row.items():
                if key == "name":
                    continue
                if key == "enabled":
                    item[key] = bool(row.get(key, default_value))
                else:
                    item[key] = self._float(row.get(key), default_value, 0, 10)
            result.append(item)
        return result or copy.deepcopy(defaults)

    def _text_list(self, value, default, max_len, max_count):
        rows = value if isinstance(value, list) else default
        result = [self._text(x, "", max_len) for x in rows]
        result = [x for x in result if x][:max_count]
        return result or copy.deepcopy(default)

    def _text(self, value, default, max_len):
        text = str(value if value is not None else default).strip()
        text = re.sub(r"[\r\n\t]+", " ", text)
        return (text or default)[:max_len]

    def _id(self, value, name, seen):
        raw = str(value or "").strip().lower()
        raw = re.sub(r"[^a-z0-9_-]+", "_", raw)[:40].strip("_")
        if not raw:
            raw = f"job_{int(time.time() * 1000)}"
        base = raw
        idx = 2
        while raw in seen:
            raw = f"{base}_{idx}"
            idx += 1
        seen.add(raw)
        return raw

    def _int(self, value, default, low, high):
        try:
            value = int(float(value))
        except Exception:
            value = int(default)
        return max(int(low), min(int(high), value))

    def _float(self, value, default, low, high):
        try:
            value = float(value)
        except Exception:
            value = float(default)
        return max(float(low), min(float(high), value))

    def _ensure_order(self, obj, low_key, high_key):
        if obj[high_key] < obj[low_key]:
            obj[high_key] = obj[low_key]
