from __future__ import annotations

import asyncio
import html as html_lib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from PIL import Image, ImageChops

from config import PluginConfig
from models import ProblemQueryResult
from paths import PluginPaths


HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    )
}


class NyojProblemClient:
    def __init__(self, config: PluginConfig, paths: PluginPaths):
        self.config = config
        self.paths = paths
        self.base_url = config.nyoj_base_url.rstrip("/")

    async def fetch_problem(self, contest_name: str, display_id: str) -> ProblemQueryResult:
        if not self.base_url or not self.config.nyoj_username or not self.config.nyoj_password:
            raise RuntimeError("NYOJ API 配置不完整，请填写网站地址、登录账号和登录密码。")

        html_path = self.paths.data_root / "problem_img.html"
        image_path = self.paths.data_root / "problem_img.png"
        self.paths.data_root.mkdir(parents=True, exist_ok=True)

        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
            token = await self._login(session)
            contest_id = await self._find_contest_id(session, contest_name)
            problem_url = f"{self.base_url}/contest/{contest_id}/problem/{display_id}"
            ac_count, total_count, acceptance_rate = await self._get_problem_stats(
                session,
                token,
                display_id,
                contest_id,
            )
            html_url = await self._get_html_url(session, token, display_id, contest_id)
            problem_title = await self._download_and_rewrite_html(
                session,
                html_url,
                html_path,
                contest_name,
                display_id,
            )

        await asyncio.to_thread(self._html_to_image, html_path, image_path)
        await asyncio.to_thread(self._crop_bottom_whitespace, image_path)

        return ProblemQueryResult(
            contest_name=contest_name,
            display_id=display_id,
            contest_id=contest_id,
            problem_title=problem_title,
            problem_url=problem_url,
            ac_count=ac_count,
            total_count=total_count,
            acceptance_rate=acceptance_rate,
            html_path=str(html_path),
            image_path=str(image_path),
        )

    async def _login(self, session: aiohttp.ClientSession) -> str:
        async with session.post(
            f"{self.base_url}/api/login",
            json={
                "username": self.config.nyoj_username,
                "password": self.config.nyoj_password,
            },
            headers={"Content-Type": "application/json"},
            timeout=30,
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"NYOJ 登录失败，HTTP={response.status}，响应={text[:200]}")
            token = response.headers.get("Authorization") or response.headers.get("authorization")
            if not token:
                raise RuntimeError("NYOJ 登录成功但响应头没有 Authorization。")
            return token

    async def _find_contest_id(self, session: aiohttp.ClientSession, contest_name: str) -> str:
        url = f"{self.base_url}/api/get-contest-list?currentPage=1&limit=10000"
        async with session.get(url, timeout=30) as response:
            payload = await self._read_json(response, "读取比赛列表")

        records = payload.get("data", {}).get("records", [])
        if not isinstance(records, list):
            raise RuntimeError("读取比赛列表失败，返回数据格式不正确。")

        for record in records:
            if isinstance(record, dict) and record.get("title") == contest_name:
                contest_id = record.get("id")
                if contest_id is None:
                    break
                contest_auth = int(record.get("auth", -1))
                if contest_auth != 0 and not self.config.allow_private_contest_rank:
                    raise RuntimeError(
                        "该比赛不是公开赛。如需查询非公开赛，请在插件配置中开启“允许查询非公开赛”。"
                    )
                return str(contest_id)
        raise RuntimeError(f"没有找到比赛：{contest_name}")

    async def _get_html_url(
        self,
        session: aiohttp.ClientSession,
        token: str,
        display_id: str,
        contest_id: str,
    ) -> str:
        async with session.get(
            f"{self.base_url}/api/get-contest-problem-details",
            params={
                "displayId": display_id,
                "cid": contest_id,
                "containsEnd": "true",
            },
            headers={
                "Authorization": token,
                "Url-Type": "general",
            },
            timeout=30,
        ) as response:
            payload = await self._read_json(response, "读取题面详情")

        try:
            pdf_path = payload["data"]["problem"]["pdfDescription"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("题面详情里没有 pdfDescription。") from exc

        if not isinstance(pdf_path, str) or not pdf_path.strip():
            raise RuntimeError("题面 pdfDescription 为空。")

        pdf_path = pdf_path.strip()
        if not pdf_path.lower().endswith(".pdf"):
            raise RuntimeError(f"题面 pdfDescription 不是 PDF 路径：{pdf_path}")
        return urljoin(self.base_url + "/", pdf_path[:-4] + ".html")

    async def _get_problem_stats(
        self,
        session: aiohttp.ClientSession,
        token: str,
        display_id: str,
        contest_id: str,
    ) -> tuple[int, int, float]:
        async with session.get(
            f"{self.base_url}/api/get-contest-problem",
            params={"cid": contest_id},
            headers={
                "Authorization": token,
                "Url-Type": "general",
            },
            timeout=30,
        ) as response:
            payload = await self._read_json(response, "读取比赛题目列表")

        problem_list = payload.get("data")
        if not isinstance(problem_list, list):
            raise RuntimeError("比赛题目列表格式不正确。")

        problem = next(
            (
                item
                for item in problem_list
                if isinstance(item, dict)
                and str(item.get("displayId", "")).strip() == display_id
            ),
            None,
        )
        if problem is None:
            raise RuntimeError(f"比赛中没有找到题号：{display_id}")

        try:
            ac_count = int(problem["ac"])
            total_count = int(problem["total"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("题目的通过数或提交总数格式不正确。") from exc

        if ac_count < 0 or total_count < 0:
            raise RuntimeError("题目的通过数或提交总数为负数。")
        acceptance_rate = ac_count / total_count * 100 if total_count else 0.0
        return ac_count, total_count, acceptance_rate

    async def _download_and_rewrite_html(
        self,
        session: aiohttp.ClientSession,
        url: str,
        output_path: Path,
        contest_name: str,
        display_id: str,
    ) -> str:
        async with session.get(url, timeout=30) as response:
            content = await response.read()
            if response.status >= 400:
                text = content.decode("utf-8", errors="replace")
                raise RuntimeError(f"下载题面 HTML 失败，HTTP={response.status}，响应={text[:200]}")

        try:
            html = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            html = content.decode("utf-8", errors="replace")

        problem_title = self._extract_problem_title(html, display_id)
        asset_origin = self._asset_origin()
        html = re.sub(
            r"https?://(?:xcpc\.nyist\.edu\.cn|nyoj\.online)(?=[:/])",
            asset_origin,
            html,
            flags=re.IGNORECASE,
        )
        html = self._inject_problem_styles(html, asset_origin, contest_name)
        output_path.write_text(html, encoding="utf-8")
        return problem_title

    def _asset_origin(self) -> str:
        configured = self.base_url
        if "://" not in configured:
            configured = "https://" + configured
        parsed = urlsplit(configured)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"NYOJ 网站地址不合法：{configured}")
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _extract_problem_title(html: str, display_id: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return display_id
        title = re.sub(r"<[^>]+>", "", match.group(1))
        title = html_lib.unescape(title)
        title = re.sub(r"\s+", " ", title).strip()
        title = re.sub(r"^Problem\s+[^.．]+[.．]\s*", "", title, flags=re.IGNORECASE).strip()
        return title or display_id

    @staticmethod
    def _inject_problem_styles(html: str, asset_origin: str, contest_name: str) -> str:
        image_style = """
<style id="nyoj-image-sizing">
html, body {
    box-sizing: border-box;
}
body {
    margin: 0 !important;
    padding: 16px 48px 32px !important;
}
img {
    max-width: 100% !important;
    width: auto !important;
    height: auto !important;
    box-sizing: border-box;
}
#nyoj-custom-header {
    width: 100%;
    margin: 8px 0 22px 0;
    text-align: center;
}
#nyoj-custom-header .school-name {
    margin: 0 0 10px 0;
    font-size: 30px;
    font-weight: 700;
    line-height: 1.45;
}
#nyoj-custom-header .header-line {
    width: 100%;
    border-top: 1px solid #222;
    height: 0;
}
table th,
table td {
    text-align: left !important;
    vertical-align: top !important;
}
table th *,
table td *,
table th pre,
table td pre {
    text-align: left !important;
    vertical-align: top !important;
}
table th pre,
table td pre {
    margin: 0 !important;
}
</style>
"""
        title = html_lib.escape(contest_name.strip() or "题目")
        header_markup = f"""
<div id="nyoj-custom-header">
    <div class="school-name">{title}</div>
    <div class="header-line"></div>
</div>
"""
        if re.search(r"<head(?:\s[^>]*)?>", html, flags=re.IGNORECASE):
            html = re.sub(
                r"(<head(?:\s[^>]*)?>)",
                rf'\1<base href="{asset_origin}/">{image_style}',
                html,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            html = image_style + html

        if re.search(r"<body(?:\s[^>]*)?>", html, flags=re.IGNORECASE):
            html = re.sub(
                r"(<body(?:\s[^>]*)?>)",
                rf"\1{header_markup}",
                html,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            html = header_markup + html
        return html

    @staticmethod
    def _find_chromium() -> str | None:
        candidates: list[str | Path | None] = [
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("chromium.exe"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path(r"C:\Program Files\Chromium\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Chromium\Application\chrome.exe"),
        ]
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Chromium" / "Application" / "chrome.exe")

        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(candidate)
        return None

    @classmethod
    def _html_to_image(cls, html_path: Path, image_path: Path) -> None:
        chromium = cls._find_chromium()
        if not chromium:
            raise RuntimeError("没有找到 Chromium，请确认服务器容器内已安装 chromium。")

        with tempfile.TemporaryDirectory(prefix="nyoj-chromium-") as profile_dir:
            temporary_image = Path(profile_dir) / "capture.png"
            subprocess.run(
                [
                    chromium,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--hide-scrollbars",
                    "--allow-file-access-from-files",
                    "--disable-extensions",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--user-data-dir={profile_dir}",
                    "--window-size=794,12000",
                    f"--screenshot={temporary_image}",
                    "--virtual-time-budget=5000",
                    html_path.resolve().as_uri(),
                ],
                check=True,
                timeout=60,
            )
            if not temporary_image.is_file():
                raise RuntimeError("Chromium 执行完成但没有生成截图。")
            shutil.copyfile(temporary_image, image_path)

    @staticmethod
    def _crop_bottom_whitespace(image_path: Path, padding: int = 24) -> None:
        with Image.open(image_path) as source:
            image = source.convert("RGB")
            white = Image.new("RGB", image.size, (255, 255, 255))
            difference = ImageChops.difference(image, white).convert("L")
            mask = difference.point(lambda value: 255 if value > 8 else 0)
            bounds = mask.getbbox()
            if not bounds or bounds[3] >= image.height - padding:
                return

            bottom = min(image.height, bounds[3] + padding)
            image.crop((0, 0, image.width, bottom)).save(image_path)

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse, action: str) -> dict[str, Any]:
        text = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"{action}失败，HTTP={response.status}，响应={text[:200]}")
        try:
            payload = await response.json(content_type=None)
        except Exception as exc:
            raise RuntimeError(f"{action}失败，返回内容不是 JSON：{text[:200]}") from exc
        if payload.get("status") not in (None, 200) or payload.get("code") not in (None, 200):
            raise RuntimeError(f"{action}失败：{payload.get('msg') or payload.get('message') or payload}")
        return payload
