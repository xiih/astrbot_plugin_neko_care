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
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .storage import JsonStore
from .economy import EconomyService


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def now_ts() -> int:
    return int(time.time())


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_IMAGE_WIDTH = 4096
MAX_IMAGE_HEIGHT = 4096
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def current_feed_slot() -> Optional[str]:
    h = datetime.now().hour
    if 5 <= h < 12:
        return "morning"
    if h >= 12 or h < 2:
        return "afternoon"
    return None


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

    def _get(self, uid: str) -> Optional[Dict]:
        return self.store.get("catgirls", uid, default=None)

    def _save(self, uid: str, data: Dict):
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
        return {
            "user": uid,
            "name": random.choice(names),
            "weight": 5.0,
            "satiety": 80.0,
            "mood": 85,
            "created_at": now_ts(),
            "last_decay": now_ts(),
            "last_feed_date": "",
            "fed_slots": {},
            "last_wish_date": today_str(),
            "wish_count": 0,
            "image": str(img) if img else "",
        }

    def prepare_wish(self, uid: str):
        cat = self._get(uid)
        today = today_str()

        if cat and cat.get("name"):
            return False, "already", f"你已经有猫娘「{cat['name']}」啦，要好好疼她喔～", None, None

        wish_data = self.store.get("sign", uid, default={}) or {}
        if wish_data.get("last_catgirl_wish_date") == today:
            current = int(wish_data.get("catgirl_wish_count", 0))
            return (
                False,
                "cooldown",
                f"今天已经许愿过啦～\n当前许愿进度：{current}/{self.wish_pity}\n每天许愿有 {int(self.wish_probability * 100)}% 概率遇见猫娘，{self.wish_pity} 次一定会有猫娘回应你喔～",
                None,
                None,
            )

        current = int(wish_data.get("catgirl_wish_count", 0)) + 1
        success = random.random() < self.wish_probability or current >= self.wish_pity

        def op(root):
            sign = root.setdefault("sign", {})
            user = sign.setdefault(uid, {})
            user["last_catgirl_wish_date"] = today
            user["catgirl_wish_count"] = 0 if success else current

        self.store.update(op)

        if not success:
            return (
                False,
                "failed",
                f"今天的愿望还没有被猫娘听见……\n当前许愿进度：{current}/{self.wish_pity}\n别灰心喔，{self.wish_pity} 次内一定会有猫娘来找你～",
                None,
                None,
            )

        first = self._new_catgirl(uid)
        second = self._new_catgirl(uid, exclude_image=first.get("image", ""))
        msg = (
            f"✨叮铃铃——许愿成功啦！\n"
            f"一位软乎乎的猫娘听见了你的愿望，悄悄来到了你身边。\n\n"
            f"名字：{first.get('name', '猫娘')}\n"
            f"体重：{float(first.get('weight', 5.0)):.1f} 斤\n"
            f"饱食度：{float(first.get('satiety', 80)):.0f}\n"
            f"心情：{int(first.get('mood', 85))}\n\n"
            f"2 分钟内发送：\n"
            f"「带她回家」或「确认收养」：就让她成为你的猫娘。\n"
            f"「更换猫猫形象」或「换一只」：重新遇见另一位猫娘。\n\n"
            f"如果你害羞不回复，2 分钟后她也会默认跟你回家喔～"
        )
        return True, "pending", msg, first, second

    def finalize_adoption(self, gid: str, uid: str, cat: Dict):
        cat["home_gid"] = gid
        self._save(uid, cat)
        return True, f"收养完成啦～\n猫娘「{cat.get('name', '猫娘')}」轻轻牵住了你的手，以后就住在你的小窝里啦 ฅ^•ﻌ•^ฅ", self.image_path(cat)

    def _weight_floor(self, weight: float) -> float:
        if weight >= 150:
            return 150
        if weight >= 100:
            return 100
        if weight >= 50:
            return 50
        return 5

    def _apply_decay(self, cat: Dict) -> Dict:
        last_decay = int(cat.get("last_decay", now_ts()))
        days = max(0, int((now_ts() - last_decay) // 86400))
        if days <= 0:
            return cat

        cat["satiety"] = max(0.0, float(cat.get("satiety", 0)) - days * 8)

        last_feed_date = cat.get("last_feed_date", "")
        if last_feed_date:
            try:
                last_feed = datetime.strptime(last_feed_date, "%Y-%m-%d")
                no_feed_days = (datetime.now() - last_feed).days
            except Exception:
                no_feed_days = days
        else:
            no_feed_days = days

        if no_feed_days >= 7:
            periods = no_feed_days // 7
            weight = float(cat.get("weight", 5.0))
            floor = self._weight_floor(weight)
            loss = periods * max(0.5, weight * 0.03)
            cat["weight"] = max(floor, weight - loss)
            cat["mood"] = max(0, int(cat.get("mood", 80)) - periods * 5)

        cat["last_decay"] = now_ts()
        return cat

    def _feed_gain(self, weight: float) -> float:
        if weight < 50:
            return 0.5
        if weight < 100:
            return 0.25
        if weight < 150:
            return 0.1
        return 0.03

    def status(self, uid: str) -> Tuple[bool, str, Optional[Path]]:
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", None

        cat = self._apply_decay(cat)
        self._save(uid, cat)
        img = self.image_path(cat)
        msg = f"猫娘「{cat['name']}」现在是这样的喵～\n\n体重：{float(cat.get('weight', 5.0)):.1f} 斤\n饱食度：{float(cat.get('satiety', 0)):.0f}\n心情：{int(cat.get('mood', 0))}"
        return True, msg, img

    def _random_food(self):
        foods = [
            ("草莓奶油蛋糕", 18, 38, "吃"),
            ("热乎乎的蛋包饭", 15, 32, "吃"),
            ("芝士焗饭", 20, 45, "吃"),
            ("炸鸡块", 12, 28, "吃"),
            ("牛奶布丁", 8, 18, "吃"),
            ("珍珠奶茶", 10, 24, "喝"),
            ("抹茶拿铁", 12, 26, "喝"),
            ("可颂面包", 8, 20, "吃"),
            ("巧克力曲奇", 6, 16, "吃"),
            ("海鲜乌冬面", 25, 55, "吃"),
            ("小鱼干便当", 16, 36, "吃"),
            ("奶油蘑菇汤", 12, 30, "喝"),
        ]
        name, low, high, verb = random.choice(foods)
        return name, random.randint(low, high), verb

    def feed(self, uid: str) -> Tuple[bool, str, Optional[Path]]:
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", None

        slot = current_feed_slot()
        if not slot:
            return False, "现在猫娘还不想吃饭饭喔～喂养时间是 05:00-12:00 和 12:00-02:00。", self.image_path(cat)

        today = today_str()
        fed_slots = cat.get("fed_slots", {})
        if fed_slots.get(today, {}).get(slot):
            return False, "这个时间段已经喂过啦～再喂的话小肚子会鼓起来的喵。", self.image_path(cat)

        food, cost, verb = self._random_food()
        balance = self.economy.get_balance(uid)
        if balance < cost:
            return False, f"你想带「{cat['name']}」去{verb}{food}，但是需要 {cost} {self.coin_name}。\n你的小钱包里只有 {balance} {self.coin_name}，不够喔～", self.image_path(cat)

        cat = self._apply_decay(cat)
        weight = float(cat.get("weight", 5.0))
        gain = self._feed_gain(weight)

        cat["weight"] = weight + gain
        cat["satiety"] = min(100.0, float(cat.get("satiety", 0)) + random.randint(20, 35))
        cat["mood"] = min(100, int(cat.get("mood", 80)) + random.randint(2, 8))
        cat["last_feed_date"] = today
        fed_slots.setdefault(today, {})
        fed_slots[today][slot] = True
        cat["fed_slots"] = fed_slots

        self.economy.add_balance(uid, -cost)
        self._save(uid, cat)

        img = self.image_path(cat)
        msg = (
            f"你带「{cat['name']}」{verb}了{food}。\n"
            f"花费：{cost} {self.coin_name}\n"
            f"她幸福地眯起眼睛，看起来超级满足～\n\n"
            f"体重 +{gain:.2f} 斤\n"
            f"当前体重：{cat['weight']:.1f} 斤\n"
            f"饱食度：{cat['satiety']:.0f}\n"
            f"心情：{cat['mood']}\n"
            f"钱包余额：{self.economy.get_balance(uid)} {self.coin_name}"
        )
        return True, msg, img

    def work(self, uid: str):
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。"

        today = today_str()
        if cat.get("last_work_date") == today:
            return False, f"「{cat['name']}」今天已经努力打过工啦，让她窝在你身边休息一下吧～"

        cat = self._apply_decay(cat)

        if float(cat.get("satiety", 0)) < 30:
            return False, f"「{cat['name']}」小肚子咕咕叫，完全没有力气去打工啦。"

        if int(cat.get("mood", 0)) < 40:
            return False, f"「{cat['name']}」现在心情有点低落，不想出门工作呢。"

        jobs = [
            ("猫咖服务员", 120, 220),
            ("甜点店看板娘", 100, 200),
            ("便利店夜班", 80, 180),
            ("同人展摊位助手", 150, 260),
            ("奶茶店试喝员", 90, 180),
        ]

        job, low, high = random.choice(jobs)
        reward = random.randint(low, high)

        cat["satiety"] = max(0.0, float(cat.get("satiety", 0)) - random.randint(10, 20))
        cat["mood"] = max(0, int(cat.get("mood", 80)) - random.randint(3, 10))
        cat["last_work_date"] = today

        self._save(uid, cat)
        self.economy.add_balance(uid, reward)

        return True, f"「{cat['name']}」去了{job}打工。\n她抱着小钱包跑回来啦～\n赚到了 {reward} {self.coin_name}。\n当前余额：{self.economy.get_balance(uid)} {self.coin_name}"

    def interact(self, uid: str, action: str):
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", None

        today = today_str()
        interact_data = cat.setdefault("interactions", {})
        today_count = int(interact_data.get(today, 0))

        if today_count >= 5:
            return False, f"今天已经陪「{cat['name']}」玩了好多次啦～\n她现在窝在你身边打盹，明天再继续贴贴吧。", self.image_path(cat)

        mood_add = random.randint(4, 12)
        texts = {
            "撸猫": "你轻轻撸了撸她的头发，她舒服地眯起眼睛。",
            "逗猫": "你拿出小玩具逗她，她开心地扑来扑去。",
            "摸猫": "你摸了摸她的脑袋，她小声地喵了一下。",
            "rua猫": "你把她 rua 成了一团软乎乎的猫猫球。",
            "陪猫娘": "你陪她聊了一会儿，她看起来安心了很多。",
            "陪猫猫": "你陪她窝在一起晒太阳，气氛软绵绵的。",
            "贴贴猫娘": "你和她贴贴了一下，她脸红地别过头。",
            "贴贴猫猫": "你和她贴贴了一下，她尾巴轻轻晃了晃。",
        }

        cat["mood"] = min(100, int(cat.get("mood", 80)) + mood_add)
        interact_data[today] = today_count + 1
        cat["interactions"] = interact_data
        self._save(uid, cat)

        text = texts.get(action, "你陪她玩了一会儿。")
        msg = f"{text}\n心情 +{mood_add}\n当前心情：{cat['mood']}\n今日互动次数：{interact_data[today]}/5"
        return True, msg, self.image_path(cat)

    def rename(self, uid: str, name: str):
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, "你还没有猫娘喔～"

        name = name.strip()
        if not name or len(name) > 12:
            return False, "名字不能为空，且长度不能超过 12。"

        cat["name"] = name
        self._save(uid, cat)
        return True, f"改名成功啦～以后就叫她「{name}」喵。"

    async def change_image(self, uid: str, image_src: str):
        """安全保存图片，成功后原子扣费。"""
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", None

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
            return False, f"保存图片失败：{e}", self.image_path(cat)
        finally:
            tmp.unlink(missing_ok=True)

        def op(root):
            wallet = root.setdefault("wallet", {})
            cats = root.setdefault("catgirls", {})
            current_cat = cats.get(uid)
            if not current_cat or not current_cat.get("name"):
                return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", None, None

            balance = int(wallet.get(uid, 0))
            if balance < self.appearance_change_price:
                return False, f"更换形象需要 {self.appearance_change_price} {self.coin_name}，你目前有 {balance} {self.coin_name}，还不够喔～", current_cat, None

            old_image = current_cat.get("image", "")
            wallet[uid] = balance - self.appearance_change_price
            current_cat["image"] = str(out)
            return True, "", current_cat, old_image

        ok, msg, updated_cat, old_image = self.store.update(op)
        if not ok:
            out.unlink(missing_ok=True)
            return False, msg, self.image_path(updated_cat or cat)

        self._delete_old_uploaded_image(old_image)
        balance = self.economy.get_balance(uid)
        return True, f"✨ 「{updated_cat['name']}」换好新形象啦～\n花费：{self.appearance_change_price} {self.coin_name}\n当前余额：{balance} {self.coin_name}\n\n当前状态：\n体重：{float(updated_cat.get('weight', 5.0)):.1f} 斤\n饱食度：{float(updated_cat.get('satiety', 0)):.0f}\n心情：{int(updated_cat.get('mood', 0))}", self.image_path(updated_cat)

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
        cat = self._get(uid)
        if not cat or not cat.get("name"):
            return False, "你还没有猫娘喔～发送「请赐我一只可爱猫娘吧」试试看。", None

        old_gid = cat.get("home_gid", "")
        cat["home_gid"] = gid
        self._save(uid, cat)

        if old_gid == gid:
            msg = f"「{cat.get('name', '猫娘')}」本来就在当前群登记啦～"
        else:
            msg = f"迁移完成喵～\n「{cat.get('name', '猫娘')}」已经登记到当前群，以后会出现在本群的猫娘排行榜里啦。"

        return True, msg, self.image_path(cat)

    def draw_rank(self, gid: str = None) -> Optional[Path]:
        all_cats = self.store.get("catgirls", default={}) or {}
        sign_data = self.store.get("sign", default={}) or {}
        
        cats = []
        for uid, cat in all_cats.items():
            if not isinstance(cat, dict) or not cat.get("name"):
                continue
            if gid and cat.get("home_gid") != gid:
                continue
            
            nickname = sign_data.get(uid, {}).get("last_nickname", uid)
            cat["owner_nickname"] = nickname
            cats.append(cat)

        if not cats:
            return None

        cats.sort(key=lambda x: float(x.get("weight", 0)), reverse=True)
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

        d.text((total_w // 2, 60), "猫娘排行榜", font=title_font, fill=(255, 140, 0), anchor="mm")

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
            d.text((x + card_w // 2, info_y), f"体重: {float(cat.get('weight', 0)):.1f}斤", font=info_font, fill=(60, 60, 60), anchor="mm")
            
            owner = cat.get("owner_nickname", cat.get("user", ""))
            if len(owner) > 10:
                owner = owner[:10] + "..."
            d.text((x + card_w // 2, info_y + 35), f"主人: {owner}", font=info_font, fill=(60, 60, 60), anchor="mm")

        out = self.cache_dir / f"catgirl_rank_{gid or 'global'}.png"
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
