from __future__ import annotations

import base64
import colorsys
import io
import random
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageOps

from models import UserProfileCard
from paths import DEFAULT_LOGO_FILENAME, PluginPaths

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
WIDTH = 720
HEIGHT = 960
PADDING = 36
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/png,image/jpeg,image/*,*/*;q=0.8",
}

THEMES = {
    "gray": {
        "background": (156, 163, 175),
        "background_2": (107, 114, 128),
        "panel": (249, 250, 251),
        "panel_2": (229, 231, 235),
        "accent": (75, 85, 99),
        "accent_2": (31, 41, 55),
        "text": (31, 41, 55),
        "muted": (75, 85, 99),
        "white": (255, 255, 255),
    },
    "lime": {
        "background": (163, 230, 53),
        "background_2": (101, 163, 13),
        "panel": (247, 254, 231),
        "panel_2": (236, 252, 203),
        "accent": (101, 163, 13),
        "accent_2": (63, 98, 18),
        "text": (54, 83, 20),
        "muted": (77, 124, 15),
        "white": (255, 255, 255),
    },
    "cyan": {
        "background": (34, 211, 238),
        "background_2": (8, 145, 178),
        "panel": (236, 254, 255),
        "panel_2": (207, 250, 254),
        "accent": (8, 145, 178),
        "accent_2": (21, 94, 117),
        "text": (22, 78, 99),
        "muted": (14, 116, 144),
        "white": (255, 255, 255),
    },
    "blue": {
        "background": (56, 189, 248),
        "background_2": (37, 99, 235),
        "panel": (239, 246, 255),
        "panel_2": (219, 234, 254),
        "accent": (29, 78, 216),
        "accent_2": (30, 64, 175),
        "text": (23, 37, 84),
        "muted": (30, 64, 175),
        "white": (255, 255, 255),
    },
    "indigo": {
        "background": (129, 140, 248),
        "background_2": (79, 70, 229),
        "panel": (238, 242, 255),
        "panel_2": (224, 231, 255),
        "accent": (79, 70, 229),
        "accent_2": (55, 48, 163),
        "text": (49, 46, 129),
        "muted": (67, 56, 202),
        "white": (255, 255, 255),
    },
    "orange": {
        "background": (255, 167, 38),
        "background_2": (239, 108, 0),
        "panel": (255, 247, 237),
        "panel_2": (255, 237, 213),
        "accent": (251, 140, 0),
        "accent_2": (194, 65, 12),
        "text": (124, 45, 18),
        "muted": (154, 52, 18),
        "white": (255, 255, 255),
    },
    "rose": {
        "background": (251, 113, 133),
        "background_2": (225, 29, 72),
        "panel": (255, 241, 242),
        "panel_2": (255, 228, 230),
        "accent": (225, 29, 72),
        "accent_2": (159, 18, 57),
        "text": (76, 5, 25),
        "muted": (136, 19, 55),
        "white": (255, 255, 255),
    },
    "aurora": {
        "background": (168, 85, 247),
        "background_2": (109, 40, 217),
        "panel": (250, 245, 255),
        "panel_2": (237, 233, 254),
        "accent": (217, 70, 239),
        "accent_2": (147, 51, 234),
        "text": (59, 7, 100),
        "muted": (88, 28, 135),
        "white": (255, 255, 255),
    },
}


class ProfileCardRenderer:
    def __init__(self, paths: PluginPaths):
        self.paths = paths
        self.custom_font_path = paths.fonts_root / "zsft184.ttf"
        self.bold_font_path = paths.fonts_root / "msyhbd.ttc"
        self.main_font_path = paths.fonts_root / "simhei.ttf"
        self.logo_path = paths.images_root / DEFAULT_LOGO_FILENAME

    def draw(self, profile: UserProfileCard, output_filename: str = "user_profile.png") -> str:
        output_path = self.paths.data_root / output_filename
        self._draw_profile_card(profile, output_path)
        return str(output_path)

    def _font(
        self,
        size: int,
        bold: bool = False,
        prefer_custom: bool = False,
        text: str = "",
    ) -> ImageFont.ImageFont:
        candidates: list[str] = []
        if prefer_custom and self.custom_font_path.exists() and all(ord(ch) < 128 for ch in text):
            candidates.append(str(self.custom_font_path))
        if bold and self.bold_font_path.exists():
            candidates.append(str(self.bold_font_path))
        candidates.extend(
            [
                str(self.main_font_path),
            ]
        )
        for path in candidates:
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                pass
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    def _fit_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_width: int,
        size: int,
        bold: bool = False,
        prefer_custom: bool = False,
    ) -> ImageFont.ImageFont:
        for font_size in range(size, 17, -1):
            font = self._font(font_size, bold=bold, prefer_custom=prefer_custom, text=text)
            if draw.textlength(text, font=font) <= max_width:
                return font
        return self._font(18, bold=bold, prefer_custom=prefer_custom, text=text)

    @staticmethod
    def _vivid_color(hue: float, saturation: float = 0.72, value: float = 0.94) -> tuple[int, int, int]:
        red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, saturation, value)
        return (int(red * 255), int(green * 255), int(blue * 255))

    def _random_rainbow_theme(self) -> dict[str, object]:
        start = random.random()
        background = [self._vivid_color((start + i / 5) % 1.0, 0.62, 0.96) for i in range(5)]
        accent = [self._vivid_color((start + 0.08 + i / 4) % 1.0, 0.82, 0.92) for i in range(4)]
        text = [self._vivid_color((start + 0.14 + i / 5) % 1.0, 0.86, 0.72) for i in range(5)]
        return {
            "background": background[0],
            "background_2": background[-1],
            "background_gradient": background,
            "panel": (255, 251, 255),
            "panel_2": (245, 235, 255),
            "accent": accent[0],
            "accent_2": accent[-1],
            "accent_gradient": accent,
            "text_gradient": text,
            "text": (67, 24, 96),
            "muted": (107, 33, 168),
            "white": (255, 255, 255),
        }

    def _theme_for_ac(self, total_ac: int) -> dict[str, object]:
        if total_ac < 25:
            return THEMES["gray"]
        if total_ac < 50:
            return THEMES["lime"]
        if total_ac < 75:
            return THEMES["cyan"]
        if total_ac < 100:
            return THEMES["blue"]
        if total_ac < 125:
            return THEMES["indigo"]
        if total_ac < 150:
            return THEMES["orange"]
        if total_ac < 175:
            return THEMES["rose"]
        if total_ac < 200:
            return THEMES["aurora"]
        return self._random_rainbow_theme()

    @staticmethod
    def _gradient_color(stops: list[tuple[int, int, int]], t: float) -> tuple[int, int, int]:
        segment = min(int(t * (len(stops) - 1)), len(stops) - 2)
        local_t = t * (len(stops) - 1) - segment
        return tuple(int(stops[segment][i] * (1 - local_t) + stops[segment + 1][i] * local_t) for i in range(3))

    def _draw_vertical_gradient(
        self,
        img: Image.Image,
        top: tuple[int, int, int] | list[tuple[int, int, int]],
        bottom: tuple[int, int, int] | None = None,
    ) -> None:
        stops = top if bottom is None and isinstance(top, list) else [top, bottom or top]
        pixels = img.load()
        for y in range(img.height):
            color = self._gradient_color(stops, y / max(img.height - 1, 1))
            for x in range(img.width):
                pixels[x, y] = color

    def _draw_rounded_gradient_rect(
        self,
        img: Image.Image,
        box: tuple[int, int, int, int],
        radius: int,
        stops: list[tuple[int, int, int]],
    ) -> None:
        x1, y1, x2, y2 = box
        gradient = Image.new("RGBA", (x2 - x1, y2 - y1), (0, 0, 0, 0))
        pixels = gradient.load()
        for x in range(gradient.width):
            color = self._gradient_color(stops, x / max(gradient.width - 1, 1))
            for y in range(gradient.height):
                pixels[x, y] = (*color, 255)
        mask = Image.new("L", gradient.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, gradient.width - 1, gradient.height - 1), radius=radius, fill=255)
        img.paste(gradient, (x1, y1), mask)

    def _draw_gradient_text(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        font: ImageFont.ImageFont,
        stops: list[tuple[int, int, int]],
        anchor: str | None = None,
    ) -> None:
        box = draw.textbbox(xy, text, font=font, anchor=anchor)
        width = max(box[2] - box[0], 1)
        height = max(box[3] - box[1], 1)
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).text((-box[0] + xy[0], -box[1] + xy[1]), text, font=font, fill=255, anchor=anchor)
        gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pixels = gradient.load()
        for x in range(width):
            color = self._gradient_color(stops, x / max(width - 1, 1))
            for y in range(height):
                pixels[x, y] = (*color, 255)
        img.paste(gradient, (box[0], box[1]), mask)

    def _draw_theme_text(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int],
        theme: dict[str, object],
        anchor: str | None = None,
    ) -> None:
        text_gradient = theme.get("text_gradient")
        if isinstance(text_gradient, list):
            self._draw_gradient_text(img, draw, xy, text, font, text_gradient, anchor=anchor)
        else:
            draw.text(xy, text, font=font, fill=fill, anchor=anchor)

    @staticmethod
    def _round_image_corners(img: Image.Image, radius: int = 38) -> Image.Image:
        rounded = img.convert("RGBA")
        mask = Image.new("L", rounded.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, rounded.width - 1, rounded.height - 1), radius=radius, fill=255)
        rounded.putalpha(mask)
        return rounded

    @staticmethod
    def _mix_color(first: tuple[int, int, int], second: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        return tuple(int(first[i] * (1 - amount) + second[i] * amount) for i in range(3))

    @staticmethod
    def _quote_url(url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                urllib.parse.quote(parts.path),
                urllib.parse.quote_plus(parts.query, safe="=&"),
                parts.fragment,
            )
        )

    def _read_avatar(self, source: str) -> Image.Image | None:
        if not source:
            return None
        try:
            if source.startswith("data:image"):
                raw = source.split(",", 1)[1]
                return Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGBA")
            if source.startswith(("http://", "https://")):
                request = urllib.request.Request(self._quote_url(source), headers=HTTP_HEADERS)
                with urllib.request.urlopen(request, timeout=8) as response:
                    return Image.open(io.BytesIO(response.read())).convert("RGBA")
            path = Path(source)
            if path.exists():
                return Image.open(path).convert("RGBA")
        except Exception:
            return None
        return None

    def _make_avatar(self, profile: UserProfileCard, theme: dict[str, object], size: int = 168) -> Image.Image:
        avatar = self._read_avatar(profile.avatar)
        if avatar is None:
            avatar = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            bg = Image.new("RGB", (size, size))
            self._draw_vertical_gradient(bg, theme["panel_2"], theme["accent"])
            avatar.alpha_composite(bg.convert("RGBA"))
            draw = ImageDraw.Draw(avatar)
            initials = profile.username[:1].upper() or "U"
            font = self._fit_text(draw, initials, size - 28, 68, bold=True)
            draw.text((size // 2, size // 2), initials, font=font, fill=theme["white"], anchor="mm")
        else:
            avatar = ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)

        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=36, fill=255)
        framed = Image.new("RGBA", (size + 12, size + 12), (0, 0, 0, 0))
        ImageDraw.Draw(framed).rounded_rectangle((0, 0, size + 11, size + 11), radius=42, fill=(255, 255, 255, 235))
        framed.paste(avatar, (6, 6), mask)
        return framed

    @staticmethod
    def _is_female(gender: str) -> bool:
        return gender.strip().lower() in {"女", "女生", "女性", "female", "f", "girl"}

    @staticmethod
    def _is_secret_gender(gender: str) -> bool:
        return gender.strip().lower() in {"", "保密", "未知", "隐藏", "secret", "secrecy", "private", "unknown", "none"}

    @staticmethod
    def _is_admin_permission(permission: str) -> bool:
        admin_roles = {"超级管理员", "普通管理员", "题目管理员", "教练管理员"}
        roles = {item.strip() for item in permission.split("、") if item.strip()}
        return bool(admin_roles & roles)

    def _draw_gender_icon(self, draw: ImageDraw.ImageDraw, center: tuple[int, int], gender: str) -> None:
        cx, cy = center
        width = 5
        if self._is_secret_gender(gender):
            color = (100, 116, 139)
            draw.arc((cx - 15, cy - 20, cx + 15, cy + 12), 200, -20, fill=color, width=width)
            draw.rounded_rectangle((cx - 22, cy - 1, cx + 22, cy + 29), radius=7, outline=color, width=width)
            draw.ellipse((cx - 4, cy + 10, cx + 4, cy + 18), fill=color)
            return
        if self._is_female(gender):
            color = (225, 29, 72)
            draw.ellipse((cx - 13, cy - 20, cx + 13, cy + 6), outline=color, width=width)
            draw.line((cx, cy + 6, cx, cy + 28), fill=color, width=width)
            draw.line((cx - 11, cy + 19, cx + 11, cy + 19), fill=color, width=width)
            return
        color = (37, 99, 235)
        draw.ellipse((cx - 14, cy - 6, cx + 14, cy + 22), outline=color, width=width)
        draw.line((cx + 10, cy - 2, cx + 27, cy - 19), fill=color, width=width)
        draw.line((cx + 27, cy - 19, cx + 27, cy - 5), fill=color, width=width)
        draw.line((cx + 27, cy - 19, cx + 13, cy - 19), fill=color, width=width)

    def _draw_nyoj_logo(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        right: int,
        y: int,
        oj_name: str,
    ) -> None:
        logo_size = 52
        gap = 12
        text = oj_name or "NYOJ"
        font = self._font(40, bold=True, prefer_custom=True, text=text)
        text_width = int(draw.textlength(text, font=font))
        logo_x = right - logo_size - gap - text_width
        text_x = logo_x + logo_size + gap

        try:
            logo = Image.open(self.logo_path).convert("RGBA")
            logo = ImageOps.fit(logo, (logo_size, logo_size), method=Image.Resampling.LANCZOS)
        except Exception:
            logo = Image.new("RGBA", (logo_size, logo_size), (255, 255, 255, 0))
            logo_draw = ImageDraw.Draw(logo)
            logo_draw.ellipse((2, 2, logo_size - 2, logo_size - 2), fill=(255, 255, 255, 230))
            logo_draw.text(
                (logo_size // 2, logo_size // 2),
                text[:2] or "OJ",
                font=self._font(18, bold=True),
                fill=(37, 99, 235),
                anchor="mm",
            )
        img.paste(logo, (int(logo_x), y), logo)

        text_y = y + logo_size // 2
        stroke = (15, 23, 42)
        draw.text(
            (int(text_x), text_y),
            text,
            font=font,
            fill=stroke,
            stroke_width=2,
            stroke_fill=stroke,
            anchor="lm",
        )

        raw_box = draw.textbbox((0, 0), text, font=font)
        placed_box = draw.textbbox((int(text_x), text_y), text, font=font, anchor="lm")
        mask = Image.new("L", (raw_box[2] - raw_box[0], raw_box[3] - raw_box[1]), 0)
        ImageDraw.Draw(mask).text((-raw_box[0], -raw_box[1]), text, font=font, fill=255)
        gradient = Image.new("RGBA", mask.size, (0, 0, 0, 0))
        pixels = gradient.load()
        stops = [(37, 99, 235), (124, 58, 237), (236, 72, 153), (245, 158, 11)]
        for x in range(gradient.width):
            t = x / max(gradient.width - 1, 1)
            segment = min(int(t * (len(stops) - 1)), len(stops) - 2)
            local_t = t * (len(stops) - 1) - segment
            color = tuple(
                int(stops[segment][i] * (1 - local_t) + stops[segment + 1][i] * local_t)
                for i in range(3)
            )
            for yy in range(gradient.height):
                pixels[x, yy] = (*color, 255)
        img.paste(gradient, (placed_box[0], placed_box[1]), mask)

    def _draw_info_row(self, img: Image.Image, draw: ImageDraw.ImageDraw, y: int, label: str, value: str, theme: dict[str, object]) -> None:
        label_font = self._font(25, bold=True)
        value_font = self._fit_text(draw, value, 380, 28, bold=True, prefer_custom=True)
        self._draw_theme_text(img, draw, (78, y), label, label_font, theme["muted"], theme)
        self._draw_theme_text(img, draw, (310, y), value, value_font, theme["text"], theme)

    def _draw_profile_card(self, profile: UserProfileCard, output_path: Path) -> None:
        theme = self._theme_for_ac(profile.total_ac)
        img = Image.new("RGB", (WIDTH, HEIGHT), theme["background"])
        background_gradient = theme.get("background_gradient")
        if isinstance(background_gradient, list):
            self._draw_vertical_gradient(img, background_gradient)
        else:
            self._draw_vertical_gradient(img, theme["background"], theme["background_2"])
        draw = ImageDraw.Draw(img)

        self._draw_nyoj_logo(img, draw, WIDTH - 52, 24, profile.oj_name)

        card = (PADDING, 140, WIDTH - PADDING, HEIGHT - 120)
        shadow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow_layer).rounded_rectangle((card[0] + 8, card[1] + 10, card[2] + 8, card[3] + 10), radius=32, fill=(0, 0, 0, 42))
        img.paste(Image.alpha_composite(img.convert("RGBA"), shadow_layer).convert("RGB"))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(card, radius=32, fill=theme["panel"])

        avatar = self._make_avatar(profile, theme)
        img.paste(avatar, (72, 92), avatar)

        title_font = self._fit_text(draw, profile.username, 430, 56, bold=True, prefer_custom=True)
        self._draw_theme_text(img, draw, (284, 174), profile.username, title_font, theme["text"], theme)

        draw.rounded_rectangle((284, 248, 450, 294), radius=23, fill=theme["panel_2"])
        self._draw_theme_text(img, draw, (326, 270), "性别", self._font(23, bold=True), theme["muted"], theme, anchor="mm")
        self._draw_gender_icon(draw, (390, 266), profile.gender)

        permission_font = self._fit_text(draw, profile.permission, 150, 25, bold=True, prefer_custom=True)
        permission_fill = theme["panel_2"]
        if self._is_admin_permission(profile.permission):
            permission_fill = self._mix_color(theme["panel_2"], theme["accent_2"], 0.32)
        draw.rounded_rectangle((466, 248, 648, 294), radius=23, fill=permission_fill)
        self._draw_theme_text(img, draw, (557, 270), profile.permission, permission_font, theme["accent_2"], theme, anchor="mm")

        draw.rounded_rectangle((70, 342, 650, 590), radius=24, fill=theme["panel_2"])
        info_rows = [
            ("注册时间", profile.registered_at),
            ("最近提交", profile.last_submission_at),
            ("提交结果", profile.last_submission_result),
            ("最近登录", profile.last_login_at),
        ]
        for y, (label, value) in zip((378, 430, 482, 534), info_rows):
            self._draw_info_row(img, draw, y, label, value, theme)

        ac_box = (70, 642, 650, 780)
        accent_gradient = theme.get("accent_gradient")
        if isinstance(accent_gradient, list):
            self._draw_rounded_gradient_rect(img, ac_box, 24, accent_gradient)
            draw = ImageDraw.Draw(img)
        else:
            draw.rounded_rectangle(ac_box, radius=24, fill=theme["accent"])
        draw.text(
            (106, 711),
            "总 AC 题数",
            font=self._font(34, bold=True),
            fill=theme["white"],
            anchor="lm",
        )
        draw.text(
            (612, 711),
            str(profile.total_ac),
            font=self._font(88, bold=True, prefer_custom=True, text=str(profile.total_ac)),
            fill=theme["white"],
            anchor="rm",
        )

        now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        draw.text((WIDTH // 2, 902), now, font=self._font(22, prefer_custom=True, text=now), fill=theme["white"], anchor="mm")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._round_image_corners(img).save(output_path)
