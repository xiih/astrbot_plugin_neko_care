import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .storage import JsonStore
from .economy import EconomyService


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def hour_word() -> str:
    h = datetime.now().hour
    if 5 <= h < 9:
        return "早安"
    if 9 <= h < 12:
        return "上午好"
    if 12 <= h < 14:
        return "午安"
    if 14 <= h < 18:
        return "下午好"
    if 18 <= h < 23:
        return "晚上好"
    return "夜安"


DEFAULT_QUOTES = [
    ("愿你今天也被温柔以待。", ""),
    ("要把普通的日子过得浪漫一点。", ""),
    ("慢慢来，好运正在路上。", ""),
    ("心里装着小星星，生活才会亮晶晶。", ""),
    ("今天也要好好吃饭，好好睡觉。", ""),
    ("猫猫偷偷告诉你：今天会有好事发生。", ""),
]


class SignService:
    def __init__(
        self,
        store: JsonStore,
        economy: EconomyService,
        coin_name: str,
        sign_min: int,
        sign_max: int,
        base_dir: Path,
        background_dir: Path,
        font_dir: Path,
        cache_dir: Path,
        quote_file: Path,
        runtime_config_provider: Callable[[], Dict] | None = None,
    ):
        self.store = store
        self.economy = economy
        self.coin_name = coin_name
        self.sign_min = sign_min
        self.sign_max = sign_max
        self.base_dir = Path(base_dir)
        self.background_dir = Path(background_dir)
        self.font_dir = Path(font_dir)
        self.cache_dir = Path(cache_dir)
        self.quote_file = Path(quote_file)
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

    def _economy_rules(self) -> Dict:
        rules = self._runtime().get("economy", {})
        return rules if isinstance(rules, dict) else {}

    def _sign_reward_range(self) -> Tuple[int, int]:
        rules = self._economy_rules()
        low = max(0, int(rules.get("sign_min_reward", self.sign_min)))
        high = max(low, int(rules.get("sign_max_reward", self.sign_max)))
        return low, high

    def _random_quote(self) -> Tuple[str, str]:
        try:
            if self.quote_file and self.quote_file.exists():
                lines = []
                with self.quote_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            lines.append(line)
                if lines:
                    raw = random.choice(lines)
                    if "|" in raw:
                        q, author = raw.split("|", 1)
                        return q.strip(), author.strip()
                    return raw.strip(), ""
        except Exception:
            pass
        return random.choice(DEFAULT_QUOTES)

    def sign(self, uid: str, nickname: str):
        today = today_str()
        sign_min, sign_max = self._sign_reward_range()
        inc = random.randint(sign_min, sign_max)
        quote, quote_from = self._random_quote()

        def op(root):
            sign = root.setdefault("sign", {})
            wallet = root.setdefault("wallet", {})
            user = sign.setdefault(uid, {})
            if user.get("last_sign_date") == today:
                return False, "今天已经签到过啦，明天再来喵～"

            count = int(user.get("count", 0)) + 1
            user["last_sign_date"] = today
            user["count"] = count
            user["last_nickname"] = nickname
            wallet[uid] = int(wallet.get(uid, 0)) + inc

            return True, {
                "uid": uid,
                "nickname": nickname,
                "inc": inc,
                "balance": int(wallet[uid]),
                "count": count,
                "quote": quote,
                "quote_from": quote_from,
            }

        return self.store.update(op)

    def _font(self, size: int, bold: bool = False):
        candidates = []
        if bold:
            candidates.extend([
                self.font_dir / "HYGuoTuChuangXinHongLouMeng-85U.ttf",
                self.font_dir / "FZKATJW.ttf",
                self.font_dir / "GBK.TTF",
            ])
        else:
            candidates.extend([
                self.font_dir / "HYGuoTuChuangXinHongLouMeng-85U.ttf",
                self.font_dir / "GBK.TTF",
                self.font_dir / "FZKATJW.ttf",
            ])
        candidates.extend([
            self.base_dir / "HYGuoTuChuangXinHongLouMeng-85U.ttf",
            self.base_dir / "GBK.TTF",
            self.base_dir / "FZKATJW.ttf",
        ])
        for p in candidates:
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def _pick_background(self) -> Image.Image:
        imgs = []
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            imgs.extend(self.background_dir.glob(ext))
        if imgs:
            try:
                return Image.open(random.choice(imgs)).convert("RGB")
            except Exception:
                pass
        img = Image.new("RGB", (1920, 1080), (232, 240, 255))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, 1920, 1080), fill=(232, 240, 255))
        d.ellipse((-260, -260, 760, 760), fill=(190, 210, 255))
        d.ellipse((1250, 520, 2300, 1500), fill=(255, 210, 230))
        return img

    def _cover(self, img: Image.Image, w: int, h: int) -> Image.Image:
        iw, ih = img.size
        scale = max(w / iw, h / ih)
        nw, nh = int(iw * scale), int(ih * scale)
        img = img.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - w) // 2, (nh - h) // 2
        return img.crop((left, top, left + w, top + h))

    def _round_mask(self, size, radius):
        mask = Image.new("L", size, 0)
        d = ImageDraw.Draw(mask)
        d.rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
        return mask

    def _outlined_text(self, draw: ImageDraw.ImageDraw, pos, text, font, fill=(255, 255, 255), stroke=4, anchor=None):
        x, y = pos
        for dx in range(-stroke, stroke + 1):
            for dy in range(-stroke, stroke + 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0), anchor=anchor)
        draw.text(pos, text, font=font, fill=fill, anchor=anchor)

    def _wrap_text(self, text: str, font: ImageFont.ImageFont, max_width: int):
        lines = []
        cur = ""
        dummy = Image.new("RGB", (10, 10))
        d = ImageDraw.Draw(dummy)
        for ch in text:
            test = cur + ch
            box = d.textbbox((0, 0), test, font=font)
            width = box[2] - box[0]
            if width > max_width and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = test
        if cur:
            lines.append(cur)
        return lines

    def draw_sign(self, uid: str, nickname: str, inc: int, balance: int, count: int, quote: str = "", quote_from: str = "") -> Path:
        today = today_str()
        out = self.cache_dir / f"sign_{uid}_{today}.png"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        coin_name = str(self._economy_rules().get("coin_name") or self.coin_name or "宝石")

        canvas_w, canvas_h = 1920, 1080
        back = self._pick_background()
        bg = self._cover(back, canvas_w, canvas_h)
        blur_bg = bg.filter(ImageFilter.GaussianBlur(8))
        canvas = blur_bg.convert("RGBA")
        white_mask = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 35))
        canvas = Image.alpha_composite(canvas, white_mask)
        draw = ImageDraw.Draw(canvas)

        font_hello = self._font(45, bold=True)
        font_big = self._font(160, bold=True)
        font_mid = self._font(80, bold=True)
        font_date = self._font(70, bold=True)
        font_quote_title = self._font(58, bold=True)
        font_quote = self._font(52, bold=True)

        hello_text = f"Hello {nickname}"
        bbox = draw.textbbox((0, 0), hello_text, font=font_hello)
        text_w = bbox[2] - bbox[0]
        name_box_x, name_box_y = -8, 30
        name_box_w, name_box_h = text_w + 80, 78
        draw.rounded_rectangle((name_box_x, name_box_y, name_box_x + name_box_w, name_box_y + name_box_h), radius=14, fill=(245, 245, 245, 180), outline=(40, 40, 40, 170), width=3)
        self._outlined_text(draw, (32, 69), hello_text, font_hello, anchor="lm", stroke=4)

        card_x, card_y = 630, 160
        card_w, card_h = 1190, 665
        card_radius = 26
        card_img = self._cover(back, card_w, card_h).convert("RGBA")
        card_mask = self._round_mask((card_w, card_h), card_radius)
        canvas.paste(card_img, (card_x, card_y), card_mask)
        draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h), radius=card_radius, outline=(25, 25, 25, 185), width=3)

        shift_y = -45
        hword = hour_word()
        self._outlined_text(draw, (70, 280 + shift_y), hword, font_big, anchor="lm", stroke=4)
        self._outlined_text(draw, (72, 450 + shift_y), f"{coin_name}  +{inc}", font_mid, anchor="lm", stroke=4)
        self._outlined_text(draw, (72, 710 + shift_y), f"{coin_name}：{balance}", font_mid, anchor="lm", stroke=4)
        self._outlined_text(draw, (72, 820 + shift_y), datetime.now().strftime("%Y.%m.%d"), font_date, anchor="lm", stroke=4)

        if not quote:
            quote, quote_from = self._random_quote()

        # 移除了状态条，直接在卡片下方显示今日一言
        quote_title_y = card_y + card_h + 30
        self._outlined_text(draw, (72, quote_title_y), "今日一言：", font_quote_title, anchor="lm", stroke=4)

        quote_x, quote_y = 72, int(quote_title_y + 58)
        max_w = 1250
        lines = self._wrap_text(quote, font_quote, max_w)
        for i, line in enumerate(lines[:3]):
            self._outlined_text(draw, (quote_x, quote_y + i * 68), line, font_quote, anchor="la", stroke=4)

        if quote_from:
            self._outlined_text(draw, (canvas_w - 120, canvas_h - 70), quote_from, font_quote_title, anchor="rd", stroke=4)

        canvas.convert("RGB").save(out, "PNG")
        return out
