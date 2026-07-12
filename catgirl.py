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
from typing import Any, Callable, Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

from .storage import JsonStore
from .economy import EconomyService
from .image_output import save_scaled_image
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
MOOD_DECAY_PER_MINUTE = 4 / (24 * 60)
ENERGY_RECOVERY_PER_MINUTE = 32 / (24 * 60)
HEALTH_HUNGRY_DECAY_PER_MINUTE = 6 / (24 * 60)
HEALTH_LOW_MOOD_DECAY_PER_MINUTE = 3 / (24 * 60)
HEALTH_RECOVERY_PER_MINUTE = 1.5 / (24 * 60)
RUNAWAY_AFTER_ZERO_SECONDS = 24 * 60 * 60
WEIGHT_MIN = 40.0
WEIGHT_MAX = 90.0
STARTER_CARD_GRANT_ID = "starter_cards_v1"
RENAME_CARD_ID = "rename_card"
APPEARANCE_CARD_ID = "appearance_card"
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# ---- 设计 token：统一所有卡片的配色与圆角 ----
# 主色降饱和、提高明度一致性，避免原 #FF8C00 + #00BFFF 的撞色观感。
NEKO_THEME = {
    "primary": (232, 131, 58),      # 标题 / 关键数值：暖橙降饱和
    "primary_alpha": (232, 131, 58, 255),
    "accent": (91, 141, 184),       # 强调：莫兰迪灰蓝
    "pink": (214, 123, 166),        # 互动 / 心情：莫兰迪粉
    "green": (110, 173, 124),       # 健康 / 成长：低饱和绿
    "purple": (135, 121, 184),      # 成长 / 档位：低饱和紫
    "text": (58, 62, 68),           # 正文：近黑深灰
    "text_dark": (38, 42, 48),      # 猫娘名 / 重点正文
    "muted": (138, 144, 150),       # 次要文字
    "panel_fill": (255, 255, 255, 200),    # 面板底色
    "panel_outline": (255, 255, 255, 210), # 面板描边（浅白）
    "card_outline": (255, 255, 255, 215),  # 卡片描边
    "row_a": (255, 250, 244, 175),        # 表格奇数行
    "row_b": (252, 244, 236, 160),        # 表格偶数行
    "shadow": (0, 0, 0, 26),              # 阴影：中性灰
    "radius_card": 22,
    "radius_panel": 14,
    "radius_cell": 10,
    "radius_bar": 12,
    "shadow_offset": (0, 6),
    "shadow_blur": 14,
}


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
        appearance_change_price: int = 900,
        runtime_config_provider: Callable[[], Dict] | None = None,
        events: "EventService | None" = None,
        astrbot_temp_dir: Path | None = None,
    ):
        self.store = store
        self.economy = economy
        self.coin_name = coin_name
        self.base_dir = Path(base_dir)
        self.catgirl_dir = Path(catgirl_dir)
        self.upload_dir = Path(upload_dir)
        self.font_dir = Path(font_dir)
        self.cache_dir = Path(cache_dir)
        self.astrbot_temp_dir = Path(astrbot_temp_dir) if astrbot_temp_dir else None
        self.wish_probability = float(wish_probability)
        self.wish_pity = int(wish_pity)
        self.appearance_change_price = int(appearance_change_price)
        self.runtime_config_provider = runtime_config_provider
        self.events = events
        self.daily_wish_status_provider = None
        self.theme = NEKO_THEME
        # 渐变背景缓存：(w, h) -> Image，避免每次渲染都重算逐像素渐变
        self._bg_cache: Dict[Tuple[int, int], Image.Image] = {}
        self._mask_cache: Dict[Tuple[int, int, int], Image.Image] = {}
        # 限制缓存条数，避免长驻进程无限增长
        self._bg_cache_limit = 24
        self._mask_cache_limit = 256

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

    def _output_image_scale(self) -> float:
        render = self._rules("render")
        try:
            return max(0.4, min(1.0, float(render.get("output_image_scale", 0.85))))
        except Exception:
            return 0.85

    def _care_rules(self) -> Dict:
        care = self._rules("care")
        rules = {
            "feed_satiety_limit": float(care.get("feed_satiety_limit", FEED_SATIETY_LIMIT)),
            "satiety_decay_per_minute": 100 / max(1.0, float(care.get("satiety_decay_minutes", SATIETY_DECAY_MINUTES))),
            "mood_decay_per_minute": max(0.0, float(care.get("mood_decay_per_day", 4))) / (24 * 60),
            "energy_recovery_per_minute": max(0.0, float(care.get("energy_recovery_per_day", 32))) / (24 * 60),
            "health_hungry_decay_per_minute": max(0.0, float(care.get("health_hungry_decay_per_day", 6))) / (24 * 60),
            "health_low_mood_decay_per_minute": max(0.0, float(care.get("health_low_mood_decay_per_day", 3))) / (24 * 60),
            "health_recovery_per_minute": max(0.0, float(care.get("health_recovery_per_day", 1.5))) / (24 * 60),
            "health_hungry_satiety_threshold": float(care.get("health_hungry_satiety_threshold", 20)),
            "health_low_mood_threshold": float(care.get("health_low_mood_threshold", 30)),
            "runaway_after_zero_seconds": max(1, int(float(care.get("runaway_after_zero_hours", 168)) * 60 * 60)),
            "interaction_daily_limit": max(0, int(care.get("interaction_daily_limit", 5))),
            "interaction_cooldown_seconds": max(0, int(care.get("interaction_cooldown_seconds", 300))),
            "interaction_energy_cost": max(0, int(care.get("interaction_energy_cost", 4))),
            "interaction_soft_limit_extra": max(0, int(care.get("interaction_soft_limit_extra", 3))),
            "interaction_heavy_limit_extra": max(0, int(care.get("interaction_heavy_limit_extra", 7))),
            "interaction_soft_limit_multiplier": max(0.0, float(care.get("interaction_soft_limit_multiplier", 0.65))),
            "interaction_heavy_limit_multiplier": max(0.0, float(care.get("interaction_heavy_limit_multiplier", 0.35))),
            "interaction_minimal_limit_multiplier": max(0.0, float(care.get("interaction_minimal_limit_multiplier", 0.15))),
            "interaction_good_mood_threshold": float(care.get("interaction_good_mood_threshold", 80)),
            "interaction_low_mood_threshold": float(care.get("interaction_low_mood_threshold", 50)),
            "interaction_bad_mood_threshold": float(care.get("interaction_bad_mood_threshold", 30)),
            "interaction_high_mood_multiplier": max(0.0, float(care.get("interaction_high_mood_multiplier", 1.12))),
            "interaction_low_mood_multiplier": max(0.0, float(care.get("interaction_low_mood_multiplier", 0.8))),
            "interaction_bad_mood_multiplier": max(0.0, float(care.get("interaction_bad_mood_multiplier", 0.55))),
            "feed_healthy_threshold": float(care.get("feed_healthy_threshold", 70)),
            "feed_low_health_threshold": float(care.get("feed_low_health_threshold", 40)),
            "feed_bad_health_threshold": float(care.get("feed_bad_health_threshold", 20)),
            "feed_low_health_multiplier": max(0.0, float(care.get("feed_low_health_multiplier", 0.9))),
            "feed_bad_health_multiplier": max(0.0, float(care.get("feed_bad_health_multiplier", 0.72))),
            "feed_critical_health_multiplier": max(0.0, float(care.get("feed_critical_health_multiplier", 0.55))),
            "work_stable_energy_threshold": float(care.get("work_stable_energy_threshold", 55)),
            "work_high_energy_threshold": float(care.get("work_high_energy_threshold", 85)),
            "work_stable_energy_reward_multiplier": max(0.0, float(care.get("work_stable_energy_reward_multiplier", 1.04))),
            "work_high_energy_reward_multiplier": max(0.0, float(care.get("work_high_energy_reward_multiplier", 1.12))),
            "work_min_health": float(care.get("work_min_health", 35)),
            "interact_min_health": float(care.get("interact_min_health", 20)),
            "work_min_satiety": float(care.get("work_min_satiety", 20)),
            "work_min_mood": float(care.get("work_min_mood", 30)),
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
            "satiety_add_min": 18,
            "satiety_add_max": 30,
            "mood_add_min": 2,
            "mood_add_max": 7,
            "health_add_min": 0,
            "health_add_max": 3,
            "energy_add_min": 4,
            "energy_add_max": 9,
            "growth_add_min": 3,
            "growth_add_max": 7,
            "intimacy_add_min": 1,
            "intimacy_add_max": 3,
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
            ok, cat, _ = self._load_active_cat(root, uid, consume_notice=False)
            return cat if ok else None
        return self.store.update(op)

    def _save(self, uid: str, data: Dict):
        data, _ = normalize_catgirl(data, uid)
        self.store.set("catgirls", uid, value=data)

    def has_catgirl(self, uid: str) -> bool:
        cat = self._get(uid)
        return bool(cat and cat.get("name"))

    def update_notify_target(self, uid: str, session: str = "", platform: str = "") -> None:
        session = str(session or "").strip()
        platform = str(platform or "").strip()
        if not session and not platform:
            return

        def op(root):
            cats = root.setdefault("catgirls", {})
            cat = cats.get(uid)
            if not isinstance(cat, dict) or not cat.get("name"):
                return
            if session:
                cat["notify_session"] = session
            if platform:
                cat["notify_platform"] = platform
            cats[uid] = cat

        self.store.update(op)

    def missing_cat_message(self, uid: str) -> str:
        def op(root):
            notice = self._pop_runaway_notice(root, uid)
            return notice or "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。"

        return self.store.update(op)

    def _daily_wish_status_brief(self, uid: str) -> Dict[str, Any]:
        provider = getattr(self, "daily_wish_status_provider", None)
        if not callable(provider):
            return {}
        try:
            data = provider(uid)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _font(self, size: int, role: str = "body"):
        """按角色选字体：title=卡通体(猫娘题材)，body/num=黑体。"""
        if role == "title":
            candidates = [
                self.font_dir / "FZKATJW.ttf",
                self.font_dir / "GBK.TTF",
                self.base_dir / "FZKATJW.ttf",
            ]
        else:
            candidates = [self.font_dir / "GBK.TTF", self.font_dir / "FZKATJW.ttf", self.base_dir / "GBK.TTF"]
        for p in candidates:
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def _title_font(self, size: int):
        return self._font(size, role="title")

    def _metric_palette(self, label: str, idx: int = 0):
        t = self.theme
        palettes = [
            (t["primary"], (200, 96, 36)),   # 0 饱食
            (t["pink"], (190, 92, 132)),     # 1 心情
            (t["green"], (74, 154, 96)),     # 2 健康
            (t["accent"], (52, 108, 148)),   # 3 精力
            (t["pink"], (200, 96, 60)),      # 4 亲密
            (t["purple"], (104, 86, 168)),   # 5 成长
            (t["accent"], (60, 120, 160)),   # 6 效率/报酬/获得/余额
            ((172, 177, 182), (132, 139, 145)),  # 7 体重
        ]
        text = str(label)
        preferred = {
            "饱食": 0,
            "心情": 1,
            "健康": 2,
            "精力": 3,
            "亲密": 4,
            "成长": 5,
            "效率": 6,
            "报酬": 6,
            "获得": 6,
            "余额": 6,
            "体重": 7,
            "耗时": 3,
            "剩余": 3,
            "档位": 5,
            "加成": 5,
        }
        for key, value in preferred.items():
            if key in text:
                return palettes[value]
        return palettes[idx % len(palettes)]

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
            "unlocks": {"work_jobs": []},
            "buffs": {},
            "care_cooldowns": {},
            "gift_stats": {},
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
                root.setdefault("runaway_catgirls", {}).pop(uid, None)
                self._grant_starter_cards(root, uid)
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
                cat, runaway = self._apply_decay(cat)
                if runaway:
                    cats.pop(uid, None)
                    self._set_runaway_notice(root, uid, cat)
                    cat = None
                else:
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
                        granted = self._grant_starter_cards(root, uid)
                        pending_adoptions.pop(uid, None)
                        return False, "already", f"你已经有猫娘「{adopted['name']}」啦，要好好疼她喔～{self._starter_card_notice(granted)}", None, None
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
        return self.draw_showcase_card(
            title,
            cat,
            subtitle=f"{cat.get('personality', '温柔')}｜{cat.get('body_type', '匀称')}",
            lines=[
                f"一位{cat.get('personality', '温柔')}的猫娘听见了你的愿望，悄悄来到了你身边。",
                "她正在等你给出回应。",
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
            root.setdefault("runaway_catgirls", {}).pop(uid, None)
            granted = self._grant_starter_cards(root, uid)
            pending_adoptions.pop(uid, None)
            return True, f"收养完成啦～\n猫娘「{selected.get('name', '猫娘')}」轻轻牵住了你的手。\n从今天开始，你们的羁绊会在每一次陪伴里慢慢成长 ฅ^•ﻌ•^ฅ{self._starter_card_notice(granted)}", selected

        ok, msg, selected = self.store.update(op)
        card = self.draw_showcase_card(
            "收养完成" if ok else "收养未完成",
            selected,
            subtitle=f"{selected.get('personality', '温柔')}｜{selected.get('body_type', '匀称')}" if selected else "",
            lines=[msg],
            footer="新的羁绊已经开始。发送「猫娘状态」查看完整档案。",
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
            "她的档案会暂时保留。你可以在商店购买并使用「命运的红线」召回她，也可以再次发送「请赐我一只可爱猫娘吧」重新遇见新的猫娘。"
        )

    def _set_runaway_notice(self, root: Dict, uid: str, cat: Dict) -> str:
        cat = dict(cat or {})
        cat["runaway_at"] = now_ts()
        cat["is_runaway"] = True
        root.setdefault("runaway_catgirls", {})[uid] = cat
        message = self._runaway_message(cat)
        root.setdefault("runaway_notices", {})[uid] = {
            "message": message,
            "cat_name": str(cat.get("name", "猫娘")),
            "created_at": now_ts(),
        }
        return message

    def _pop_runaway_notice(self, root: Dict, uid: str) -> Optional[str]:
        notice = root.setdefault("runaway_notices", {}).pop(uid, None)
        if isinstance(notice, dict):
            message = str(notice.get("message", "")).strip()
            return message or None
        if isinstance(notice, str):
            return notice.strip() or None
        return None

    def _load_active_cat(self, root: Dict, uid: str, consume_notice: bool = True):
        cats = root.setdefault("catgirls", {})
        cat = cats.get(uid)
        if not cat or not cat.get("name"):
            if consume_notice:
                notice = self._pop_runaway_notice(root, uid)
                if notice:
                    return False, None, notice
            return False, None, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。"
        cat, _ = normalize_catgirl(cat, uid)
        cat, runaway = self._apply_decay(cat)
        if runaway:
            cats.pop(uid, None)
            return False, cat, self._set_runaway_notice(root, uid, cat)
        cats[uid] = cat
        self._grant_starter_cards(root, uid)
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

    def _work_energy_tier(self, energy: float) -> Tuple[str, float]:
        rules = self._care_rules()
        energy = float(energy or 0)
        if energy >= rules["work_high_energy_threshold"]:
            return "高收益打工", float(rules["work_high_energy_reward_multiplier"])
        if energy >= rules["work_stable_energy_threshold"]:
            return "稳定打工", float(rules["work_stable_energy_reward_multiplier"])
        return "普通打工", 1.0

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

    def _lerp_color(self, a, b, t: float):
        t = max(0.0, min(1.0, float(t)))
        return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))

    def _make_linear_gradient(self, size, stops):
        """生成水平线性渐变。按 1D 向量化构造再垂直广播，避免逐像素循环。"""
        w, h = max(1, int(size[0])), max(1, int(size[1]))
        stops = sorted((max(0.0, min(1.0, float(pos))), tuple(color)) for pos, color in stops)
        if not stops:
            return Image.new("RGB", (w, h))
        # 分段插值：对每个 x 算颜色，得到 (w, 3) 的颜色行，再 resize 到整张图。
        ts = [i / max(1, w - 1) for i in range(w)]
        colors = []
        for t in ts:
            left, right = stops[0], stops[-1]
            for idx in range(len(stops) - 1):
                if stops[idx][0] <= t <= stops[idx + 1][0]:
                    left, right = stops[idx], stops[idx + 1]
                    break
            span = max(0.0001, right[0] - left[0])
            colors.append(self._lerp_color(left[1], right[1], (t - left[0]) / span))
        row = Image.new("RGB", (w, 1))
        row.putdata(colors)
        # 垂直广播：resize 一行到 h 高度即可
        img = row.resize((w, h), Image.NEAREST)
        return img

    def _make_card_background(self, size):
        w, h = max(1, int(size[0])), max(1, int(size[1]))
        cached = self._bg_cache.get((w, h))
        if cached is not None:
            return cached.copy()
        bg = self._make_linear_gradient(
            (w, h),
            [
                (0.00, (226, 244, 250)),  # 浅蓝白
                (0.42, (247, 250, 252)),
                (0.70, (253, 246, 240)),  # 浅暖白
                (1.00, (250, 236, 232)),  # 浅暖橘
            ],
        ).convert("RGBA")
        glow = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        gd = ImageDraw.Draw(glow)
        # 光晕颜色对齐 theme，降低强度，更柔和
        gd.ellipse((-160, 90, 360, 610), fill=(170, 206, 226, 56))
        gd.ellipse((w - 430, -170, w + 150, 360), fill=(244, 198, 196, 54))
        gd.ellipse((w - 430, h - 360, w + 180, h + 180), fill=(248, 210, 168, 42))
        out = Image.alpha_composite(bg, glow.filter(ImageFilter.GaussianBlur(52))).convert("RGB")
        if len(self._bg_cache) >= self._bg_cache_limit:
            # 简单 FIFO 淘汰最早的 key
            self._bg_cache.pop(next(iter(self._bg_cache)))
        self._bg_cache[(w, h)] = out
        return out.copy()

    def _rounded_mask(self, size, radius: int):
        key = (int(size[0]), int(size[1]), int(radius))
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached.copy()
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
        if len(self._mask_cache) >= self._mask_cache_limit:
            self._mask_cache.pop(next(iter(self._mask_cache)))
        self._mask_cache[key] = mask
        return mask.copy()

    def _paste_round(self, canvas: Image.Image, img: Image.Image, box, radius: int):
        x, y, w, h = box
        mask = self._rounded_mask((w, h), radius)
        canvas.paste(img.convert("RGBA"), (x, y), mask)

    def _draw_shadow(self, canvas: Image.Image, box, radius: int):
        """绘制柔和投影。颜色/偏移/模糊来自 theme，避免硬编码。"""
        x1, y1, x2, y2 = [int(v) for v in box]
        t = self.theme
        ox, oy = t["shadow_offset"]
        sb = t["shadow_blur"]
        shadow_img = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow_img)
        sd.rounded_rectangle((x1 + ox, y1 + oy, x2 + ox, y2 + oy), radius=radius, fill=t["shadow"])
        canvas.alpha_composite(shadow_img.filter(ImageFilter.GaussianBlur(sb)))

    def _draw_round_panel(self, canvas: Image.Image, box, radius: int, fill, outline=None, width: int = 1, shadow=True):
        x1, y1, x2, y2 = [int(v) for v in box]
        if shadow:
            self._draw_shadow(canvas, box, radius)
        d = ImageDraw.Draw(canvas, "RGBA")
        d.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=fill, outline=outline, width=width)

    def _metric_progress(self, label: str, value: str, cat: Optional[Dict]):
        """返回 0~1 进度，仅当数据有明确的上限语义（0~100 / x/total / 百分比 / 状态条字段）时。
        对余额、体重、宝石数量、亲密等级、档位等无固定上限的数据返回 None，不画进度条。"""
        raw = str(value or "")
        # 1) 显式 x/total 形式：有明确上限，可量化
        ratio = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", raw)
        if ratio:
            try:
                current = float(ratio.group(1))
                total = float(ratio.group(2))
                if total > 0:
                    return clamp(current / total * 100, 0, 100) / 100
            except Exception:
                pass
        try:
            first_num = re.search(r"[-+]?\d+(?:\.\d+)?", raw)
            number = float(first_num.group(0)) if first_num else 0.0
        except Exception:
            first_num = None
            number = 0.0
        if not first_num:
            # 纯文字值（如体型「匀称」、性格名）无量化语义
            return None
        label = str(label or "")
        # 2) 饱食/心情/健康/精力：固定 0~100，从猫娘档案取真实值
        if any(key in label for key in ("饱食", "心情", "健康", "精力")) and cat:
            field = {"饱食": "satiety", "心情": "mood", "健康": "health", "精力": "energy"}
            for key, attr in field.items():
                if key in label:
                    try:
                        return clamp(float(cat.get(attr, 0) or 0), 0, 100) / 100
                    except Exception:
                        return None
        # 3) 成长进度：返回值带 % 且在 0~100，可量化
        if "成长" in label and cat:
            try:
                return clamp(float(self._growth_display(cat).rstrip("%")), 0, 100) / 100
            except Exception:
                pass
        # 4) 显式百分号：可量化
        if "%" in raw:
            return clamp(number, 0, 100) / 100
        # 其余（余额、体重、宝石数量、亲密等级 Lv.x、档位、加成、耗时、报酬等）无固定上限，不画进度条
        return None

    def _draw_gradient_bar(self, canvas: Image.Image, box, colors, progress: float, text: str = "", font=None):
        """进度条：3x 超采样抗锯齿 + 1px 细描边 + 玻璃高光。
        小尺寸 rounded_rectangle 直接画会锯齿，所以在 3 倍分辨率上画完再 LANCZOS 缩回。
        """
        x1, y1, x2, y2 = [int(v) for v in box]
        bar_w = max(1, x2 - x1)
        bar_h = max(1, y2 - y1)
        radius = max(3, min(bar_h // 2, 6))
        d = ImageDraw.Draw(canvas, "RGBA")

        # 超采样倍数：3x 足以消除 12px 高圆角的锯齿
        ss = 3
        sw, sh = bar_w * ss, bar_h * ss
        sr = radius * ss

        progress = max(0.0, min(1.0, float(progress or 0)))

        # palette 取色
        c0 = colors[0]
        c1 = colors[1] if len(colors) > 1 else colors[0]
        # 描边色：palette 深端再压暗一点
        stroke_color = tuple(max(0, int(v * 0.55)) for v in c1)

        # ---- 在超采样画布上绘制 ----
        bar = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bar)

        # 轨道：半透明白底
        bd.rounded_rectangle((0, 0, sw - 1, sh - 1), radius=sr, fill=(255, 255, 255, 80))

        # 填充
        if progress > 0:
            fill_w = max(sr, int(sw * progress))
            # 超采样渐变（直接用 _make_linear_gradient 在大尺寸生成）
            fill = self._make_linear_gradient((fill_w, sh), [(0, c0), (1, c1)]).convert("RGBA")
            # 顶部高光
            hl_h = max(1, int(sh * 0.40))
            highlight = Image.new("RGBA", (fill_w, sh), (0, 0, 0, 0))
            hd = ImageDraw.Draw(highlight)
            for hy in range(hl_h):
                alpha = int(70 * (1 - hy / max(1, hl_h)))
                hd.line([(0, hy), (fill_w, hy)], fill=(255, 255, 255, alpha))
            fill = Image.alpha_composite(fill, highlight)
            # 用圆角 mask 裁填充
            fill_mask = self._rounded_mask((fill_w, sh), sr)
            bar.paste(fill, (0, 0), fill_mask)

        # 1px 细描边（超采样下是 ss px，缩回后约 1px）
        bd.rounded_rectangle((0, 0, sw - 1, sh - 1), radius=sr, outline=stroke_color + (180,), width=max(1, ss - 1))

        # 缩回原尺寸，LANCZOS 高质量
        bar = bar.resize((bar_w, bar_h), Image.LANCZOS)
        canvas.alpha_composite(bar, (x1, y1))

        if text and font:
            text_w = self._text_size(d, text, font)[0]
            fill_w_orig = max(radius, int(bar_w * progress))
            tx = min(x1 + fill_w_orig + 8, x2 - text_w - 6)
            if tx < x1 + fill_w_orig + 8:
                tx = x1 + fill_w_orig // 2
                anchor = "mm"
                fill_color = (255, 255, 255, 245)
            else:
                anchor = "lm"
                fill_color = (60, 66, 72, 255)
            d.text((tx, y1 + bar_h // 2 + 1), text, font=font, fill=fill_color, anchor=anchor)

    def _draw_diamond_icon(self, draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int):
        w = int(size)
        h = int(size * 0.9)
        top_y = cy - h // 2
        mid_y = top_y + int(h * 0.36)
        bottom_y = cy + h // 2
        left = cx - w // 2
        right = cx + w // 2
        pts = [
            (cx - int(w * 0.28), top_y),
            (cx + int(w * 0.28), top_y),
            (right, mid_y),
            (cx, bottom_y),
            (left, mid_y),
        ]
        draw.polygon(pts, fill=(66, 191, 238, 255), outline=(255, 255, 255, 245))
        draw.polygon([(left, mid_y), (cx - int(w * 0.28), top_y), (cx, mid_y)], fill=(255, 225, 102, 230))
        draw.polygon([(cx - int(w * 0.28), top_y), (cx + int(w * 0.28), top_y), (cx, mid_y)], fill=(255, 148, 178, 230))
        draw.polygon([(cx + int(w * 0.28), top_y), (right, mid_y), (cx, mid_y)], fill=(111, 213, 238, 230))
        draw.polygon([(left, mid_y), (cx, mid_y), (cx, bottom_y)], fill=(83, 213, 188, 230))
        draw.polygon([(right, mid_y), (cx, mid_y), (cx, bottom_y)], fill=(147, 111, 232, 230))
        draw.line(((left, mid_y), (right, mid_y)), fill=(255, 255, 255, 190), width=1)
        draw.line(((cx, mid_y), (cx, bottom_y)), fill=(255, 255, 255, 160), width=1)
        draw.line(((cx - int(w * 0.28), top_y), (cx, mid_y), (cx + int(w * 0.28), top_y)), fill=(255, 255, 255, 145), width=1)

    def _draw_gradient_panel(self, canvas: Image.Image, box, radius: int, alpha: int = 77, outline=None, width: int = 1):
        x1, y1, x2, y2 = [int(v) for v in box]
        panel = self._make_card_background((max(1, x2 - x1), max(1, y2 - y1))).convert("RGBA")
        panel.putalpha(max(0, min(255, int(alpha))))
        mask = self._rounded_mask((x2 - x1, y2 - y1), radius)
        canvas.paste(panel, (x1, y1), mask)
        if outline:
            ImageDraw.Draw(canvas, "RGBA").rounded_rectangle((x1, y1, x2, y2), radius=radius, outline=outline, width=width)

    def _contain_in_frame(self, img: Image.Image, w: int, h: int, fill=(255, 255, 255)):
        img = ImageOps.contain(img, (w, h), method=Image.LANCZOS)
        frame = Image.new("RGB", (w, h), fill)
        x = (w - img.width) // 2
        y = (h - img.height) // 2
        frame.paste(img, (x, y))
        return frame

    def draw_showcase_card(
        self,
        title: str,
        cat: Optional[Dict] = None,
        subtitle: str = "",
        lines=None,
        footer: str = "",
        tag: str = "showcase",
    ) -> Path:
        lines = [str(x) for x in (lines or []) if str(x).strip()]
        t = self.theme
        width = 780
        padding = 20
        header_h = 105
        image_w, image_h = 700, 820
        title_font = self._title_font(48)
        sub_font = self._font(25)
        name_font = self._title_font(39)
        text_font = self._font(30)
        small_font = self._font(26)
        line_h = 45
        footer_line_h = 36
        paragraph_gap = 12
        image_text_gap = 52
        card_w = width - padding * 2
        inner_x = padding + 20
        inner_w = card_w - 40

        measure = ImageDraw.Draw(Image.new("RGB", (width, 1), "white"))
        text_lines = []
        if cat:
            text_lines.append(f"{cat.get('name', '猫娘')}｜{stage_name(cat.get('stage', 0))}｜{status_tag(cat)}")
        text_lines.extend(lines)
        wrapped = []
        for idx, line in enumerate(text_lines):
            font = name_font if idx == 0 and cat else text_font
            wrapped.extend((idx, part) for part in self._wrap_by_width(measure, line, font, inner_w, 20))
        footer_lines = self._wrap_by_width(measure, footer, small_font, inner_w, 2) if footer else []
        paragraph_count = len({idx for idx, _ in wrapped})
        text_h = len(wrapped) * line_h + max(0, paragraph_count - 1) * paragraph_gap + 44
        footer_h = len(footer_lines) * footer_line_h + 60 if footer_lines else 0
        content_h = 32 + image_h + image_text_gap + text_h + footer_h
        height = padding + header_h + content_h + padding

        canvas = self._make_card_background((width, height)).convert("RGBA")
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.text((width // 2, padding + 6), title, font=title_font, fill=t["primary"], anchor="ma")
        if subtitle:
            draw.text((width // 2, padding + 64), subtitle, font=sub_font, fill=t["text"], anchor="ma")

        card_x, card_y = padding, padding + header_h
        self._draw_gradient_panel(
            canvas,
            (card_x, card_y, card_x + card_w, card_y + content_h),
            radius=t["radius_card"],
            alpha=86,
            outline=t["card_outline"],
            width=2,
        )

        img_x = inner_x
        img_y = card_y + 32
        img_source = self.image_path(cat) if cat else None
        if img_source and Path(img_source).exists():
            try:
                img = Image.open(img_source).convert("RGB")
                img = self._contain_in_frame(img, image_w, image_h, fill=(255, 250, 246))
                self._paste_round(canvas, img, (img_x, img_y, image_w, image_h), 20)
            except Exception:
                self._draw_no_image(draw, img_x, img_y, image_w, image_h)
        else:
            self._draw_no_image(draw, img_x, img_y, image_w, image_h)

        y = img_y + image_h + image_text_gap
        colors = [t["primary_alpha"], (*t["pink"], 240), (*t["accent"], 240), (*t["green"], 240)]
        prev_idx = None
        for idx, text in wrapped:
            if prev_idx is not None and idx != prev_idx:
                y += paragraph_gap
            font = name_font if idx == 0 and cat else text_font
            fill = t["text_dark"] if idx == 0 and cat else colors[idx % len(colors)]
            draw.text((inner_x, y), text, font=font, fill=fill)
            y += line_h
            prev_idx = idx

        if footer_lines:
            fy = card_y + content_h - 42 - (len(footer_lines) - 1) * footer_line_h
            for footer_line in footer_lines:
                draw.text((card_x + card_w // 2, fy), footer_line, font=small_font, fill=t["muted"], anchor="ma")
                fy += footer_line_h

        safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", str(tag or "showcase"))[:40] or "showcase"
        out = self.cache_dir / f"showcase_card_{safe_tag}_{int(time.time() * 1000)}.png"
        save_scaled_image(canvas.convert("RGB"), out, "PNG", scale=self._output_image_scale())
        return out

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
        raw_metrics = [(str(k), str(v)) for k, v in (metrics or [])]
        balance_metric = None
        metrics = []
        for item in raw_metrics:
            if item[0] == "余额" and balance_metric is None:
                balance_metric = item
            else:
                metrics.append(item)
        width = 780
        padding = 20
        header_h = 105
        panel_gap = 24
        image_w, image_h = 455, 635
        line_h = 34
        t = self.theme
        title_font = self._title_font(48)
        sub_font = self._font(25)
        name_font = self._title_font(35)
        text_font = self._font(25)
        small_font = self._font(23)
        metric_font = self._font(25)

        card_w = width - padding * 2
        img_x = padding + 12
        text_x = img_x + image_w + panel_gap
        text_w = width - padding - 12 - text_x
        measure = ImageDraw.Draw(Image.new("RGB", (width, 1), "white"))
        left_info_lines = []
        if cat:
            profile = f"{cat.get('personality', '温柔')}｜{status_tag(cat)}｜羁绊 {bond_score(cat)}"
            left_info_lines = [profile]
            left_info_lines.extend(lines)
        metric_h = 70
        metric_gap = 13
        metrics_h = len(metrics) * metric_h + max(0, len(metrics) - 1) * metric_gap
        balance_h = 96 if balance_metric else 0
        balance_gap = 22 if balance_metric and metrics else 0
        footer_lines = self._wrap_by_width(measure, footer, small_font, card_w - 64, 2) if footer else []
        footer_h = len(footer_lines) * 30 + 64 if footer_lines else 0
        left_text_w = card_w - 40
        left_wrapped = []
        for idx, line in enumerate(left_info_lines):
            font = small_font if idx == 0 else text_font
            left_wrapped.extend((idx, text) for text in self._wrap_by_width(measure, line, font, left_text_w, 50))
        name_lines = self._wrap_by_width(measure, f"{cat.get('name', '猫娘')}｜{stage_name(cat.get('stage', 0))}", name_font, left_text_w, 10) if cat else []
        left_text_h = len(name_lines) * 42 + (8 if name_lines else 0) + len(left_wrapped) * line_h + 30
        left_h = 32 + image_h + 40 + left_text_h + footer_h
        right_h = 32 + metrics_h + balance_gap + balance_h + footer_h + 24
        content_h = max(left_h, right_h)
        height = padding + header_h + content_h + padding

        canvas = self._make_card_background((width, height)).convert("RGBA")
        draw = ImageDraw.Draw(canvas, "RGBA")

        draw.text((width // 2, padding + 6), title, font=title_font, fill=t["primary"], anchor="ma")
        if subtitle:
            draw.text((width // 2, padding + 64), subtitle, font=sub_font, fill=t["text"], anchor="ma")

        card_x, card_y = padding, padding + header_h
        card_h = content_h
        self._draw_gradient_panel(
            canvas,
            (card_x, card_y, card_x + card_w, card_y + card_h),
            radius=t["radius_card"],
            alpha=86,
            outline=t["card_outline"],
            width=2,
        )

        img_y = card_y + 32
        img_source = image_path or (self.image_path(cat) if cat else None)
        if img_source and Path(img_source).exists():
            try:
                img = Image.open(img_source).convert("RGB")
                img = self._contain_in_frame(img, image_w, image_h, fill=(255, 255, 255))
                self._paste_round(canvas, img, (img_x, img_y, image_w, image_h), 18)
            except Exception:
                self._draw_no_image(draw, img_x, img_y, image_w, image_h)
        else:
            self._draw_no_image(draw, img_x, img_y, image_w, image_h)

        left_y = img_y + image_h + 40
        if cat:
            for name_line in name_lines:
                draw.text((img_x + 8, left_y), name_line, font=name_font, fill=t["text_dark"])
                left_y += 42
            left_y += 8
            left_colors = [
                t["primary_alpha"],
                (*t["pink"], 240),
                (*t["accent"], 240),
                (*t["green"], 240),
                (*t["purple"], 240),
            ]
            for idx, text in left_wrapped:
                font = small_font if idx == 0 else text_font
                draw.text((img_x + 8, left_y), text, font=font, fill=left_colors[idx % len(left_colors)])
                left_y += line_h

        if metrics:
            y = card_y + 32
            for idx, (label, value) in enumerate(metrics):
                colors = self._metric_palette(label, idx)
                if label == "体重":
                    progress = None
                else:
                    progress = self._metric_progress(label, value, cat)
                value_font = self._fit_font(draw, value, 28, 112, min_size=21)
                label_w_avail = 112
                draw.text((text_x + 4, y), self._truncate_text(draw, label, metric_font, label_w_avail), font=metric_font, fill=t["text"])
                if progress is None:
                    # 无固定上限的数据（余额/体重/宝石/亲密等级/档位等）：纯数值徽章，垂直居中，不画进度条
                    draw.text((text_x + text_w - 4, y + 18), self._truncate_text(draw, value, value_font, 112), font=value_font, fill=colors[1], anchor="ra")
                else:
                    draw.text((text_x + text_w - 4, y + 1), self._truncate_text(draw, value, value_font, 112), font=value_font, fill=colors[1], anchor="ra")
                    # 细条：高度从 24 降到 12，更精致；与数值之间留 6px 呼吸
                    bar_y = y + 36
                    bar_margin = 4
                    self._draw_gradient_bar(
                        canvas,
                        (text_x + bar_margin, bar_y, text_x + text_w - bar_margin, bar_y + 12),
                        colors,
                        progress,
                    )
                y += metric_h + metric_gap
        else:
            y = card_y + 32

        if balance_metric:
            y += balance_gap
            label, value = balance_metric
            box_x1 = text_x
            box_y1 = y
            box_x2 = text_x + text_w
            box_y2 = y + balance_h
            self._draw_round_panel(
                canvas,
                (box_x1, box_y1, box_x2, box_y2),
                radius=t["radius_panel"],
                fill=t["panel_fill"],
                outline=t["panel_outline"],
                width=2,
                shadow=True,
            )
            value_font = self._fit_font(draw, value, 29, text_w - 58, min_size=19)
            draw.text((box_x1 + 16, box_y1 + 24), label, font=self._font(27), fill=t["primary"], anchor="lm")
            icon_x = box_x1 + 30
            value_y = box_y1 + 66
            self._draw_diamond_icon(draw, icon_x, value_y, 26)
            draw.text((box_x2 - 12, value_y + 1), value, font=value_font, fill=t["primary"], anchor="rm")
            y = box_y2

        if footer:
            footer_lines = self._wrap_by_width(draw, footer, small_font, card_w - 64, 2)
            fy = card_y + card_h - 34 - (len(footer_lines) - 1) * 30
            for footer_line in footer_lines:
                draw.text((card_x + card_w // 2, fy), footer_line, font=small_font, fill=t["muted"], anchor="ma")
                fy += 30

        safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", str(tag or "care"))[:40] or "care"
        out = self.cache_dir / f"cat_card_{safe_tag}_{int(time.time() * 1000)}.png"
        save_scaled_image(canvas.convert("RGB"), out, "PNG", scale=self._output_image_scale())
        return out

    def draw_info_card(
        self,
        title: str,
        subtitle: str = "",
        lines=None,
        metrics=None,
        footer: str = "",
        tag: str = "info",
    ) -> Path:
        lines = [str(x) for x in (lines or []) if str(x).strip()]
        metrics = [(str(k), str(v)) for k, v in (metrics or [])]
        t = self.theme
        width = 980
        padding = 42
        header_h = 112
        card_w = width - padding * 2
        inner_x = padding + 30
        inner_w = card_w - 60
        title_font = self._title_font(48)
        sub_font = self._font(26)
        text_font = self._font(25)
        small_font = self._font(22)
        metric_font = self._font(23)
        metric_value_font = self._font(29)
        line_h = 36
        cell_h = 58
        cell_gap = 14

        measure = ImageDraw.Draw(Image.new("RGB", (width, 1), "white"))
        wrapped_lines = []
        for line in lines:
            wrapped_lines.extend(self._wrap_by_width(measure, line, text_font, inner_w, 3))
        metrics_rows = math.ceil(len(metrics) / 2) if metrics else 0
        metrics_h = metrics_rows * cell_h + max(0, metrics_rows - 1) * cell_gap
        text_h = len(wrapped_lines) * line_h
        footer_lines = self._wrap_by_width(measure, footer, small_font, inner_w, 3) if footer else []
        footer_h = len(footer_lines) * 28 + 30 if footer_lines else 0
        gap_after_metrics = 24 if metrics and wrapped_lines else 0
        content_h = 32 + metrics_h + gap_after_metrics + text_h + footer_h + 32
        height = padding + header_h + content_h + padding

        canvas = self._make_card_background((width, height)).convert("RGBA")
        draw = ImageDraw.Draw(canvas, "RGBA")

        draw.text((width // 2, padding + 18), title, font=title_font, fill=t["primary"], anchor="ma")
        if subtitle:
            draw.text((width // 2, padding + 76), subtitle, font=sub_font, fill=t["muted"], anchor="ma")

        card_x, card_y = padding, padding + header_h
        # info 卡现在也用渐变底 + 浅白描边，和其它卡统一视觉语言
        self._draw_gradient_panel(
            canvas,
            (card_x, card_y, card_x + card_w, card_y + content_h),
            radius=t["radius_card"],
            alpha=96,
            outline=t["card_outline"],
            width=2,
        )

        y = card_y + 32
        if metrics:
            cell_w = (inner_w - 16) // 2
            for idx, (label, value) in enumerate(metrics):
                col = idx % 2
                row = idx // 2
                x = inner_x + col * (cell_w + 16)
                yy = y + row * (cell_h + cell_gap)
                mid_y = yy + cell_h // 2
                # 不再按 44% 硬切：label 左对齐，value 右对齐，给数值留更宽的呼吸空间
                label_w = min(int(cell_w * 0.46), max(70, self._text_size(draw, label, metric_font)[0] + 8))
                value_w = max(60, cell_w - label_w - 28)
                value_font = self._fit_font(draw, value, 29, value_w)
                self._draw_round_panel(
                    canvas,
                    (x, yy, x + cell_w, yy + cell_h),
                    radius=t["radius_cell"],
                    fill=t["panel_fill"],
                    outline=t["panel_outline"],
                    width=1,
                    shadow=False,
                )
                draw.text((x + 14, mid_y), self._truncate_text(draw, label, metric_font, label_w), font=metric_font, fill=t["muted"], anchor="lm")
                draw.text((x + cell_w - 14, mid_y), self._truncate_text(draw, value, value_font, value_w), font=value_font, fill=t["primary"], anchor="rm")
            y += metrics_h + gap_after_metrics

        for wrapped in wrapped_lines:
            draw.text((inner_x, y), wrapped, font=text_font, fill=t["text"])
            y += line_h

        if footer_lines:
            y += 16
            for footer_line in footer_lines:
                draw.text((inner_x, y), footer_line, font=small_font, fill=t["muted"])
                y += 28

        safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", str(tag or "info"))[:40] or "info"
        out = self.cache_dir / f"info_card_{safe_tag}_{int(time.time() * 1000)}.png"
        save_scaled_image(canvas.convert("RGB"), out, "PNG", scale=self._output_image_scale())
        return out

    def draw_section_card(
        self,
        title: str,
        subtitle: str = "",
        sections=None,
        metrics=None,
        footer: str = "",
        tag: str = "sections",
    ) -> Path:
        sections = sections or []
        metrics = [(str(k), str(v)) for k, v in (metrics or [])]
        t = self.theme
        width = 980
        padding = 42
        header_h = 112
        card_w = width - padding * 2
        inner_x = padding + 30
        inner_w = card_w - 60
        col_gap = 18
        section_gap = 18
        section_w = (inner_w - col_gap) // 2

        title_font = self._title_font(48)
        sub_font = self._font(26)
        section_font = self._title_font(27)
        command_font = self._font(22)
        desc_font = self._font(21)
        small_font = self._font(22)
        metric_font = self._font(23)
        cell_h = 58
        cell_gap = 14
        row_h = 58

        def normalize_rows(rows):
            result = []
            for row in rows or []:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    result.append((str(row[0]), str(row[1])))
                else:
                    text = str(row)
                    if "｜" in text:
                        left, right = text.split("｜", 1)
                    elif ":" in text:
                        left, right = text.split(":", 1)
                    elif "：" in text:
                        left, right = text.split("：", 1)
                    else:
                        left, right = text, ""
                    result.append((left.strip(), right.strip()))
            return result

        normalized_sections = []
        for section in sections:
            if isinstance(section, dict):
                name = str(section.get("title", ""))
                rows = normalize_rows(section.get("rows", []))
            elif isinstance(section, (list, tuple)) and len(section) >= 2:
                name = str(section[0])
                rows = normalize_rows(section[1])
            else:
                continue
            if name and rows:
                normalized_sections.append((name, rows))

        metrics_rows = math.ceil(len(metrics) / 2) if metrics else 0
        metrics_h = metrics_rows * cell_h + max(0, metrics_rows - 1) * cell_gap
        top_gap = 24 if metrics else 0
        section_heights = [44 + len(rows) * row_h + 18 for _, rows in normalized_sections]
        col_heights = [0, 0]
        placements = []
        for idx, height in enumerate(section_heights):
            col = 0 if col_heights[0] <= col_heights[1] else 1
            placements.append((col, col_heights[col], height))
            col_heights[col] += height + section_gap
        sections_h = max(col_heights) - section_gap if placements else 0

        measure = ImageDraw.Draw(Image.new("RGB", (width, 1), "white"))
        footer_lines = self._wrap_by_width(measure, footer, small_font, inner_w, 2) if footer else []
        footer_h = len(footer_lines) * 28 + 34 if footer_lines else 0
        content_h = 32 + metrics_h + top_gap + sections_h + footer_h + 32
        height = padding + header_h + content_h + padding

        canvas = self._make_card_background((width, height)).convert("RGBA")
        draw = ImageDraw.Draw(canvas, "RGBA")
        soft = t["panel_fill"]
        line = t["panel_outline"]

        draw.text((width // 2, padding + 18), title, font=title_font, fill=t["primary"], anchor="ma")
        if subtitle:
            draw.text((width // 2, padding + 76), subtitle, font=sub_font, fill=t["muted"], anchor="ma")

        card_x, card_y = padding, padding + header_h
        self._draw_gradient_panel(
            canvas,
            (card_x, card_y, card_x + card_w, card_y + content_h),
            radius=t["radius_card"],
            alpha=88,
            outline=t["card_outline"],
            width=2,
        )
        y = card_y + 32

        if metrics:
            metric_cell_w = (inner_w - 16) // 2
            for idx, (label, value) in enumerate(metrics):
                col = idx % 2
                row = idx // 2
                x = inner_x + col * (metric_cell_w + 16)
                yy = y + row * (cell_h + cell_gap)
                mid_y = yy + cell_h // 2
                label_w = min(int(metric_cell_w * 0.46), max(70, self._text_size(draw, label, metric_font)[0] + 8))
                value_w = max(60, metric_cell_w - label_w - 28)
                value_font = self._fit_font(draw, value, 29, value_w)
                self._draw_round_panel(
                    canvas,
                    (x, yy, x + metric_cell_w, yy + cell_h),
                    radius=t["radius_cell"],
                    fill=soft,
                    outline=line,
                    width=1,
                    shadow=False,
                )
                draw.text((x + 14, mid_y), self._truncate_text(draw, label, metric_font, label_w), font=metric_font, fill=t["muted"], anchor="lm")
                draw.text((x + metric_cell_w - 14, mid_y), self._truncate_text(draw, value, value_font, value_w), font=value_font, fill=t["primary"], anchor="rm")
            y += metrics_h + top_gap

        for idx, (section_title, rows) in enumerate(normalized_sections):
            col, rel_y, section_h = placements[idx]
            x = inner_x + col * (section_w + col_gap)
            yy = y + rel_y
            self._draw_gradient_panel(
                canvas,
                (x, yy, x + section_w, yy + section_h),
                radius=t["radius_panel"],
                alpha=98,
                outline=line,
                width=2,
            )
            draw.text((x + 16, yy + 24), section_title, font=section_font, fill=t["primary"], anchor="lm")
            draw.line((x + 14, yy + 44, x + section_w - 14, yy + 44), fill=t["panel_outline"], width=1)
            row_y = yy + 54
            text_w = section_w - 32
            for command, desc in rows:
                fitted_command_font = self._fit_font(draw, command, 22, text_w, min_size=18)
                draw.text((x + 16, row_y + 17), self._truncate_text(draw, command, fitted_command_font, text_w), font=fitted_command_font, fill=t["text"], anchor="lm")
                draw.text((x + 16, row_y + 42), self._truncate_text(draw, desc, desc_font, text_w), font=desc_font, fill=t["muted"], anchor="lm")
                row_y += row_h

        if footer_lines:
            fy = card_y + content_h - 34 - (len(footer_lines) - 1) * 28
            for footer_line in footer_lines:
                draw.text((card_x + card_w // 2, fy), footer_line, font=small_font, fill=t["muted"], anchor="ma")
                fy += 28

        safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", str(tag or "sections"))[:40] or "sections"
        out = self.cache_dir / f"section_card_{safe_tag}_{int(time.time() * 1000)}.png"
        save_scaled_image(canvas.convert("RGB"), out, "PNG", scale=self._output_image_scale())
        return out

    def draw_table_card(
        self,
        title: str,
        subtitle: str = "",
        headers=None,
        rows=None,
        col_widths=None,
        metrics=None,
        footer: str = "",
        tag: str = "table",
    ) -> Path:
        headers = [str(x) for x in (headers or [])]
        rows = [[str(cell) for cell in row] for row in (rows or [])]
        metrics = [(str(k), str(v)) for k, v in (metrics or [])]
        t = self.theme
        width = 980
        padding = 42
        header_h = 112
        card_w = width - padding * 2
        inner_x = padding + 30
        inner_w = card_w - 60
        title_font = self._title_font(48)
        sub_font = self._font(26)
        table_font = self._font(21)
        header_font = self._font(22)
        small_font = self._font(22)
        metric_font = self._font(23)
        cell_h = 58
        cell_gap = 14
        min_row_h = 42
        table_line_h = 25
        header_row_h = 44

        if not headers and rows:
            headers = ["" for _ in rows[0]]
        col_count = max(len(headers), max((len(row) for row in rows), default=0), 1)
        if not col_widths or len(col_widths) != col_count:
            col_widths = [1 for _ in range(col_count)]
        total_weight = max(1, sum(float(x) for x in col_widths))
        widths = [int(inner_w * float(weight) / total_weight) for weight in col_widths]
        widths[-1] += inner_w - sum(widths)

        metrics_rows = math.ceil(len(metrics) / 2) if metrics else 0
        metrics_h = metrics_rows * cell_h + max(0, metrics_rows - 1) * cell_gap
        top_gap = 24 if metrics else 0
        measure = ImageDraw.Draw(Image.new("RGB", (width, 1), "white"))

        def wrap_cell(text: str, max_width: int) -> list[str]:
            max_width = max(1, int(max_width))
            result = []
            for paragraph in str(text or "").splitlines() or [""]:
                current = ""
                for ch in paragraph:
                    candidate = current + ch
                    if current and self._text_size(measure, candidate, table_font)[0] > max_width:
                        result.append(current)
                        current = ch
                    else:
                        current = candidate
                result.append(current)
            return result or [""]

        wrapped_rows = []
        row_heights = []
        for row in rows:
            wrapped = []
            max_lines = 1
            for idx in range(col_count):
                cell = row[idx] if idx < len(row) else ""
                cell_lines = wrap_cell(cell, widths[idx] - 20)
                wrapped.append(cell_lines)
                max_lines = max(max_lines, len(cell_lines))
            wrapped_rows.append(wrapped)
            row_heights.append(max(min_row_h, max_lines * table_line_h + 16))

        table_h = (header_row_h if headers else 0) + sum(row_heights)
        footer_lines = self._wrap_by_width(measure, footer, small_font, inner_w, 3) if footer else []
        footer_h = len(footer_lines) * 28 + 34 if footer_lines else 0
        content_h = 32 + metrics_h + top_gap + table_h + footer_h + 32
        height = padding + header_h + content_h + padding

        canvas = self._make_card_background((width, height)).convert("RGBA")
        draw = ImageDraw.Draw(canvas, "RGBA")
        soft = t["panel_fill"]
        line = t["panel_outline"]
        row_fill_a = t["row_a"]
        row_fill_b = t["row_b"]

        draw.text((width // 2, padding + 18), title, font=title_font, fill=t["primary"], anchor="ma")
        if subtitle:
            draw.text((width // 2, padding + 76), subtitle, font=sub_font, fill=t["muted"], anchor="ma")

        card_x, card_y = padding, padding + header_h
        self._draw_gradient_panel(
            canvas,
            (card_x, card_y, card_x + card_w, card_y + content_h),
            radius=t["radius_card"],
            alpha=88,
            outline=t["card_outline"],
            width=2,
        )
        y = card_y + 32

        if metrics:
            metric_cell_w = (inner_w - 16) // 2
            for idx, (label, value) in enumerate(metrics):
                col = idx % 2
                row = idx // 2
                x = inner_x + col * (metric_cell_w + 16)
                yy = y + row * (cell_h + cell_gap)
                mid_y = yy + cell_h // 2
                label_w = min(int(metric_cell_w * 0.46), max(70, self._text_size(draw, label, metric_font)[0] + 8))
                value_w = max(60, metric_cell_w - label_w - 28)
                value_font = self._fit_font(draw, value, 29, value_w)
                self._draw_round_panel(
                    canvas,
                    (x, yy, x + metric_cell_w, yy + cell_h),
                    radius=t["radius_cell"],
                    fill=soft,
                    outline=line,
                    width=1,
                    shadow=False,
                )
                draw.text((x + 14, mid_y), self._truncate_text(draw, label, metric_font, label_w), font=metric_font, fill=t["muted"], anchor="lm")
                draw.text((x + metric_cell_w - 14, mid_y), self._truncate_text(draw, value, value_font, value_w), font=value_font, fill=t["primary"], anchor="rm")
            y += metrics_h + top_gap

        x = inner_x
        if headers:
            draw.rounded_rectangle((inner_x, y, inner_x + inner_w, y + header_row_h), radius=t["radius_cell"], fill=(255, 246, 234, 195), outline=line, width=1)
            cx = inner_x
            for idx, header in enumerate(headers[:col_count]):
                draw.text((cx + 10, y + header_row_h // 2), self._truncate_text(draw, header, header_font, widths[idx] - 20), font=header_font, fill=t["primary"], anchor="lm")
                cx += widths[idx]
            y += header_row_h

        for row_idx, row in enumerate(rows):
            current_row_h = row_heights[row_idx]
            fill = row_fill_a if row_idx % 2 == 0 else row_fill_b
            draw.rounded_rectangle((inner_x, y + 2, inner_x + inner_w, y + current_row_h - 2), radius=8, fill=fill)
            draw.line((inner_x + 8, y + current_row_h, inner_x + inner_w - 8, y + current_row_h), fill=t["panel_outline"], width=1)
            cx = inner_x
            for idx in range(col_count):
                color = t["text"] if idx == 0 else t["muted"]
                cell_lines = wrapped_rows[row_idx][idx]
                text_block_h = len(cell_lines) * table_line_h
                line_y = y + max(8, (current_row_h - text_block_h) // 2 + 2)
                for cell_line in cell_lines:
                    draw.text((cx + 10, line_y), cell_line, font=table_font, fill=color)
                    line_y += table_line_h
                cx += widths[idx]
            y += current_row_h

        if footer_lines:
            fy = card_y + content_h - 34 - (len(footer_lines) - 1) * 28
            for footer_line in footer_lines:
                draw.text((card_x + card_w // 2, fy), footer_line, font=small_font, fill=t["muted"], anchor="ma")
                fy += 28

        safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", str(tag or "table"))[:40] or "table"
        out = self.cache_dir / f"table_card_{safe_tag}_{int(time.time() * 1000)}.png"
        save_scaled_image(canvas.convert("RGB"), out, "PNG", scale=self._output_image_scale())
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
        wish_brief = self._daily_wish_status_brief(uid)
        wish_lines = [str(line).strip() for line in wish_brief.get("lines", []) if str(line).strip()]
        wish_metric = wish_brief.get("metric")
        if not (isinstance(wish_metric, (list, tuple)) and len(wish_metric) == 2):
            wish_metric = None
        satiety_zero_line, runaway_line = self._satiety_risk_lines(cat)
        detail_lines = [
            satiety_zero_line,
            runaway_line,
            next_line,
        ]
        detail_lines = [line for line in detail_lines if line]
        card_status_lines = [work_line.strip() if work_line else "", *wish_lines, *detail_lines]
        card_status_lines = [line for line in card_status_lines if line]
        detail_text = "\n".join([*wish_lines, *detail_lines])
        metrics = [
            ("亲密等级", self._intimacy_display(cat)),
            ("成长进度", self._growth_display(cat)),
        ]
        if wish_metric:
            metrics.append((str(wish_metric[0]), str(wish_metric[1])))
        metrics.extend([
            ("饱食度", self._fmt_int(cat.get("satiety", 0))),
            ("心情", self._fmt_int(cat.get("mood", 0))),
            ("健康", self._fmt_int(cat.get("health", 0))),
            ("精力", self._fmt_int(cat.get("energy", 0))),
            ("体重", self._weight_display(cat)),
            ("体型", cat.get("body_type", "匀称")),
            (self._coin_name(), f"{self.economy.get_balance(uid)} {self._coin_name()}"),
        ])
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
            lines=card_status_lines,
            metrics=metrics,
            footer=f"状态：{status_tag(cat)}",
            tag=f"status_{uid}",
        )
        return True, msg, card

    def _food_options(self):
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
            foods = [("草莓奶油蛋糕", 32, 58, "吃")]
        return foods

    def _random_food(self):
        foods = self._food_options()
        name, low, high, verb = random.choice(foods)
        return name, random.randint(low, high), verb

    def _find_food(self, query: str):
        query = str(query or "").strip()
        if not query:
            return self._random_food(), []
        foods = self._food_options()
        normalized = query.lower()
        for name, low, high, verb in foods:
            if normalized == name.lower():
                return (name, random.randint(low, high), verb), []
        matches = [(name, low, high, verb) for name, low, high, verb in foods if normalized in name.lower()]
        if len(matches) == 1:
            name, low, high, verb = matches[0]
            return (name, random.randint(low, high), verb), []
        return None, matches

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
                "unlock_cost": max(0, int(item.get("unlock_cost", 0))),
                "min_stage": max(0, int(item.get("min_stage", 0))),
            })
        if not jobs:
            jobs = [{
                "id": "cat_cafe",
                "name": "猫咖服务员",
                "low": 80,
                "high": 150,
                "duration": 45 * 60,
                "energy": 18,
                "satiety": 7,
                "mood": 2,
                "growth": (4, 7),
                "intimacy": (1, 2),
                "mood_reward": 1,
                "unlock_cost": 0,
                "min_stage": 0,
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
                f"{'，解锁 ' + str(job.get('unlock_cost', 0)) if int(job.get('unlock_cost', 0) or 0) else ''}"
            )
        if len(jobs) > limit:
            lines.append(f"还有 {len(jobs) - limit} 个地点，可在插件拓展页查看。")
        return lines

    def _shop_rules(self) -> Dict:
        shop = self._rules("shop")
        return shop if isinstance(shop, dict) else {}

    def _shop_items(self) -> list[Dict]:
        items = []
        for item in self._shop_rules().get("items", []):
            if isinstance(item, dict) and item.get("enabled", True):
                items.append(item)
        return items

    def _care_services(self) -> list[Dict]:
        services = []
        for service in self._shop_rules().get("care_services", []):
            if isinstance(service, dict) and service.get("enabled", True):
                services.append(service)
        return services

    def _find_named(self, rows: list[Dict], query: str) -> Tuple[Optional[Dict], list[Dict]]:
        query = str(query or "").strip()
        if not query:
            return None, []
        normalized = query.lower()
        for row in rows:
            names = {str(row.get("name", "")).lower(), str(row.get("id", "")).lower()}
            if normalized in names:
                return row, []
        matches = [
            row for row in rows
            if normalized in str(row.get("name", "")).lower() or normalized in str(row.get("id", "")).lower()
        ]
        if len(matches) == 1:
            return matches[0], []
        return None, matches

    def _shop_summary_lines(self, category: str = "", limit: int = 12) -> list[str]:
        category = str(category or "").strip()
        rows = self._shop_items()
        if category:
            rows = [row for row in rows if str(row.get("category", "")) == category]
        lines = []
        for item in rows[:limit]:
            lines.append(f"{item.get('name', '道具')}：{int(item.get('price', 0))} {self._coin_name()}｜{item.get('description', '')}")
        if len(rows) > limit:
            lines.append(f"还有 {len(rows) - limit} 个道具，可在插件拓展页查看。")
        return lines or ["当前没有可购买的道具。"]

    def _gift_daily_multiplier(self, cat: Dict) -> Tuple[float, str, int]:
        shop = self._shop_rules()
        limit = max(0, int(shop.get("gift_daily_limit", 5)))
        soft_extra = max(0, int(shop.get("gift_soft_limit_extra", 5)))
        stats = cat.setdefault("gift_stats", {})
        today_count = int(stats.get(today_str(), 0) or 0)
        if limit <= 0 or today_count < limit:
            return 1.0, "正常", today_count
        if today_count < limit + soft_extra:
            return max(0.0, float(shop.get("gift_soft_limit_multiplier", 0.5))), "轻度递减", today_count
        return max(0.0, float(shop.get("gift_minimal_limit_multiplier", 0.2))), "极低收益", today_count

    def _week_key(self) -> str:
        year, week, _ = datetime.now().isocalendar()
        return f"{year}-W{week:02d}"

    def _item_display_name(self, item_id: str) -> str:
        builtins = {
            RENAME_CARD_ID: "改名卡",
            APPEARANCE_CARD_ID: "形象更改卡",
        }
        if str(item_id) in builtins:
            return builtins[str(item_id)]
        for item in self._shop_items():
            if str(item.get("id")) == str(item_id):
                return str(item.get("name") or item_id)
        return str(item_id)

    def _item_quantity_map(self, root: Dict, uid: str) -> Dict:
        items = root.setdefault("items", {})
        bag = items.setdefault(uid, {})
        if not isinstance(bag, dict):
            bag = {}
            items[uid] = bag
        return bag

    def _item_count(self, value) -> int:
        try:
            return max(0, int(value or 0))
        except Exception:
            return 0

    def _grant_starter_cards(self, root: Dict, uid: str) -> bool:
        grants = root.setdefault("item_grants", {})
        user_grants = grants.setdefault(uid, {})
        if not isinstance(user_grants, dict):
            user_grants = {}
            grants[uid] = user_grants
        if user_grants.get(STARTER_CARD_GRANT_ID):
            return False
        bag = self._item_quantity_map(root, uid)
        bag[RENAME_CARD_ID] = self._item_count(bag.get(RENAME_CARD_ID)) + 1
        bag[APPEARANCE_CARD_ID] = self._item_count(bag.get(APPEARANCE_CARD_ID)) + 1
        user_grants[STARTER_CARD_GRANT_ID] = now_ts()
        return True

    def _starter_card_notice(self, granted: bool) -> str:
        return "\n\n初始赠送：改名卡 x1、形象更改卡 x1 已放入背包。" if granted else ""

    def _consume_bag_item(self, bag: Dict, item_id: str) -> bool:
        count = self._item_count(bag.get(item_id))
        if count <= 0:
            return False
        if count == 1:
            bag.pop(item_id, None)
        else:
            bag[item_id] = count - 1
        return True

    def feed(self, uid: str, food_query: str = "") -> Tuple[bool, str, Optional[Path], Dict[str, Any]]:
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, self.missing_cat_message(uid), None, {}

        today = today_str()
        care_rules = self._care_rules()
        feed_rules = self._feed_rules()
        coin_name = self._coin_name()
        selected_food, food_matches = self._find_food(food_query)
        if not selected_food:
            if food_matches:
                names = "、".join(name for name, *_ in food_matches[:8])
                msg = f"找到多个相近的食物：{names}\n请发送更完整的食物名。"
            else:
                names = "、".join(name for name, *_ in self._food_options()[:12])
                msg = f"没有找到「{str(food_query or '').strip()}」这个食物。\n可选食物：{names}"
            return False, msg, None, {}
        food, cost, verb = selected_food

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
            wallet[uid] = balance - cost

            # Step 5：喂食锚点的性格专属随机事件
            event_text = ""
            if self.events is not None:
                uid_key = str(current_cat.get("user", uid))
                today_count_ev = self.events.today_anchor_count(self.store, uid_key, today, "feed")
                triggered_ids = self.events.today_triggered_ids(self.store, uid_key, today)
                try:
                    event_roll = self.events.roll("feed", current_cat, today_count_ev, triggered_ids)
                except Exception:
                    event_roll = None
                if event_roll is not None:
                    ev, deltas = event_roll
                    coin_delta = int(deltas.get("coin", 0) or 0)
                    if coin_delta:
                        wallet[uid_key] = int(wallet.get(uid_key, 0)) + coin_delta
                    current_cat["mood"] = round(clamp(float(current_cat.get("mood", 80)) + int(deltas.get("mood", 0) or 0), 0, 100), 4)
                    current_cat["energy"] = round(clamp(float(current_cat.get("energy", 80)) + int(deltas.get("energy", 0) or 0), 0, 100), 4)
                    current_cat["intimacy"] = int(current_cat.get("intimacy", 0)) + int(deltas.get("intimacy", 0) or 0)
                    current_cat["growth"] = int(current_cat.get("growth", 0)) + int(deltas.get("growth", 0) or 0)
                    event_text = str(ev.get("text", "") or "")
                    self.events.record_event(root, uid_key, today, "feed", str(ev.get("id", "") or ""), event_text)

            current_cat, stage_msg = self._advance_stage(current_cat)
            root.setdefault("catgirls", {})[uid] = current_cat
            return True, "", current_cat, satiety_add, mood_add, health_add, energy_add, growth_add, wallet[uid], stage_msg, intimacy_add, weight_gain, health_multiplier, health_label, event_text

        result = self.store.update(op)
        ok = result[0]
        if not ok:
            _, msg, current_cat, *_ = result
            return False, msg, None, {"food": food, "verb": verb}

        _, _, cat, satiety_add, mood_add, health_add, energy_add, growth_add, balance, stage_msg, intimacy_add, weight_gain, health_multiplier, health_label, event_text = result
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
        if event_text:
            msg += f"\n✦ 今日奇遇：{event_text}"
        card = self.draw_care_card(
            "喂猫结果",
            cat,
            subtitle=f"{verb}{food}｜花费 {cost} {coin_name}",
            lines=[
                "她幸福地眯起眼睛，看起来超级满足～",
                f"喂食效率：{self._fmt_percent(float(health_multiplier) * 100)}（健康{health_label}）" if float(health_multiplier or 1) < 0.999 else "",
                f"今日奇遇：{event_text}" if event_text else "",
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
        return True, msg, card, {"food": food, "verb": verb}

    def work(self, uid: str, job_query: str = ""):
        self._finalize_expired_adoption(uid)
        now = now_ts()
        coin_name = self._coin_name()
        care_rules = self._care_rules()
        work_rules = self._rules("work")
        jobs = self._work_jobs(work_rules)
        job_query = str(job_query or "").strip()
        unlock_prefix = "解锁"
        if job_query == unlock_prefix:
            return self.work_unlock(uid)
        if job_query.startswith(f"{unlock_prefix} "):
            return self.work_unlock(uid, job_query[len(unlock_prefix):].strip())
        if job_query in ("列表", "地点", "地点列表", "打工地点"):
            hint = "发送「猫娘打工 地点名」指定地点；发送「猫娘打工 解锁 地点名」解锁地点。"
            lines = self._work_job_summary_lines(jobs, len(jobs))
            table_rows = []
            for job in jobs:
                unlock_cost = int(job.get("unlock_cost", 0) or 0)
                table_rows.append([
                    job.get("name", "打工地点"),
                    self._format_duration(job.get("duration", 0)),
                    f"{job.get('energy', 0)}/{job.get('satiety', 0)}/{job.get('mood', 0)}",
                    f"{job.get('low', 0)}-{job.get('high', 0)}",
                    stage_name(job.get("min_stage", 0)),
                    "开放" if unlock_cost <= 0 else f"{unlock_cost} {coin_name}",
                ])
            msg = "可选猫娘打工地点：\n" + "\n".join(lines) + f"\n\n{hint}"
            card = self.draw_table_card(
                "打工地点",
                subtitle="耗时、消耗与基础报酬",
                headers=["地点", "耗时", "精/饱/心", "基础报酬", "阶段", "解锁"],
                rows=table_rows,
                col_widths=[2.4, 1.15, 1.2, 1.35, 1.05, 1.45],
                metrics=[
                    ("地点数", str(len(jobs))),
                    ("用法", "猫娘打工 地点名"),
                ],
                footer=hint,
                tag=f"work_jobs_{uid}",
            )
            return True, msg, card
        selected_job, job_matches = self._find_work_job(jobs, job_query)

        def finish_work(cat: Dict, pending: Dict, wallet: Dict, root: Dict, uid_key: str, today: str):
            job_name = pending.get("job", "打工")
            reward = int(pending.get("reward", 0))
            growth_add = int(pending.get("growth_add", 0))
            intimacy_add = int(pending.get("intimacy_add", 0))

            # Step 4：打工风险结算（80/15/5 分布）
            work_mode = "normal"
            work_bonus_msg = ""
            if self.events is not None:
                try:
                    work_mode, wf_payload = self.events.roll_work_finish()
                except Exception:
                    work_mode, wf_payload = "normal", {"coin_multiplier": 1.0, "mood": 0, "intimacy": 0, "growth": 0, "text": ""}
                if work_mode != "normal":
                    multiplier = float(wf_payload.get("coin_multiplier", 1.0) or 1.0)
                    reward = max(1, int(reward * multiplier))
                    mood_delta = int(wf_payload.get("mood", 0) or 0)
                    intimacy_add = intimacy_add + int(wf_payload.get("intimacy", 0) or 0)
                    growth_add = growth_add + int(wf_payload.get("growth", 0) or 0)
                    wf_text = str(wf_payload.get("text", "") or "").strip()
                    if wf_text:
                        work_bonus_msg = f"\n✦ {wf_text}"

            wallet[uid_key] = int(wallet.get(uid_key, 0)) + reward
            cat["growth"] = int(cat.get("growth", 0)) + growth_add
            cat["intimacy"] = int(cat.get("intimacy", 0)) + intimacy_add
            base_mood_reward = float(pending.get("mood_reward", 1))
            if work_mode == "accident":
                base_mood_reward = base_mood_reward + int(wf_payload.get("mood", -10) or 0)
            cat["mood"] = round(clamp(float(cat.get("mood", 80)) + base_mood_reward, 0, 100), 4)
            stats = cat.setdefault("care_stats", {})
            stats["total_works"] = int(stats.get("total_works", 0)) + 1
            cat.pop("pending_work", None)

            # Step 2：猫娘打工完成锚点的随机事件（独立于风险结算，叠加额外 delta）
            event_text = ""
            event_deltas = {"coin": 0, "mood": 0, "energy": 0, "intimacy": 0, "growth": 0}
            if self.events is not None:
                today_count = self.events.today_anchor_count(self.store, uid_key, today, "cat_work_finish")
                triggered_ids = self.events.today_triggered_ids(self.store, uid_key, today)
                try:
                    event_roll = self.events.roll("cat_work_finish", cat, today_count, triggered_ids)
                except Exception:
                    event_roll = None
                if event_roll is not None:
                    ev, deltas = event_roll
                    event_deltas = {k: int(v or 0) for k, v in deltas.items()}
                    coin_delta = int(event_deltas.get("coin", 0) or 0)
                    if coin_delta:
                        wallet[uid_key] = int(wallet.get(uid_key, 0)) + coin_delta
                    cat["mood"] = round(clamp(float(cat.get("mood", 80)) + int(event_deltas.get("mood", 0) or 0), 0, 100), 4)
                    cat["energy"] = round(clamp(float(cat.get("energy", 80)) + int(event_deltas.get("energy", 0) or 0), 0, 100), 4)
                    cat["intimacy"] = int(cat.get("intimacy", 0)) + int(event_deltas.get("intimacy", 0) or 0)
                    cat["growth"] = int(cat.get("growth", 0)) + int(event_deltas.get("growth", 0) or 0)
                    event_text = str(ev.get("text", "") or "")
                    self.events.record_event(root, uid_key, today, "cat_work_finish", str(ev.get("id", "") or ""), event_text)

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
            if work_bonus_msg:
                msg += work_bonus_msg
            if event_text:
                msg += f"\n✦ 今日奇遇：{event_text}"
            return msg, {
                "job_name": job_name,
                "reward": reward,
                "growth_add": growth_add,
                "intimacy_add": intimacy_add,
                "balance": wallet[uid_key],
                "stage_msg": stage_msg,
                "work_mode": work_mode,
                "event_text": event_text,
                "event_deltas": event_deltas,
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
                msg, detail = finish_work(cat, pending, wallet, root, str(cat.get("user", uid)), today_str())
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

            unlocked = set((cat.get("unlocks") or {}).get("work_jobs", []))

            def is_job_unlocked(job):
                return int(job.get("unlock_cost", 0) or 0) <= 0 or str(job.get("id", "")) in unlocked

            def stage_ok(job):
                return int(cat.get("stage", 0) or 0) >= int(job.get("min_stage", 0) or 0)

            if selected_job and not is_job_unlocked(selected_job):
                cats[uid] = cat
                return False, f"「{selected_job['name']}」还没有解锁，需要 {int(selected_job.get('unlock_cost', 0))} {coin_name}。\n发送「猫娘打工 解锁 {selected_job['name']}」解锁。", cat, "blocked", {}
            if selected_job and not stage_ok(selected_job):
                cats[uid] = cat
                return False, f"「{selected_job['name']}」需要达到 {stage_name(selected_job.get('min_stage', 0))} 阶段后才能去。", cat, "blocked", {}

            available_jobs = [
                job for job in jobs
                if is_job_unlocked(job)
                and stage_ok(job)
                and float(cat.get("energy", 0)) >= job["energy"]
            ]
            if selected_job and selected_job not in available_jobs:
                cats[uid] = cat
                return False, f"「{cat['name']}」现在精力只有 {self._fmt_int(cat.get('energy', 0))}，去{selected_job['name']}需要 {selected_job['energy']}。先让她休息一下吧～", cat, "blocked", {
                    "min_energy": selected_job["energy"],
                }
            if not available_jobs:
                cats[uid] = cat
                candidate_jobs = [job for job in jobs if is_job_unlocked(job) and stage_ok(job)] or jobs
                min_energy = min(job["energy"] for job in candidate_jobs)
                return False, f"「{cat['name']}」现在精力只有 {self._fmt_int(cat.get('energy', 0))}，至少需要 {min_energy} 才能去当前可用工作。先让她休息或喂点好吃的吧～", cat, "blocked", {
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
            buffs = cat.setdefault("buffs", {})
            work_buff = max(1.0, float(buffs.get("next_work_reward_multiplier", 1) or 1))
            stage_multiplier = float(work_rules.get("reward_stage_base", 0.75)) + int(cat.get("stage", 0)) * float(work_rules.get("reward_stage_step", 0.10))
            stage_multiplier *= self._personality_multiplier(cat, "work_reward_multiplier")
            stage_multiplier *= energy_reward_multiplier
            stage_multiplier *= work_buff
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
            buffs.pop("next_work_reward_multiplier", None)
            cat["buffs"] = buffs
            cats[uid] = cat
            msg = (
                f"「{cat['name']}」出发去{job['name']}啦～\n"
                f"预计耗时：{self._format_duration(duration)}\n"
                f"精力档位：{energy_tier}（报酬 {self._fmt_percent(energy_reward_multiplier * 100)}）\n"
                f"{'道具加成：' + self._fmt_percent(work_buff * 100) + chr(10) if work_buff > 1 else ''}"
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
                "work_buff": work_buff,
            }

        ok, msg, cat, event_type, detail = self.store.update(op)
        if not cat:
            card = self.draw_info_card(
                "暂不能打工",
                lines=[msg],
                metrics=[("用法", "猫娘打工 [地点名]")],
                footer="发送「请赐我一只可爱猫娘吧」获取猫娘后再打工。",
                tag=f"work_err_{uid}",
            )
            return ok, msg, card, event_type
        if event_type == "finished":
            metrics = [
                ("获得", f"{detail.get('reward', 0)} {coin_name}"),
                ("成长值", self._fmt_delta(detail.get("growth_add", 0))),
                ("亲密度", self._fmt_delta(detail.get("intimacy_add", 0))),
                ("余额", f"{detail.get('balance', 0)} {coin_name}"),
            ]
            if str(detail.get("work_mode", "normal")) != "normal":
                metrics.append(("结算", str(detail.get("work_mode", ""))))
            card_msg = str(msg or "").replace(str(detail.get("stage_msg", "") or ""), "").strip()
            lines = [card_msg] if card_msg else [f"完成了{detail.get('job_name', '打工')}，抱着小钱包跑回来啦～"]
            title = "打工结果"
        elif event_type == "started":
            metrics = [
                ("预计报酬", f"{detail.get('reward', 0)} {coin_name}"),
                ("耗时", self._format_duration(detail.get("duration", 0))),
                ("档位", detail.get("energy_tier", "普通打工")),
                ("道具加成", self._fmt_percent(float(detail.get("work_buff", 1)) * 100)),
                ("精力", self._fmt_delta(-float(detail.get("energy_cost", 0)))),
                ("饱食度", self._fmt_delta(-float(detail.get("satiety_cost", 0)))),
                ("成长值", self._fmt_delta(detail.get("growth_add", 0))),
                ("亲密度", self._fmt_delta(detail.get("intimacy_add", 0))),
            ]
            lines = [
                f"出发去{detail.get('job_name', '打工')}啦～",
                f"精力档位：{detail.get('energy_tier', '普通打工')}，报酬 {self._fmt_percent(float(detail.get('energy_reward_multiplier', 1)) * 100)}。",
                f"道具加成：{self._fmt_percent(float(detail.get('work_buff', 1)) * 100)}。" if float(detail.get("work_buff", 1)) > 1 else "",
                "等她回来后，再发送「猫娘打工」领取报酬和成长奖励。",
            ]
            title = "打工结果"
        elif event_type == "working":
            metrics = [
                ("剩余", detail.get("remain", "-")),
                ("预计报酬", f"{detail.get('reward', 0)} {coin_name}"),
                ("成长值", self._fmt_delta(detail.get("growth_add", 0))),
                ("亲密度", self._fmt_delta(detail.get("intimacy_add", 0))),
            ]
            lines = [msg if msg else f"正在{detail.get('job_name', '打工')}，还需要一点时间。"]
            title = "打工结果"
        else:
            metrics = [
                ("健康", self._fmt_int(cat.get("health", 0))),
                ("精力", self._fmt_int(cat.get("energy", 0))),
                ("饱食度", self._fmt_int(cat.get("satiety", 0))),
                ("心情", self._fmt_int(cat.get("mood", 0))),
            ]
            lines = [msg]
            title = "打工结果"

        card = self.draw_care_card(
            title,
            cat,
            subtitle=str(detail.get("job_name", "")) if isinstance(detail, dict) else "",
            lines=lines,
            metrics=metrics,
            footer="发送「猫娘打工 列表」查看地点；发送「猫娘状态」查看完整档案。",
            tag=f"work_{uid}",
        )
        return ok, msg, card, event_type, detail or {}

    def work_unlock(self, uid: str, job_query: str = ""):
        self._finalize_expired_adoption(uid)
        coin_name = self._coin_name()
        jobs = self._work_jobs(self._rules("work"))
        job_query = str(job_query or "").strip()

        if not job_query:
            cat = self._get(uid)
            unlocked = set(((cat or {}).get("unlocks") or {}).get("work_jobs", []))
            rows = []
            for job in jobs:
                cost = int(job.get("unlock_cost", 0) or 0)
                if cost <= 0:
                    state = "默认开放"
                elif str(job.get("id", "")) in unlocked:
                    state = "已解锁"
                else:
                    state = f"解锁 {cost} {coin_name}"
                rows.append(f"{job.get('name', '打工地点')}：{state}，阶段 {stage_name(job.get('min_stage', 0))}")
            table_rows = []
            for job in jobs:
                cost = int(job.get("unlock_cost", 0) or 0)
                if cost <= 0:
                    state = "默认开放"
                elif str(job.get("id", "")) in unlocked:
                    state = "已解锁"
                else:
                    state = f"{cost} {coin_name}"
                table_rows.append([job.get("name", "打工地点"), state, stage_name(job.get("min_stage", 0))])
            hint = "发送「猫娘打工 解锁 地点名」解锁指定地点；发送「猫娘打工 列表」查看收益和消耗。"
            msg = "打工地点解锁：\n" + "\n".join(rows) + f"\n\n{hint}"
            card = self.draw_table_card(
                "打工解锁",
                headers=["地点", "状态 / 费用", "阶段要求"],
                rows=table_rows,
                col_widths=[2.8, 1.8, 1.2],
                metrics=[("地点数", str(len(jobs))), ("余额", f"{self.economy.get_balance(uid)} {coin_name}")],
                footer=hint,
                tag=f"work_unlock_{uid}",
            )
            return True, msg, card

        selected_job, matches = self._find_work_job(jobs, job_query)
        if not selected_job:
            if matches:
                names = "、".join(str(job.get("name", "打工地点")) for job in matches[:8])
                msg = f"找到多个相近的打工地点：{names}\n请发送更完整的地点名。"
            else:
                msg = f"没有找到「{job_query}」这个打工地点。\n可选地点：{self._work_job_names(jobs)}"
            card = self.draw_info_card(
                "打工解锁未完成",
                lines=[msg],
                metrics=[("用法", "猫娘打工 解锁 地点名")],
                footer="发送「猫娘打工 解锁」查看地点解锁状态。",
                tag=f"work_unlock_err_{uid}",
            )
            return False, msg, card

        def op(root):
            wallet = root.setdefault("wallet", {})
            ok, cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, cat, 0
            cats = root.setdefault("catgirls", {})
            cost = int(selected_job.get("unlock_cost", 0) or 0)
            unlocks = cat.setdefault("unlocks", {})
            work_jobs = unlocks.setdefault("work_jobs", [])
            job_id = str(selected_job.get("id", ""))
            if cost <= 0:
                if job_id and job_id not in work_jobs:
                    work_jobs.append(job_id)
                cats[uid] = cat
                return True, f"「{selected_job['name']}」本来就是开放地点，已经登记到解锁列表。", cat, int(wallet.get(uid, 0))
            if job_id in work_jobs:
                cats[uid] = cat
                return False, f"「{selected_job['name']}」已经解锁过了。", cat, int(wallet.get(uid, 0))
            if int(cat.get("stage", 0) or 0) < int(selected_job.get("min_stage", 0) or 0):
                cats[uid] = cat
                return False, f"「{selected_job['name']}」需要达到 {stage_name(selected_job.get('min_stage', 0))} 阶段后才能解锁。", cat, int(wallet.get(uid, 0))
            balance = int(wallet.get(uid, 0))
            if balance < cost:
                cats[uid] = cat
                return False, f"解锁「{selected_job['name']}」需要 {cost} {coin_name}，你目前只有 {balance} {coin_name}。", cat, balance
            wallet[uid] = balance - cost
            work_jobs.append(job_id)
            unlocks["work_jobs"] = work_jobs
            cat["unlocks"] = unlocks
            cats[uid] = cat
            return True, f"已解锁打工地点「{selected_job['name']}」，花费 {cost} {coin_name}。\n当前余额：{wallet[uid]} {coin_name}", cat, wallet[uid]

        ok, msg, cat, balance = self.store.update(op)
        card = self.draw_info_card(
            "打工解锁完成" if ok else "打工解锁未完成",
            subtitle=str(selected_job.get("name", "打工地点")),
            lines=[msg],
            metrics=[
                ("地点", selected_job.get("name", "打工地点")),
                ("费用", f"{int(selected_job.get('unlock_cost', 0) or 0)} {coin_name}"),
                ("余额", f"{balance} {coin_name}"),
                ("阶段要求", stage_name(selected_job.get("min_stage", 0))),
            ],
            footer="发送「猫娘打工 列表」查看地点收益与消耗。",
            tag=f"work_unlock_{uid}",
        )
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
                return False, err_msg, cat, None, 0, 0, 0, 0, 0, 1, 1, "", "", 1, ""

            cats = root.setdefault("catgirls", {})
            interact_data = cat.setdefault("interactions", {})
            today_count = int(interact_data.get(today, 0))
            cooldown = int(care_rules["interaction_cooldown_seconds"])
            last_interact_at = int(interact_data.get("_last_at", 0) or 0)
            if cooldown > 0 and last_interact_at and now - last_interact_at < cooldown:
                cats[uid] = cat
                remain = self._format_duration(cooldown - (now - last_interact_at))
                return False, f"「{cat['name']}」刚刚才互动过，先让她缓一缓吧。\n冷却剩余：{remain}", cat, None, 0, 0, 0, today_count, 0, 1, 1, "", "冷却中", 1, ""

            if float(cat.get("health", 0)) < care_rules["interact_min_health"]:
                cats[uid] = cat
                return False, f"「{cat['name']}」现在很虚弱，先喂点东西、让她好好休息一下吧。", cat, None, 0, 0, 0, today_count, 0, 1, 1, "", "", 1, ""
            if int(cat.get("stage", 0)) < min_stage:
                cats[uid] = cat
                return False, f"「{cat['name']}」还有些害羞，等你们更亲近一点再做这个互动吧。", cat, None, 0, 0, 0, today_count, 0, 1, 1, "", "", 1, ""
            effective_base_energy_cost = max(int(base_energy_cost), int(care_rules["interaction_energy_cost"]))
            energy_cost = self._scaled_int(effective_base_energy_cost, self._personality_multiplier(cat, "interaction_energy_cost_multiplier"))
            if energy_cost and float(cat.get("energy", 0)) < energy_cost:
                cats[uid] = cat
                return False, f"「{cat['name']}」现在精力只有 {self._fmt_int(cat.get('energy', 0))}，这次互动需要 {energy_cost}。先让她休息一下再玩吧～", cat, None, 0, 0, 0, today_count, energy_cost, 1, 1, "", "", 1, ""

            mood_add = self._scaled_int(base_mood_add, self._personality_multiplier(cat, "interaction_mood_multiplier"))
            mood_multiplier, mood_label = self._mood_interaction_multiplier(cat)
            daily_multiplier, daily_label = self._interaction_daily_multiplier(today_count)
            buffs = cat.setdefault("buffs", {})
            interaction_buff = max(1.0, float(buffs.get("next_interaction_multiplier", 1) or 1))
            reward_multiplier = mood_multiplier * daily_multiplier * interaction_buff
            intimacy_add = self._scaled_int(base_intimacy_add, self._personality_multiplier(cat, "interaction_intimacy_multiplier") * reward_multiplier)
            growth_add = self._scaled_int(base_growth_add, self._personality_multiplier(cat, "interaction_growth_multiplier") * reward_multiplier)
            cat["mood"] = round(clamp(float(cat.get("mood", 80)) + mood_add, 0, 100), 4)
            cat["intimacy"] = int(cat.get("intimacy", 0)) + intimacy_add
            cat["growth"] = int(cat.get("growth", 0)) + growth_add
            cat["energy"] = round(clamp(float(cat.get("energy", 80)) - energy_cost, 0, 100), 4)
            interact_data[today] = today_count + 1
            interact_data["_last_at"] = now
            cat["interactions"] = interact_data
            buffs.pop("next_interaction_multiplier", None)
            cat["buffs"] = buffs
            stats = cat.setdefault("care_stats", {})
            stats["total_interacts"] = int(stats.get("total_interacts", 0)) + 1

            # Step 2/5：互动锚点的随机事件（含性格专属事件二次 roll）
            event_text = ""
            if self.events is not None:
                uid_key = str(cat.get("user", uid))
                today_count_ev = self.events.today_anchor_count(self.store, uid_key, today, "interact")
                triggered_ids = self.events.today_triggered_ids(self.store, uid_key, today)
                try:
                    event_roll = self.events.roll("interact", cat, today_count_ev, triggered_ids)
                except Exception:
                    event_roll = None
                if event_roll is not None:
                    ev, deltas = event_roll
                    coin_delta = int(deltas.get("coin", 0) or 0)
                    if coin_delta:
                        wallet = root.setdefault("wallet", {})
                        wallet[uid_key] = int(wallet.get(uid_key, 0)) + coin_delta
                    cat["mood"] = round(clamp(float(cat.get("mood", 80)) + int(deltas.get("mood", 0) or 0), 0, 100), 4)
                    cat["energy"] = round(clamp(float(cat.get("energy", 80)) + int(deltas.get("energy", 0) or 0), 0, 100), 4)
                    cat["intimacy"] = int(cat.get("intimacy", 0)) + int(deltas.get("intimacy", 0) or 0)
                    cat["growth"] = int(cat.get("growth", 0)) + int(deltas.get("growth", 0) or 0)
                    event_text = str(ev.get("text", "") or "")
                    self.events.record_event(root, uid_key, today, "interact", str(ev.get("id", "") or ""), event_text)

            cat, stage_msg = self._advance_stage(cat)
            cats[uid] = cat
            return True, "", cat, stage_msg, mood_add, intimacy_add, growth_add, interact_data[today], energy_cost, mood_multiplier, daily_multiplier, mood_label, daily_label, interaction_buff, event_text

        ok, err_msg, cat, stage_msg, mood_add, intimacy_add, growth_add, today_count, energy_cost, mood_multiplier, daily_multiplier, mood_label, daily_label, interaction_buff, event_text = self.store.update(op)
        if not ok:
            return False, err_msg, None

        msg = (
            f"{text}\n"
            f"心情 {self._fmt_delta(mood_add)}\n"
            f"亲密度 {self._fmt_delta(intimacy_add)}\n"
            f"成长值 {self._fmt_delta(growth_add)}\n"
            f"{'道具加成：' + self._fmt_percent(float(interaction_buff) * 100) + chr(10) if float(interaction_buff or 1) > 1 else ''}"
            f"当前阶段：{stage_name(cat.get('stage', 0))}\n"
            f"今日互动次数：{today_count}/{daily_limit if daily_limit else '不限'}"
        )
        if energy_cost:
            msg += f"\n精力 {self._fmt_delta(-energy_cost)}"
        if stage_msg:
            msg += stage_msg
        if event_text:
            msg += f"\n✦ 今日奇遇：{event_text}"
        card = self.draw_care_card(
            "互动结果",
            cat,
            subtitle=action,
            lines=[
                text,
                f"道具加成：{self._fmt_percent(float(interaction_buff) * 100)}" if float(interaction_buff or 1) > 1 else "",
            ],
            metrics=[
                ("心情", self._fmt_delta(mood_add)),
                ("亲密度", self._fmt_delta(intimacy_add)),
                ("成长值", self._fmt_delta(growth_add)),
                ("精力", self._fmt_delta(-energy_cost) if energy_cost else "0.0"),
                ("今日互动", f"{today_count}/{daily_limit if daily_limit else '不限'}"),
                ("道具", self._fmt_percent(float(interaction_buff or 1) * 100)),
                ("阶段", stage_name(cat.get("stage", 0))),
            ],
            tag=f"interact_{uid}",
        )
        return True, msg, card

    def shop(self, uid: str, category: str = ""):
        category = str(category or "").strip()
        if category in ("列表", "全部"):
            category = ""
        shop_rows = self._shop_items()
        if category:
            shop_rows = [row for row in shop_rows if str(row.get("category", "")) == category]
        lines = self._shop_summary_lines(category, len(shop_rows) or 1)
        table_rows = [
            [item.get("name", "道具"), item.get("category", "道具"), f"{int(item.get('price', 0))} {self._coin_name()}", item.get("description", "")]
            for item in shop_rows
        ]
        if not table_rows:
            table_rows = [["无", category or "全部", "-", "当前没有可购买的道具"]]
        hint = "发送「购买 道具名 [数量]」购买；发送「背包」查看已拥有道具；可用「猫娘商店 礼物/道具/护理/食物/功能卡」筛选。"
        msg = "猫娘商店：\n" + "\n".join(lines) + f"\n\n{hint}"
        card = self.draw_table_card(
            "猫娘商店",
            subtitle=category or "全部道具",
            headers=["道具", "分类", "价格", "说明"],
            rows=table_rows,
            col_widths=[1.6, 0.9, 1.15, 2.8],
            metrics=[
                ("余额", f"{self.economy.get_balance(uid)} {self._coin_name()}"),
                ("道具数", str(len(shop_rows))),
            ],
            footer=hint,
            tag=f"shop_{uid}",
        )
        return True, msg, card

    def bag(self, uid: str):
        def op(root):
            self._grant_starter_cards(root, uid)
            return dict(self._item_quantity_map(root, uid))

        items = self.store.update(op) or {}
        rows = []
        table_rows = []
        for item_id, count in items.items():
            count = self._item_count(count)
            if count > 0:
                name = self._item_display_name(item_id)
                rows.append(f"{name} x{count}")
                table_rows.append([name, str(count), "发送「使用 道具名」"])
        cat = self._get(uid)
        buffs = (cat or {}).get("buffs") if isinstance((cat or {}).get("buffs"), dict) else {}
        if buffs:
            if float(buffs.get("next_interaction_multiplier", 1) or 1) > 1:
                value = self._fmt_percent(float(buffs.get("next_interaction_multiplier")) * 100)
                rows.append(f"互动加成待生效：{value}")
                table_rows.append(["互动加成", value, "下次互动生效"])
            if float(buffs.get("next_work_reward_multiplier", 1) or 1) > 1:
                value = self._fmt_percent(float(buffs.get("next_work_reward_multiplier")) * 100)
                rows.append(f"打工加成待生效：{value}")
                table_rows.append(["打工加成", value, "下次打工生效"])
        if not rows:
            rows = ["背包还是空的。发送「猫娘商店」看看可以买什么。"]
            table_rows = [["空", "0", "发送「猫娘商店」购买"]]
        hint = "发送「使用 道具名」使用道具；发送「购买 道具名 [数量]」购买更多道具。"
        msg = "背包：\n" + "\n".join(rows) + f"\n\n{hint}"
        card = self.draw_table_card(
            "猫娘背包",
            headers=["物品 / 加成", "数量 / 倍率", "说明"],
            rows=table_rows,
            col_widths=[1.8, 1.2, 2.4],
            metrics=[
                ("道具种类", str(len([v for v in items.values() if self._item_count(v) > 0]))),
                ("余额", f"{self.economy.get_balance(uid)} {self._coin_name()}"),
            ],
            footer=hint,
            tag=f"bag_{uid}",
        )
        return True, msg, card

    def _parse_item_quantity(self, query: str) -> Tuple[str, int]:
        query = str(query or "").strip()
        match = re.search(r"\s+(\d{1,3})$", query)
        if not match:
            return query, 1
        qty = max(1, int(match.group(1)))
        name = query[:match.start()].strip()
        return name or query, qty

    def buy_item(self, uid: str, query: str):
        self._finalize_expired_adoption(uid)
        item_query, quantity = self._parse_item_quantity(query)
        item, matches = self._find_named(self._shop_items(), item_query)
        if not item:
            if matches:
                names = "、".join(str(row.get("name", "道具")) for row in matches[:8])
                msg = f"找到多个相近道具：{names}\n请发送更完整的道具名。"
            else:
                msg = f"没有找到「{item_query}」这个道具。发送「猫娘商店」查看可购买道具。"
            card = self.draw_info_card(
                "购买未完成",
                lines=[msg],
                metrics=[("用法", "购买 道具名 [数量]")],
                footer="发送「猫娘商店」查看可购买道具。",
                tag=f"buy_err_{uid}",
            )
            return False, msg, card
        coin_name = self._coin_name()
        price = int(item.get("price", 0) or 0)
        total = price * quantity

        def op(root):
            wallet = root.setdefault("wallet", {})
            if str(item.get("effect", "")) == "recall_runaway":
                cat = root.setdefault("catgirls", {}).get(uid)
                if isinstance(cat, dict):
                    cat, _ = normalize_catgirl(cat, uid)
            else:
                ok, cat, err_msg = self._load_active_cat(root, uid)
                if not ok:
                    return False, err_msg, cat, 0, 0
            if str(item.get("effect", "")) != "recall_runaway" and not cat:
                return False, err_msg, cat, 0, 0
            balance = int(wallet.get(uid, 0))
            if balance < total:
                return False, f"购买 {quantity} 个「{item['name']}」需要 {total} {coin_name}，你目前只有 {balance} {coin_name}。", cat, balance, 0
            bag = self._item_quantity_map(root, uid)
            item_id = str(item.get("id"))
            bag[item_id] = int(bag.get(item_id, 0) or 0) + quantity
            wallet[uid] = balance - total
            if cat:
                root.setdefault("catgirls", {})[uid] = cat
            return True, f"购买成功：{item['name']} x{quantity}\n花费：{total} {coin_name}\n当前余额：{wallet[uid]} {coin_name}", cat, wallet[uid], int(bag[item_id])

        ok, msg, cat, balance, owned = self.store.update(op)
        card = self.draw_info_card(
            "购买完成" if ok else "购买未完成",
            subtitle=str(item.get("name", "道具")),
            lines=[msg, str(item.get("description", ""))],
            metrics=[
                ("道具", item.get("name", "道具")),
                ("数量", str(quantity)),
                ("花费", f"{total} {coin_name}"),
                ("拥有", str(owned)),
                ("余额", f"{balance} {coin_name}"),
            ],
            footer="发送「背包」查看已拥有道具；发送「使用 道具名」使用道具。",
            tag=f"buy_{uid}",
        )
        return ok, msg, card

    def use_item(self, uid: str, query: str):
        self._finalize_expired_adoption(uid)
        item_query = str(query or "").strip()
        item, matches = self._find_named(self._shop_items(), item_query)
        if not item:
            if matches:
                names = "、".join(str(row.get("name", "道具")) for row in matches[:8])
                msg = f"找到多个相近道具：{names}\n请发送更完整的道具名。"
            else:
                msg = f"没有找到「{item_query}」这个道具。"
            card = self.draw_info_card(
                "使用未完成",
                lines=[msg],
                metrics=[("用法", "使用 道具名")],
                footer="发送「背包」查看已拥有道具；发送「猫娘商店」查看可购买道具。",
                tag=f"use_err_{uid}",
            )
            return False, msg, card, {}
        effect = str(item.get("effect", "instant"))

        if effect == "recall_runaway":
            def recall_op(root):
                bag = self._item_quantity_map(root, uid)
                item_id = str(item.get("id"))
                count = self._item_count(bag.get(item_id))
                if count <= 0:
                    return False, f"背包里没有「{item['name']}」。", None, 0
                cats = root.setdefault("catgirls", {})
                active = cats.get(uid)
                if isinstance(active, dict) and active.get("name"):
                    return False, f"你已经有猫娘「{active.get('name', '猫娘')}」在身边啦，不需要召回。", active, count
                runaway_map = root.setdefault("runaway_catgirls", {})
                runaway_cat = runaway_map.get(uid)
                if not isinstance(runaway_cat, dict) or not runaway_cat.get("name"):
                    return False, "没有可召回的离家猫娘。你也可以发送「请赐我一只可爱猫娘吧」重新遇见新的猫娘。", None, count
                runaway_cat, _ = normalize_catgirl(runaway_cat, uid)
                runaway_cat.pop("is_runaway", None)
                runaway_cat.pop("runaway_at", None)
                runaway_cat.pop("satiety_zero_since", None)
                runaway_cat["satiety"] = max(float(runaway_cat.get("satiety", 0) or 0), 35)
                runaway_cat["mood"] = max(float(runaway_cat.get("mood", 0) or 0), 60)
                runaway_cat["health"] = max(float(runaway_cat.get("health", 0) or 0), 70)
                runaway_cat["energy"] = max(float(runaway_cat.get("energy", 0) or 0), 55)
                runaway_cat["last_decay"] = now_ts()
                cats[uid] = runaway_cat
                runaway_map.pop(uid, None)
                root.setdefault("runaway_notices", {}).pop(uid, None)
                bag[item_id] = count - 1
                if bag[item_id] <= 0:
                    bag.pop(item_id, None)
                self._grant_starter_cards(root, uid)
                return True, f"命运的红线轻轻发亮，「{runaway_cat.get('name', '猫娘')}」循着羁绊回到了你身边。", runaway_cat, int(bag.get(item_id, 0) or 0)

            ok, msg, cat, left = self.store.update(recall_op)
            if not ok:
                card = self.draw_info_card(
                    "召回未完成",
                    subtitle=str(item.get("name", "道具")),
                    lines=[msg, str(item.get("description", ""))],
                    metrics=[("道具", item.get("name", "道具")), ("剩余", str(left))],
                    footer="没有可召回猫娘时，可以发送「请赐我一只可爱猫娘吧」重新遇见新的猫娘。",
                    tag=f"recall_err_{uid}",
                )
                return False, msg, card, {}
            card = self.draw_care_card(
                "猫娘召回",
                cat,
                lines=[msg],
                metrics=[
                    ("饱食度", self._fmt_int(cat.get("satiety", 0))),
                    ("心情", self._fmt_int(cat.get("mood", 0))),
                    ("健康", self._fmt_int(cat.get("health", 0))),
                    ("精力", self._fmt_int(cat.get("energy", 0))),
                    ("剩余红线", str(left)),
                ],
                footer="她回来了。记得好好照顾她喔。",
                tag=f"recall_{uid}",
            )
            return True, msg, card, {"effect": "recall_runaway"}

        def op(root):
            ok, cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, cat, {}
            bag = self._item_quantity_map(root, uid)
            item_id = str(item.get("id"))
            count = self._item_count(bag.get(item_id))
            if count <= 0:
                return False, f"背包里没有「{item['name']}」。", cat, {}
            if effect == "rename_card":
                return False, "改名卡会在发送「猫娘改名 新名字」时自动消耗。", cat, {"hint_only": True}
            if effect == "appearance_card":
                return False, "形象更改卡会在发送「更换猫娘形象」并上传图片时自动消耗。", cat, {"hint_only": True}

            detail = {"item": item.get("name", "道具"), "effect": effect}
            mood_add = health_add = energy_add = satiety_add = growth_add = intimacy_add = 0

            if effect == "gift":
                stats = cat.setdefault("gift_stats", {})
                today = today_str()
                week = self._week_key()
                daily_item_key = f"{today}:{item_id}"
                weekly_item_key = f"{week}:{item_id}"
                daily_limit = int(item.get("daily_limit", 0) or 0)
                weekly_limit = int(item.get("weekly_limit", 0) or 0)
                if daily_limit and int(stats.get(daily_item_key, 0) or 0) >= daily_limit:
                    return False, f"「{item['name']}」今天已经送过很多次了，明天再送吧。", cat, {}
                if weekly_limit and int(stats.get(weekly_item_key, 0) or 0) >= weekly_limit:
                    return False, f"「{item['name']}」本周已经达到使用上限。", cat, {}
                gift_multiplier, gift_label, today_count = self._gift_daily_multiplier(cat)
                mood_add = random.randint(int(item.get("mood_min", 0) or 0), int(item.get("mood_max", item.get("mood_min", 0)) or 0))
                intimacy_add = self._scaled_int(random.randint(int(item.get("intimacy_min", 0) or 0), int(item.get("intimacy_max", item.get("intimacy_min", 0)) or 0)), gift_multiplier)
                growth_add = self._scaled_int(random.randint(int(item.get("growth_min", 0) or 0), int(item.get("growth_max", item.get("growth_min", 0)) or 0)), gift_multiplier)
                stats[today] = today_count + 1
                stats[daily_item_key] = int(stats.get(daily_item_key, 0) or 0) + 1
                stats[weekly_item_key] = int(stats.get(weekly_item_key, 0) or 0) + 1
                cat["gift_stats"] = stats
                detail.update({"gift_multiplier": gift_multiplier, "gift_label": gift_label, "today_count": stats[today]})
            elif effect == "next_interaction":
                buffs = cat.setdefault("buffs", {})
                buffs["next_interaction_multiplier"] = max(float(buffs.get("next_interaction_multiplier", 1) or 1), float(item.get("multiplier", 1) or 1))
                cat["buffs"] = buffs
                detail["buff"] = f"下一次互动加成 {self._fmt_percent(float(buffs['next_interaction_multiplier']) * 100)}"
            elif effect == "next_work":
                buffs = cat.setdefault("buffs", {})
                buffs["next_work_reward_multiplier"] = max(float(buffs.get("next_work_reward_multiplier", 1) or 1), float(item.get("multiplier", 1) or 1))
                cat["buffs"] = buffs
                detail["buff"] = f"下一次猫娘打工报酬 {self._fmt_percent(float(buffs['next_work_reward_multiplier']) * 100)}"
            else:
                satiety_add = int(item.get("satiety_add", 0) or 0)
                mood_add = int(item.get("mood_add", 0) or 0)
                health_add = int(item.get("health_add", 0) or 0)
                energy_add = int(item.get("energy_add", 0) or 0)
                growth_add = random.randint(int(item.get("growth_min", 0) or 0), int(item.get("growth_max", item.get("growth_min", 0)) or 0))
                intimacy_add = random.randint(int(item.get("intimacy_min", 0) or 0), int(item.get("intimacy_max", item.get("intimacy_min", 0)) or 0))

            if satiety_add:
                cat["satiety"] = round(clamp(float(cat.get("satiety", 0)) + satiety_add, 0, 100), 4)
                if cat["satiety"] > 0:
                    cat.pop("satiety_zero_since", None)
            if mood_add:
                cat["mood"] = round(clamp(float(cat.get("mood", 80)) + mood_add, 0, 100), 4)
            if health_add:
                cat["health"] = round(clamp(float(cat.get("health", 90)) + health_add, 0, 100), 4)
            if energy_add:
                cat["energy"] = round(clamp(float(cat.get("energy", 80)) + energy_add, 0, 100), 4)
            if growth_add:
                cat["growth"] = int(cat.get("growth", 0)) + growth_add
            if intimacy_add:
                cat["intimacy"] = int(cat.get("intimacy", 0)) + intimacy_add
            cat, stage_msg = self._advance_stage(cat)

            bag[item_id] = count - 1
            if bag[item_id] <= 0:
                bag.pop(item_id, None)
            root.setdefault("catgirls", {})[uid] = cat
            detail.update({
                "satiety_add": satiety_add,
                "mood_add": mood_add,
                "health_add": health_add,
                "energy_add": energy_add,
                "growth_add": growth_add,
                "intimacy_add": intimacy_add,
                "left": int(bag.get(item_id, 0) or 0),
                "stage_msg": stage_msg,
            })
            return True, "", cat, detail

        ok, msg, cat, detail = self.store.update(op)
        if not ok:
            card = self.draw_info_card(
                "使用未完成",
                subtitle=str(item.get("name", "道具")),
                lines=[msg, str(item.get("description", ""))],
                metrics=[
                    ("道具", item.get("name", "道具")),
                    ("用法", "使用 道具名"),
                ],
                footer="发送「背包」查看已拥有道具；功能卡会在对应操作中自动消耗。",
                tag=f"use_err_{uid}",
            )
            return False, msg, card, detail or {}

        msg_lines = [f"使用了「{detail.get('item')}」。"]
        card_lines = [f"使用了「{detail.get('item')}」。"]
        if detail.get("buff"):
            msg_lines.append(detail["buff"])
            card_lines.append(detail["buff"])
        if detail.get("gift_label"):
            gift_line = f"礼物收益：{self._fmt_percent(float(detail.get('gift_multiplier', 1)) * 100)}（{detail.get('gift_label')}）"
            msg_lines.append(gift_line)
            card_lines.append(gift_line)
        if detail.get("stage_msg"):
            msg_lines.append(str(detail.get("stage_msg")).strip())
        msg = "\n".join(msg_lines) + (
            f"\n饱食度 {self._fmt_delta(detail.get('satiety_add', 0))}"
            f"\n心情 {self._fmt_delta(detail.get('mood_add', 0))}"
            f"\n健康 {self._fmt_delta(detail.get('health_add', 0))}"
            f"\n精力 {self._fmt_delta(detail.get('energy_add', 0))}"
            f"\n亲密度 {self._fmt_delta(detail.get('intimacy_add', 0))}"
            f"\n成长值 {self._fmt_delta(detail.get('growth_add', 0))}"
        )
        card = self.draw_info_card(
            "道具使用",
            subtitle=str(detail.get("item", "")),
            lines=card_lines,
            metrics=[
                ("饱食度", self._fmt_delta(detail.get("satiety_add", 0))),
                ("心情", self._fmt_delta(detail.get("mood_add", 0))),
                ("健康", self._fmt_delta(detail.get("health_add", 0))),
                ("精力", self._fmt_delta(detail.get("energy_add", 0))),
                ("亲密度", self._fmt_delta(detail.get("intimacy_add", 0))),
                ("成长值", self._fmt_delta(detail.get("growth_add", 0))),
                ("剩余", str(detail.get("left", 0))),
            ],
            footer="发送「背包」查看剩余道具；发送「猫娘商店」购买更多道具。",
            tag=f"use_{uid}",
        )
        return True, msg, card, detail or {}

    def care_service(self, uid: str, service_query: str = ""):
        self._finalize_expired_adoption(uid)
        service_query = str(service_query or "").strip()
        services = self._care_services()
        coin_name = self._coin_name()
        if not service_query or service_query in ("列表", "护理"):
            lines = []
            table_rows = []
            for service in services:
                base = int(service.get("base_price", 0) or 0)
                per = int(service.get("price_per_missing", 0) or 0)
                price = f"{base}+缺失健康x{per}" if per else str(base)
                effects = []
                if int(service.get("target_health", 0) or 0):
                    effects.append(f"健康到{int(service.get('target_health', 0))}")
                for key, label in [("health_add", "健"), ("mood_add", "心"), ("energy_add", "精"), ("satiety_add", "饱")]:
                    value = int(service.get(key, 0) or 0)
                    if value:
                        effects.append(f"{label}{value:+d}")
                effect_text = " / ".join(effects) or "-"
                lines.append(f"{service.get('name', '护理')}：{price} {coin_name}，冷却 {self._format_duration(service.get('cooldown_seconds', 0))}")
                table_rows.append([
                    service.get("name", "护理"),
                    f"{price} {coin_name}",
                    effect_text,
                    self._format_duration(service.get("cooldown_seconds", 0)),
                ])
            hint = "发送「猫娘护理 服务名」购买护理服务；例如「猫娘护理 看病」。"
            msg = "猫娘护理服务：\n" + "\n".join(lines) + f"\n\n{hint}"
            card = self.draw_table_card(
                "猫娘护理",
                subtitle="服务价格、效果与冷却",
                headers=["服务", "价格", "效果", "冷却"],
                rows=table_rows,
                col_widths=[1.5, 1.6, 2.1, 1.2],
                metrics=[("服务数", str(len(services))), ("余额", f"{self.economy.get_balance(uid)} {coin_name}")],
                footer=hint,
                tag=f"care_{uid}",
            )
            return True, msg, card

        service, matches = self._find_named(services, service_query)
        if not service:
            if matches:
                names = "、".join(str(row.get("name", "护理")) for row in matches[:8])
                return False, f"找到多个相近护理服务：{names}\n请发送更完整的服务名。", None
            return False, f"没有找到「{service_query}」这个护理服务。发送「猫娘护理」查看列表。", None

        def op(root):
            wallet = root.setdefault("wallet", {})
            ok, cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, cat, 0, {}
            now = now_ts()
            service_id = str(service.get("id", ""))
            cooldowns = cat.setdefault("care_cooldowns", {})
            until = int(cooldowns.get(service_id, 0) or 0)
            if until > now:
                return False, f"「{service['name']}」还在冷却中，剩余 {self._format_duration(until - now)}。", cat, int(wallet.get(uid, 0)), {}
            current_health = float(cat.get("health", 0) or 0)
            target_health = int(service.get("target_health", 0) or 0)
            missing = max(0, target_health - int(current_health))
            price = int(service.get("base_price", 0) or 0) + missing * int(service.get("price_per_missing", 0) or 0)
            balance = int(wallet.get(uid, 0))
            if balance < price:
                return False, f"「{service['name']}」需要 {price} {coin_name}，你目前只有 {balance} {coin_name}。", cat, balance, {}
            health_before = float(cat.get("health", 0) or 0)
            mood_before = float(cat.get("mood", 0) or 0)
            energy_before = float(cat.get("energy", 0) or 0)
            satiety_before = float(cat.get("satiety", 0) or 0)
            if target_health:
                cat["health"] = round(max(float(cat.get("health", 0) or 0), float(target_health)), 4)
            cat["health"] = round(clamp(float(cat.get("health", 90)) + float(service.get("health_add", 0) or 0), 0, 100), 4)
            cat["mood"] = round(clamp(float(cat.get("mood", 80)) + float(service.get("mood_add", 0) or 0), 0, 100), 4)
            cat["energy"] = round(clamp(float(cat.get("energy", 80)) + float(service.get("energy_add", 0) or 0), 0, 100), 4)
            cat["satiety"] = round(clamp(float(cat.get("satiety", 0)) + float(service.get("satiety_add", 0) or 0), 0, 100), 4)
            if cat["satiety"] > 0:
                cat.pop("satiety_zero_since", None)
            cooldowns[service_id] = now + int(service.get("cooldown_seconds", 0) or 0)
            cat["care_cooldowns"] = cooldowns
            wallet[uid] = balance - price
            root.setdefault("catgirls", {})[uid] = cat
            detail = {
                "service": str(service.get("name", "护理")),
                "price": price,
                "health_add": cat["health"] - health_before,
                "mood_add": cat["mood"] - mood_before,
                "energy_add": cat["energy"] - energy_before,
                "satiety_add": cat["satiety"] - satiety_before,
                "balance": wallet[uid],
                "cooldown": int(service.get("cooldown_seconds", 0) or 0),
            }
            return True, "", cat, wallet[uid], detail

        ok, msg, cat, balance, detail = self.store.update(op)
        if not ok:
            card = self.draw_care_card("护理未完成", cat, lines=[msg], metrics=[("余额", f"{balance} {coin_name}")], tag=f"care_err_{uid}") if cat else None
            return False, msg, card, {"service": str(service.get("name", "护理"))}
        msg = (
            f"完成护理：{service['name']}\n"
            f"花费：{detail['price']} {coin_name}\n"
            f"饱食度 {self._fmt_delta(detail.get('satiety_add', 0))}\n"
            f"心情 {self._fmt_delta(detail.get('mood_add', 0))}\n"
            f"健康 {self._fmt_delta(detail.get('health_add', 0))}\n"
            f"精力 {self._fmt_delta(detail.get('energy_add', 0))}\n"
            f"当前余额：{detail['balance']} {coin_name}"
        )
        card = self.draw_care_card(
            "护理完成",
            cat,
            subtitle=service.get("name", "护理"),
            lines=[f"{service.get('name', '护理')} 已完成。", f"冷却：{self._format_duration(detail.get('cooldown', 0))}"],
            metrics=[
                ("花费", f"{detail['price']} {coin_name}"),
                ("饱食度", self._fmt_delta(detail.get("satiety_add", 0))),
                ("心情", self._fmt_delta(detail.get("mood_add", 0))),
                ("健康", self._fmt_delta(detail.get("health_add", 0))),
                ("精力", self._fmt_delta(detail.get("energy_add", 0))),
                ("余额", f"{detail['balance']} {coin_name}"),
            ],
            tag=f"care_{uid}",
        )
        return True, msg, card, detail or {"service": str(service.get("name", "护理"))}

    def rename(self, uid: str, name: str):
        self._finalize_expired_adoption(uid)

        name = name.strip()
        if not name or len(name) > 12:
            return False, "名字不能为空，且长度不能超过 12。", None

        def op(root):
            ok, cat, err_msg = self._load_active_cat(root, uid)
            if not ok:
                return False, err_msg, cat, 0
            bag = self._item_quantity_map(root, uid)
            if not self._consume_bag_item(bag, RENAME_CARD_ID):
                return False, "改名需要消耗 1 张改名卡。发送「猫娘商店 功能卡」购买，或查看背包确认是否拥有。", cat, 0
            cat["name"] = name
            root.setdefault("catgirls", {})[uid] = cat
            return True, f"改名成功啦～以后就叫她「{name}」喵。\n消耗：改名卡 x1", cat, self._item_count(bag.get(RENAME_CARD_ID))

        ok, msg, cat, left = self.store.update(op)
        card = self.draw_care_card(
            "改名完成" if ok else "改名未完成",
            cat,
            lines=[msg],
            metrics=[
                ("名字", name if ok else "-"),
                ("改名卡", str(left)),
                ("阶段", stage_name(cat.get("stage", 0)) if cat else "-"),
            ],
            tag=f"rename_{uid}",
        ) if cat else None
        return ok, msg, card

    def _precheck_appearance_change_payment(self, uid: str, price: int, coin_name: str):
        def op(root):
            wallet = root.setdefault("wallet", {})
            cats = root.setdefault("catgirls", {})
            current_cat = cats.get(uid)
            if not current_cat or not current_cat.get("name"):
                return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", current_cat, 0

            current_cat, changed = normalize_catgirl(current_cat, uid)
            if changed:
                cats[uid] = current_cat
            self._grant_starter_cards(root, uid)

            bag = self._item_quantity_map(root, uid)
            balance = int(wallet.get(uid, 0))
            if self._item_count(bag.get(APPEARANCE_CARD_ID)) > 0:
                return True, "", current_cat, balance
            if balance < price:
                return False, f"更换形象需要 {price} {coin_name}，你目前有 {balance} {coin_name}，还不够喔～", current_cat, balance
            return True, "", current_cat, balance

        return self.store.update(op)

    async def change_image(self, uid: str, image_src: str):
        """安全保存图片，成功后原子扣费。"""
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, self.missing_cat_message(uid), None
        _, _, appearance_change_price = self._wish_rules()
        coin_name = self._coin_name()
        can_pay, pay_msg, pay_cat, pay_balance = self._precheck_appearance_change_payment(
            uid,
            appearance_change_price,
            coin_name,
        )
        if not can_pay:
            card = self.draw_care_card(
                "形象更换未完成",
                pay_cat or cat,
                lines=[pay_msg],
                metrics=[("余额", f"{pay_balance} {coin_name}")],
                tag=f"image_err_{uid}",
            ) if (pay_cat or cat) else None
            return False, pay_msg, card

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

            self._grant_starter_cards(root, uid)
            balance = int(wallet.get(uid, 0))
            bag = self._item_quantity_map(root, uid)
            used_card = self._consume_bag_item(bag, APPEARANCE_CARD_ID)
            if not used_card and balance < appearance_change_price:
                return False, f"更换形象需要 {appearance_change_price} {coin_name}，你目前有 {balance} {coin_name}，还不够喔～", current_cat, None

            current_cat, _ = normalize_catgirl(current_cat, uid)
            old_image = current_cat.get("image", "")
            if not used_card:
                wallet[uid] = balance - appearance_change_price
            current_cat["image"] = str(out)
            cats[uid] = current_cat
            return True, "", current_cat, {"old_image": old_image, "used_card": used_card, "balance": int(wallet.get(uid, 0)), "card_left": self._item_count(bag.get(APPEARANCE_CARD_ID))}

        ok, msg, updated_cat, detail = self.store.update(op)
        if not ok:
            out.unlink(missing_ok=True)
            card = self.draw_care_card("形象更换未完成", updated_cat or cat, lines=[msg], tag=f"image_err_{uid}")
            return False, msg, card

        detail = detail if isinstance(detail, dict) else {}
        used_card = bool(detail.get("used_card"))
        self._delete_old_uploaded_image(str(detail.get("old_image", "")))
        balance = int(detail.get("balance", self.economy.get_balance(uid)))
        cost_line = f"消耗：形象更改卡 x1" if used_card else f"花费：{appearance_change_price} {coin_name}"
        msg = f"✨ 「{updated_cat['name']}」换好新形象啦～\n{cost_line}\n当前余额：{balance} {coin_name}\n\n当前档案：\n阶段：{stage_name(updated_cat.get('stage', 0))}\n亲密等级：{self._intimacy_display(updated_cat)}\n成长进度：{self._growth_display(updated_cat)}\n心情：{self._fmt_int(updated_cat.get('mood', 0))}\n状态：{status_tag(updated_cat)}"
        card = self.draw_care_card(
            "形象更换完成",
            updated_cat,
            lines=["新形象已经保存。"],
            metrics=[
                ("消耗", "形象更改卡 x1" if used_card else f"{appearance_change_price} {coin_name}"),
                ("形象卡", str(detail.get("card_left", 0))),
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
        allowed_dirs = [self.upload_dir.resolve()]
        if self.astrbot_temp_dir:
            allowed_dirs.append(self.astrbot_temp_dir.resolve())
        if not any(p == directory or p.is_relative_to(directory) for directory in allowed_dirs):
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
            wallet = root.setdefault("wallet", {})
            rows = []
            for uid, cat in list(all_cats.items()):
                if not isinstance(cat, dict) or not cat.get("name"):
                    continue
                cat, _ = normalize_catgirl(cat, uid)
                cat, runaway = self._apply_decay(cat)
                if runaway:
                    all_cats.pop(uid, None)
                    self._set_runaway_notice(root, uid, cat)
                    continue
                all_cats[uid] = cat
                if gid and cat.get("home_gid") != gid:
                    continue

                rank_cat = dict(cat)
                nickname = sign_data.get(uid, {}).get("last_nickname", uid)
                rank_cat["owner_nickname"] = nickname
                rank_cat["wallet_balance"] = int(wallet.get(uid, 0) or 0)
                rows.append(rank_cat)
            return rows

        cats = self.store.update(op)

        if not cats:
            return None

        cats.sort(key=lambda x: bond_score(x), reverse=True)
        cats = cats[:12]

        cols = 4
        card_w = 340
        card_h = 660
        padding = 30
        img_w = 312
        img_h = 360
        title_h = 120
        bottom_padding = 80
        side_margin = 50

        rows = math.ceil(len(cats) / cols)
        content_w = cols * card_w + (cols - 1) * padding
        total_w = content_w + side_margin * 2
        total_h = title_h + rows * (card_h + padding) - padding + bottom_padding

        canvas = self._make_card_background((total_w, total_h)).convert("RGBA")
        d = ImageDraw.Draw(canvas, "RGBA")
        t = self.theme

        title_font = self._title_font(76)
        name_font = self._title_font(32)
        info_font = self._font(26)

        d.text((total_w // 2, 60), "猫娘羁绊排行榜", font=title_font, fill=t["primary"], anchor="mm")

        for i, cat in enumerate(cats):
            row = i // cols
            col = i % cols
            x = side_margin + col * (card_w + padding)
            y = title_h + row * (card_h + padding)

            self._draw_gradient_panel(
                canvas,
                (x, y, x + card_w, y + card_h),
                radius=t["radius_panel"],
                alpha=108,
                outline=t["card_outline"],
                width=2,
            )

            cat_name = str(cat.get("name", "猫娘"))
            if len(cat_name) > 8:
                cat_name = cat_name[:8] + "..."
            d.text((x + card_w // 2, y + 30), cat_name, font=name_font, fill=t["text_dark"], anchor="mm")

            img_y = y + 60
            img_path = self.image_path(cat)
            if img_path and Path(img_path).exists():
                try:
                    img = Image.open(img_path).convert("RGB")
                    img = self._contain_in_frame(img, img_w, img_h, fill=(255, 250, 246))
                    self._paste_round(canvas, img, (x + 14, img_y, img_w, img_h), 14)
                except Exception:
                    self._draw_no_image(d, x + 14, img_y, img_w, img_h)
            else:
                self._draw_no_image(d, x + 14, img_y, img_w, img_h)

            info_y = img_y + img_h + 30
            owner = cat.get("owner_nickname", cat.get("user", ""))
            if len(owner) > 10:
                owner = owner[:10] + "..."
            info_items = [
                ("阶段", stage_name(cat.get("stage", 0)), t["primary"]),
                ("羁绊", str(bond_score(cat)), t["pink"]),
                ("主人", str(owner), t["accent"]),
            ]
            info_gap = 8
            info_w = (card_w - 28 - info_gap * 2) // 3
            info_h = 74
            for info_idx, (label, value, text_fill) in enumerate(info_items):
                ix = x + 14 + info_idx * (info_w + info_gap)
                iy = info_y
                self._draw_round_panel(
                    canvas,
                    (ix, iy, ix + info_w, iy + info_h),
                    radius=10,
                    fill=t["panel_fill"],
                    outline=t["panel_outline"],
                    width=1,
                    shadow=False,
                )
                d.text((ix + info_w // 2, iy + 23), label, font=self._font(20), fill=t["muted"], anchor="mm")
                value_font = self._fit_font(d, value, 24, info_w - 12, min_size=17)
                d.text((ix + info_w // 2, iy + 52), self._truncate_text(d, value, value_font, info_w - 12), font=value_font, fill=text_fill, anchor="mm")

            coin_name = self._coin_name()
            balance_text = f"{int(cat.get('wallet_balance', 0) or 0)} {coin_name}"
            coin_y = info_y + info_h + 12
            self._draw_round_panel(
                canvas,
                (x + 14, coin_y, x + card_w - 14, coin_y + 58),
                radius=12,
                fill=(255, 244, 232, 195),
                outline=t["panel_outline"],
                width=1,
                shadow=False,
            )
            d.text((x + 30, coin_y + 29), f"{coin_name}数量", font=self._font(22), fill=t["muted"], anchor="lm")
            value_font = self._fit_font(d, balance_text, 26, card_w - 150, min_size=18)
            self._draw_diamond_icon(d, x + card_w - self._text_size(d, balance_text, value_font)[0] - 54, coin_y + 29, 24)
            d.text((x + card_w - 28, coin_y + 30), balance_text, font=value_font, fill=t["primary"], anchor="rm")

        out = self.cache_dir / f"bond_rank_{gid or 'global'}.png"
        save_scaled_image(canvas.convert("RGB"), out, "PNG", scale=self._output_image_scale())
        return out

    def _draw_no_image(self, d: ImageDraw.ImageDraw, x: int, y: int, img_w: int, img_h: int):
        t = self.theme
        d.rounded_rectangle((x, y, x + img_w, y + img_h), radius=10, fill=(238, 242, 246), outline=t["panel_outline"], width=1)
        font = self._font(28)
        d.text((x + img_w // 2, y + img_h // 2), "暂无图片", font=font, fill=t["muted"], anchor="mm")

    def _cover(self, img: Image.Image, w: int, h: int) -> Image.Image:
        iw, ih = img.size
        scale = max(w / iw, h / ih)
        nw, nh = int(iw * scale), int(ih * scale)
        img = img.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - w) // 2, (nh - h) // 2
        return img.crop((left, top, left + w, top + h))
