import asyncio
import random
import re
import time
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core.star.filter.command import GreedyStr

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
        awaitable = filter.custom_filter(keyword_command_filter(*command_names), False)(awaitable)
        return filter.command(command_name, alias=alias, **kwargs)(awaitable)

    return decorator

@register("astrbot_plugin_neko_care", "若梦和我一起", "猫娘羁绊养成、签到打工", "1.3.0")
class SapphireEconomyPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        self.coin_name = str(self.config.get("coin_name", "宝石"))[:16] or "宝石"
        self.sign_mode = "图片签到" if self.config.get("sign_mode", "图片签到") in ("图片签到", "image") else "文字签到"
        self.keyword_trigger_enabled = bool(self.config.get("keyword_trigger_enabled", True))
        global KEYWORD_TRIGGER_ENABLED
        KEYWORD_TRIGGER_ENABLED = self.keyword_trigger_enabled

        self.sign_min = max(0, int(self.config.get("sign_min_reward", 80)))
        self.sign_max = max(self.sign_min, int(self.config.get("sign_max_reward", 150)))
        self.work_min = max(0, int(self.config.get("work_min_reward", 50)))
        self.work_max = max(self.work_min, int(self.config.get("work_max_reward", 120)))

        self.extra_admin_ids = set(str(x) for x in self.config.get("extra_admin_ids", []))
        self.wish_probability = min(1.0, max(0.0, float(self.config.get("catgirl_wish_probability", 0.8))))
        self.wish_pity = max(1, int(self.config.get("catgirl_wish_pity", 3)))
        self.appearance_change_price = max(0, int(self.config.get("appearance_change_price", 1200)))

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
            self.runtime_config.snapshot
        )
        self.page_api = None
        self._register_page_api_if_available()

        self._pending_adoptions = {}
        global PENDING_IMAGE_CHANGES
        self._pending_image_changes = PENDING_IMAGE_CHANGES
        self._background_tasks = set()

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

    def _uid(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _gid(self, event: AstrMessageEvent) -> str:
        gid = event.get_group_id()
        return f"group_{gid}" if gid else f"private_{event.get_sender_id()}"

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
        try:
            msg = event.message_obj.message
            for seg in msg:
                seg_type = str(getattr(seg, "type", "")).lower()
                data = getattr(seg, "data", {}) or {}
                if seg_type == "at":
                    target = data.get("qq") or data.get("user_id") or data.get("id")
                    if target and str(target) != "all":
                        return str(target)
        except Exception:
            pass
        text = event.message_str or ""
        m = re.search(r"@(\d+)", text)
        return m.group(1) if m else None

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

    async def _auto_finalize_adoption(self, uid: str, token: str, gid: str):
        await asyncio.sleep(120)
        key = uid
        pending = self._pending_adoptions.get(key)
        if not pending or pending.get("token") != token:
            return
        self.catgirl.finalize_adoption(gid, uid, choice="first")
        self._pending_adoptions.pop(key, None)

    @neko_command("猫猫帮助", alias={"猫娘帮助"})
    async def catgirl_help(self, event: AstrMessageEvent):
        coin_name = self._coin_name()
        wish_probability, wish_pity, appearance_price = self._wish_rules()
        care = self._care_rules()
        feed_limit = care.get("feed_satiety_limit", 85)
        satiety_minutes = int(float(care.get("satiety_decay_minutes", 2880)))
        runaway_hours = float(care.get("runaway_after_zero_hours", 24))
        text = (
            "猫猫小助手来啦 ฅ^•ﻌ•^ฅ\n\n"
            "常用指令：\n"
            "1. 签到 / 猫猫签到\n"
            "2. 查看猫猫钱包 / 查看猫娘钱包\n"
            "3. 请赐我一只可爱猫娘吧\n"
            "4. 成长档案 / 猫娘状态 / 猫猫状态\n"
            "5. 喂猫 / 喂猫娘 / 喂猫猫\n"
            "6. 猫娘打工 / 猫猫打工 / 打工（可加地点名；猫娘打工 列表）\n"
            "7. 撸猫 / 逗猫 / 摸猫 / rua猫 / 陪猫娘\n"
            "   自定义互动：猫娘互动 动作名\n"
            "8. 猫娘改名 名字\n"
            f"9. 更换猫娘形象 + 图片（{appearance_price} {coin_name}）\n"
            "10. 羁绊排行榜 / 猫娘排行榜\n"
            "许愿说明：\n"
            f"每天许愿有 {int(wish_probability * 100)}% 概率遇见猫娘，{wish_pity} 次内必定成功喔～\n\n"
            "喂养说明：\n"
            f"状态按分钟结算，饱食度低于 {round(feed_limit)} 时可以喂猫。\n"
            f"饱食度约 {round(satiety_minutes / 60)} 小时从 100 降到 0，归零超过 {round(runaway_hours)} 小时会离家出走。"
        )
        yield event.plain_result(text)

    @neko_command("查看猫猫钱包", alias={"查看猫娘钱包"})
    async def my_wallet(self, event: AstrMessageEvent):
        uid = self._uid(event)
        bal = self.economy.get_balance(uid)
        yield event.plain_result(f"你的小钱包里有 {bal} {self._coin_name()} 喔～")

    @neko_command("钱包转账")
    async def wallet_transfer(self, event: AstrMessageEvent, amount: int):
        uid = self._uid(event)
        target = self._extract_at_uid(event)
        if not target:
            yield event.plain_result("要 @ 想转账的小伙伴喔～")
            return
        ok, msg = self.economy.transfer(uid, target, amount)
        yield event.plain_result(msg)

    @neko_command("每日打工")
    async def daily_work(self, event: AstrMessageEvent):
        uid = self._uid(event)
        ok, msg = self.economy.daily_work(uid)
        yield event.plain_result(msg)

    @neko_command("签到", alias={"猫猫签到"})
    async def sign_entry(self, event: AstrMessageEvent):
        uid = self._uid(event)
        name = self._name(event)
        ok, data_or_msg = self.sign.sign(uid, name)
        if not ok:
            yield event.plain_result(data_or_msg)
            return

        data = data_or_msg
        if self.sign_mode == "图片签到":
            try:
                img_path = self.sign.draw_sign(uid, name, data["inc"], data["balance"], data["count"], data.get("quote", ""), data.get("quote_from", ""))
                yield event.image_result(str(img_path))
                return
            except Exception as e:
                coin_name = self._coin_name()
                yield event.plain_result(f"签到成功啦，但图片生成失败：{e}\n你获得了 {data['inc']} {coin_name}，现在有 {data['balance']} {coin_name} 喔～")
                return

        quote_line = data.get("quote", "")
        quote_from = data.get("quote_from", "")
        if quote_from:
            quote_line = f"{quote_line}\n—— {quote_from}"
        coin_name = self._coin_name()
        yield event.plain_result(f"签到成功喵～ ฅ^•ﻌ•^ฅ\n今天捡到了 {data['inc']} {coin_name}！\n小钱包里现在有 {data['balance']} {coin_name} 啦～\n\n今日一言：\n{quote_line}")

    @neko_command("请赐我一只可爱猫娘吧")
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
        task = asyncio.create_task(self._auto_finalize_adoption(uid, token, gid))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        img = self.catgirl.draw_wish_card(first)
        yield self._mixed_result(event, msg, img)

    @neko_command("带她回家", alias={"确认收养", "换一只猫娘", "换个形象"})
    async def confirm_catgirl_adoption(self, event: AstrMessageEvent):
        uid = self._uid(event)
        gid = self._gid(event)
        raw = (event.message_str or "").strip()
        pending = self._pending_adoptions.get(uid) or self.catgirl.get_pending_adoption(uid)
        if not pending:
            return

        choice = "second" if raw in ("换一只猫娘", "换个形象") else "first"
        ok, msg, img = self.catgirl.finalize_adoption(gid, uid, choice=choice)
        self._pending_adoptions.pop(uid, None)
        yield self._mixed_result(event, msg, img)

    @neko_command("成长档案", alias={"猫娘状态", "猫猫状态", "猫猫档案"})
    async def catgirl_status(self, event: AstrMessageEvent):
        uid = self._uid(event)
        ok, msg, img_path = self.catgirl.status(uid)
        yield self._mixed_result(event, msg, img_path)

    @neko_command("喂猫", alias={"喂猫娘", "喂猫猫"})
    async def feed_catgirl(self, event: AstrMessageEvent):
        uid = self._uid(event)
        ok, msg, img_path = self.catgirl.feed(uid)
        yield self._mixed_result(event, msg, img_path)

    @neko_command("猫娘打工", alias={"猫猫打工", "打工"})
    async def catgirl_work(self, event: AstrMessageEvent, job_name: GreedyStr):
        uid = self._uid(event)
        ok, msg, img = self.catgirl.work(uid, str(job_name or "").strip())
        yield self._mixed_result(event, msg, img)

    @neko_command("撸猫", alias={"逗猫", "摸猫", "rua猫", "陪猫娘", "陪猫猫", "贴贴猫娘", "贴贴猫猫"})
    async def interact_catgirl(self, event: AstrMessageEvent):
        uid = self._uid(event)
        action = (event.message_str or "").strip()
        ok, msg, img = self.catgirl.interact(uid, action)
        yield self._mixed_result(event, msg, img)

    @neko_command("猫娘互动", alias={"猫猫互动"})
    async def interact_catgirl_custom(self, event: AstrMessageEvent, action: GreedyStr):
        uid = self._uid(event)
        action = str(action or "").strip()
        if not action:
            yield event.plain_result("请输入要进行的互动动作。")
            return
        ok, msg, img = self.catgirl.interact(uid, action)
        yield self._mixed_result(event, msg, img)

    @neko_command("猫娘改名")
    async def rename_catgirl(self, event: AstrMessageEvent, name: GreedyStr):
        uid = self._uid(event)
        ok, msg, img = self.catgirl.rename(uid, name)
        yield self._mixed_result(event, msg, img)

    @neko_command("更换猫娘形象", alias={"更换猫猫形象"})
    async def change_catgirl_image(self, event: AstrMessageEvent):

        uid = self._uid(event)
        image_src = self._extract_first_image(event)

        if not self.catgirl.has_catgirl(uid):
            yield event.plain_result("你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。")
            return

        if not image_src:
            token = f"{time.time()}:{random.random()}"
            self._pending_image_changes[uid] = {"token": token, "expire": time.time() + 120}
            _, _, appearance_price = self._wish_rules()
            yield event.plain_result(f"请在 2 分钟内发送新的猫娘图片～\n成功更换后将扣除 {appearance_price} {self._coin_name()}。")
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


    @neko_command("羁绊排行榜", alias={"猫娘排行榜", "猫猫排行榜"})
    async def catgirl_rank(self, event: AstrMessageEvent):
        gid = self._gid(event)
        img = self.catgirl.draw_rank(gid)
        if not img:
            yield event.plain_result("本群还没有登记猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。")
            return
        yield event.image_result(str(img))

    @neko_command("迁移猫娘到本群", alias={"猫娘迁移"})
    async def migrate_catgirl_to_group(self, event: AstrMessageEvent):
        uid = self._uid(event)
        gid = self._gid(event)
        ok, msg, img = self.catgirl.migrate_to_group(gid, uid)
        yield self._mixed_result(event, msg, img)

    @neko_command("钱包排行榜")
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

    @neko_command("管理员给")
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

    @neko_command("管理员扣")
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

    @neko_command("管理员查看")
    async def admin_check(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return
        target = self._extract_at_uid(event)
        if not target:
            yield event.plain_result("要 @ 目标用户喔～")
            return
        bal = self.economy.get_balance(target)
        yield event.plain_result(f"用户 {target} 当前余额：{bal} {self._coin_name()}")
