from __future__ import annotations

import binascii
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List

import aiohttp
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad

from config import PluginConfig

ProgressReporter = Callable[[str], Awaitable[None]]


@dataclass
class ContestRankEntry:
    rank: int
    username: str
    ac_count: int
    gender: str = ""


class NyojContestClient:
    def __init__(self, config: PluginConfig, progress: ProgressReporter | None = None):
        self.config = config
        self.base_url = config.nyoj_base_url.rstrip("/")
        self.progress = progress
        self.login_body_format = "raw-json"

    async def fetch_contest_rank(self, contest_name: str) -> List[ContestRankEntry]:
        if not self.config.has_nyoj_api_config():
            raise RuntimeError("NYOJ 接口配置不完整，请填写网站地址、登录账号、密码和 secret_key。")

        await self._emit(
            "比赛榜单配置："
            f"base_url={self.base_url}，"
            f"username_set={bool(self.config.nyoj_username)}，"
            f"password_set={bool(self.config.nyoj_password)}，"
            f"secret_key_len={len(self.config.nyoj_secret_key.encode('utf-8'))}，"
            f"limit={self.config.ranking_limit}，"
            f"fetch_limit={self._rank_fetch_limit()}，"
            f"allow_private={self.config.allow_private_contest_rank}"
        )
        async with aiohttp.ClientSession() as session:
            contest_id, contest_type, contest_auth = await self._find_contest(session, contest_name)
            if contest_auth != 0 and not self.config.allow_private_contest_rank:
                raise RuntimeError("该比赛不是公开赛。如需查询非公开赛，请在插件配置中开启“允许查询非公开赛”。")

            authorization = await self._login(session)
            records = await self._fetch_rank_records(session, contest_id, authorization)

        return self._build_entries(records)

    async def _find_contest(self, session: aiohttp.ClientSession, contest_name: str) -> tuple[int, int, int]:
        url = f"{self.base_url}/api/get-contest-list?currentPage=1&limit=10000"
        await self._emit(f"比赛列表：GET {url}")
        async with session.get(url) as response:
            payload = await self._read_json(response, "读取比赛列表")

        records = payload.get("data", {}).get("records", [])
        await self._emit(f"比赛列表：records={len(records)}，开始匹配《{contest_name}》")
        for record in records:
            if record.get("title") == contest_name:
                contest_id = int(record.get("id"))
                contest_type = int(record.get("type", -1))
                contest_auth = int(record.get("auth", -1))
                await self._emit(f"比赛匹配：id={contest_id}，type={contest_type}，auth={contest_auth}")
                return contest_id, contest_type, contest_auth
        raise RuntimeError(f"没有找到比赛：{contest_name}")

    async def _login(self, session: aiohttp.ClientSession) -> str:
        if not self.config.nyoj_username or not self.config.nyoj_password:
            raise RuntimeError("NYOJ 登录账号或密码为空，请在插件配置中填写。")
        encrypted_data = self._encrypt_json(
            {
                "username": self.config.nyoj_username,
                "password": self.config.nyoj_password,
            }
        )
        await self._emit(
            "登录 NYOJ："
            f"POST {self.base_url}/api/login，"
            f"encrypted_len={len(encrypted_data)}，"
            f"secret_key_len={len(self.config.nyoj_secret_key.encode('utf-8'))}"
        )
        login_formats = (
            "plain-json",
            "raw-json",
            "raw-text",
            "json-string",
            "json-data",
            "json-params",
            "form-data",
        )
        await self._emit("登录 NYOJ：将依次尝试格式=" + ", ".join(login_formats))
        errors = []
        for body_format in login_formats:
            response_text = ""
            try:
                async with session.post(
                    f"{self.base_url}/api/login",
                    **self._login_post_kwargs(body_format, encrypted_data),
                ) as response:
                    response_text = await response.text()
                    authorization = response.headers.get("authorization")
                    await self._emit(
                        "登录尝试："
                        f"format={body_format}，HTTP={response.status}，"
                        f"authorization_set={bool(authorization)}，"
                        f"响应前80={response_text[:80]}"
                    )
                    if response.status < 400 and authorization:
                        self.login_body_format = body_format
                        await self._emit(
                            f"登录 NYOJ：成功，使用格式={body_format}，authorization_len={len(authorization)}"
                        )
                        return authorization
                    errors.append(f"{body_format}:HTTP{response.status}:{response_text[:80]}")
                    if "密码不能为空" in response_text:
                        await self._emit(
                            f"登录尝试：format={body_format} 返回密码不能为空，"
                            "这表示该请求格式没有被 NYOJ 登录接口识别，不代表插件配置密码为空。"
                        )
            except Exception as exc:
                errors.append(f"{body_format}:{type(exc).__name__}:{exc}")
                await self._emit(
                    f"登录尝试：format={body_format}，异常={type(exc).__name__}：{exc}"
                )

        raise RuntimeError(
            "登录 NYOJ失败，所有登录格式均未成功。"
            "请检查 secret_key 是否正确，或查看上面的登录尝试结果。"
            f"尝试摘要：{'; '.join(errors)}"
        )

    async def _fetch_rank_records(
        self,
        session: aiohttp.ClientSession,
        contest_id: int,
        authorization: str,
    ) -> list[dict[str, Any]]:
        fetch_limit = self._rank_fetch_limit()
        rank_payload = {
            "currentPage": 1,
            "limit": fetch_limit,
            "cid": str(contest_id),
            "forceRefresh": False,
            "removeStar": True,
            "concernedList": [],
            "keyword": "",
        }
        encrypted_data = self._encrypt_json(rank_payload)
        await self._emit(
            "比赛排行："
            f"POST {self.base_url}/api/get-contest-rank，"
            f"cid={contest_id}，limit={rank_payload['limit']}，"
            f"encrypted_len={len(encrypted_data)}"
        )
        async with session.post(
            f"{self.base_url}/api/get-contest-rank",
            **self._rank_post_kwargs(
                self.login_body_format,
                encrypted_data,
                rank_payload,
                extra_headers={
                    "authorization": authorization,
                    "referer": f"{self.base_url}/contest/{contest_id}/rank",
                },
            ),
        ) as response:
            payload = await self._read_json(response, "读取比赛排行榜")
        records = payload.get("data", {}).get("records", [])
        await self._emit(f"比赛排行：records={len(records)}")
        return records

    def _rank_fetch_limit(self) -> int:
        """Fetch the requested rows plus possible blacklisted rows for refill."""
        blacklist_count = len(
            {
                username.strip()
                for username in self.config.ranking_username_blacklist
                if username.strip()
            }
        )
        return int(self.config.ranking_limit) + blacklist_count

    def _login_post_kwargs(
        self,
        body_format: str,
        encrypted_data: str,
    ) -> dict[str, Any]:
        if body_format == "plain-json":
            return {
                "json": {
                    "username": self.config.nyoj_username,
                    "password": self.config.nyoj_password,
                }
            }
        return self._encrypted_post_kwargs(body_format, encrypted_data)

    def _rank_post_kwargs(
        self,
        body_format: str,
        encrypted_data: str,
        rank_payload: dict[str, Any],
        extra_headers: dict[str, str],
    ) -> dict[str, Any]:
        if body_format == "plain-json":
            return {"json": rank_payload, "headers": extra_headers}
        return self._encrypted_post_kwargs(
            body_format,
            encrypted_data,
            extra_headers=extra_headers,
        )

    def _encrypted_post_kwargs(
        self,
        body_format: str,
        encrypted_data: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = dict(extra_headers or {})
        if body_format == "raw-json":
            headers["Content-Type"] = "application/json"
            return {"data": encrypted_data, "headers": headers}
        if body_format == "raw-text":
            headers["Content-Type"] = "text/plain"
            return {"data": encrypted_data, "headers": headers}
        if body_format == "json-string":
            return {"json": encrypted_data, "headers": headers}
        if body_format == "json-data":
            return {"json": {"data": encrypted_data}, "headers": headers}
        if body_format == "json-params":
            return {"json": {"params": encrypted_data}, "headers": headers}
        if body_format == "form-data":
            return {"data": {"data": encrypted_data}, "headers": headers}
        headers["Content-Type"] = "application/json"
        return {"data": encrypted_data, "headers": headers}

    async def _read_json(self, response: aiohttp.ClientResponse, action: str) -> dict[str, Any]:
        text = await response.text()
        await self._emit(
            f"{action}：HTTP {response.status}，响应前120={text[:120]}"
        )
        if response.status >= 400:
            if "Failed to parse parameter format" in text:
                raise RuntimeError(
                    f"{action}失败，NYOJ 无法解析加密参数。"
                    "请优先检查 secret_key 是否和 NYOJ 前端 AES 密钥一致，"
                    f"HTTP 状态码={response.status}，响应={text[:200]}"
                )
            raise RuntimeError(f"{action}失败，HTTP 状态码={response.status}，响应={text[:200]}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{action}失败，返回内容不是 JSON：{text[:200]}") from exc
        if payload.get("code") not in (None, 200):
            raise RuntimeError(f"{action}失败：{payload.get('msg') or payload.get('message') or payload}")
        return payload

    async def _emit(self, message: str) -> None:
        if self.progress is None:
            return
        await self.progress(message)

    def _encrypt_json(self, data: dict[str, Any]) -> str:
        secret_key = self.config.nyoj_secret_key.encode("utf-8")
        if len(secret_key) not in (16, 24, 32):
            raise RuntimeError("NYOJ secret_key 长度不正确，AES 密钥必须是 16、24 或 32 字节。")

        iv = get_random_bytes(16)
        cipher = AES.new(secret_key, AES.MODE_CBC, iv)
        encrypted_data = cipher.encrypt(pad(json.dumps(data).encode("utf-8"), AES.block_size))
        return f"{binascii.hexlify(iv).decode('utf-8')}:{binascii.hexlify(encrypted_data).decode('utf-8')}"

    def _build_entries(self, records: list[dict[str, Any]]) -> List[ContestRankEntry]:
        entries: List[ContestRankEntry] = []
        cutoff_ac: int | None = None
        blacklist = {
            username.strip()
            for username in self.config.ranking_username_blacklist
            if username.strip()
        }
        for record in records:
            if int(record.get("rank", 0)) <= 0:
                continue
            username = str(record.get("username", "")).strip()
            if username in blacklist:
                continue
            ac_count = int(record.get("ac", 0))
            if len(entries) >= self.config.ranking_limit:
                if cutoff_ac is None or ac_count != cutoff_ac:
                    break
            entries.append(
                ContestRankEntry(
                    rank=len(entries) + 1,
                    username=username,
                    ac_count=ac_count,
                )
            )
            if len(entries) == self.config.ranking_limit:
                cutoff_ac = ac_count
        return entries
