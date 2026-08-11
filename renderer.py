from __future__ import annotations

import io
import base64
from datetime import datetime
from typing import Dict, List, Protocol, Sequence
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from models import RankingEntry
from paths import DEFAULT_BACKGROUND_FILENAME, DEFAULT_LOGO_FILENAME, DEFAULT_NOTICE_BG_FILENAME, PluginPaths

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class RankLike(Protocol):
    rank: int
    username: str
    ac_count: int
    gender: str


class ImageRenderer:
    """Generates notification and ranking images using PIL.

    The drawing logic is preserved verbatim from the original makeImg.py.
    """

    def __init__(self, paths: PluginPaths):
        self.paths = paths
        self.font_path = paths.fonts_root / "simhei.ttf"
        self.ac_path = paths.images_root / DEFAULT_NOTICE_BG_FILENAME
        self.back_path = paths.images_root / DEFAULT_BACKGROUND_FILENAME
        self.gold_medal_path = paths.images_root / "金牌.png"
        self.silver_medal_path = paths.images_root / "银牌.png"
        self.bronze_medal_path = paths.images_root / "铜牌.png"
        self.up_path = paths.images_root / "上升.png"
        self.down_path = paths.images_root / "下降.png"
        self.icon_path = paths.images_root / DEFAULT_LOGO_FILENAME

    # ------------------------------------------------------------------ #
    #  Notification image                                                #
    # ------------------------------------------------------------------ #

    async def make_img(self, username: str, ac: int) -> str:
        """Render a single-user AC notification, returning the image path."""
        text = "NewAc " + str(ac)

        font = ImageFont.truetype(str(self.font_path), size=30)
        image = Image.open(str(self.ac_path))
        image_width, _ = image.size

        dr = ImageDraw.Draw(image)

        user_bbox = dr.textbbox((0, 0), username, font=font)
        text_bbox = dr.textbbox((0, 0), text, font=font)
        user_width = user_bbox[2] - user_bbox[0]
        text_width = text_bbox[2] - text_bbox[0]

        user_x = (image_width - user_width) // 2
        text_x = (image_width - text_width) // 2

        user_y = 180
        text_y = 210

        offset = 20
        for i in range(3):
            dr.text((user_x + i - offset, user_y), username, font=font, fill="#FFA500")
        for i in range(3):
            dr.text((text_x + i - offset, text_y), text, font=font, fill="#FFA500")

        notify_path = self.paths.data_root / "notify.png"
        image.save(str(notify_path))

        return str(notify_path)

    # ------------------------------------------------------------------ #
    #  Ranking image                                                     #
    # ------------------------------------------------------------------ #

    async def draw_rank(
        self,
        old_ranking: Sequence[RankingEntry],
        new_ranking: Sequence[RankingEntry],
        title: str = "2026 新生总排行",
        output_filename: str = "current_rank.png",
        ranking_after_date: str = "2026-06-01",
        show_legend: bool = True,
        subtitle: str | None = None,
    ) -> str:
        """Render the ranking image, returning a base64 string."""
        data = self._build_rank_payload(old_ranking, new_ranking)
        return await self._draw_rank_payload(
            data,
            title,
            output_filename,
            ranking_after_date=ranking_after_date,
            show_legend=show_legend,
            subtitle=subtitle,
        )

    async def draw_static_rank(
        self,
        ranking: Sequence[RankLike],
        title: str,
        output_filename: str = "contest_rank.png",
        subtitle: str | None = None,
    ) -> str:
        data = {
            "排名": [item.rank for item in ranking],
            "用户": [item.username for item in ranking],
            "性别": [getattr(item, "gender", "") for item in ranking],
            "通过": [item.ac_count for item in ranking],
            "排名趋势": ["不变" for _ in ranking],
            "题目变化": [0 for _ in ranking],
        }
        return await self._draw_rank_payload(
            data,
            title,
            output_filename,
            show_legend=False,
            subtitle=subtitle,
        )

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _build_rank_payload(
        self,
        old_ranking: Sequence[RankingEntry],
        new_ranking: Sequence[RankingEntry],
    ) -> Dict[str, List]:
        old_map = {item.uuid: item for item in old_ranking}
        payload: Dict[str, List] = {
            "排名": [],
            "用户": [],
            "性别": [],
            "通过": [],
            "题目变化": [],
            "排名趋势": [],
        }

        for user in new_ranking:
            payload["排名"].append(user.rank)
            payload["用户"].append(user.username)
            payload["性别"].append(user.gender)
            payload["通过"].append(user.ac_count)

            old_user = old_map.get(user.uuid)
            if old_user:
                change = user.ac_count - old_user.ac_count
                payload["题目变化"].append(change)
                if user.rank < old_user.rank:
                    payload["排名趋势"].append("上升")
                elif user.rank > old_user.rank:
                    payload["排名趋势"].append("下降")
                else:
                    payload["排名趋势"].append("不变")
            else:
                payload["题目变化"].append(0)
                payload["排名趋势"].append("不变")

        return payload

    async def _draw_rank_payload(
        self,
        data: Dict[str, List],
        title: str,
        output_filename: str,
        ranking_after_date: str | None = None,
        show_legend: bool = True,
        subtitle: str | None = None,
    ) -> str:
        gold_medal = Image.open(self.gold_medal_path).convert("RGBA")
        silver_medal = Image.open(self.silver_medal_path).convert("RGBA")
        bronze_medal = Image.open(self.bronze_medal_path).convert("RGBA")
        up = Image.open(self.up_path).convert("RGBA")
        down = Image.open(self.down_path).convert("RGBA")

        img_width = 1200

        length = len(data["排名"]) if len(data["排名"]) > 1 else 2
        subtitle_offset = 28 if subtitle else 0
        image_height = 80 * (length + 1) + 130 + subtitle_offset
        size = (img_width, image_height)

        images = Image.open(self.back_path)
        img = self._pad_image(images, size)
        draw = ImageDraw.Draw(img)

        bar_length = 650
        bar_height = 25

        max_pass_num = max(data["通过"], default=1) or 1
        scale = bar_length / max_pass_num
        bar_length = int(scale * max_pass_num)

        self._draw_title(img, title, img_width)
        if subtitle:
            subtitle_font = ImageFont.truetype(self.font_path, 22)
            subtitle_box = draw.textbbox((0, 0), subtitle, font=subtitle_font)
            subtitle_width = subtitle_box[2] - subtitle_box[0]
            draw.text(
                ((img_width - subtitle_width) // 2, 100),
                subtitle,
                font=subtitle_font,
                fill="#444444",
            )

        if not data["排名"]:
            font = ImageFont.truetype(self.font_path, 46)
            draw.text((420, 170 + subtitle_offset), "暂无排行数据", font=font, fill="black")
            return self._save_and_encode(img, output_filename)

        font = ImageFont.truetype(self.font_path, 50)
        len_height = 80
        start_len = 130 + subtitle_offset

        for i in range(len(data["排名"])):
            if data["排名趋势"][i] == "上升":
                img.paste(up, (10, start_len + i * len_height), mask=up)
            elif data["排名趋势"][i] == "下降":
                img.paste(down, (10, start_len + i * len_height), mask=down)

            rank = int(data["排名"][i])
            if rank < 4:
                if rank == 1:
                    img.paste(gold_medal, (40, start_len + i * len_height), mask=gold_medal)
                elif rank == 2:
                    img.paste(silver_medal, (40, start_len + i * len_height), mask=silver_medal)
                else:
                    img.paste(bronze_medal, (40, start_len + i * len_height), mask=bronze_medal)
            else:
                draw.text(
                    (48, start_len - 5 + i * len_height),
                    str(rank),
                    font=font,
                    fill="black",
                )

            name = data["用户"][i]
            short_name = name[:10] + ".." if len(name) > 8 else name
            gender = data.get("性别", [""] * len(data["用户"]))[i]
            self._draw_rank_username(
                draw,
                short_name,
                gender,
                (150, start_len - 5 + i * len_height),
                font,
            )

            draw.rectangle(
                (
                    440,
                    start_len + 10 + i * len_height,
                    440 + bar_length,
                    start_len + 10 + i * len_height + bar_height,
                ),
                fill="#DDDDDD",
            )

            ac, change = data["通过"][i], data["题目变化"][i]

            if ac:
                pass_num_width = int(scale * (ac - change))
                draw.rectangle(
                    (
                        440,
                        start_len + 10 + i * len_height,
                        440 + pass_num_width,
                        start_len + 10 + i * len_height + bar_height,
                    ),
                    fill="#FF0000",
                )

                if change:
                    change_width = int(scale * change)
                    draw.rectangle(
                        (
                            440 + pass_num_width,
                            start_len + 10 + i * len_height,
                            440 + pass_num_width + change_width,
                            start_len + 10 + i * len_height + bar_height,
                        ),
                        fill="#00FF00",
                    )

            font_1 = ImageFont.truetype(self.font_path, 30)
            draw.text(
                (460 + bar_length, start_len + 5 + i * len_height),
                str(data["通过"][i]),
                font=font_1,
                fill="black",
            )

        start_len = 350 + bar_length
        start_row = (length + 2) * len_height

        if show_legend:
            font_2 = ImageFont.truetype(self.font_path, 10)

            if any(item != 0 for item in data["通过"]):
                draw.rectangle(
                    (start_len, start_row, start_len + 40, start_row + 10),
                    fill="#FF0000",
                )
                draw.text((start_len + 40 + 10, start_row), " 既定", font=font_2, fill="black")

            if any(item != 0 for item in data["题目变化"]):
                draw.rectangle(
                    (start_len, start_row + 20, start_len + 40, start_row + 20 + 10),
                    fill="#00FF00",
                )
                draw.text(
                    (start_len + 40 + 10, start_row + 20),
                    " 新增",
                    font=font_2,
                    fill="black",
                )

        if ranking_after_date:
            font_1 = ImageFont.truetype(self.font_path, 30)
            draw.text(
                (40, start_row),
                f"* 此排行榜仅记录{ranking_after_date}之后注册的账号QAQ。",
                font=font_1,
                fill="black",
            )

        return self._save_and_encode(img, output_filename)

    def _draw_rank_username(
        self,
        draw: ImageDraw.ImageDraw,
        username: str,
        gender: str,
        position: tuple[int, int],
        font: ImageFont.FreeTypeFont,
    ) -> None:
        x, y = position
        if not self._is_female_gender(gender):
            draw.text((x, y), username, font=font, fill="black")
            return

        bbox = draw.textbbox((x, y), username, font=font)
        text_width = bbox[2] - bbox[0]
        text_top = bbox[1]
        text_bottom = bbox[3]
        text_center_y = (text_top + text_bottom) // 2
        icon_size = 30
        icon_gap = 8
        pad_x = 10
        pad_y = 6
        box = (
            x - pad_x,
            text_top - pad_y,
            x + icon_size + icon_gap + text_width + pad_x,
            text_bottom + pad_y,
        )
        draw.rounded_rectangle(box, radius=12, fill="#FFE3EE", outline="#F9A8D4", width=2)
        icon_center_y = text_center_y - 4
        self._draw_female_icon(draw, (x + icon_size // 2, icon_center_y), scale=0.82)
        draw.text(
            (x + icon_size + icon_gap, y),
            username,
            font=font,
            fill="#9F1239",
        )

    @staticmethod
    def _is_female_gender(gender: str) -> bool:
        return str(gender or "").strip().lower() in {"女", "女生", "女性", "female", "f", "girl"}

    @staticmethod
    def _draw_female_icon(
        draw: ImageDraw.ImageDraw,
        center: tuple[int, int],
        scale: float = 1.0,
    ) -> None:
        cx, cy = center
        color = (225, 29, 72)
        width = max(2, int(4 * scale))
        rx = int(13 * scale)
        ry = int(13 * scale)
        draw.ellipse(
            (
                int(cx - rx),
                int(cy - 20 * scale),
                int(cx + rx),
                int(cy + 6 * scale),
            ),
            outline=color,
            width=width,
        )
        draw.line(
            (int(cx), int(cy + 6 * scale), int(cx), int(cy + 28 * scale)),
            fill=color,
            width=width,
        )
        draw.line(
            (
                int(cx - 11 * scale),
                int(cy + 19 * scale),
                int(cx + 11 * scale),
                int(cy + 19 * scale),
            ),
            fill=color,
            width=width,
        )

    def _save_and_encode(self, img: Image.Image, output_filename: str) -> str:
        save_path = self.paths.data_root / output_filename
        img.save(str(save_path))
        image_data = io.BytesIO()
        img.save(image_data, format="PNG")
        return base64.b64encode(image_data.getvalue()).decode("utf-8")

    def _pad_image(self, image: Image.Image, target_size: tuple) -> Image.Image:
        iw, ih = image.size
        w, h = target_size
        scale = min((w - 100) / iw, h / ih)
        nw = int(iw * scale + 0.5)
        nh = int(ih * scale + 0.5)
        image = image.resize((nw, nh), resample=Image.Resampling.BICUBIC)
        new_image = Image.new("RGB", target_size, "white")
        new_image.paste(image, ((w - nw) // 2, (h - nh) // 2))
        return new_image

    def _get_text_size(self, draw: ImageDraw.ImageDraw, text: str, font_size: int):
        font = ImageFont.truetype(str(self.font_path), font_size)
        bbox = draw.textbbox((0, 0), text, font=font)
        size = (bbox[2] - bbox[0], bbox[3] - bbox[1])
        return font, size

    def _draw_middle_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font_size: int = 20,
        text_height: int = 0,
        img_width: int = 720,
        icon: bool = False,
        img: Image.Image | None = None,
    ):
        font, size = self._get_text_size(draw, text, font_size)
        draw.text(
            ((img_width - size[0] + 50) // 2, text_height),
            text,
            font=font,
            fill="black",
        )

        if icon and img is not None:
            with Image.open(self.icon_path) as source_icon:
                icon_img = source_icon.resize((size[1], size[1]))
            img.paste(
                icon_img,
                ((img_width - size[0]) // 2 - size[1], text_height),
                mask=icon_img,
            )

    def _draw_right_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font_size: int = 20,
        text_height: int = 0,
        img_width: int = 720,
    ):
        font, size = self._get_text_size(draw, text, font_size)
        draw.text(
            ((img_width - size[0] - 30), text_height),
            text,
            font=font,
            fill="black",
        )

    def _draw_title(
        self,
        img: Image.Image,
        title: str,
        img_width: int = 1200,
        title_size: int = 60,
        time_size: int = 20,
    ):
        draw = ImageDraw.Draw(img)
        self._draw_middle_text(
            draw,
            title,
            font_size=title_size,
            text_height=20,
            icon=True,
            img=img,
            img_width=img_width,
        )

        t = datetime.now(BEIJING_TZ).strftime("%m月%d日 %H点%M分")
        self._draw_right_text(
            draw,
            t,
            font_size=time_size,
            text_height=80,
            img_width=img_width,
        )
