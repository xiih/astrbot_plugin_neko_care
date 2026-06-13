import asyncio
import ipaddress
import math
import random
import re
import shutil
import socket
import time
import aiohttp
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable, Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .storage import JsonStore
from .economy import EconomyService
from .catgirl_schema import (
    CATGIRL_SCHEMA_VERSION,
    WEIGHT_UNIT,
    PERSONALITIES,
    normalize_catgirl,
    random_body_profile,
    calc_stage,
    stage_name,
    stage_description,
    next_stage_need,
    format_intimacy_level,
    format_stage_growth_progress,
    companion_days,
    bond_score,
    status_tag,
    clamp,
    clamp_int,
)


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def now_ts() -> int:
    return int(time.time())


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_IMAGE_WIDTH = 4096
MAX_IMAGE_HEIGHT = 4096
FEED_SATIETY_LIMIT = 85
SATIETY_DECAY_MINUTES = 48 * 60
SATIETY_DECAY_PER_MINUTE = 100 / SATIETY_DECAY_MINUTES
MOOD_DECAY_PER_MINUTE = 3 / (24 * 60)
ENERGY_RECOVERY_PER_MINUTE = 20 / (24 * 60)
HEALTH_HUNGRY_DECAY_PER_MINUTE = 5 / (24 * 60)
HEALTH_LOW_MOOD_DECAY_PER_MINUTE = 2 / (24 * 60)
HEALTH_RECOVERY_PER_MINUTE = 1 / (24 * 60)
RUNAWAY_AFTER_ZERO_SECONDS = 24 * 60 * 60
WEIGHT_MIN = 40.0
WEIGHT_MAX = 90.0
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class CatgirlService:
    def __init__(
        self,
        store: JsonStore,
        economy: EconomyService,
        coin_name: str,
        base_dir: Path,
        catgirl_dir: Path,
        upload_dir: Path,
        font_dir: Path,
        cache_dir: Path,
        wish_probability: float = 0.8,
        wish_pity: int = 3,
        appearance_change_price: int = 1200,
        runtime_config_provider: Callable[[], Dict] | None = None,
    ):
        self.store = store
        self.economy = economy
        self.coin_name = coin_name
        self.base_dir = Path(base_dir)
        self.catgirl_dir = Path(catgirl_dir)
        self.upload_dir = Path(upload_dir)
        self.font_dir = Path(font_dir)
        self.cache_dir = Path(cache_dir)
        self.wish_probability = float(wish_probability)
        self.wish_pity = int(wish_pity)
        self.appearance_change_price = int(appearance_change_price)
        self.runtime_config_provider = runtime_config_provider

    def _runtime(self) -> Dict:
        if callable(self.runtime_config_provider):
            try:
                data = self.runtime_config_provider()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _rules(self, section: str) -> Dict:
        data = self._runtime().get(section, {})
        return data if isinstance(data, dict) else {}

    def _coin_name(self) -> str:
        economy = self._rules("economy")
        return str(economy.get("coin_name") or self.coin_name or "宝石")

    def _wish_rules(self) -> Tuple[float, int, int]:
        wish = self._rules("wish")
        probability = min(1.0, max(0.0, float(wish.get("probability", self.wish_probability))))
        pity = max(1, int(wish.get("pity", self.wish_pity)))
        price = max(0, int(wish.get("appearance_change_price", self.appearance_change_price)))
        return probability, pity, price

    def _care_rules(self) -> Dict:
        care = self._rules("care")
        rules = {
            "feed_satiety_limit": float(care.get("feed_satiety_limit", FEED_SATIETY_LIMIT)),
            "satiety_decay_per_minute": 100 / max(1.0, float(care.get("satiety_decay_minutes", SATIETY_DECAY_MINUTES))),
            "mood_decay_per_minute": max(0.0, float(care.get("mood_decay_per_day", 3))) / (24 * 60),
            "energy_recovery_per_minute": max(0.0, float(care.get("energy_recovery_per_day", 20))) / (24 * 60),
            "health_hungry_decay_per_minute": max(0.0, float(care.get("health_hungry_decay_per_day", 5))) / (24 * 60),
            "health_low_mood_decay_per_minute": max(0.0, float(care.get("health_low_mood_decay_per_day", 2))) / (24 * 60),
            "health_recovery_per_minute": max(0.0, float(care.get("health_recovery_per_day", 1))) / (24 * 60),
            "health_hungry_satiety_threshold": float(care.get("health_hungry_satiety_threshold", 20)),
            "health_low_mood_threshold": float(care.get("health_low_mood_threshold", 30)),
            "runaway_after_zero_seconds": max(1, int(float(care.get("runaway_after_zero_hours", 24)) * 60 * 60)),
            "interaction_daily_limit": max(0, int(care.get("interaction_daily_limit", 5))),
            "interaction_cooldown_seconds": max(0, int(care.get("interaction_cooldown_seconds", 300))),
            "interaction_energy_cost": max(0, int(care.get("interaction_energy_cost", 6))),
            "interaction_soft_limit_extra": max(0, int(care.get("interaction_soft_limit_extra", 3))),
            "interaction_heavy_limit_extra": max(0, int(care.get("interaction_heavy_limit_extra", 7))),
            "interaction_soft_limit_multiplier": max(0.0, float(care.get("interaction_soft_limit_multiplier", 0.6))),
            "interaction_heavy_limit_multiplier": max(0.0, float(care.get("interaction_heavy_limit_multiplier", 0.3))),
            "interaction_minimal_limit_multiplier": max(0.0, float(care.get("interaction_minimal_limit_multiplier", 0.1))),
            "interaction_good_mood_threshold": float(care.get("interaction_good_mood_threshold", 80)),
            "interaction_low_mood_threshold": float(care.get("interaction_low_mood_threshold", 50)),
            "interaction_bad_mood_threshold": float(care.get("interaction_bad_mood_threshold", 30)),
            "interaction_high_mood_multiplier": max(0.0, float(care.get("interaction_high_mood_multiplier", 1.15))),
            "interaction_low_mood_multiplier": max(0.0, float(care.get("interaction_low_mood_multiplier", 0.75))),
            "interaction_bad_mood_multiplier": max(0.0, float(care.get("interaction_bad_mood_multiplier", 0.5))),
            "feed_healthy_threshold": float(care.get("feed_healthy_threshold", 70)),
            "feed_low_health_threshold": float(care.get("feed_low_health_threshold", 40)),
            "feed_bad_health_threshold": float(care.get("feed_bad_health_threshold", 20)),
            "feed_low_health_multiplier": max(0.0, float(care.get("feed_low_health_multiplier", 0.85))),
            "feed_bad_health_multiplier": max(0.0, float(care.get("feed_bad_health_multiplier", 0.65))),
            "feed_critical_health_multiplier": max(0.0, float(care.get("feed_critical_health_multiplier", 0.45))),
            "work_stable_energy_threshold": float(care.get("work_stable_energy_threshold", 50)),
            "work_high_energy_threshold": float(care.get("work_high_energy_threshold", 80)),
            "work_stable_energy_reward_multiplier": max(0.0, float(care.get("work_stable_energy_reward_multiplier", 1.05))),
            "work_high_energy_reward_multiplier": max(0.0, float(care.get("work_high_energy_reward_multiplier", 1.15))),
            "work_min_health": float(care.get("work_min_health", 40)),
            "interact_min_health": float(care.get("interact_min_health", 25)),
            "work_min_satiety": float(care.get("work_min_satiety", 25)),
            "work_min_mood": float(care.get("work_min_mood", 35)),
        }
        rules["interaction_heavy_limit_extra"] = max(rules["interaction_heavy_limit_extra"], rules["interaction_soft_limit_extra"])
        rules["interaction_low_mood_threshold"] = max(rules["interaction_low_mood_threshold"], rules["interaction_bad_mood_threshold"])
        rules["interaction_good_mood_threshold"] = max(rules["interaction_good_mood_threshold"], rules["interaction_low_mood_threshold"])
        rules["feed_low_health_threshold"] = max(rules["feed_low_health_threshold"], rules["feed_bad_health_threshold"])
        rules["feed_healthy_threshold"] = max(rules["feed_healthy_threshold"], rules["feed_low_health_threshold"])
        rules["work_high_energy_threshold"] = max(rules["work_high_energy_threshold"], rules["work_stable_energy_threshold"])
        return rules

    def _feed_rules(self) -> Dict:
        feed = self._rules("feed")
        defaults = {
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
        }
        rules = {}
        for key, default in defaults.items():
            rules[key] = int(feed.get(key, default))
        for low_key, high_key in [
            ("satiety_add_min", "satiety_add_max"),
            ("mood_add_min", "mood_add_max"),
            ("health_add_min", "health_add_max"),
            ("energy_add_min", "energy_add_max"),
            ("growth_add_min", "growth_add_max"),
            ("intimacy_add_min", "intimacy_add_max"),
        ]:
            if rules[high_key] < rules[low_key]:
                rules[high_key] = rules[low_key]
        rules["foods"] = feed.get("foods") if isinstance(feed.get("foods"), list) else []
        return rules

    def _personality_effect(self, cat: Dict) -> Dict:
        name = str(cat.get("personality") or "")
        effects = self._rules("personalities").get("effects", [])
        if isinstance(effects, list):
            for item in effects:
                if isinstance(item, dict) and item.get("enabled", True) and str(item.get("name")) == name:
                    return item
        return {}

    def _personality_multiplier(self, cat: Dict, key: str, default: float = 1.0) -> float:
        effect = self._personality_effect(cat)
        try:
            value = float(effect.get(key, default))
        except Exception:
            value = float(default)
        return max(0.0, value)

    def _scaled_int(self, value: int, multiplier: float) -> int:
        return max(0, int(round(int(value) * float(multiplier))))

    def _fmt_int(self, value) -> str:
        try:
            return str(int(round(float(value or 0))))
        except Exception:
            return "0"

    def _fmt_delta(self, value) -> str:
        try:
            return f"{float(value or 0):+.1f}"
        except Exception:
            return "+0.0"

    def _fmt_percent(self, value) -> str:
        try:
            return f"{int(round(float(value or 0)))}%"
        except Exception:
            return "0%"

    def _weight_display(self, cat: Dict) -> str:
        return f"{self._fmt_int((cat or {}).get('weight', 0))} 斤"

    def _get(self, uid: str) -> Optional[Dict]:
        self._finalize_expired_adoption(uid)
        def op(root):
            ok, cat, _ = self._load_active_cat(root, uid)
            return cat if ok else None
        return self.store.update(op)

    def _save(self, uid: str, data: Dict):
        data, _ = normalize_catgirl(data, uid)
        self.store.set("catgirls", uid, value=data)

    def has_catgirl(self, uid: str) -> bool:
        cat = self._get(uid)
        return bool(cat and cat.get("name"))

    def _font(self, size: int):
        for p in [self.font_dir / "GBK.TTF", self.font_dir / "FZKATJW.ttf", self.base_dir / "GBK.TTF"]:
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def _all_default_images(self):
        imgs = []
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            imgs.extend(self.catgirl_dir.glob(ext))
        return imgs

    def _default_image(self, exclude: str = "") -> Optional[Path]:
        imgs = self._all_default_images()
        if exclude:
            imgs = [x for x in imgs if str(x) != str(exclude)]
        return random.choice(imgs) if imgs else None

    def image_path(self, cat: Dict) -> Optional[Path]:
        p = cat.get("image")
        if p:
            path = Path(p)
            if path.exists():
                return path
        return self._default_image()

    def _new_catgirl(self, uid: str, exclude_image: str = "") -> Dict:
        img = self._default_image(exclude=exclude_image)
        names = ["小蓝", "咪露", "铃音", "砂糖", "琉璃", "桃桃", "小夜", "奶芙", "可可", "柚子", "绵绵", "露露", "米娅", "白桃", "星奈"]
        body_type, weight, ideal_weight = random_body_profile()
        cat = {
            "schema_version": CATGIRL_SCHEMA_VERSION,
            "weight_unit": WEIGHT_UNIT,
            "user": uid,
            "name": random.choice(names),
            "personality": random.choice(PERSONALITIES),
            "stage": 0,
            "growth": 0,
            "intimacy": 0,
            "satiety": random.randint(65, 85),
            "mood": random.randint(70, 90),
            "health": random.randint(85, 100),
            "energy": random.randint(70, 90),
            "body_type": body_type,
            "ideal_weight": ideal_weight,
            "weight": weight,
            "created_at": now_ts(),
            "last_decay": now_ts(),
            "last_feed_date": "",
            "fed_slots": {},
            "last_wish_date": today_str(),
            "wish_count": 0,
            "care_stats": {},
            "unlocks": [],
            "image": str(img) if img else "",
        }
        return cat

    def _finish_adoption_data(self, gid: str, uid: str, cat: Dict) -> Dict:
        cat, _ = normalize_catgirl(cat, uid)
        cat["home_gid"] = gid
        cat["intimacy"] = max(int(cat.get("intimacy", 0)), 10)
        cat["growth"] = max(int(cat.get("growth", 0)), 5)
        cat["stage"] = calc_stage(cat.get("growth", 0), cat.get("intimacy", 0))
        return cat

    def _finalize_expired_adoption(self, uid: str):
        pending = self.store.get("pending_adoptions", uid, default=None)
        if not isinstance(pending, dict):
            return
        if now_ts() <= int(pending.get("expire", 0) or 0):
            return

        def op(root):
            cats = root.setdefault("catgirls", {})
            existing = cats.get(uid)
            if isinstance(existing, dict) and existing.get("name"):
                root.setdefault("pending_adoptions", {}).pop(uid, None)
                return
            pending_adoptions = root.setdefault("pending_adoptions", {})
            current = pending_adoptions.get(uid)
            if not isinstance(current, dict) or now_ts() <= int(current.get("expire", 0) or 0):
                return
            first = current.get("first")
            if isinstance(first, dict):
                cats[uid] = self._finish_adoption_data(str(current.get("gid", "")), uid, first)
            pending_adoptions.pop(uid, None)

        self.store.update(op)

    def prepare_wish(self, uid: str, gid: str = ""):
        today = today_str()
        wish_probability, wish_pity, _ = self._wish_rules()
        first = None
        second = None
        success = False
        current = 0

        def op(root):
            nonlocal first, second, success, current
            cats = root.setdefault("catgirls", {})
            cat = cats.get(uid)
            if isinstance(cat, dict):
                cat, changed = normalize_catgirl(cat, uid)
                if changed:
                    cats[uid] = cat
            if cat and cat.get("name"):
                return False, "already", f"你已经有猫娘「{cat['name']}」啦，要好好疼她喔～", None, None

            pending_adoptions = root.setdefault("pending_adoptions", {})
            pending = pending_adoptions.get(uid)
            if isinstance(pending, dict):
                if now_ts() > int(pending.get("expire", 0) or 0):
                    first_pending = pending.get("first")
                    if isinstance(first_pending, dict):
                        adopted = self._finish_adoption_data(str(pending.get("gid", gid)), uid, first_pending)
                        cats[uid] = adopted
                        pending_adoptions.pop(uid, None)
                        return False, "already", f"你已经有猫娘「{adopted['name']}」啦，要好好疼她喔～", None, None
                    pending_adoptions.pop(uid, None)
                else:
                    first = pending.get("first")
                    second = pending.get("second")
                    if isinstance(first, dict):
                        return True, "pending", self._wish_pending_message(first), first, second

            sign = root.setdefault("sign", {})
            user = sign.setdefault(uid, {})
            if user.get("last_catgirl_wish_date") == today:
                current = int(user.get("catgirl_wish_count", 0))
                return (
                    False,
                    "cooldown",
                    f"今天已经许愿过啦～\n当前许愿进度：{current}/{wish_pity}\n每天许愿有 {int(wish_probability * 100)}% 概率遇见猫娘，{wish_pity} 次一定会有猫娘回应你喔～",
                    None,
                    None,
                )

            current = int(user.get("catgirl_wish_count", 0)) + 1
            success = random.random() < wish_probability or current >= wish_pity
            user["last_catgirl_wish_date"] = today
            user["catgirl_wish_count"] = 0 if success else current

            if not success:
                return (
                    False,
                    "failed",
                    f"今天的愿望还没有被猫娘听见……\n当前许愿进度：{current}/{wish_pity}\n别灰心喔，{wish_pity} 次内一定会有猫娘来找你～",
                    None,
                    None,
                )

            first = self._new_catgirl(uid)
            second = self._new_catgirl(uid, exclude_image=first.get("image", ""))
            pending_adoptions[uid] = {
                "uid": uid,
                "gid": gid,
                "first": first,
                "second": second,
                "expire": now_ts() + 120,
            }
            return True, "pending", "", first, second

        ok, status, msg, first, second = self.store.update(op)
        if not ok:
            return ok, status, msg, first, second
        if msg:
            return True, status, msg, first, second

        msg = self._wish_pending_message(first)
        return True, "pending", msg, first, second

    def _wish_pending_message(self, first: Dict) -> str:
        msg = (
            f"✨叮铃铃——许愿成功啦！\n"
            f"一位{first.get('personality', '温柔')}的猫娘听见了你的愿望，悄悄来到了你身边。\n\n"
            f"名字：{first.get('name', '猫娘')}\n"
            f"性格：{first.get('personality', '温柔')}\n"
            f"阶段：{stage_name(first.get('stage', 0))}\n"
            f"体型：{first.get('body_type', '匀称')}\n"
            f"状态：{status_tag(first)}\n\n"
            f"{stage_description(first.get('stage', 0))}\n\n"
            f"2 分钟内发送：\n"
            f"「带她回家」或「确认收养」：就让她成为你的猫娘。\n"
            f"「换个形象」或「换一只猫娘」：重新遇见另一位猫娘。\n\n"
            f"如果你害羞不回复，2 分钟后她也会默认跟你回家喔～"
        )
        return msg

    def draw_wish_card(self, cat: Dict, title: str = "许愿成功", footer: str = "发送「带她回家」确认，或发送「换一只猫娘」看看另一种相遇。") -> Path:
        return self.draw_care_card(
            title,
            cat,
            subtitle=f"{cat.get('personality', '温柔')}｜{cat.get('body_type', '匀称')}",
            lines=[
                f"一位{cat.get('personality', '温柔')}的猫娘听见了你的愿望，悄悄来到了你身边。",
                "她正在等你给出回应。",
            ],
            metrics=[
                ("名字", cat.get("name", "猫娘")),
                ("阶段", stage_name(cat.get("stage", 0))),
                ("饱食度", self._fmt_int(cat.get("satiety", 0))),
                ("心情", self._fmt_int(cat.get("mood", 0))),
                ("健康", self._fmt_int(cat.get("health", 0))),
                ("精力", self._fmt_int(cat.get("energy", 0))),
            ],
            footer=footer,
            tag=f"wish_{cat.get('user', 'user')}",
        )

    def get_pending_adoption(self, uid: str) -> Optional[Dict]:
        pending = self.store.get("pending_adoptions", uid, default=None)
        if not isinstance(pending, dict):
            return None
        return pending

    def finalize_adoption(self, gid: str, uid: str, cat: Dict = None, choice: str = "first"):
        def op(root):
            cats = root.setdefault("catgirls", {})
            existing = cats.get(uid)
            if isinstance(existing, dict):
                existing, changed = normalize_catgirl(existing, uid)
                if changed:
                    cats[uid] = existing
            if existing and existing.get("name"):
                root.setdefault("pending_adoptions", {}).pop(uid, None)
                return False, f"你已经有猫娘「{existing['name']}」啦，要好好疼她喔～", existing

            pending_adoptions = root.setdefault("pending_adoptions", {})
            pending = pending_adoptions.get(uid) if isinstance(pending_adoptions.get(uid), dict) else None
            selected = None
            if pending:
                selected = pending.get("second") if choice == "second" and now_ts() <= int(pending.get("expire", 0) or 0) else pending.get("first")
                if not isinstance(selected, dict):
                    selected = pending.get("first")
            if selected is None:
                selected = cat
            if not isinstance(selected, dict):
                pending_adoptions.pop(uid, None)
                return False, "这次相遇已经结束啦～请重新许愿试试看。", None

            selected = self._finish_adoption_data(gid, uid, selected)
            cats[uid] = selected
            pending_adoptions.pop(uid, None)
            return True, f"收养完成啦～\n猫娘「{selected.get('name', '猫娘')}」轻轻牵住了你的手。\n从今天开始，你们的羁绊会在每一次陪伴里慢慢成长 ฅ^•ﻌ•^ฅ", selected

        ok, msg, selected = self.store.update(op)
        card = self.draw_care_card(
            "收养完成" if ok else "收养未完成",
            selected,
            lines=[msg],
            metrics=[
                ("名字", selected.get("name", "猫娘") if selected else "-"),
                ("阶段", stage_name(selected.get("stage", 0)) if selected else "-"),
                ("亲密等级", self._intimacy_display(selected) if selected else "Lv.1"),
                ("成长进度", self._growth_display(selected) if selected else "0%"),
            ],
            tag=f"adopt_{uid}",
        ) if selected else None
        return ok, msg, card

    def _weight_floor(self, weight: float) -> float:
        return max(WEIGHT_MIN, float(weight) * 0.92)

    def _apply_decay(self, cat: Dict) -> Tuple[Dict, bool]:
        cat, _ = normalize_catgirl(cat, str(cat.get("user", "")))
        rules = self._care_rules()
        last_decay = int(cat.get("last_decay", now_ts()))
        now = now_ts()
        elapsed = max(0, now - last_decay)
        minutes = elapsed // 60
        if minutes <= 0:
            return cat, False
        personality = self._personality_effect(cat)

        old_satiety = float(cat.get("satiety", 0))
        satiety_decay_per_minute = rules["satiety_decay_per_minute"] * float(personality.get("satiety_decay_multiplier", 1))
        satiety_loss = minutes * satiety_decay_per_minute
        new_satiety = clamp(old_satiety - satiety_loss, 0, 100)
        cat["satiety"] = round(new_satiety, 4)

        zero_since = cat.get("satiety_zero_since")
        if new_satiety <= 0:
            if not zero_since:
                minutes_to_zero = old_satiety / satiety_decay_per_minute if old_satiety > 0 else 0
                zero_since = int(last_decay + min(minutes, minutes_to_zero) * 60)
                cat["satiety_zero_since"] = zero_since
            if now - int(zero_since) >= rules["runaway_after_zero_seconds"]:
                return cat, True
        else:
            cat.pop("satiety_zero_since", None)

        mood = float(cat.get("mood", 80))
        energy = float(cat.get("energy", 80))
        health = float(cat.get("health", 90))
        mood = clamp(mood - minutes * rules["mood_decay_per_minute"] * float(personality.get("mood_decay_multiplier", 1)), 0, 100)
        energy = clamp(energy + minutes * rules["energy_recovery_per_minute"] * float(personality.get("energy_recovery_multiplier", 1)), 0, 100)
        if new_satiety < rules["health_hungry_satiety_threshold"]:
            health = clamp(health - minutes * rules["health_hungry_decay_per_minute"], 0, 100)
        elif mood < rules["health_low_mood_threshold"]:
            health = clamp(health - minutes * rules["health_low_mood_decay_per_minute"], 0, 100)
        else:
            health = clamp(health + minutes * rules["health_recovery_per_minute"] * float(personality.get("health_recovery_multiplier", 1)), 0, 100)
        cat["mood"] = round(mood, 4)
        cat["energy"] = round(energy, 4)
        cat["health"] = round(health, 4)

        last_feed_date = cat.get("last_feed_date", "")
        try:
            if last_feed_date:
                no_feed_anchor = int(datetime.strptime(last_feed_date, "%Y-%m-%d").timestamp())
            else:
                no_feed_anchor = int(cat.get("created_at", last_decay) or last_decay)
            no_feed_days = max(0, (now - no_feed_anchor) // (24 * 60 * 60))
        except Exception:
            no_feed_days = max(0, (now - last_decay) // (24 * 60 * 60))

        if no_feed_days >= 7:
            periods = no_feed_days // 7
            settled_periods = max(0, int(cat.get("no_feed_weight_decay_periods", 0) or 0))
            pending_periods = max(0, periods - settled_periods)
            if pending_periods > 0:
                weight = float(cat.get("weight", 60.0))
                ideal = float(cat.get("ideal_weight", weight))
                loss = pending_periods * max(0.1, abs(weight - ideal) * 0.05)
                cat["weight"] = round(max(self._weight_floor(weight), weight - loss), 2)
                cat["mood"] = round(clamp(float(cat.get("mood", 80)) - pending_periods * 5, 0, 100), 4)
                cat["no_feed_weight_decay_periods"] = periods
        else:
            cat.pop("no_feed_weight_decay_periods", None)

        cat["last_decay"] = last_decay + minutes * 60
        return cat, False

    def _runaway_message(self, cat: Dict) -> str:
        return (
            f"「{cat.get('name', '猫娘')}」已经饿着肚子太久了，留下了一张小纸条后离家出走了。\n"
            "她的档案已清除。想重新开始的话，可以再次发送「请赐我一只可爱猫娘吧」。"
        )

    def _load_active_cat(self, root: Dict, uid: str):
        cats = root.setdefault("catgirls", {})
        cat = cats.get(uid)
        if not cat or not cat.get("name"):
            return False, None, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。"
        cat, _ = normalize_catgirl(cat, uid)
        cat, runaway = self._apply_decay(cat)
        if runaway:
            cats.pop(uid, None)
            return False, cat, self._runaway_message(cat)
        cats[uid] = cat
        return True, cat, ""

    def _feed_gain(self, cat: Dict) -> float:
        weight = float(cat.get("weight", 60.0))
        ideal = float(cat.get("ideal_weight", weight))
        if weight < ideal - 3:
            return round(random.uniform(0.4, 0.8), 2)
        if weight > ideal + 6:
            return round(random.uniform(-0.2, 0.2), 2)
        return round(random.uniform(0.2, 0.5), 2)

    def _advance_stage(self, cat: Dict) -> Tuple[Dict, Optional[str]]:
        old_stage = int(cat.get("stage", 0) or 0)
        new_stage = calc_stage(cat.get("growth", 0), cat.get("intimacy", 0))
        cat["stage"] = new_stage
        if new_stage > old_stage:
            return cat, f"\n\n✨ 成长阶段提升：{stage_name(old_stage)} → {stage_name(new_stage)}\n{stage_description(new_stage)}"
        return cat, None

    def _intimacy_display(self, cat: Dict) -> str:
        return format_intimacy_level(cat.get("intimacy", 0) if cat else 0)

    def _growth_display(self, cat: Dict) -> str:
        if not cat:
            return "0%"
        return format_stage_growth_progress(cat.get("growth", 0), cat.get("stage", 0))

    def _next_stage_line(self, stage: int) -> str:
        need = next_stage_need(stage)
        if not need:
            return "下一阶段：已经是最高羁绊啦"
        return f"下一阶段：{need[0]}（好感度要求 {format_intimacy_level(need[2])}）"

    def _satiety_risk_lines(self, cat: Dict) -> Tuple[str, str]:
        rules = self._care_rules()
        satiety = float(cat.get("satiety", 0) or 0)
        if satiety > 0:
            return "", ""
        zero_since = cat.get("satiety_zero_since")
        if zero_since:
            seconds_left = int(zero_since) + int(rules["runaway_after_zero_seconds"]) - now_ts()
            return "", f"离家出走：{self._format_duration(seconds_left)} 后"
        return "", f"离家出走：约 {self._format_duration(rules['runaway_after_zero_seconds'])} 后"

    def _health_effect_multiplier(self, cat: Dict) -> Tuple[float, str]:
        rules = self._care_rules()
        health = float((cat or {}).get("health", 0) or 0)
        if health < rules["feed_bad_health_threshold"]:
            return float(rules["feed_critical_health_multiplier"]), "重病"
        if health < rules["feed_low_health_threshold"]:
            return float(rules["feed_bad_health_multiplier"]), "危险"
        if health < rules["feed_healthy_threshold"]:
            return float(rules["feed_low_health_multiplier"]), "偏低"
        return 1.0, "正常"

    def _mood_interaction_multiplier(self, cat: Dict) -> Tuple[float, str]:
        rules = self._care_rules()
        mood = float((cat or {}).get("mood", 0) or 0)
        if mood < rules["interaction_bad_mood_threshold"]:
            return float(rules["interaction_bad_mood_multiplier"]), "危险心情"
        if mood < rules["interaction_low_mood_threshold"]:
            return float(rules["interaction_low_mood_multiplier"]), "低心情"
        if mood >= rules["interaction_good_mood_threshold"]:
            return float(rules["interaction_high_mood_multiplier"]), "高心情"
        return 1.0, "正常心情"

    def _interaction_daily_multiplier(self, today_count: int) -> Tuple[float, str]:
        rules = self._care_rules()
        limit = int(rules["interaction_daily_limit"])
        if limit <= 0 or today_count < limit:
            return 1.0, "正常"
        soft_until = limit + int(rules["interaction_soft_limit_extra"])
        heavy_until = limit + int(rules["interaction_heavy_limit_extra"])
        if today_count < soft_until:
            return float(rules["interaction_soft_limit_multiplier"]), "轻度递减"
        if today_count < heavy_until:
            return float(rules["interaction_heavy_limit_multiplier"]), "重度递减"
        return float(rules["interaction_minimal_limit_multiplier"]), "极低收益"

    def _interaction_status_line(self, cat: Dict) -> str:
        today_count = int(((cat or {}).get("interactions") or {}).get(today_str(), 0) or 0)
        mood_multiplier, mood_label = self._mood_interaction_multiplier(cat)
        daily_multiplier, daily_label = self._interaction_daily_multiplier(today_count)
        multiplier = mood_multiplier * daily_multiplier
        limit = int(self._care_rules()["interaction_daily_limit"])
        count_text = f"{today_count}/{limit}" if limit else f"{today_count}/不限"
        return f"互动收益：{self._fmt_percent(multiplier * 100)}（{mood_label}，{daily_label}，今日 {count_text}）"

    def _health_trend_line(self, cat: Dict) -> str:
        rules = self._care_rules()
        satiety = float((cat or {}).get("satiety", 0) or 0)
        mood = float((cat or {}).get("mood", 0) or 0)
        if satiety < rules["health_hungry_satiety_threshold"]:
            return "健康趋势：饥饿下降"
        if mood < rules["health_low_mood_threshold"]:
            return "健康趋势：心情低落下降"
        return "健康趋势：缓慢恢复"

    def _work_energy_tier(self, energy: float) -> Tuple[str, float]:
        rules = self._care_rules()
        energy = float(energy or 0)
        if energy >= rules["work_high_energy_threshold"]:
            return "高收益打工", float(rules["work_high_energy_reward_multiplier"])
        if energy >= rules["work_stable_energy_threshold"]:
            return "稳定打工", float(rules["work_stable_energy_reward_multiplier"])
        return "普通打工", 1.0

    def _energy_status_line(self, cat: Dict) -> str:
        tier, multiplier = self._work_energy_tier(float((cat or {}).get("energy", 0) or 0))
        return f"精力状态：{tier}（打工报酬 {self._fmt_percent(multiplier * 100)}）"

    def _format_duration(self, seconds: int) -> str:
        seconds = max(0, int(seconds))
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} 分钟" if minutes else "不到 1 分钟"
        hours, minutes = divmod(minutes, 60)
        if minutes:
            return f"{hours} 小时 {minutes} 分钟"
        return f"{hours} 小时"

    def _text_size(self, draw: ImageDraw.ImageDraw, text: str, font):
        box = draw.textbbox((0, 0), str(text), font=font)
        return box[2] - box[0], box[3] - box[1]

    def _truncate_text(self, draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
        text = str(text or "")
        if self._text_size(draw, text, font)[0] <= max_width:
            return text
        result = ""
        for ch in text:
            candidate = result + ch
            if self._text_size(draw, candidate + "...", font)[0] > max_width:
                return (result or text[:1]) + "..."
            result = candidate
        return result

    def _wrap_by_width(self, draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int = 4):
        paragraphs = str(text or "").splitlines() or [""]
        lines = []
        for paragraph in paragraphs:
            current = ""
            for ch in paragraph:
                candidate = current + ch
                if current and self._text_size(draw, candidate, font)[0] > max_width:
                    lines.append(current)
                    current = ch
                    if len(lines) >= max_lines:
                        break
                else:
                    current = candidate
            if len(lines) >= max_lines:
                break
            if current:
                lines.append(current)
            if len(lines) >= max_lines:
                break
        if len(lines) > max_lines:
            lines = lines[:max_lines]
        if lines and self._text_size(draw, lines[-1], font)[0] > max_width:
            lines[-1] = self._truncate_text(draw, lines[-1], font, max_width)
        return lines[:max_lines]

    def _fit_font(self, draw: ImageDraw.ImageDraw, text: str, size: int, max_width: int, min_size: int = 22):
        size = max(min_size, int(size))
        font = self._font(size)
        while size > min_size and self._text_size(draw, text, font)[0] > max_width:
            size -= 2
            font = self._font(size)
        return font

    def draw_care_card(
        self,
        title: str,
        cat: Optional[Dict] = None,
        subtitle: str = "",
        lines=None,
        metrics=None,
        footer: str = "",
        image_path: Optional[Path] = None,
        tag: str = "care",
    ) -> Path:
        lines = [str(x) for x in (lines or []) if str(x).strip()]
        metrics = [(str(k), str(v)) for k, v in (metrics or [])]
        width = 980
        padding = 42
        header_h = 112
        image_w, image_h = 300, 360
        line_h = 36
        title_font = self._font(48)
        sub_font = self._font(26)
        name_font = self._font(34)
        text_font = self._font(25)
        small_font = self._font(22)
        metric_font = self._font(24)
        metric_value_font = self._font(30)

        card_w = width - padding * 2
        img_x = padding + 30
        text_x = img_x + image_w + 32
        text_w = padding + card_w - text_x - 30
        measure = ImageDraw.Draw(Image.new("RGB", (width, 1), "white"))
        wrapped_line_count = sum(len(self._wrap_by_width(measure, line, text_font, text_w, 2)) for line in lines)
        metrics_rows = math.ceil(len(metrics) / 2) if metrics else 0
        cell_h = 60
        cell_gap = 14
        metric_grid_h = metrics_rows * cell_h + max(0, metrics_rows - 1) * cell_gap
        text_intro_h = 90 if cat else 0
        details_h = text_intro_h + wrapped_line_count * line_h
        grid_y_rel = max(32 + details_h + 20, 210) if metrics else 32 + details_h
        metrics_h = metric_grid_h if metrics else 0
        footer_lines = self._wrap_by_width(measure, footer, small_font, card_w - 60, 2) if footer else []
        footer_h = len(footer_lines) * 28 + 46 if footer_lines else 0
        right_content_h = grid_y_rel + metrics_h + footer_h + 28
        image_content_h = 32 + image_h + footer_h + 28
        content_h = max(image_content_h, right_content_h)
        height = padding + header_h + content_h + padding

        canvas = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(canvas)

        orange = (255, 140, 0)
        blue = (0, 191, 255)
        dark = (40, 40, 40)
        muted = (90, 90, 90)
        soft = (246, 250, 255)

        draw.text((width // 2, padding + 18), title, font=title_font, fill=orange, anchor="ma")
        if subtitle:
            draw.text((width // 2, padding + 76), subtitle, font=sub_font, fill=muted, anchor="ma")

        card_x, card_y = padding, padding + header_h
        card_h = content_h
        draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h), radius=18, outline=blue, width=5, fill=(255, 255, 255))

        img_y = card_y + 32
        img_source = image_path or (self.image_path(cat) if cat else None)
        if img_source and Path(img_source).exists():
            try:
                img = Image.open(img_source).convert("RGB")
                img = self._cover(img, image_w, image_h)
                canvas.paste(img, (img_x, img_y))
            except Exception:
                self._draw_no_image(draw, img_x, img_y, image_w, image_h)
        else:
            self._draw_no_image(draw, img_x, img_y, image_w, image_h)

        y = card_y + 32
        if cat:
            name = self._truncate_text(draw, f"{cat.get('name', '猫娘')}｜{stage_name(cat.get('stage', 0))}", name_font, text_w)
            draw.text((text_x, y), name, font=name_font, fill=dark)
            y += 46
            profile = f"{cat.get('personality', '温柔')}｜{status_tag(cat)}｜羁绊 {bond_score(cat)}"
            draw.text((text_x, y), self._truncate_text(draw, profile, small_font, text_w), font=small_font, fill=orange)
            y += 44

        for line in lines:
            for wrapped in self._wrap_by_width(draw, line, text_font, text_w, 2):
                draw.text((text_x, y), wrapped, font=text_font, fill=dark)
                y += line_h

        if metrics:
            grid_x = text_x
            grid_y = max(y + 20, card_y + 210)
            cell_w = (text_w - 16) // 2
            for idx, (label, value) in enumerate(metrics):
                col = idx % 2
                row = idx // 2
                x = grid_x + col * (cell_w + 16)
                yy = grid_y + row * (cell_h + cell_gap)
                mid_y = yy + cell_h // 2
                label_w = min(int(cell_w * 0.44), max(68, self._text_size(draw, label, metric_font)[0] + 6))
                value_w = max(60, cell_w - label_w - 42)
                value_font = self._fit_font(draw, value, 30, value_w)
                draw.rounded_rectangle((x, yy, x + cell_w, yy + cell_h), radius=10, fill=soft, outline=(220, 238, 248), width=2)
                draw.text((x + 14, mid_y), self._truncate_text(draw, label, metric_font, label_w), font=metric_font, fill=muted, anchor="lm")
                draw.text((x + cell_w - 14, mid_y), self._truncate_text(draw, value, value_font, value_w), font=value_font, fill=orange, anchor="rm")

        if footer:
            footer_lines = self._wrap_by_width(draw, footer, small_font, card_w - 60, 2)
            fy = card_y + card_h - 34 - (len(footer_lines) - 1) * 28
            for footer_line in footer_lines:
                draw.text((card_x + card_w // 2, fy), footer_line, font=small_font, fill=muted, anchor="ma")
                fy += 28

        safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", str(tag or "care"))[:40] or "care"
        out = self.cache_dir / f"cat_card_{safe_tag}_{int(time.time() * 1000)}.png"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        canvas.save(out, "PNG")
        return out

    def status(self, uid: str) -> Tuple[bool, str, Optional[Path]]:
        self._finalize_expired_adoption(uid)

        def op(root):
            ok, cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, cat
            return True, "", cat

        ok, msg, cat = self.store.update(op)
        if not ok:
            img = self.draw_care_card("猫娘状态", cat, lines=[msg], tag=f"status_err_{uid}") if cat else None
            return False, msg, img
        stage = int(cat.get("stage", 0) or 0)
        next_line = self._next_stage_line(stage)
        pending_work = cat.get("pending_work") if isinstance(cat.get("pending_work"), dict) else None
        work_line = ""
        if pending_work and pending_work.get("finish_at"):
            remain = int(pending_work.get("finish_at", 0)) - now_ts()
            if remain > 0:
                work_line = f"\n打工中：{pending_work.get('job', '打工')}，剩余 {self._format_duration(remain)}"
            else:
                work_line = f"\n打工中：{pending_work.get('job', '打工')} 已完成，发送「猫娘打工」领取报酬"
        satiety_zero_line, runaway_line = self._satiety_risk_lines(cat)
        detail_lines = [
            satiety_zero_line,
            runaway_line,
            self._health_trend_line(cat),
            self._energy_status_line(cat),
            self._interaction_status_line(cat),
            next_line,
        ]
        detail_lines = [line for line in detail_lines if line]
        card_status_lines = [work_line.strip() if work_line else "", *detail_lines]
        card_status_lines = [line for line in card_status_lines if line]
        detail_text = "\n".join(detail_lines)
        msg = (
            f"猫娘「{cat['name']}」的成长档案已更新～\n\n"
            f"性格：{cat.get('personality', '温柔')}\n"
            f"阶段：{stage_name(stage)}\n"
            f"状态：{status_tag(cat)}\n"
            f"体型：{cat.get('body_type', '匀称')}\n"
            f"体重：{self._weight_display(cat)}\n\n"
            f"亲密等级：{self._intimacy_display(cat)}\n"
            f"成长进度：{self._growth_display(cat)}\n"
            f"饱食度：{self._fmt_int(cat.get('satiety', 0))}\n"
            f"心情：{self._fmt_int(cat.get('mood', 0))}\n"
            f"健康：{self._fmt_int(cat.get('health', 0))}\n"
            f"精力：{self._fmt_int(cat.get('energy', 0))}\n"
            f"相伴：{companion_days(cat)} 天{work_line}\n"
            f"{detail_text}\n\n"
            f"{stage_description(stage)}"
        )
        card = self.draw_care_card(
            "猫娘成长档案",
            cat,
            subtitle=f"相伴 {companion_days(cat)} 天",
            lines=[stage_description(stage), *card_status_lines],
            metrics=[
                ("亲密等级", self._intimacy_display(cat)),
                ("成长进度", self._growth_display(cat)),
                ("饱食度", self._fmt_int(cat.get("satiety", 0))),
                ("心情", self._fmt_int(cat.get("mood", 0))),
                ("健康", self._fmt_int(cat.get("health", 0))),
                ("精力", self._fmt_int(cat.get("energy", 0))),
                ("体重", self._weight_display(cat)),
                ("体型", cat.get("body_type", "匀称")),
            ],
            footer=f"状态：{status_tag(cat)}",
            tag=f"status_{uid}",
        )
        return True, msg, card

    def _random_food(self):
        foods = []
        for item in self._feed_rules().get("foods", []):
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            name = str(item.get("name") or "食物").strip() or "食物"
            low = max(0, int(item.get("cost_min", 1)))
            high = max(low, int(item.get("cost_max", low)))
            verb = str(item.get("verb") or "吃").strip() or "吃"
            foods.append((name, low, high, verb))
        if not foods:
            foods = [("草莓奶油蛋糕", 18, 38, "吃")]
        name, low, high, verb = random.choice(foods)
        return name, random.randint(low, high), verb

    def _work_jobs(self, work_rules: Optional[Dict] = None) -> list[Dict]:
        work_rules = work_rules or self._rules("work")
        jobs = []
        for item in work_rules.get("jobs", []) if isinstance(work_rules.get("jobs"), list) else []:
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            low = max(1, int(item.get("reward_min", 1)))
            high = max(low, int(item.get("reward_max", low)))
            growth_low = max(0, int(item.get("growth_min", 0)))
            growth_high = max(growth_low, int(item.get("growth_max", growth_low)))
            intimacy_low = max(0, int(item.get("intimacy_min", 0)))
            intimacy_high = max(intimacy_low, int(item.get("intimacy_max", intimacy_low)))
            jobs.append({
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "打工地点").strip() or "打工地点",
                "low": low,
                "high": high,
                "duration": max(60, int(item.get("duration_minutes", 30)) * 60),
                "energy": max(0, int(item.get("energy_cost", 0))),
                "satiety": max(0, int(item.get("satiety_cost", 0))),
                "mood": max(0, int(item.get("mood_cost", 0))),
                "growth": (growth_low, growth_high),
                "intimacy": (intimacy_low, intimacy_high),
                "mood_reward": float(item.get("mood_reward", 1)),
            })
        if not jobs:
            jobs = [{
                "id": "cat_cafe",
                "name": "猫咖服务员",
                "low": 120,
                "high": 220,
                "duration": 45 * 60,
                "energy": 22,
                "satiety": 8,
                "mood": 2,
                "growth": (5, 10),
                "intimacy": (1, 3),
                "mood_reward": 1,
            }]
        return jobs

    def _find_work_job(self, jobs: list[Dict], query: str) -> Tuple[Optional[Dict], list[Dict]]:
        query = str(query or "").strip()
        if not query:
            return None, []
        normalized = query.lower()
        for job in jobs:
            names = {str(job.get("name", "")).lower(), str(job.get("id", "")).lower()}
            if normalized in names:
                return job, []
        matches = [
            job for job in jobs
            if normalized in str(job.get("name", "")).lower() or normalized in str(job.get("id", "")).lower()
        ]
        if len(matches) == 1:
            return matches[0], []
        return None, matches

    def _work_job_names(self, jobs: list[Dict], limit: int = 8) -> str:
        names = [str(job.get("name") or "打工地点") for job in jobs[:limit]]
        suffix = f" 等 {len(jobs)} 个" if len(jobs) > limit else ""
        return "、".join(names) + suffix

    def _work_job_summary_lines(self, jobs: list[Dict], limit: int = 6) -> list[str]:
        lines = []
        for job in jobs[:limit]:
            lines.append(
                f"{job.get('name', '打工地点')}：{self._format_duration(job.get('duration', 0))}，"
                f"精力 {job.get('energy', 0)}，饱食 {job.get('satiety', 0)}，"
                f"报酬 {job.get('low', 0)}-{job.get('high', 0)}"
            )
        if len(jobs) > limit:
            lines.append(f"还有 {len(jobs) - limit} 个地点，可在插件拓展页查看。")
        return lines

    def feed(self, uid: str) -> Tuple[bool, str, Optional[Path]]:
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", None

        today = today_str()
        care_rules = self._care_rules()
        feed_rules = self._feed_rules()
        coin_name = self._coin_name()
        food, cost, verb = self._random_food()

        def op(root):
            wallet = root.setdefault("wallet", {})
            ok, current_cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, current_cat, 0, 0, 0, 0, 0, 0, None

            balance = int(wallet.get(uid, 0))
            if balance < cost:
                return False, f"你想带「{current_cat['name']}」去{verb}{food}，但是需要 {cost} {coin_name}。\n你的小钱包里只有 {balance} {coin_name}，不够喔～", current_cat, 0, 0, 0, 0, 0, balance, None

            satiety = float(current_cat.get("satiety", 0))
            feed_limit = care_rules["feed_satiety_limit"]
            if satiety >= feed_limit:
                return False, f"「{current_cat['name']}」现在饱食度 {self._fmt_int(satiety)}，还不饿喔～等饱食度低于 {self._fmt_int(feed_limit)} 再喂吧。", current_cat, 0, 0, 0, 0, 0, balance, None

            health_multiplier, health_label = self._health_effect_multiplier(current_cat)
            satiety_add = self._scaled_int(random.randint(feed_rules["satiety_add_min"], feed_rules["satiety_add_max"]), self._personality_multiplier(current_cat, "feed_satiety_multiplier"))
            mood_add = self._scaled_int(random.randint(feed_rules["mood_add_min"], feed_rules["mood_add_max"]), self._personality_multiplier(current_cat, "feed_mood_multiplier") * health_multiplier)
            health_add = random.randint(feed_rules["health_add_min"], feed_rules["health_add_max"])
            energy_add = self._scaled_int(random.randint(feed_rules["energy_add_min"], feed_rules["energy_add_max"]), health_multiplier)
            growth_add = self._scaled_int(random.randint(feed_rules["growth_add_min"], feed_rules["growth_add_max"]), self._personality_multiplier(current_cat, "feed_growth_multiplier") * health_multiplier)
            intimacy_add = self._scaled_int(random.randint(feed_rules["intimacy_add_min"], feed_rules["intimacy_add_max"]), self._personality_multiplier(current_cat, "feed_intimacy_multiplier") * health_multiplier)
            weight_gain = self._feed_gain(current_cat)

            current_cat["weight"] = round(clamp(float(current_cat.get("weight", 60.0)) + weight_gain, WEIGHT_MIN, WEIGHT_MAX), 2)
            current_cat["satiety"] = clamp(float(current_cat.get("satiety", 0)) + satiety_add, 0, 100)
            current_cat["mood"] = round(clamp(float(current_cat.get("mood", 80)) + mood_add, 0, 100), 4)
            current_cat["health"] = round(clamp(float(current_cat.get("health", 90)) + health_add, 0, 100), 4)
            current_cat["energy"] = round(clamp(float(current_cat.get("energy", 80)) + energy_add, 0, 100), 4)
            current_cat["growth"] = int(current_cat.get("growth", 0)) + growth_add
            current_cat["intimacy"] = int(current_cat.get("intimacy", 0)) + intimacy_add
            current_cat["last_feed_date"] = today
            current_cat.pop("no_feed_weight_decay_periods", None)
            stats = current_cat.setdefault("care_stats", {})
            stats["total_feeds"] = int(stats.get("total_feeds", 0)) + 1
            current_cat, stage_msg = self._advance_stage(current_cat)

            wallet[uid] = balance - cost
            root.setdefault("catgirls", {})[uid] = current_cat
            return True, "", current_cat, satiety_add, mood_add, health_add, energy_add, growth_add, wallet[uid], stage_msg, intimacy_add, weight_gain, health_multiplier, health_label

        result = self.store.update(op)
        ok = result[0]
        if not ok:
            _, msg, current_cat, *_ = result
            card = self.draw_care_card(
                "喂猫未完成",
                current_cat or cat,
                lines=[msg],
                metrics=[
                    ("饱食度", self._fmt_int((current_cat or cat).get("satiety", 0))) if (current_cat or cat) else ("饱食度", "0"),
                ],
                tag=f"feed_err_{uid}",
            ) if (current_cat or cat) else None
            return False, msg, card

        _, _, cat, satiety_add, mood_add, health_add, energy_add, growth_add, balance, stage_msg, intimacy_add, weight_gain, health_multiplier, health_label = result
        weight_delta_display = self._fmt_delta(weight_gain)
        weight_line = f"\n体重变化：{weight_delta_display} 斤" if abs(float(weight_gain or 0)) >= 0.05 else ""
        feed_effect_line = ""
        if float(health_multiplier or 1) < 0.999:
            feed_effect_line = f"\n喂食效率：{self._fmt_percent(float(health_multiplier) * 100)}（健康{health_label}，饱食和健康恢复不受影响）"
        msg = (
            f"你带「{cat['name']}」{verb}了{food}。\n"
            f"花费：{cost} {coin_name}\n"
            f"她幸福地眯起眼睛，看起来超级满足～\n\n"
            f"饱食度 {self._fmt_delta(satiety_add)}\n"
            f"心情 {self._fmt_delta(mood_add)}\n"
            f"健康 {self._fmt_delta(health_add)}\n"
            f"精力 {self._fmt_delta(energy_add)}\n"
            f"亲密度 {self._fmt_delta(intimacy_add)}\n"
            f"成长值 {self._fmt_delta(growth_add)}{weight_line}{feed_effect_line}\n\n"
            f"当前阶段：{stage_name(cat.get('stage', 0))}\n"
            f"钱包余额：{balance} {coin_name}"
        )
        if stage_msg:
            msg += stage_msg
        card = self.draw_care_card(
            "喂猫结果",
            cat,
            subtitle=f"{verb}{food}｜花费 {cost} {coin_name}",
            lines=[
                "她幸福地眯起眼睛，看起来超级满足～",
                f"喂食效率：{self._fmt_percent(float(health_multiplier) * 100)}（健康{health_label}）" if float(health_multiplier or 1) < 0.999 else "",
                stage_msg.strip() if stage_msg else "",
            ],
            metrics=[
                ("饱食度", self._fmt_delta(satiety_add)),
                ("心情", self._fmt_delta(mood_add)),
                ("健康", self._fmt_delta(health_add)),
                ("精力", self._fmt_delta(energy_add)),
                ("亲密度", self._fmt_delta(intimacy_add)),
                ("成长值", self._fmt_delta(growth_add)),
                ("体重", f"{weight_delta_display} 斤"),
                ("喂食效率", self._fmt_percent(float(health_multiplier) * 100)),
                ("余额", f"{balance} {coin_name}"),
            ],
            footer=f"当前阶段：{stage_name(cat.get('stage', 0))}",
            tag=f"feed_{uid}",
        )
        return True, msg, card

    def work(self, uid: str, job_query: str = ""):
        self._finalize_expired_adoption(uid)
        now = now_ts()
        coin_name = self._coin_name()
        care_rules = self._care_rules()
        work_rules = self._rules("work")
        jobs = self._work_jobs(work_rules)
        job_query = str(job_query or "").strip()
        if job_query in ("列表", "地点", "地点列表", "打工地点"):
            msg = "可选猫娘打工地点：\n" + "\n".join(self._work_job_summary_lines(jobs, 12))
            card = self.draw_care_card(
                "打工地点",
                lines=self._work_job_summary_lines(jobs, 8),
                metrics=[
                    ("地点数", str(len(jobs))),
                    ("用法", "猫娘打工 地点名"),
                ],
                tag=f"work_jobs_{uid}",
            )
            return True, msg, card
        selected_job, job_matches = self._find_work_job(jobs, job_query)

        def finish_work(cat: Dict, pending: Dict, wallet: Dict):
            job_name = pending.get("job", "打工")
            reward = int(pending.get("reward", 0))
            growth_add = int(pending.get("growth_add", 0))
            intimacy_add = int(pending.get("intimacy_add", 0))
            uid_key = str(cat.get("user", uid))
            wallet[uid_key] = int(wallet.get(uid_key, 0)) + reward
            cat["growth"] = int(cat.get("growth", 0)) + growth_add
            cat["intimacy"] = int(cat.get("intimacy", 0)) + intimacy_add
            cat["mood"] = round(clamp(float(cat.get("mood", 80)) + float(pending.get("mood_reward", 1)), 0, 100), 4)
            stats = cat.setdefault("care_stats", {})
            stats["total_works"] = int(stats.get("total_works", 0)) + 1
            cat.pop("pending_work", None)
            cat, stage_msg = self._advance_stage(cat)
            msg = (
                f"「{cat['name']}」完成了{job_name}，抱着小钱包跑回来啦～\n"
                f"获得：{reward} {coin_name}\n"
                f"成长值 {self._fmt_delta(growth_add)}\n"
                f"亲密度 {self._fmt_delta(intimacy_add)}\n"
                f"当前余额：{wallet[uid_key]} {coin_name}"
            )
            if stage_msg:
                msg += stage_msg
            return msg, {
                "job_name": job_name,
                "reward": reward,
                "growth_add": growth_add,
                "intimacy_add": intimacy_add,
                "balance": wallet[uid_key],
                "stage_msg": stage_msg,
            }

        def op(root):
            wallet = root.setdefault("wallet", {})
            ok, cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, cat, "error", {}

            cats = root.setdefault("catgirls", {})
            pending = cat.get("pending_work")
            if isinstance(pending, dict) and "finish_at" in pending:
                finish_at = int(pending.get("finish_at", 0))
                if now < finish_at:
                    cats[uid] = cat
                    remain = self._format_duration(finish_at - now)
                    return False, f"「{cat['name']}」正在{pending.get('job', '打工')}，还需要 {remain} 才能回来喔～", cat, "working", {
                        "job_name": pending.get("job", "打工"),
                        "remain": remain,
                        "reward": int(pending.get("reward", 0)),
                        "growth_add": int(pending.get("growth_add", 0)),
                        "intimacy_add": int(pending.get("intimacy_add", 0)),
                    }
                msg, detail = finish_work(cat, pending, wallet)
                cats[uid] = cat
                return True, msg, cat, "finished", detail

            if float(cat.get("health", 0)) < care_rules["work_min_health"]:
                cats[uid] = cat
                return False, f"「{cat['name']}」现在身体有点虚弱，先照顾一下健康再让她出门工作吧。", cat, "blocked", {}

            if job_query and not selected_job:
                if job_matches:
                    lines = "、".join(str(job.get("name", "打工地点")) for job in job_matches[:8])
                    msg = f"找到多个相近的打工地点：{lines}\n请发送更完整的地点名。"
                else:
                    msg = f"没有找到「{job_query}」这个打工地点。\n可选地点：{self._work_job_names(jobs)}"
                cats[uid] = cat
                return False, msg, cat, "job_not_found", {"jobs": self._work_job_names(jobs)}

            available_jobs = [job for job in jobs if float(cat.get("energy", 0)) >= job["energy"]]
            if selected_job and selected_job not in available_jobs:
                cats[uid] = cat
                return False, f"「{cat['name']}」现在精力只有 {self._fmt_int(cat.get('energy', 0))}，去{selected_job['name']}需要 {selected_job['energy']}。先让她休息一下吧～", cat, "blocked", {
                    "min_energy": selected_job["energy"],
                }
            if not available_jobs:
                cats[uid] = cat
                min_energy = min(job["energy"] for job in jobs)
                return False, f"「{cat['name']}」现在精力只有 {self._fmt_int(cat.get('energy', 0))}，至少需要 {min_energy} 才能去最轻松的工作。先让她休息或喂点好吃的吧～", cat, "blocked", {
                    "min_energy": min_energy,
                }

            if float(cat.get("satiety", 0)) < care_rules["work_min_satiety"]:
                cats[uid] = cat
                return False, f"「{cat['name']}」小肚子咕咕叫，先喂点东西再让她去打工吧。", cat, "blocked", {}
            if float(cat.get("mood", 0)) < care_rules["work_min_mood"]:
                cats[uid] = cat
                return False, f"「{cat['name']}」现在心情有点低落，不太想出门工作呢。", cat, "blocked", {}

            job = selected_job or random.choice(available_jobs)
            energy_before = float(cat.get("energy", 0) or 0)
            energy_tier, energy_reward_multiplier = self._work_energy_tier(energy_before)
            stage_multiplier = float(work_rules.get("reward_stage_base", 0.8)) + int(cat.get("stage", 0)) * float(work_rules.get("reward_stage_step", 0.12))
            stage_multiplier *= self._personality_multiplier(cat, "work_reward_multiplier")
            stage_multiplier *= energy_reward_multiplier
            stage_multiplier = max(0.01, stage_multiplier)

            reward = max(1, int(random.randint(job["low"], job["high"]) * stage_multiplier))
            growth_add = self._scaled_int(random.randint(*job["growth"]), self._personality_multiplier(cat, "work_growth_multiplier"))
            intimacy_add = self._scaled_int(random.randint(*job["intimacy"]), self._personality_multiplier(cat, "work_intimacy_multiplier"))
            duration = int(job["duration"])
            energy_cost = self._scaled_int(job["energy"], self._personality_multiplier(cat, "work_energy_cost_multiplier"))
            cat["energy"] = round(clamp(float(cat.get("energy", 80)) - energy_cost, 0, 100), 4)
            cat["satiety"] = round(clamp(float(cat.get("satiety", 0)) - job["satiety"], 0, 100), 4)
            cat["mood"] = round(clamp(float(cat.get("mood", 80)) - job["mood"], 0, 100), 4)
            cat["pending_work"] = {
                "job": job["name"],
                "started_at": now,
                "finish_at": now + duration,
                "duration": duration,
                "energy_cost": energy_cost,
                "satiety_cost": job["satiety"],
                "mood_cost": job["mood"],
                "reward": reward,
                "growth_add": growth_add,
                "intimacy_add": intimacy_add,
                "mood_reward": job["mood_reward"],
            }
            cats[uid] = cat
            msg = (
                f"「{cat['name']}」出发去{job['name']}啦～\n"
                f"预计耗时：{self._format_duration(duration)}\n"
                f"精力档位：{energy_tier}（报酬 {self._fmt_percent(energy_reward_multiplier * 100)}）\n"
                f"消耗精力：{self._fmt_delta(-energy_cost)}\n"
                f"消耗饱食度：{self._fmt_delta(-job['satiety'])}\n"
                f"预计报酬：{reward} {coin_name}\n\n"
                f"等她回来后，再发送「猫娘打工」领取报酬和成长奖励喔。"
            )
            return True, msg, cat, "started", {
                "job_name": job["name"],
                "duration": duration,
                "energy_cost": energy_cost,
                "satiety_cost": job["satiety"],
                "reward": reward,
                "growth_add": growth_add,
                "intimacy_add": intimacy_add,
                "energy_tier": energy_tier,
                "energy_reward_multiplier": energy_reward_multiplier,
            }

        ok, msg, cat, event_type, detail = self.store.update(op)
        if not cat:
            return ok, msg, None
        if event_type == "finished":
            metrics = [
                ("获得", f"{detail.get('reward', 0)} {coin_name}"),
                ("成长值", self._fmt_delta(detail.get("growth_add", 0))),
                ("亲密度", self._fmt_delta(detail.get("intimacy_add", 0))),
                ("余额", f"{detail.get('balance', 0)} {coin_name}"),
            ]
            lines = [f"完成了{detail.get('job_name', '打工')}，抱着小钱包跑回来啦～"]
            if detail.get("stage_msg"):
                lines.append(str(detail.get("stage_msg")).strip())
            title = "打工完成"
        elif event_type == "started":
            metrics = [
                ("预计报酬", f"{detail.get('reward', 0)} {coin_name}"),
                ("耗时", self._format_duration(detail.get("duration", 0))),
                ("档位", detail.get("energy_tier", "普通打工")),
                ("精力", self._fmt_delta(-float(detail.get("energy_cost", 0)))),
                ("饱食度", self._fmt_delta(-float(detail.get("satiety_cost", 0)))),
                ("成长值", self._fmt_delta(detail.get("growth_add", 0))),
                ("亲密度", self._fmt_delta(detail.get("intimacy_add", 0))),
            ]
            lines = [
                f"出发去{detail.get('job_name', '打工')}啦～",
                f"精力档位：{detail.get('energy_tier', '普通打工')}，报酬 {self._fmt_percent(float(detail.get('energy_reward_multiplier', 1)) * 100)}。",
                "等她回来后，再发送「猫娘打工」领取报酬和成长奖励。",
            ]
            title = "猫娘打工"
        elif event_type == "working":
            metrics = [
                ("剩余", detail.get("remain", "-")),
                ("预计报酬", f"{detail.get('reward', 0)} {coin_name}"),
                ("成长值", self._fmt_delta(detail.get("growth_add", 0))),
                ("亲密度", self._fmt_delta(detail.get("intimacy_add", 0))),
            ]
            lines = [f"正在{detail.get('job_name', '打工')}，还需要一点时间。"]
            title = "打工进行中"
        else:
            metrics = [
                ("健康", self._fmt_int(cat.get("health", 0))),
                ("精力", self._fmt_int(cat.get("energy", 0))),
                ("饱食度", self._fmt_int(cat.get("satiety", 0))),
                ("心情", self._fmt_int(cat.get("mood", 0))),
            ]
            lines = [msg]
            title = "暂不能打工"

        card = self.draw_care_card(title, cat, lines=lines, metrics=metrics, tag=f"work_{uid}")
        return ok, msg, card

    def interact(self, uid: str, action: str):
        self._finalize_expired_adoption(uid)
        today = today_str()
        care_rules = self._care_rules()
        effects = {}
        for item in self._rules("interactions").get("effects", []):
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            command = str(item.get("command") or "").strip()
            if not command:
                continue
            mood_l = max(0, int(item.get("mood_min", 1)))
            mood_h = max(mood_l, int(item.get("mood_max", mood_l)))
            intimacy_l = max(0, int(item.get("intimacy_min", 1)))
            intimacy_h = max(intimacy_l, int(item.get("intimacy_max", intimacy_l)))
            growth_l = max(0, int(item.get("growth_min", 1)))
            growth_h = max(growth_l, int(item.get("growth_max", growth_l)))
            effects[command] = {
                "mood": (mood_l, mood_h),
                "intimacy": (intimacy_l, intimacy_h),
                "growth": (growth_l, growth_h),
                "energy_cost": max(0, int(item.get("energy_cost", 0))),
                "min_stage": max(0, int(item.get("min_stage", 0))),
                "text": str(item.get("text") or "你陪她玩了一会儿。").strip() or "你陪她玩了一会儿。",
            }
        effect = effects.get(action)
        if not effect:
            return False, f"还没有配置「{action}」这个互动动作。可以在插件拓展页添加或启用。", None
        mood_l, mood_h = effect["mood"]
        intimacy_l, intimacy_h = effect["intimacy"]
        growth_l, growth_h = effect["growth"]
        base_energy_cost = effect["energy_cost"]
        base_mood_add = random.randint(mood_l, mood_h)
        base_intimacy_add = random.randint(intimacy_l, intimacy_h)
        base_growth_add = random.randint(growth_l, growth_h)
        min_stage = effect["min_stage"]
        daily_limit = care_rules["interaction_daily_limit"]
        text = effect["text"]

        def op(root):
            now = now_ts()
            ok, cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, cat, None, 0, 0, 0, 0, 0, 1, 1, "", ""

            cats = root.setdefault("catgirls", {})
            interact_data = cat.setdefault("interactions", {})
            today_count = int(interact_data.get(today, 0))
            cooldown = int(care_rules["interaction_cooldown_seconds"])
            last_interact_at = int(interact_data.get("_last_at", 0) or 0)
            if cooldown > 0 and last_interact_at and now - last_interact_at < cooldown:
                cats[uid] = cat
                remain = self._format_duration(cooldown - (now - last_interact_at))
                return False, f"「{cat['name']}」刚刚才互动过，先让她缓一缓吧。\n冷却剩余：{remain}", cat, None, 0, 0, 0, today_count, 0, 1, 1, "", "冷却中"

            if float(cat.get("health", 0)) < care_rules["interact_min_health"]:
                cats[uid] = cat
                return False, f"「{cat['name']}」现在很虚弱，先喂点东西、让她好好休息一下吧。", cat, None, 0, 0, 0, today_count, 0, 1, 1, "", ""
            if int(cat.get("stage", 0)) < min_stage:
                cats[uid] = cat
                return False, f"「{cat['name']}」还有些害羞，等你们更亲近一点再做这个互动吧。", cat, None, 0, 0, 0, today_count, 0, 1, 1, "", ""
            effective_base_energy_cost = max(int(base_energy_cost), int(care_rules["interaction_energy_cost"]))
            energy_cost = self._scaled_int(effective_base_energy_cost, self._personality_multiplier(cat, "interaction_energy_cost_multiplier"))
            if energy_cost and float(cat.get("energy", 0)) < energy_cost:
                cats[uid] = cat
                return False, f"「{cat['name']}」现在精力只有 {self._fmt_int(cat.get('energy', 0))}，这次互动需要 {energy_cost}。先让她休息一下再玩吧～", cat, None, 0, 0, 0, today_count, energy_cost, 1, 1, "", ""

            mood_add = self._scaled_int(base_mood_add, self._personality_multiplier(cat, "interaction_mood_multiplier"))
            mood_multiplier, mood_label = self._mood_interaction_multiplier(cat)
            daily_multiplier, daily_label = self._interaction_daily_multiplier(today_count)
            reward_multiplier = mood_multiplier * daily_multiplier
            intimacy_add = self._scaled_int(base_intimacy_add, self._personality_multiplier(cat, "interaction_intimacy_multiplier") * reward_multiplier)
            growth_add = self._scaled_int(base_growth_add, self._personality_multiplier(cat, "interaction_growth_multiplier") * reward_multiplier)
            cat["mood"] = round(clamp(float(cat.get("mood", 80)) + mood_add, 0, 100), 4)
            cat["intimacy"] = int(cat.get("intimacy", 0)) + intimacy_add
            cat["growth"] = int(cat.get("growth", 0)) + growth_add
            cat["energy"] = round(clamp(float(cat.get("energy", 80)) - energy_cost, 0, 100), 4)
            interact_data[today] = today_count + 1
            interact_data["_last_at"] = now
            cat["interactions"] = interact_data
            stats = cat.setdefault("care_stats", {})
            stats["total_interacts"] = int(stats.get("total_interacts", 0)) + 1
            cat, stage_msg = self._advance_stage(cat)
            cats[uid] = cat
            return True, "", cat, stage_msg, mood_add, intimacy_add, growth_add, interact_data[today], energy_cost, mood_multiplier, daily_multiplier, mood_label, daily_label

        ok, err_msg, cat, stage_msg, mood_add, intimacy_add, growth_add, today_count, energy_cost, mood_multiplier, daily_multiplier, mood_label, daily_label = self.store.update(op)
        if not ok:
            card = self.draw_care_card(
                "互动未完成",
                cat,
                lines=[err_msg],
                metrics=[
                    ("健康", self._fmt_int(cat.get("health", 0))) if cat else ("健康", "-"),
                    ("精力", self._fmt_int(cat.get("energy", 0))) if cat else ("精力", "-"),
                    ("今日互动", str(today_count) if cat else "-"),
                ],
                tag=f"interact_err_{uid}",
            ) if cat else None
            return False, err_msg, card

        reward_multiplier = float(mood_multiplier or 1) * float(daily_multiplier or 1)
        msg = (
            f"{text}\n"
            f"心情 {self._fmt_delta(mood_add)}\n"
            f"亲密度 {self._fmt_delta(intimacy_add)}\n"
            f"成长值 {self._fmt_delta(growth_add)}\n"
            f"互动收益：{self._fmt_percent(reward_multiplier * 100)}（{mood_label}，{daily_label}）\n"
            f"当前阶段：{stage_name(cat.get('stage', 0))}\n"
            f"今日互动次数：{today_count}/{daily_limit if daily_limit else '不限'}"
        )
        if energy_cost:
            msg += f"\n精力 {self._fmt_delta(-energy_cost)}"
        if stage_msg:
            msg += stage_msg
        card = self.draw_care_card(
            "互动结果",
            cat,
            subtitle=action,
            lines=[
                text,
                f"互动收益：{self._fmt_percent(reward_multiplier * 100)}（{mood_label}，{daily_label}）",
                stage_msg.strip() if stage_msg else "",
            ],
            metrics=[
                ("心情", self._fmt_delta(mood_add)),
                ("亲密度", self._fmt_delta(intimacy_add)),
                ("成长值", self._fmt_delta(growth_add)),
                ("精力", self._fmt_delta(-energy_cost) if energy_cost else "0.0"),
                ("今日互动", f"{today_count}/{daily_limit if daily_limit else '不限'}"),
                ("收益", self._fmt_percent(reward_multiplier * 100)),
                ("阶段", stage_name(cat.get("stage", 0))),
            ],
            tag=f"interact_{uid}",
        )
        return True, msg, card

    def rename(self, uid: str, name: str):
        self._finalize_expired_adoption(uid)

        name = name.strip()
        if not name or len(name) > 12:
            return False, "名字不能为空，且长度不能超过 12。", None

        def op(root):
            ok, cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, cat
            cat["name"] = name
            root.setdefault("catgirls", {})[uid] = cat
            return True, f"改名成功啦～以后就叫她「{name}」喵。", cat

        ok, msg, cat = self.store.update(op)
        card = self.draw_care_card(
            "改名完成" if ok else "改名未完成",
            cat,
            lines=[msg],
            metrics=[
                ("名字", name if ok else "-"),
                ("阶段", stage_name(cat.get("stage", 0)) if cat else "-"),
            ],
            tag=f"rename_{uid}",
        ) if cat else None
        return ok, msg, card

    async def change_image(self, uid: str, image_src: str):
        """安全保存图片，成功后原子扣费。"""
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", None
        _, _, appearance_change_price = self._wish_rules()
        coin_name = self._coin_name()

        safe_uid = self._safe_uid(uid)
        stamp = int(time.time() * 1000)
        tmp = self.upload_dir / f"{safe_uid}_{stamp}.tmp"
        out = self.upload_dir / f"{safe_uid}_{stamp}.jpg"

        try:
            await self._save_image_src(image_src, tmp)
            await asyncio.to_thread(self._validate_and_normalize_image, tmp, out)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            out.unlink(missing_ok=True)
            msg = f"保存图片失败：{e}"
            card = self.draw_care_card("形象更换未完成", cat, lines=[msg], tag=f"image_err_{uid}")
            return False, msg, card
        finally:
            tmp.unlink(missing_ok=True)

        def op(root):
            wallet = root.setdefault("wallet", {})
            cats = root.setdefault("catgirls", {})
            current_cat = cats.get(uid)
            if not current_cat or not current_cat.get("name"):
                return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", None, None

            balance = int(wallet.get(uid, 0))
            if balance < appearance_change_price:
                return False, f"更换形象需要 {appearance_change_price} {coin_name}，你目前有 {balance} {coin_name}，还不够喔～", current_cat, None

            current_cat, _ = normalize_catgirl(current_cat, uid)
            old_image = current_cat.get("image", "")
            wallet[uid] = balance - appearance_change_price
            current_cat["image"] = str(out)
            cats[uid] = current_cat
            return True, "", current_cat, old_image

        ok, msg, updated_cat, old_image = self.store.update(op)
        if not ok:
            out.unlink(missing_ok=True)
            card = self.draw_care_card("形象更换未完成", updated_cat or cat, lines=[msg], tag=f"image_err_{uid}")
            return False, msg, card

        self._delete_old_uploaded_image(old_image)
        balance = self.economy.get_balance(uid)
        msg = f"✨ 「{updated_cat['name']}」换好新形象啦～\n花费：{appearance_change_price} {coin_name}\n当前余额：{balance} {coin_name}\n\n当前档案：\n阶段：{stage_name(updated_cat.get('stage', 0))}\n亲密等级：{self._intimacy_display(updated_cat)}\n成长进度：{self._growth_display(updated_cat)}\n心情：{self._fmt_int(updated_cat.get('mood', 0))}\n状态：{status_tag(updated_cat)}"
        card = self.draw_care_card(
            "形象更换完成",
            updated_cat,
            lines=["新形象已经保存。"],
            metrics=[
                ("花费", f"{appearance_change_price} {coin_name}"),
                ("余额", f"{balance} {coin_name}"),
                ("亲密等级", self._intimacy_display(updated_cat)),
                ("成长进度", self._growth_display(updated_cat)),
                ("心情", self._fmt_int(updated_cat.get("mood", 0))),
                ("阶段", stage_name(updated_cat.get("stage", 0))),
            ],
            tag=f"image_{uid}",
        )
        return True, msg, card

    def _safe_uid(self, uid: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(uid))[:64]
        return safe or "user"

    def _delete_old_uploaded_image(self, old_image: str):
        if not old_image:
            return
        try:
            old_path = Path(old_image).resolve()
            old_path.relative_to(self.upload_dir.resolve())
            if old_path.is_file():
                old_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _host_is_blocked(self, host: str) -> bool:
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                    or ip.is_unspecified
                ):
                    return True
            return False
        except Exception:
            return True

    def _validate_remote_url(self, src: str):
        parsed = urlparse(src)
        if parsed.scheme != "https":
            raise ValueError("只允许 https 图片链接")
        if not parsed.hostname:
            raise ValueError("图片链接缺少主机名")
        if self._host_is_blocked(parsed.hostname):
            raise PermissionError("不允许访问内网、本机或保留地址")

    async def _save_image_src(self, src: str, out: Path):
        out.parent.mkdir(parents=True, exist_ok=True)

        if src.startswith(("http://", "https://")):
            self._validate_remote_url(src)
            timeout = aiohttp.ClientTimeout(total=20, connect=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(src, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://qq.com/",
                }, allow_redirects=False) as resp:
                    resp.raise_for_status()

                    content_type = resp.headers.get("Content-Type", "").lower()
                    if not content_type.startswith("image/"):
                        raise ValueError("链接内容不是图片")

                    content_length = resp.headers.get("Content-Length")
                    if content_length and int(content_length) > MAX_IMAGE_BYTES:
                        raise ValueError("图片文件过大")

                    total = 0
                    with out.open("wb") as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > MAX_IMAGE_BYTES:
                                raise ValueError("图片文件过大")
                            f.write(chunk)
            return

        if src.startswith("file://"):
            src = src[7:]

        p = Path(src).resolve()
        try:
            p.relative_to(self.upload_dir.resolve())
        except ValueError:
            raise PermissionError(f"安全限制：不允许访问 {src}")

        if p.exists() and p.is_file():
            if p.stat().st_size > MAX_IMAGE_BYTES:
                raise ValueError("图片文件过大")
            await asyncio.to_thread(shutil.copyfile, p, out)
            return

        raise FileNotFoundError(f"无法识别图片来源：{src}")

    def _validate_and_normalize_image(self, src: Path, out: Path):
        if src.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("图片文件过大")

        with Image.open(src) as img:
            img.verify()

        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            width, height = img.size
            if width <= 0 or height <= 0:
                raise ValueError("图片尺寸无效")
            if width * height > MAX_IMAGE_PIXELS or width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT:
                raise ValueError("图片尺寸过大")

            img = img.convert("RGB")
            img.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT), Image.LANCZOS)
            out.parent.mkdir(parents=True, exist_ok=True)
            img.save(out, "JPEG", quality=90, optimize=True)


    def migrate_to_group(self, gid: str, uid: str):
        self._finalize_expired_adoption(uid)

        def op(root):
            ok, cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, cat, ""
            old_gid = cat.get("home_gid", "")
            cat["home_gid"] = gid
            root.setdefault("catgirls", {})[uid] = cat
            return True, "", cat, old_gid

        ok, err_msg, cat, old_gid = self.store.update(op)
        if not ok:
            return False, err_msg, None
        if old_gid == gid:
            msg = f"「{cat.get('name', '猫娘')}」本来就在当前群登记啦～"
        else:
            msg = f"迁移完成喵～\n「{cat.get('name', '猫娘')}」已经登记到当前群，以后会出现在本群的羁绊排行榜里啦。"

        card = self.draw_care_card(
            "猫娘迁移",
            cat,
            lines=[msg],
            metrics=[
                ("登记群", gid),
                ("阶段", stage_name(cat.get("stage", 0))),
                ("羁绊分", bond_score(cat)),
                ("亲密等级", self._intimacy_display(cat)),
            ],
            tag=f"migrate_{uid}",
        )
        return True, msg, card

    def draw_rank(self, gid: str = None) -> Optional[Path]:
        sign_data = self.store.get("sign", default={}) or {}

        def op(root):
            all_cats = root.setdefault("catgirls", {})
            rows = []
            for uid, cat in list(all_cats.items()):
                if not isinstance(cat, dict) or not cat.get("name"):
                    continue
                cat, _ = normalize_catgirl(cat, uid)
                cat, runaway = self._apply_decay(cat)
                if runaway:
                    all_cats.pop(uid, None)
                    continue
                all_cats[uid] = cat
                if gid and cat.get("home_gid") != gid:
                    continue

                rank_cat = dict(cat)
                nickname = sign_data.get(uid, {}).get("last_nickname", uid)
                rank_cat["owner_nickname"] = nickname
                rows.append(rank_cat)
            return rows

        cats = self.store.update(op)

        if not cats:
            return None

        cats.sort(key=lambda x: bond_score(x), reverse=True)
        cats = cats[:12]

        cols = 4
        card_w = 340
        card_h = 540
        padding = 30
        img_w = 280
        img_h = 320
        title_h = 120
        bottom_padding = 80
        side_margin = 50

        rows = math.ceil(len(cats) / cols)
        content_w = cols * card_w + (cols - 1) * padding
        total_w = content_w + side_margin * 2
        total_h = title_h + rows * (card_h + padding) - padding + bottom_padding

        canvas = Image.new("RGB", (total_w, total_h), "white")
        d = ImageDraw.Draw(canvas)

        title_font = self._font(76)
        name_font = self._font(32)
        info_font = self._font(26)

        d.text((total_w // 2, 60), "猫娘羁绊排行榜", font=title_font, fill=(255, 140, 0), anchor="mm")

        for i, cat in enumerate(cats):
            row = i // cols
            col = i % cols
            x = side_margin + col * (card_w + padding)
            y = title_h + row * (card_h + padding)

            d.rounded_rectangle((x, y, x + card_w, y + card_h), radius=15, outline=(0, 191, 255), width=5)

            cat_name = str(cat.get("name", "猫娘"))
            if len(cat_name) > 8:
                cat_name = cat_name[:8] + "..."
            d.text((x + card_w // 2, y + 30), cat_name, font=name_font, fill=(40, 40, 40), anchor="mm")

            img_y = y + 60
            img_path = self.image_path(cat)
            if img_path and Path(img_path).exists():
                try:
                    img = Image.open(img_path).convert("RGB")
                    img = self._cover(img, img_w, img_h)
                    canvas.paste(img, (x + 30, img_y))
                except Exception:
                    self._draw_no_image(d, x + 30, img_y, img_w, img_h)
            else:
                self._draw_no_image(d, x + 30, img_y, img_w, img_h)

            info_y = img_y + img_h + 20
            d.text((x + card_w // 2, info_y), f"{stage_name(cat.get('stage', 0))}｜亲密 {self._intimacy_display(cat)}", font=info_font, fill=(60, 60, 60), anchor="mm")
            d.text((x + card_w // 2, info_y + 35), f"羁绊分: {bond_score(cat)}", font=info_font, fill=(255, 140, 0), anchor="mm")

            owner = cat.get("owner_nickname", cat.get("user", ""))
            if len(owner) > 10:
                owner = owner[:10] + "..."
            d.text((x + card_w // 2, info_y + 70), f"主人: {owner}", font=info_font, fill=(60, 60, 60), anchor="mm")

        out = self.cache_dir / f"bond_rank_{gid or 'global'}.png"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        canvas.save(out, "PNG")
        return out

    def _draw_no_image(self, d: ImageDraw.ImageDraw, x: int, y: int, img_w: int, img_h: int):
        d.rounded_rectangle((x, y, x + img_w, y + img_h), radius=10, fill=(240, 240, 240))
        font = self._font(28)
        d.text((x + img_w // 2, y + img_h // 2), "暂无图片", font=font, fill=(100, 100, 100), anchor="mm")

    def _cover(self, img: Image.Image, w: int, h: int) -> Image.Image:
        iw, ih = img.size
        scale = max(w / iw, h / ih)
        nw, nh = int(iw * scale), int(ih * scale)
        img = img.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - w) // 2, (nh - h) // 2
        return img.crop((left, top, left + w, top + h))
