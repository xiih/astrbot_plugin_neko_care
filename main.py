import asyncio
import random
import re
import time
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

try:
    from astrbot.api.message_components import Plain, Image
except Exception:
    Plain = None
    Image = None

from .storage import JsonStore
from .economy import EconomyService
from .sign import SignService
from .catgirl import CatgirlService
from .runtime_config import NekoRuntimeConfig
from .events import EventService
from .quests import DailyWishService


PLUGIN_NAME = "astrbot_plugin_neko_care"
KEYWORD_TRIGGER_ENABLED = True
PENDING_IMAGE_CHANGES = {}


def pending_image_filter():
    class PendingImageFilter(filter.CustomFilter):
        def filter(self, event: AstrMessageEvent, cfg) -> bool:
            uid = str(event.get_sender_id())
            if uid not in PENDING_IMAGE_CHANGES:
                return False
            event.is_at_or_wake_command = True
            event.is_wake = True
            return True

    return PendingImageFilter


def keyword_command_filter(*command_names: str):
    class KeywordCommandFilter(filter.CustomFilter):
        def filter(self, event: AstrMessageEvent, cfg) -> bool:
            if event.is_at_or_wake_command:
                return True
            if not KEYWORD_TRIGGER_ENABLED:
                return True

            message = re.sub(r"\s+", " ", event.get_message_str().strip())
            for command_name in command_names:
                if message == command_name or message.startswith(f"{command_name} "):
                    event.is_at_or_wake_command = True
                    event.is_wake = True
                    return True
            return True

    return KeywordCommandFilter


def neko_command(command_name: str, alias: set | None = None, **kwargs):
    command_names = [command_name, *(alias or set())]

    def decorator(awaitable):
        awaitable = filter.custom_filter(keyword_command_filter(*command_names), False, **kwargs)(awaitable)
        return filter.command(command_name, alias=alias, **kwargs)(awaitable)

    return decorator

@register("astrbot_plugin_neko_care", "若梦&TenmaGabriel0721", "猫娘羁绊养成、签到打工", "1.5.6")
class SapphireEconomyPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        self.coin_name = str(self.config.get("coin_name", "宝石"))[:16] or "宝石"
        self.sign_mode = "图片签到" if self.config.get("sign_mode", "图片签到") in ("图片签到", "image") else "文字签到"
        self.keyword_trigger_enabled = bool(self.config.get("keyword_trigger_enabled", True))
        global KEYWORD_TRIGGER_ENABLED
        KEYWORD_TRIGGER_ENABLED = self.keyword_trigger_enabled

        self.sign_min = max(0, int(self.config.get("sign_min_reward", 65)))
        self.sign_max = max(self.sign_min, int(self.config.get("sign_max_reward", 125)))
        self.work_min = max(0, int(self.config.get("work_min_reward", 35)))
        self.work_max = max(self.work_min, int(self.config.get("work_max_reward", 85)))

        self.extra_admin_ids = set(str(x) for x in self.config.get("extra_admin_ids", []))
        self.wish_probability = min(1.0, max(0.0, float(self.config.get("catgirl_wish_probability", 0.8))))
        self.wish_pity = max(1, int(self.config.get("catgirl_wish_pity", 3)))
        self.appearance_change_price = max(0, int(self.config.get("appearance_change_price", 900)))
        self.cache_cleanup_enabled = bool(self.config.get("cache_cleanup_enabled", True))
        self.cache_retention_days = max(0.0, float(self.config.get("cache_retention_days", 3)))
        self.cache_max_mb = max(0, int(self.config.get("cache_max_mb", 100)))
        self.cache_cleanup_interval_hours = max(
            1.0,
            float(self.config.get("cache_cleanup_interval_hours", 6)),
        )

        base_dir = Path(__file__).resolve().parent
        self.base_dir = base_dir
        self.asset_dir = base_dir / "assets"
        self.font_dir = base_dir / "fonts"
        self.catgirl_dir = self.asset_dir / "catgirl_pool"
        self.background_dir = self.asset_dir / "sign_backgrounds"
        self.quote_file = self.asset_dir / "quotes.txt"

        try:
            astrbot_data_dir = base_dir.parent.parent
            self.data_dir = astrbot_data_dir / "plugin_data" / PLUGIN_NAME
        except Exception:
            self.data_dir = base_dir / "plugin_data"

        self.upload_dir = self.data_dir / "pic"
        self.cache_dir = self.data_dir / "cache"

        for d in [self.asset_dir, self.font_dir, self.catgirl_dir, self.background_dir, self.data_dir, self.upload_dir, self.cache_dir]:
            Path(d).mkdir(parents=True, exist_ok=True)

        self._ensure_default_quote_file()

        self.store = JsonStore(self.data_dir / "store.json")
        self.runtime_config = NekoRuntimeConfig(self.data_dir / "runtime_config.json", self.config)
        self._apply_runtime_config()

        self.economy = EconomyService(self.store, self.coin_name, self.work_min, self.work_max, self.runtime_config.snapshot)
        self.sign = SignService(
            self.store, self.economy, self.coin_name, self.sign_min, self.sign_max,
            self.base_dir, self.background_dir, self.font_dir, self.cache_dir, self.quote_file, self.runtime_config.snapshot
        )
        self.catgirl = CatgirlService(
            self.store, self.economy, self.coin_name, self.base_dir, self.catgirl_dir,
            self.upload_dir, self.font_dir, self.cache_dir, self.wish_probability, self.wish_pity, self.appearance_change_price,
            self.runtime_config.snapshot, astrbot_temp_dir=Path(get_astrbot_temp_path())
        )
        self.events = EventService(self.runtime_config.snapshot)
        # 把事件引擎注入三个现有 service，让它们在 op 内同事务触发今日奇遇
        self.economy.events = self.events
        self.sign.events = self.events
        self.catgirl.events = self.events
        self.daily_wishes = DailyWishService(self.store, self.runtime_config.snapshot, self.economy, self.catgirl, self.events)
        self.catgirl.daily_wish_status_provider = self.daily_wishes.status_brief
        self.page_api = None
        self._register_page_api_if_available()

        self._pending_adoptions = {}
        global PENDING_IMAGE_CHANGES
        self._pending_image_changes = PENDING_IMAGE_CHANGES
        self._background_tasks = set()
        self._cache_cleanup_task = None

    def _runtime_snapshot(self) -> dict:
        runtime_config = getattr(self, "runtime_config", None)
        if runtime_config is None:
            return {}
        try:
            return runtime_config.snapshot()
        except Exception:
            return {}

    def _apply_runtime_config(self) -> None:
        data = self._runtime_snapshot()
        economy = data.get("economy", {}) if isinstance(data.get("economy"), dict) else {}
        wish = data.get("wish", {}) if isinstance(data.get("wish"), dict) else {}
        self.coin_name = str(economy.get("coin_name") or self.coin_name or "宝石")[:16] or "宝石"
        self.sign_min = max(0, int(economy.get("sign_min_reward", self.sign_min)))
        self.sign_max = max(self.sign_min, int(economy.get("sign_max_reward", self.sign_max)))
        self.work_min = max(0, int(economy.get("daily_work_min_reward", self.work_min)))
        self.work_max = max(self.work_min, int(economy.get("daily_work_max_reward", self.work_max)))
        self.wish_probability = min(1.0, max(0.0, float(wish.get("probability", self.wish_probability))))
        self.wish_pity = max(1, int(wish.get("pity", self.wish_pity)))
        self.appearance_change_price = max(0, int(wish.get("appearance_change_price", self.appearance_change_price)))

        for service in (getattr(self, "economy", None), getattr(self, "sign", None), getattr(self, "catgirl", None)):
            if service is not None and hasattr(service, "coin_name"):
                service.coin_name = self.coin_name

    def _coin_name(self) -> str:
        data = self._runtime_snapshot()
        economy = data.get("economy", {}) if isinstance(data.get("economy"), dict) else {}
        return str(economy.get("coin_name") or self.coin_name or "宝石")

    def _care_rules(self) -> dict:
        data = self._runtime_snapshot()
        care = data.get("care", {}) if isinstance(data.get("care"), dict) else {}
        return care

    def _wish_rules(self) -> tuple[float, int, int]:
        data = self._runtime_snapshot()
        wish = data.get("wish", {}) if isinstance(data.get("wish"), dict) else {}
        return (
            min(1.0, max(0.0, float(wish.get("probability", self.wish_probability)))),
            max(1, int(wish.get("pity", self.wish_pity))),
            max(0, int(wish.get("appearance_change_price", self.appearance_change_price))),
        )

    def _register_page_api_if_available(self) -> None:
        try:
            if not callable(getattr(self.context, "register_web_api", None)):
                return
            from .page_api import NekoCarePageApi

            self.page_api = NekoCarePageApi(self)
            self.page_api.register_routes()
        except Exception:
            self.page_api = None

    def _ensure_default_quote_file(self):
        if self.quote_file.exists():
            return
        self.quote_file.parent.mkdir(parents=True, exist_ok=True)
        self.quote_file.write_text(
            "愿你今天也被温柔以待。\n"
            "要把普通的日子过得浪漫一点。\n"
            "慢慢来，好运正在路上。\n"
            "心里装着小星星，生活才会亮晶晶。\n"
            "今天也要好好吃饭，好好睡觉。\n"
            "猫猫偷偷告诉你：今天会有好事发生。\n"
            "每一次签到，都是和幸运打了个招呼。|小助手\n",
            encoding="utf-8",
        )

    def _track_background_task(self, coro, name: str = "") -> asyncio.Task:
        task = asyncio.create_task(coro, name=name or None)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if not self.cache_cleanup_enabled:
            return
        await self._cleanup_cache_files()
        if self._cache_cleanup_task is None or self._cache_cleanup_task.done():
            self._cache_cleanup_task = self._track_background_task(
                self._cache_cleanup_loop(),
                "neko_care_cache_cleanup",
            )

    async def _cache_cleanup_loop(self):
        interval_seconds = self.cache_cleanup_interval_hours * 3600
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                await self._cleanup_cache_files()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[NekoCare] 缓存清理循环异常退出: {exc}", exc_info=True)

    async def _cleanup_cache_files(self) -> tuple[int, int, int]:
        try:
            deleted_count, deleted_bytes, remaining_bytes = await asyncio.to_thread(
                self._cleanup_cache_files_sync
            )
        except Exception as exc:
            logger.warning(f"[NekoCare] 清理缓存图片失败: {exc}", exc_info=True)
            return 0, 0, 0

        if deleted_count:
            logger.info(
                f"[NekoCare] 已清理缓存图片 {deleted_count} 个，"
                f"释放 {deleted_bytes / 1024 / 1024:.2f} MB，"
                f"剩余 {remaining_bytes / 1024 / 1024:.2f} MB"
            )
        return deleted_count, deleted_bytes, remaining_bytes

    def _cleanup_cache_files_sync(self) -> tuple[int, int, int]:
        if not self.cache_dir.exists():
            return 0, 0, 0

        now = time.time()
        max_age_seconds = self.cache_retention_days * 86400
        max_bytes = self.cache_max_mb * 1024 * 1024 if self.cache_max_mb > 0 else 0
        min_delete_age_seconds = 3600
        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

        files = []
        deleted_count = 0
        deleted_bytes = 0

        for path in self.cache_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            age = now - stat.st_mtime
            if max_age_seconds > 0 and age > max_age_seconds:
                try:
                    size = stat.st_size
                    path.unlink(missing_ok=True)
                    deleted_count += 1
                    deleted_bytes += size
                except OSError:
                    pass
                continue
            files.append((stat.st_mtime, stat.st_size, path))

        total_bytes = sum(size for _, size, _ in files)
        if max_bytes > 0 and total_bytes > max_bytes:
            for mtime, size, path in sorted(files, key=lambda item: item[0]):
                if total_bytes <= max_bytes:
                    break
                if now - mtime < min_delete_age_seconds:
                    continue
                try:
                    path.unlink(missing_ok=True)
                    deleted_count += 1
                    deleted_bytes += size
                    total_bytes -= size
                except OSError:
                    pass

        return deleted_count, deleted_bytes, max(0, total_bytes)

    def _uid(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _gid(self, event: AstrMessageEvent) -> str:
        gid = event.get_group_id()
        return f"group_{gid}" if gid else f"private_{event.get_sender_id()}"

    def _remember_cat_notify_target(self, event: AstrMessageEvent) -> None:
        try:
            self.catgirl.update_notify_target(
                self._uid(event),
                str(getattr(event, "unified_msg_origin", "") or ""),
                str(event.get_platform_name() or ""),
            )
        except Exception:
            pass

    def _name(self, event: AstrMessageEvent) -> str:
        try:
            return event.get_sender_name()
        except Exception:
            return str(event.get_sender_id())

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        uid = str(event.get_sender_id())
        if uid in self.extra_admin_ids:
            return True
        for attr in ("is_admin", "is_superuser"):
            checker = getattr(event, attr, None)
            if callable(checker):
                try:
                    if checker():
                        return True
                except Exception:
                    pass
        try:
            role = str(getattr(event, "role", "") or getattr(event, "sender_role", "")).lower()
            return role in ("admin", "administrator", "owner", "superuser")
        except Exception:
            return False

    def _extract_at_uid(self, event: AstrMessageEvent) -> Optional[str]:
        def pick_target(seg) -> Optional[str]:
            seg_type = str(getattr(seg, "type", "")).lower()
            class_name = seg.__class__.__name__.lower()
            if seg_type not in ("at", "componenttype.at") and class_name != "at":
                return None

            target = None
            for attr in ("qq", "target", "user_id", "id"):
                target = getattr(seg, attr, None)
                if target:
                    break
            if target is None:
                data = getattr(seg, "data", {}) or {}
                if isinstance(data, dict):
                    target = data.get("qq") or data.get("target") or data.get("user_id") or data.get("id")

            target_str = str(target or "").strip()
            if not target_str or target_str == "all":
                return None
            if target_str == str(event.get_self_id()):
                return None
            return target_str

        try:
            for seg in event.get_messages():
                target = pick_target(seg)
                if target:
                    return target
        except Exception:
            pass

        try:
            msg = event.message_obj.message
            for seg in msg:
                target = pick_target(seg)
                if target:
                    return target
        except Exception:
            pass
        text = event.message_str or ""
        self_id = str(event.get_self_id())
        for target in re.findall(r"@(\d+)", text):
            if target != self_id:
                return target
        return None

    def _extract_first_image(self, event: AstrMessageEvent) -> Optional[str]:
        def pick_from_data(data: dict):
            if not isinstance(data, dict):
                return None
            return (
                data.get("url")
                or data.get("path")
                or data.get("src")
                or data.get("file")
                or data.get("file_id")
            )

        try:
            msg = event.message_obj.message
            for seg in msg:
                if isinstance(seg, dict):
                    seg_type = str(seg.get("type", "")).lower()
                    data = seg.get("data", {}) or {}
                else:
                    seg_type = str(getattr(seg, "type", "") or seg.__class__.__name__).lower()
                    data = getattr(seg, "data", None) or {
                        "url": getattr(seg, "url", None),
                        "file": getattr(seg, "file", None),
                        "path": getattr(seg, "path", None),
                        "src": getattr(seg, "src", None),
                        "file_id": getattr(seg, "file_id", None),
                    }

                if "image" in seg_type or seg_type in ("图片",):
                    result = pick_from_data(data)
                    if result:
                        return str(result)
        except Exception:
            pass

        raw = str(getattr(event.message_obj, "raw_message", "") or event.message_str or "")
        match = re.search(r"\[CQ:image,[^\]]*(?:url|file)=([^,\]]+)", raw)
        return match.group(1) if match else None


    def _mixed_result(self, event: AstrMessageEvent, text: str, img_path: Optional[Path] = None):
        if img_path:
            try:
                return event.image_result(str(img_path))
            except Exception:
                pass
        return event.plain_result(text)

    def _after_care_action(
        self,
        uid: str,
        anchor: str,
        ok: bool,
        wish_anchor: str | None = None,
        wish_target_name: str | None = None,
    ) -> None:
        """养成行为成功后的统一钩子：累加每日心愿进度。

        心愿系统是事后统计，不影响主流程；失败只记日志。
        """
        if not ok or not getattr(self, "daily_wishes", None):
            return
        try:
            self.daily_wishes.progress_daily_wish(uid, wish_anchor or anchor, 1, target_name=wish_target_name)
        except Exception as exc:
            logger.warning(f"[NekoCare] 心愿进度更新失败 uid={uid} anchor={wish_anchor or anchor}: {exc}", exc_info=True)

    def _purchase_wish_anchor(self, query: str) -> str:
        return self._purchase_wish_detail(query)[0]

    def _purchase_wish_detail(self, query: str) -> tuple[str, str]:
        try:
            item_query, _ = self.catgirl._parse_item_quantity(str(query or "").strip())
            item, _ = self.catgirl._find_named(self.catgirl._shop_items(), item_query)
            if isinstance(item, dict) and str(item.get("effect", "")) == "gift":
                return "buy_gift", str(item.get("name", "") or item_query)
        except Exception:
            pass
        return "buy", str(query or "").strip()

    async def _auto_finalize_adoption(self, uid: str, token: str, gid: str):
        await asyncio.sleep(120)
        key = uid
        pending = self._pending_adoptions.get(key)
        if not pending or pending.get("token") != token:
            return
        self.catgirl.finalize_adoption(gid, uid, choice="first")
        self._pending_adoptions.pop(key, None)

    @neko_command("猫猫帮助", alias={"猫娘帮助"}, desc="查看猫娘养成插件的完整帮助、指令分组和当前养成规则。用法：猫猫帮助")
    async def catgirl_help(self, event: AstrMessageEvent):
        coin_name = self._coin_name()
        wish_probability, wish_pity, appearance_price = self._wish_rules()
        care = self._care_rules()
        feed_limit = care.get("feed_satiety_limit", 85)
        satiety_minutes = int(float(care.get("satiety_decay_minutes", 2880)))
        runaway_hours = float(care.get("runaway_after_zero_hours", 168))
        help_sections = [
            ("基础经济", [
                ("签到 / 猫猫签到", "每日领取宝石"),
                ("每日打工", "主人每日收入"),
                ("查看猫猫钱包", "查看余额"),
                ("钱包转账 数量 @用户", "转出宝石"),
            ]),
            ("收养与状态", [
                ("请赐我一只可爱猫娘吧", "每日许愿"),
                ("带她回家 / 确认收养", "确认候选"),
                ("换一只猫娘 / 换个形象", "切换候选"),
                ("成长档案 / 猫猫状态", "状态档案"),
            ]),
            ("照顾与互动", [
                ("猫娘心愿 / 今日心愿", "查看今日小心愿"),
                ("喂猫 [食物名] / 喂猫猫", "喂食恢复，可指定食物"),
                ("撸猫 / 逗猫 / 摸猫", "内置互动"),
                ("rua猫 / 陪猫娘", "内置互动"),
                ("猫娘互动 动作名", "自定义互动"),
            ]),
            ("猫娘打工", [
                ("猫娘打工", "领取或随机打工"),
                ("猫娘打工 地点名", "指定地点"),
                ("猫娘打工 列表", "查看全量地点"),
                ("猫娘打工 解锁 地点名", "解锁地点"),
            ]),
            ("商店与护理", [
                ("猫娘商店 [分类]", "查看道具"),
                ("购买 道具名 [数量]", "购买道具"),
                ("背包 / 猫娘背包", "查看道具"),
                ("使用 道具名", "使用道具"),
                ("猫娘护理 [服务名]", "购买护理"),
            ]),
            ("档案与排行", [
                ("猫娘改名 名字", "消耗改名卡"),
                ("更换猫娘形象", "上传新图片"),
                ("羁绊排行榜", "本群排行"),
                ("钱包排行榜", "余额排行"),
                ("迁移猫娘到本群", "登记群排行"),
            ]),
            ("管理员", [
                ("管理员给 数量 @用户", "增加宝石"),
                ("管理员扣 数量 @用户", "扣除宝石"),
                ("管理员查看 @用户", "查看余额"),
                ("管理员清理缓存", "清理生成图片"),
            ]),
        ]
        text = (
            "猫猫小助手来啦 ฅ^•ﻌ•^ฅ\n\n"
            "指令说明：\n"
            + "\n".join(
                f"{title}\n" + "\n".join(f"- {command}：{desc}" for command, desc in rows)
                for title, rows in help_sections
            ) + "\n\n"
            "许愿说明：\n"
            f"每天许愿有 {int(wish_probability * 100)}% 概率遇见猫娘，{wish_pity} 次内必定成功喔～\n\n"
            "喂养说明：\n"
            f"状态按分钟结算，饱食度低于 {round(feed_limit)} 时可以喂猫。\n"
            f"饱食度约 {round(satiety_minutes / 60)} 小时从 100 降到 0，归零超过 {round(runaway_hours)} 小时会离家出走。"
        )
        card = self.catgirl.draw_section_card(
            "猫猫帮助",
            subtitle="指令说明与用法",
            sections=help_sections,
            metrics=[
                ("许愿概率", f"{int(wish_probability * 100)}%"),
                ("许愿保底", f"{wish_pity} 次"),
                ("喂食阈值", f"{round(feed_limit)}"),
                ("饱食清零", f"约 {round(satiety_minutes / 60)} 小时"),
                ("离家倒计时", f"{round(runaway_hours)} 小时"),
                ("形象价格", f"{appearance_price} {coin_name}"),
            ],
            footer="状态相关输出、帮助菜单、商店、背包、护理和打工列表均使用图片展示。",
            tag="help",
        )
        yield self._mixed_result(event, text, card)

    @neko_command("查看猫猫钱包", alias={"查看猫娘钱包"}, desc="查询自己的宝石余额。用法：查看猫猫钱包")
    async def my_wallet(self, event: AstrMessageEvent):
        uid = self._uid(event)
        bal = self.economy.get_balance(uid)
        yield event.plain_result(f"你的小钱包里有 {bal} {self._coin_name()} 喔～")

    @neko_command("钱包转账", desc="把自己的宝石转给指定用户，需要 @ 对方。用法：钱包转账 数量 @用户")
    async def wallet_transfer(self, event: AstrMessageEvent, amount: str):
        uid = self._uid(event)
        target = self._extract_at_uid(event)
        if not target:
            yield event.plain_result("要 @ 想转账的小伙伴喔～")
            return
        amount_text = str(amount or "").strip()
        if not re.fullmatch(r"\d+", amount_text):
            yield event.plain_result("转账数量必须是纯数字喔～")
            return
        amount_int = int(amount_text)
        ok, msg = self.economy.transfer(uid, target, amount_int)
        yield event.plain_result(msg)

    @neko_command("每日打工", desc="主人每日打工一次，领取随机宝石收入。用法：每日打工")
    async def daily_work(self, event: AstrMessageEvent):
        uid = self._uid(event)
        ok, msg = self.economy.daily_work(uid)
        self._after_care_action(uid, "daily_work", ok)
        yield event.plain_result(msg)

    @neko_command("签到", alias={"猫猫签到"}, desc="每日签到领取宝石奖励，按配置返回图片或文字签到卡。用法：签到")
    async def sign_entry(self, event: AstrMessageEvent):
        uid = self._uid(event)
        name = self._name(event)
        ok, data_or_msg = self.sign.sign(uid, name)
        if not ok:
            yield event.plain_result(data_or_msg)
            return

        data = data_or_msg
        self._after_care_action(uid, "sign", True)
        event_line = ""
        if data.get("event_text"):
            event_line = f"\n\n✦ 今日奇遇：{data.get('event_text')}"
        if self.sign_mode == "图片签到":
            try:
                img_path = self.sign.draw_sign(uid, name, data["inc"], data["balance"], data["count"], data.get("quote", ""), data.get("quote_from", ""))
                yield event.image_result(str(img_path))
                if event_line:
                    yield event.plain_result(event_line.strip())
                return
            except Exception as e:
                coin_name = self._coin_name()
                yield event.plain_result(f"签到成功啦，但图片生成失败：{e}\n你获得了 {data['inc']} {coin_name}，现在有 {data['balance']} {coin_name} 喔～{event_line}")
                return

        quote_line = data.get("quote", "")
        quote_from = data.get("quote_from", "")
        if quote_from:
            quote_line = f"{quote_line}\n—— {quote_from}"
        coin_name = self._coin_name()
        yield event.plain_result(f"签到成功喵～ ฅ^•ﻌ•^ฅ\n今天捡到了 {data['inc']} {coin_name}！\n小钱包里现在有 {data['balance']} {coin_name} 啦～\n\n今日一言：\n{quote_line}{event_line}")

    @neko_command("请赐我一只可爱猫娘吧", desc="每日许愿遇见猫娘，成功后可确认收养或换一只候选。用法：请赐我一只可爱猫娘吧")
    async def wish_catgirl(self, event: AstrMessageEvent):
        uid = self._uid(event)
        gid = self._gid(event)
        ok, status, msg, first, second = self.catgirl.prepare_wish(uid, gid)
        if not ok:
            yield event.plain_result(msg)
            return

        token = f"{time.time()}:{random.random()}"
        self._pending_adoptions[uid] = {
            "token": token,
            "uid": uid,
            "gid": gid,
            "first": first,
            "second": second,
            "expire": time.time() + 120,
        }
        self._track_background_task(
            self._auto_finalize_adoption(uid, token, gid),
            "neko_care_auto_finalize_adoption",
        )
        img = self.catgirl.draw_wish_card(first)
        yield self._mixed_result(event, msg, img)

    @neko_command("带她回家", alias={"确认收养", "换一只猫娘", "换个形象"}, desc="确认收养当前候选猫娘；也可切换到另一只候选。用法：带她回家 / 换一只猫娘")
    async def confirm_catgirl_adoption(self, event: AstrMessageEvent):
        uid = self._uid(event)
        gid = self._gid(event)
        raw = (event.message_str or "").strip()
        pending = self._pending_adoptions.get(uid) or self.catgirl.get_pending_adoption(uid)
        if not pending:
            return

        choice = "second" if raw in ("换一只猫娘", "换个形象") else "first"
        ok, msg, img = self.catgirl.finalize_adoption(gid, uid, choice=choice)
        self._remember_cat_notify_target(event)
        self._pending_adoptions.pop(uid, None)
        yield self._mixed_result(event, msg, img)

    @neko_command("成长档案", alias={"猫娘状态", "猫猫状态", "猫猫档案"}, desc="查看自己的猫娘档案，包括阶段、亲密、饱食、心情、健康和精力。用法：成长档案")
    async def catgirl_status(self, event: AstrMessageEvent):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        ok, msg, img_path = self.catgirl.status(uid)
        yield self._mixed_result(event, msg, img_path)

    @neko_command("喂猫", alias={"喂猫娘", "喂猫猫"}, desc="给猫娘自动购买并喂食，恢复饱食并提升养成属性。用法：喂猫 [食物名]")
    async def feed_catgirl(self, event: AstrMessageEvent, food_name: GreedyStr):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        result = self.catgirl.feed(uid, str(food_name or "").strip())
        if len(result) == 4:
            ok, msg, img_path, detail = result
        else:
            ok, msg, img_path = result
            detail = {}
        self._after_care_action(uid, "feed", ok, wish_target_name=str((detail or {}).get("food", "") or ""))
        yield self._mixed_result(event, msg, img_path)

    @neko_command("猫娘打工", alias={"猫猫打工", "打工"}, desc="派猫娘打工，支持领取完成任务、指定地点、查看列表和解锁地点。用法：猫娘打工 [地点名|列表|解锁 地点名]")
    async def catgirl_work(self, event: AstrMessageEvent, job_name: GreedyStr):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        result = self.catgirl.work(uid, str(job_name or "").strip())
        if len(result) >= 5:
            ok, msg, img, event_type, detail = result
        elif len(result) == 4:
            ok, msg, img, event_type = result
            detail = {}
        else:
            ok, msg, img = result
            event_type = ""
            detail = {}
        self._after_care_action(
            uid,
            "cat_work",
            ok and event_type == "finished",
            wish_target_name=str((detail or {}).get("job_name", "") or ""),
        )
        yield self._mixed_result(event, msg, img)

    @neko_command("猫娘商店", alias={"猫猫商店"}, desc="查看猫娘商店，可按分类浏览食物、礼物、道具和功能卡。用法：猫娘商店 [分类]")
    async def catgirl_shop(self, event: AstrMessageEvent, category: GreedyStr):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        ok, msg, img = self.catgirl.shop(uid, str(category or "").strip())
        yield self._mixed_result(event, msg, img)

    @neko_command("背包", alias={"猫娘背包", "猫猫背包"}, desc="查看自己的背包物品、数量和待生效加成。用法：背包")
    async def catgirl_bag(self, event: AstrMessageEvent):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        ok, msg, img = self.catgirl.bag(uid)
        yield self._mixed_result(event, msg, img)

    @neko_command("购买", alias={"购买道具", "猫娘购买"}, desc="消耗宝石购买商店道具，数量可省略。用法：购买 道具名 [数量]")
    async def catgirl_buy(self, event: AstrMessageEvent, item_name: GreedyStr):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        query = str(item_name or "").strip()
        ok, msg, img = self.catgirl.buy_item(uid, query)
        wish_anchor, wish_target_name = self._purchase_wish_detail(query)
        self._after_care_action(uid, "buy", ok, wish_anchor=wish_anchor, wish_target_name=wish_target_name)
        yield self._mixed_result(event, msg, img)

    @neko_command("使用", alias={"使用道具", "猫娘使用"}, desc="使用背包中的道具；部分功能卡会在对应操作时自动消耗。用法：使用 道具名")
    async def catgirl_use_item(self, event: AstrMessageEvent, item_name: GreedyStr):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        result = self.catgirl.use_item(uid, str(item_name or "").strip())
        if len(result) == 4:
            ok, msg, img, detail = result
        else:
            ok, msg, img = result
            detail = {}
        self._after_care_action(uid, "use", ok and not bool((detail or {}).get("hint_only")))
        yield self._mixed_result(event, msg, img)

    @neko_command("猫娘护理", alias={"猫猫护理", "护理猫娘", "猫娘看病"}, desc="查看或购买护理服务，用于恢复健康、心情、精力或饱食。用法：猫娘护理 [服务名]")
    async def catgirl_care_service(self, event: AstrMessageEvent, service_name: GreedyStr):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        query = str(service_name or "").strip()
        result = self.catgirl.care_service(uid, query)
        if len(result) == 4:
            ok, msg, img, detail = result
        else:
            ok, msg, img = result
            detail = {}
        self._after_care_action(
            uid,
            "care",
            ok and bool((detail or {}).get("service")),
            wish_target_name=str((detail or {}).get("service", "") or query),
        )
        yield self._mixed_result(event, msg, img)

    @neko_command("撸猫", alias={"逗猫", "摸猫", "rua猫", "陪猫娘", "陪猫猫", "贴贴猫娘", "贴贴猫猫"}, desc="进行内置互动，消耗精力并提升心情、亲密和成长，受冷却与每日递减影响。用法：撸猫")
    async def interact_catgirl(self, event: AstrMessageEvent):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        action = (event.message_str or "").strip()
        ok, msg, img = self.catgirl.interact(uid, action)
        self._after_care_action(uid, "interact", ok, wish_target_name=action)
        yield self._mixed_result(event, msg, img)

    @neko_command("猫娘互动", alias={"猫猫互动"}, desc="触发 Pages 中配置的自定义互动动作。用法：猫娘互动 动作名")
    async def interact_catgirl_custom(self, event: AstrMessageEvent, action: GreedyStr):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        action = str(action or "").strip()
        if not action:
            yield event.plain_result("请输入要进行的互动动作。")
            return
        ok, msg, img = self.catgirl.interact(uid, action)
        self._after_care_action(uid, "interact", ok, wish_target_name=action)
        yield self._mixed_result(event, msg, img)

    @neko_command("猫娘改名", desc="消耗 1 张改名卡修改猫娘名字。用法：猫娘改名 新名字")
    async def rename_catgirl(self, event: AstrMessageEvent, name: GreedyStr):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        ok, msg, img = self.catgirl.rename(uid, name)
        yield self._mixed_result(event, msg, img)

    @neko_command("更换猫娘形象", alias={"更换猫猫形象"}, desc="更换猫娘图片；可直接带图，或先发指令后 2 分钟内发图。用法：更换猫娘形象")
    async def change_catgirl_image(self, event: AstrMessageEvent):

        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        image_src = self._extract_first_image(event)

        if not self.catgirl.has_catgirl(uid):
            yield event.plain_result(self.catgirl.missing_cat_message(uid))
            return

        if not image_src:
            token = f"{time.time()}:{random.random()}"
            self._pending_image_changes[uid] = {"token": token, "expire": time.time() + 120}
            _, _, appearance_price = self._wish_rules()
            yield event.plain_result(f"请在 2 分钟内发送新的猫娘图片～\n成功更换时会优先消耗 1 张形象更改卡；没有卡时扣除 {appearance_price} {self._coin_name()}。")
            return

        ok, msg, img = await self.catgirl.change_image(uid, image_src)
        yield self._mixed_result(event, msg, img)

    @filter.custom_filter(pending_image_filter(), False)
    async def pending_image_listener(self, event: AstrMessageEvent):
        uid = self._uid(event)
        if uid not in self._pending_image_changes:
            return
        pending = self._pending_image_changes.get(uid)
        if not pending:
            return
        if time.time() > float(pending.get("expire", 0)):
            self._pending_image_changes.pop(uid, None)
            return

        image_src = self._extract_first_image(event)
        if not image_src:
            return

        self._pending_image_changes.pop(uid, None)
        ok, msg, img = await self.catgirl.change_image(uid, image_src)
        yield self._mixed_result(event, msg, img)

    @neko_command("猫娘心愿", alias={"猫猫心愿", "今日心愿"}, desc="查看猫娘今天的小心愿，完成对应养成行为后自动发放奖励。用法：猫娘心愿")
    async def daily_wish_status(self, event: AstrMessageEvent):
        uid = self._uid(event)
        self._remember_cat_notify_target(event)
        ok, msg, img = self.daily_wishes.get_daily_wish(uid)
        yield self._mixed_result(event, msg, img)

    @neko_command("羁绊排行榜", alias={"猫娘排行榜", "猫猫排行榜"}, desc="查看当前群的猫娘羁绊排行榜。用法：羁绊排行榜")
    async def catgirl_rank(self, event: AstrMessageEvent):
        gid = self._gid(event)
        img = self.catgirl.draw_rank(gid)
        if not img:
            yield event.plain_result("本群还没有登记猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。")
            return
        yield event.image_result(str(img))

    @neko_command("迁移猫娘到本群", alias={"猫娘迁移"}, desc="把自己的猫娘登记到当前群，用于参与本群羁绊排行榜。用法：迁移猫娘到本群")
    async def migrate_catgirl_to_group(self, event: AstrMessageEvent):
        uid = self._uid(event)
        gid = self._gid(event)
        ok, msg, img = self.catgirl.migrate_to_group(gid, uid)
        self._remember_cat_notify_target(event)
        yield self._mixed_result(event, msg, img)

    @neko_command("钱包排行榜", desc="查看全局宝石余额排行榜 TOP 10。用法：钱包排行榜")
    async def wallet_rank(self, event: AstrMessageEvent):
        rows = self.economy.wallet_rank(10)
        if not rows:
            yield event.plain_result("还没有人有钱钱喔～")
            return
        coin_name = self._coin_name()
        lines = [f"💰 {coin_name}排行榜 TOP 10\n"]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['uid']}: {row['balance']} {coin_name}")
        yield event.plain_result("\n".join(lines))

    @neko_command("管理员给", desc="管理员指令，给指定用户增加宝石，需要 @ 对方。用法：管理员给 数量 @用户")
    async def admin_give(self, event: AstrMessageEvent, amount: int):
        if not self._is_admin(event):
            return
        if amount <= 0:
            yield event.plain_result("金额要大于 0。")
            return
        target = self._extract_at_uid(event)
        if not target:
            yield event.plain_result("要 @ 目标用户喔～")
            return
        self.economy.add_balance(target, amount)
        yield event.plain_result(f"已给 {target} 添加 {amount} {self._coin_name()}。")

    @neko_command("管理员扣", desc="管理员指令，扣除指定用户宝石，需要 @ 对方。用法：管理员扣 数量 @用户")
    async def admin_deduct(self, event: AstrMessageEvent, amount: int):
        if not self._is_admin(event):
            return
        if amount <= 0:
            yield event.plain_result("金额要大于 0。")
            return
        target = self._extract_at_uid(event)
        if not target:
            yield event.plain_result("要 @ 目标用户喔～")
            return
        self.economy.add_balance(target, -amount)
        yield event.plain_result(f"已从 {target} 扣除 {amount} {self._coin_name()}。")

    @neko_command("管理员查看", desc="管理员指令，查看指定用户宝石余额，需要 @ 对方。用法：管理员查看 @用户")
    async def admin_check(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return
        target = self._extract_at_uid(event)
        if not target:
            yield event.plain_result("要 @ 目标用户喔～")
            return
        bal = self.economy.get_balance(target)
        yield event.plain_result(f"用户 {target} 当前余额：{bal} {self._coin_name()}")

    @neko_command("管理员清理缓存", desc="管理员指令，按配置清理插件生成的缓存图片并返回释放空间。用法：管理员清理缓存")
    async def admin_cleanup_cache(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return
        deleted_count, deleted_bytes, remaining_bytes = await self._cleanup_cache_files()
        yield event.plain_result(
            "缓存清理完成。\n"
            f"删除图片：{deleted_count} 个\n"
            f"释放空间：{deleted_bytes / 1024 / 1024:.2f} MB\n"
            f"剩余缓存：{remaining_bytes / 1024 / 1024:.2f} MB"
        )

    async def terminate(self):
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
