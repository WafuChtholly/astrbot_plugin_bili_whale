"""只读的 B 站客户端：WBI 签名、请求限速、412 熔断、字幕/弹幕/评论/搜索/取流。

移植自 dsh-bili-whale 的读路径，用纯 Python 实现，不依赖 Node 或 DSH 宿主。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import random
import re
import time
import zlib
from collections import deque
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from astrbot.api import logger

API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
BACKOFF = (300, 600, 1200, 1800)
_FILTER = re.compile(r"[!'()*]")
_DANMAKU = re.compile(r'<d p="([^"]*)">(.*?)</d>', re.S)


class BiliError(RuntimeError):
    """B 站接口返回业务错误或网络失败。"""


class BiliBlocked(BiliError):
    """命中 412/风控，正在熔断。"""


def _clean(value) -> str:
    return _FILTER.sub("", str(value))


def _key_of(url: str) -> str:
    name = urlparse(str(url or "")).path.rsplit("/", 1)[-1]
    return name.split(".")[0]


def _sign(params: dict, mixin: str) -> str:
    """按 B 站 WBI 规则排序、过滤、编码，再拼上 w_rid。"""
    values = {str(k): _clean(v) for k, v in params.items() if v is not None}
    values["wts"] = str(int(time.time()))
    query = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(values.items())
    )
    return f"{query}&w_rid={hashlib.md5((query + mixin).encode()).hexdigest()}"


def _decompress(raw: bytes, limit: int = 32 * 1024 * 1024) -> str:
    """弹幕 XML 是不带 content-encoding 的 deflate，需要逐个试。"""
    if raw.lstrip()[:1] == b"<":
        return raw.decode("utf-8", "replace")
    for wbits in (-15, 15, 31):
        try:
            return zlib.decompressobj(wbits).decompress(raw, limit).decode("utf-8", "replace")
        except zlib.error:
            continue
    try:
        import brotli  # type: ignore

        return brotli.decompress(raw).decode("utf-8", "replace")
    except Exception:
        pass
    return raw.decode("utf-8", "replace")


_MASK64 = (1 << 64) - 1
_GAIA_ACTIVE = API + "/x/internal/gaia-gateway/ExClimbWuzhi"


def _rotl64(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (64 - bits))) & _MASK64


def _fmix64(value: int) -> int:
    value ^= value >> 33
    value = (value * 0xFF51AFD7ED558CCD) & _MASK64
    value ^= value >> 33
    value = (value * 0xC4CEB9FE1A85EC53) & _MASK64
    return value ^ (value >> 33)


def _murmur3_x64_128(data: bytes, seed: int = 31) -> int:
    """MurmurHash3 x64 128，B站 buvid_fp 用的就是这个，返回 (h2 << 64) | h1。"""
    c1, c2 = 0x87C37B91114253D5, 0x4CF5AD432745937F
    h1 = h2 = seed & _MASK64
    head = len(data) - len(data) % 16
    for offset in range(0, head, 16):
        k1 = int.from_bytes(data[offset:offset + 8], "little")
        k2 = int.from_bytes(data[offset + 8:offset + 16], "little")
        k1 = (_rotl64((k1 * c1) & _MASK64, 31) * c2) & _MASK64
        h1 ^= k1
        h1 = (_rotl64(h1, 27) + h2) & _MASK64
        h1 = (h1 * 5 + 0x52DCE729) & _MASK64
        k2 = (_rotl64((k2 * c2) & _MASK64, 33) * c1) & _MASK64
        h2 ^= k2
        h2 = (_rotl64(h2, 31) + h1) & _MASK64
        h2 = (h2 * 5 + 0x38495AB5) & _MASK64
    tail = data[head:]
    k1 = k2 = 0
    for index in range(8, len(tail)):
        k2 ^= tail[index] << ((index - 8) * 8)
    if len(tail) > 8:
        k2 = (_rotl64((k2 * c2) & _MASK64, 33) * c1) & _MASK64
        h2 ^= k2
    for index in range(min(len(tail), 8)):
        k1 ^= tail[index] << (index * 8)
    if min(len(tail), 8):
        k1 = (_rotl64((k1 * c1) & _MASK64, 31) * c2) & _MASK64
        h1 ^= k1
    h1 ^= len(data)
    h2 ^= len(data)
    h1 = (h1 + h2) & _MASK64
    h2 = (h2 + h1) & _MASK64
    h1, h2 = _fmix64(h1), _fmix64(h2)
    h1 = (h1 + h2) & _MASK64
    h2 = (h2 + h1) & _MASK64
    return (h2 << 64) | h1


def _gen_uuid_infoc() -> str:
    """Gaia 激活用的 _uuid：8-4-4-4-12 段 + 5 位毫秒尾数 + infoc。"""
    alphabet = list("123456789ABCDEF") + ["10"]
    parts = ["".join(random.choice(alphabet) for _ in range(size))
             for size in (8, 4, 4, 4, 12)]
    tail = str(int(time.time() * 1000) % 100000).ljust(5, "0")
    return "-".join(parts) + tail + "infoc"


def _finger_payload(uuid: str) -> str:
    """ExClimbWuzhi 的请求体：外层 payload 是内层 JSON 再序列化一次的字符串。"""
    content = {
        "3064": 1,
        "5062": int(time.time() * 1000),
        "03bf": "https%3A%2F%2Fwww.bilibili.com%2F",
        "39c8": "333.999.fp.risk",
        "34f1": "",
        "d402": "",
        "654a": "",
        "6e7c": "1440x900",
        "3c43": {
            "2673": 0,
            "5766": 24,
            "6527": 0,
            "7003": 1,
            "807e": 1,
            "b8ce": UA,
            "641c": 0,
            "07a4": "zh-CN",
            "1c57": "not available",
            "0bd0": 8,
            "748e": [1440, 900],
            "d61f": [1440, 900],
            "fc9d": -480,
            "6aa9": "Asia/Shanghai",
            "75b8": 1,
            "3b21": 1,
            "8a1c": 0,
            "d52f": "not available",
            "adca": "Win32",
            "80c9": [],
            "13ab": "",
            "bfe9": "",
            "a3c1": [],
            "6bc5": "Google Inc. (NVIDIA)~ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "ed31": 0,
            "72bd": 0,
            "097b": 0,
            "52cd": [0, 0, 0],
            "a658": [],
            "d02f": "124.04345259929687",
        },
        "54ef": "{}",
        "8b94": "https%3A%2F%2Fwww.bilibili.com%2F",
        "df35": uuid,
        "07a4": "zh-CN",
        "5f45": None,
        "db46": 0,
    }
    return json.dumps(
        {"payload": json.dumps(content, separators=(",", ":"))},
        separators=(",", ":"),
    )


def _buvid_fp(payload: str) -> str:
    digest = _murmur3_x64_128(payload.encode("utf-8"))
    return f"{digest & _MASK64:x}{digest >> 64:x}"


class Credentials:
    """sessdata / bili_jct / buvid3 / buvid4 / DedeUserID；没有也能读，有登录信息更稳。"""

    def __init__(self, sessdata: str = "", bili_jct: str = "", buvid3: str = "",
                 buvid4: str = "", dedeuserid: str = ""):
        self.sessdata = str(sessdata or "").strip()
        self.bili_jct = str(bili_jct or "").strip()
        self.buvid3 = str(buvid3 or "").strip()
        self.buvid4 = str(buvid4 or "").strip()
        self.dedeuserid = str(dedeuserid or "").strip()

    @property
    def ok(self) -> bool:
        return bool(self.sessdata)

    @property
    def cookie(self) -> str:
        parts = []
        if self.sessdata:
            parts.append("SESSDATA=" + self.sessdata)
        if self.bili_jct:
            parts.append("bili_jct=" + self.bili_jct)
        if self.buvid3:
            parts.append("buvid3=" + self.buvid3)
        if self.buvid4:
            parts.append("buvid4=" + self.buvid4)
        if self.dedeuserid:
            parts.append("DedeUserID=" + self.dedeuserid)
        return "; ".join(parts)

    @classmethod
    def parse(cls, raw) -> "Credentials":
        if not raw:
            return cls()
        if isinstance(raw, dict):
            lowered = {str(k).lower(): v for k, v in raw.items()}
            return cls(lowered.get("sessdata", ""), lowered.get("bili_jct", ""),
                       lowered.get("buvid3", ""), lowered.get("buvid4", ""),
                       lowered.get("dedeuserid", ""))
        found = {}
        for part in str(raw).split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                found[key.strip().lower()] = value.strip()
        return cls(found.get("sessdata", ""), found.get("bili_jct", ""),
                   found.get("buvid3", ""), found.get("buvid4", ""),
                   found.get("dedeuserid", ""))


class BiliClient:
    def __init__(self, credentials: Credentials | None = None, *,
                 interval_ms: int = 600, hourly: int = 600, daily: int = 3000,
                 timeout: int = 20):
        self.credentials = credentials or Credentials()
        self.interval = max(0, int(interval_ms)) / 1000
        self.hourly = max(1, int(hourly))
        self.daily = max(1, int(daily))
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._hour: deque[float] = deque()
        self._day: deque[float] = deque()
        self._mixin = ""
        self._mixin_at = 0.0
        self._blocked_until = 0.0
        self._trips = 0
        self._buvid = ""
        self._buvid4 = ""
        self._b_nut = 0
        self._use_fp = False
        self._coin_left = 0      # 账号剩余硬币，coin_balance() 查一次缓存一段时间
        self._coin_at = 0.0

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=True,  # 让容器里的 HTTP(S)_PROXY 生效，机房 IP 被风控时可走代理
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def quota(self) -> dict:
        now = time.time()
        self._hour = deque(t for t in self._hour if now - t < 3600)
        self._day = deque(t for t in self._day if now - t < 86400)
        return {
            "hourly_used": len(self._hour),
            "hourly_limit": self.hourly,
            "daily_used": len(self._day),
            "daily_limit": self.daily,
            "blocked_seconds": max(0, int(self._blocked_until - now)),
            "trips": self._trips,
        }

    async def _trip(self, reason: str):
        self._trips += 1
        delay = BACKOFF[min(self._trips - 1, len(BACKOFF) - 1)]
        self._blocked_until = time.time() + delay
        logger.warning("[bili_whale] %s，熔断 %s 秒", reason, delay)

    async def _before(self):
        now = time.time()
        if now < self._blocked_until:
            raise BiliBlocked(f"B站熔断中，{int(self._blocked_until - now)} 秒后重试")
        async with self._lock:
            now = time.time()
            self._hour = deque(t for t in self._hour if now - t < 3600)
            self._day = deque(t for t in self._day if now - t < 86400)
            if len(self._hour) >= self.hourly or len(self._day) >= self.daily:
                raise BiliError("读取配额已用尽，请稍后再试")
            wait = self._last + self.interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.time()
            self._last = now
            self._hour.append(now)
            self._day.append(now)

    async def _ensure_buvid(self):
        """保证 spi 指纹已就绪（只在需要真指纹时调用）。

        写操作默认不用它：bili-toolbox 实测 Cookie 里带空的 buvid3 反而能过，
        带一个「有但没激活」的 buvid3 会被判 403「账号异常,操作失败」。
        """
        if self._buvid:
            return
        if self.credentials.buvid3 and not self._use_fp:
            return
        await self._fetch_buvid()

    async def _fetch_buvid(self):
        """向 spi 申请匿名 buvid3/buvid4，随后做一次 Gaia 激活。"""
        try:
            async with (await self.session()).get(
                API + "/x/frontend/finger/spi",
                headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
            ) as resp:
                if resp.status != 200:
                    return
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return
        data = (payload or {}).get("data") or {}
        self._buvid = str(data.get("b_3") or "")
        self._buvid4 = str(data.get("b_4") or "")
        if self._buvid:
            await self._activate_buvid()

    async def _activate_buvid(self):
        """把 spi 拿到的匿名 buvid3 上报指纹（ExClimbWuzhi）激活。

        未激活的 buvid3 在写接口上等同于没有，激活失败只记日志不阻断请求。
        """
        uuid = _gen_uuid_infoc()
        body = _finger_payload(uuid)
        cookie = "; ".join([
            "buvid3=" + self._buvid,
            "buvid4=" + self._buvid4,
            "buvid_fp=" + _buvid_fp(body),
            "_uuid=" + uuid,
        ])
        try:
            async with (await self.session()).post(
                _GAIA_ACTIVE, data=body.encode("utf-8"),
                headers={
                    "User-Agent": UA,
                    "Referer": "https://www.bilibili.com/",
                    "Content-Type": "application/json",
                    "Cookie": cookie,
                },
            ) as resp:
                if resp.status != 200:
                    logger.warning("[bili_whale] buvid3 激活失败：HTTP %s", resp.status)
                    return
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("[bili_whale] buvid3 激活失败：%s", type(exc).__name__)
            return
        code = (payload or {}).get("code")
        if code != 0:
            logger.warning("[bili_whale] buvid3 激活被拒：code=%s %s",
                           code, (payload or {}).get("message") or "")
            return
        self._b_nut = int(time.time())

    def _cookie(self, bare: bool = False) -> str:
        """拼 Cookie。

        bare=True 是写操作口径，对齐 bili-toolbox：带登录态与 DedeUserID，
        buvid3/buvid4 留空。完全不带 buvid 这两个 key 会判 -401「非法访问」，
        带一个没激活的值会判 403「账号异常,操作失败」，空值反而是实测能过的那种。
        """
        parts = []
        if self.credentials.sessdata:
            parts.append("SESSDATA=" + self.credentials.sessdata)
        if self.credentials.bili_jct:
            parts.append("bili_jct=" + self.credentials.bili_jct)
        if self.credentials.dedeuserid:
            parts.append("DedeUserID=" + self.credentials.dedeuserid)
        if bare:
            parts.append('buvid3=""')
            parts.append('buvid4=""')
            return "; ".join(parts)
        # 读接口沿用配置里的登录指纹；没有就退回 spi 指纹，两套不混用。
        if self.credentials.buvid3 and not self._use_fp:
            parts.append("buvid3=" + self.credentials.buvid3)
            if self.credentials.buvid4:
                parts.append("buvid4=" + self.credentials.buvid4)
        else:
            if self._buvid:
                parts.append("buvid3=" + self._buvid)
            if self._buvid4:
                parts.append("buvid4=" + self._buvid4)
        if not self._b_nut:
            self._b_nut = int(time.time())
        parts.append("b_nut=" + str(self._b_nut))
        return "; ".join(parts)

    def _headers(self, referer: str = "", bare: bool = False) -> dict:
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://www.bilibili.com",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        cookie = self._cookie(bare)
        if cookie:
            headers["Cookie"] = cookie
        if referer:
            headers["Referer"] = referer
        return headers

    def media_headers(self, referer: str = "") -> dict:
        """给 ffmpeg 直连取流用的请求头（含 Cookie 指纹），与接口请求保持同一身份。"""
        return self._headers(referer)

    async def _refresh_mixin(self):
        data = await self._json("x/web-interface/nav", {},
                                referer="https://www.bilibili.com/", allow=(-101,))
        wbi = (data or {}).get("wbi_img") or {}
        img = _key_of(wbi.get("img_url", ""))
        sub = _key_of(wbi.get("sub_url", ""))
        if len(img) < 32 or len(sub) < 32:
            raise BiliError("无法获取 WBI 签名密钥")
        self._mixin = "".join((img + sub)[i] for i in MIXIN_TAB)[:32]
        self._mixin_at = time.time()

    async def _json(self, path: str, params: dict, *, signed: bool = False,
                    referer: str = "", allow: tuple = (),
                    trip_on_412: bool = True) -> dict | list | None:
        url = path if path.startswith("http") else API + "/" + path
        query = dict(params)
        if signed:
            if not self._mixin or time.time() - self._mixin_at > 3600:
                await self._refresh_mixin()
            # _sign 返回的是已经百分号编码好的 query。不能再交给 aiohttp 的
            # params，否则会二次编码（中文变 %25E8...），搜索词会整个失效。
            url += ("&" if "?" in url else "?") + _sign(query, self._mixin)
            params = None
        else:
            params = query
        await self._ensure_buvid()
        await self._before()
        session = await self.session()
        try:
            async with session.get(url, params=params,
                                   headers=self._headers(referer)) as resp:
                if resp.status == 412:
                    if trip_on_412:
                        await self._trip("HTTP 412")
                        raise BiliBlocked("B站拒绝了请求（412），已暂停读取")
                    raise BiliBlocked("B站拒绝了播放流请求（412）")
                if resp.status != 200:
                    raise BiliError(f"B站接口 HTTP {resp.status}")
                text = await resp.text()
        except aiohttp.ClientError as exc:
            raise BiliError(f"网络请求失败：{type(exc).__name__}") from exc
        except asyncio.TimeoutError as exc:
            # aiohttp 的总超时抛的是 asyncio.TimeoutError（消息为空），
            # 不在这里转成 BiliError 的话上层只会看到一句空的失败日志
            raise BiliError(f"请求超时（{self.timeout} 秒无响应）") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BiliError("B站返回了非 JSON 内容") from exc
        code = payload.get("code") if isinstance(payload, dict) else None
        if code in (-412, -352):
            await self._trip(f"接口 code {code}")
            raise BiliBlocked("B站风控拦截，已暂停读取")
        if code != 0 and code not in allow:
            message = payload.get("message") if isinstance(payload, dict) else ""
            # 带上 code，出问题时日志和工具输出里能直接看到是哪类拒绝
            raise BiliError(f"{message or 'B站接口请求失败'}（code={code}）")
        self._trips = 0
        return payload.get("data")

    async def _raw(self, url: str, *, referer: str = "https://www.bilibili.com/") -> bytes:
        if url.startswith("//"):
            url = "https:" + url
        await self._before()
        session = await self.session()
        try:
            async with session.get(url, headers=self._headers(referer)) as resp:
                if resp.status == 412:
                    await self._trip("素材 HTTP 412")
                    raise BiliBlocked("B站拒绝了请求（412），已暂停读取")
                if resp.status != 200:
                    raise BiliError(f"素材 HTTP {resp.status}")
                return await resp.read()
        except aiohttp.ClientError as exc:
            raise BiliError(f"素材请求失败：{type(exc).__name__}") from exc
        except asyncio.TimeoutError as exc:
            raise BiliError(f"素材请求超时（{self.timeout} 秒无响应）") from exc

    async def qr_generate(self) -> tuple[str, str]:
        """申请扫码登录二维码，返回 (二维码内容 URL, qrcode_key)。"""
        session = await self.session()
        try:
            async with session.get(PASSPORT + "/x/passport-login/web/qrcode/generate",
                                   headers=self._headers("https://www.bilibili.com/")) as resp:
                if resp.status != 200:
                    raise BiliError(f"登录接口 HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise BiliError(f"登录接口连接失败：{type(exc).__name__}") from exc
        except ValueError as exc:
            raise BiliError("登录接口返回了非 JSON 内容") from exc
        data = (payload or {}).get("data") or {}
        url = str(data.get("url") or "")
        key = str(data.get("qrcode_key") or "")
        if payload.get("code") != 0 or not url or not key:
            raise BiliError(str(payload.get("message") or "登录接口返回异常"))
        return url, key

    async def qr_poll(self, key: str) -> dict:
        """轮询扫码状态。

        返回 {"state": "pending"|"scanned"|"expired"|"ok"|"failed", ...}，
        成功时附带 cookie（SESSDATA/bili_jct/buvid3/buvid4）与 mid。
        """
        session = await self.session()
        try:
            async with session.get(
                PASSPORT + "/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": key, "source": "main-fe-header"},
                headers=self._headers("https://www.bilibili.com/"),
            ) as resp:
                if resp.status != 200:
                    raise BiliError(f"登录轮询 HTTP {resp.status}")
                payload = await resp.json(content_type=None)
                cookies = resp.headers.getall("Set-Cookie", [])
        except aiohttp.ClientError as exc:
            raise BiliError(f"登录轮询连接失败：{type(exc).__name__}") from exc
        except ValueError as exc:
            raise BiliError("登录轮询返回了非 JSON 内容") from exc
        if payload.get("code") != 0:
            raise BiliError(str(payload.get("message") or "登录轮询失败"))
        data = payload.get("data") or {}
        code = int(data.get("code") or 0)
        if code == 86101:
            return {"state": "pending"}
        if code == 86090:
            return {"state": "scanned"}
        if code == 86038:
            return {"state": "expired"}
        if code != 0:
            return {"state": "failed",
                    "message": str(data.get("message") or f"code {code}")}
        found = {}
        for raw in cookies:
            head = str(raw).split(";", 1)[0]
            if "=" in head:
                name, value = head.split("=", 1)
                found[name.strip()] = value.strip()
        for name, values in parse_qs(urlparse(str(data.get("url") or "")).query).items():
            if values:
                found.setdefault(name, values[0])
        sessdata = found.get("SESSDATA", "")
        if not sessdata:
            return {"state": "failed", "message": "登录成功但没拿到 SESSDATA"}
        parts = [f"SESSDATA={sessdata}"]
        if found.get("bili_jct"):
            parts.append("bili_jct=" + found["bili_jct"])
        if found.get("DedeUserID"):
            parts.append("DedeUserID=" + found["DedeUserID"])
        if found.get("buvid3"):
            parts.append("buvid3=" + found["buvid3"])
        if found.get("buvid4"):
            parts.append("buvid4=" + found["buvid4"])
        return {"state": "ok", "cookie": "; ".join(parts), "mid": found.get("DedeUserID", "")}

    async def view(self, code: str) -> dict:
        """按 BV号或 aid 读视频信息。"""
        key = str(code or "").strip()
        if re.fullmatch(r"BV[0-9A-Za-z]{10}", key):
            params = {"bvid": key}
        elif key.isdigit():
            params = {"aid": int(key)}
        else:
            raise BiliError("视频号格式不正确")
        referer = f"https://www.bilibili.com/video/{key}"
        data = await self._json("x/web-interface/view", params, referer=referer)
        if not isinstance(data, dict):
            raise BiliError("视频信息为空，可能已失效")
        return data

    async def resolve_video(self, link: str) -> str:
        """把分享链接（含 b23.tv 短链）还原成 BV号或 aid 字符串。"""
        text = str(link or "").strip()
        found = (re.search(r"BV[0-9A-Za-z]{10}", text)
                 or re.search(r"(?:bilibili://video/|/video/av|^av)(\d+)", text, re.I))
        if found:
            return found.group(1) if found.groups() else found.group(0)
        if not text.startswith("http"):
            if "b23.tv" not in text:
                raise BiliError("分享内容里没有视频号")
            text = "https://" + text
        await self._before()
        try:
            async with (await self.session()).get(
                    text, headers=self._headers(), allow_redirects=True) as resp:
                final = str(resp.url)
        except aiohttp.ClientError as exc:
            raise BiliError(f"短链解析失败：{type(exc).__name__}") from exc
        found = (re.search(r"BV[0-9A-Za-z]{10}", final)
                 or re.search(r"/video/av(\d+)", final, re.I))
        if not found:
            raise BiliError("短链里没有找到视频")
        return found.group(1) if found.groups() else found.group(0)

    async def subtitles(self, bvid: str, cid: int) -> list[dict]:
        referer = f"https://www.bilibili.com/video/{bvid}"
        data = await self._json("x/player/wbi/v2", {"bvid": bvid, "cid": int(cid)},
                                signed=True, referer=referer)
        subs = ((data or {}).get("subtitle") or {}).get("subtitles") or []
        return [s for s in subs if s.get("subtitle_url")]

    async def subtitle_body(self, url: str) -> list[dict]:
        raw = await self._raw(url)
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise BiliError("字幕内容格式异常") from exc
        body = payload.get("body") if isinstance(payload, dict) else None
        return body or []

    async def danmaku(self, cid: int) -> list[tuple[float, str]]:
        raw = await self._raw(f"https://comment.bilibili.com/{int(cid)}.xml")
        text = _decompress(raw)
        items = []
        for match in _DANMAKU.finditer(text):
            parts = match.group(1).split(",")
            try:
                at = float(parts[0])
            except (ValueError, IndexError):
                continue
            items.append((at, html.unescape(match.group(2))))
        return items

    async def popular(self, *, limit: int = 20) -> list[dict]:
        """热门视频列表，用于自己「刷视频」时挑内容。"""
        data = await self._json("x/web-interface/popular", {
            "ps": min(50, max(1, int(limit))), "pn": 1,
        }, referer="https://www.bilibili.com/v/popular/all/")
        items = (data or {}).get("list") if isinstance(data, dict) else []
        return [it for it in (items or []) if isinstance(it, dict)]

    async def ranking(self, *, limit: int = 20) -> list[dict]:
        """全站排行榜，作为热门之外的另一个「刷视频」来源。"""
        data = await self._json("x/web-interface/ranking/v2", {
            "rid": 0, "type": "all",
        }, referer="https://www.bilibili.com/v/popular/rank/all")
        items = (data or {}).get("list") if isinstance(data, dict) else []
        return [it for it in (items or []) if isinstance(it, dict)][:max(1, int(limit))]

    async def replies(self, aid: int, *, sort: int = 2, limit: int = 8,
                      comment_type: int = 1) -> list[dict]:
        data = await self._json("x/v2/reply", {
            "type": int(comment_type), "oid": int(aid), "sort": int(sort),
            "ps": min(20, max(1, int(limit))), "pn": 1,
        }, referer="https://www.bilibili.com/")
        return ((data or {}).get("replies") or []) if isinstance(data, dict) else []

    async def dynamic_feed(self, *, limit: int = 20) -> list[dict]:
        """关注动态时间线，用于自己「刷动态」时挑内容。

        网页端这条接口对签名和指纹很敏感：先按网页端参数签名请求，再退到不签名，
        最后再试一次不带 dm_img_* 指纹占位的精简参数（指纹对不上时反而会被挡），
        都拿不到就抛错，由上层决定是否改翻个人主页。
        """
        base = {
            "timezone_offset": -480, "type": "all", "page": 1, "offset": "",
            "update_baseline": 0,
            "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
            "platform": "web", "web_location": 333.999,
        }
        full = {
            **base,
            # 网页端必带的占位参数，缺了容易被风控挡回
            "dm_img_list": "[]",
            "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
            "dm_cover_img_str": "QU5HTEUgKE5WSURJQSwgTlZJRElBIEdlRm9yY2UgR1RYIDEwNjAgNkdCIERpcmVjdDNEMTEgdnNfNV8wIHBzXzVfMCwgRDNEMTEpR29vZ2xlIEluYy4gKE5WSURJQS",
            "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
        }
        referer = "https://t.bilibili.com/"
        last: BiliError | None = None
        for params, signed in ((full, True), (full, False), (base, True)):
            try:
                data = await self._json("x/polymer/web-dynamic/v1/feed/all", params,
                                        signed=signed, referer=referer)
            except BiliBlocked:
                raise
            except BiliError as exc:
                last = exc
                continue
            items = (data or {}).get("items") if isinstance(data, dict) else []
            items = [it for it in (items or []) if isinstance(it, dict)]
            if items:
                return items[:max(1, int(limit))]
            # 拿到了 code 0：参数与身份是被认可的，这时空就是真的没有动态，
            # 继续换变体只会白白多打几次请求、甚至把自己打进风控
            return []
        if last is not None:
            raise last
        return []

    async def following(self, mid: int, *, limit: int = 10) -> list[dict]:
        """我关注的 UP 主列表，按关注时间倒序。"""
        data = await self._json("x/relation/followings", {
            "vmid": int(mid), "pn": 1, "ps": min(50, max(1, int(limit))),
            "order": "desc", "order_type": "attention", "type": 0,
        }, signed=True, referer=f"https://space.bilibili.com/{int(mid)}/fans/follow")
        items = (data or {}).get("list") if isinstance(data, dict) else []
        return [it for it in (items or []) if isinstance(it, dict)][:max(1, int(limit))]

    async def dynamic_detail(self, dynamic_id) -> dict:
        """读一条动态的完整结构（正文、卡片、统计）。"""
        data = await self._json("x/polymer/web-dynamic/v1/detail",
                                {"id": str(dynamic_id)}, signed=True,
                                referer=f"https://t.bilibili.com/{dynamic_id}")
        item = (data or {}).get("item") if isinstance(data, dict) else None
        return item if isinstance(item, dict) else {}

    async def dynamic_publish(self, text: str) -> dict:
        """发一条纯文本动态；新版 JSON 接口失败时回退旧版表单接口。"""
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法发动态")
        payload = {"dyn_req": {
            "content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]},
            "scene": 1,
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
        }}
        try:
            data = await self._post_form(
                f"{API}/x/dynamic/feed/create/dyn?platform=web"
                f"&csrf={self.credentials.bili_jct}",
                {}, referer="https://t.bilibili.com/", json_body=payload)
        except BiliBlocked:
            raise
        except BiliError:
            form = {"dynamic_id": 0, "type": 4, "rid": 0, "content": str(text),
                    "up_choose_comment": 0, "up_close_comment": 0,
                    "csrf": self.credentials.bili_jct,
                    "csrf_token": self.credentials.bili_jct}
            data = await self._post_form(
                "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create",
                form, referer="https://t.bilibili.com/")
        return data if isinstance(data, dict) else {}

    async def space_card(self, mid: int) -> dict:
        """UP主名片：昵称、签名、粉丝数、投稿数。"""
        data = await self._json("x/web-interface/card", {
            "mid": int(mid),
        }, referer=f"https://space.bilibili.com/{int(mid)}")
        return data if isinstance(data, dict) else {}

    async def space_videos(self, mid: int, *, limit: int = 10) -> list[dict]:
        """UP主主页的最近投稿，按发布时间倒序。"""
        data = await self._json("x/space/wbi/arc/search", {
            "mid": int(mid), "ps": min(30, max(1, int(limit))), "pn": 1,
            "order": "pubdate", "platform": "web", "web_location": 1550101,
            # 网页端必带的几个占位参数，缺了容易被风控挡回
            "dm_img_list": "[]",
            "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
            "dm_cover_img_str": "QU5HTEUgKE5WSURJQSwgTlZJRElBIEdlRm9yY2UgR1RYIDEwNjAgNkdCIERpcmVjdDNEMTEgdnNfNV8wIHBzXzVfMCwgRDNEMTEpR29vZ2xlIEluYy4gKE5WSURJQS",
            "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
        }, signed=True, referer=f"https://space.bilibili.com/{int(mid)}/video")
        items = ((data or {}).get("list") or {}).get("vlist") if isinstance(data, dict) else []
        return [it for it in (items or []) if isinstance(it, dict)][:max(1, int(limit))]

    async def space_dynamics(self, mid: int, *, limit: int = 8) -> list[dict]:
        """UP主主页的动态列表（转发、图文、视频投稿都算）。"""
        params = {
            "host_mid": int(mid), "offset": "", "timezone_offset": -480,
            "features": "itemOpusStyle", "platform": "web",
            "web_location": 333.1387,
        }
        referer = f"https://space.bilibili.com/{int(mid)}/dynamic"
        try:
            data = await self._json("x/polymer/web-dynamic/v1/feed/space", params,
                                    referer=referer)
        except BiliError:
            # 部分账号/时段要求 WBI 签名，补签一次
            data = await self._json("x/polymer/web-dynamic/v1/feed/space", params,
                                    signed=True, referer=referer)
        items = (data or {}).get("items") if isinstance(data, dict) else []
        return [it for it in (items or []) if isinstance(it, dict)][:max(1, int(limit))]

    async def search(self, keyword: str, *, limit: int = 8) -> list[dict]:
        data = await self._json("x/web-interface/wbi/search/type", {
            "search_type": "video", "keyword": keyword, "page": 1,
        }, signed=True, referer="https://search.bilibili.com/")
        result = (data or {}).get("result") if isinstance(data, dict) else []
        return (result or [])[:max(1, int(limit))]

    async def _stream_urls(self, path: str, params: dict, signed: bool,
                           referer: str) -> list[str]:
        data = await self._json(path, params, signed=signed,
                                referer=referer, trip_on_412=False)
        if not isinstance(data, dict):
            return []
        videos = (data.get("dash") or {}).get("video") or []
        if videos:
            # 抽帧只要能看清画面与字幕：优先 480P 以内的最高码率，直连时也省流量
            small = [v for v in videos if 0 < int(v.get("height") or 0) <= 480]
            if small:
                best = max(small, key=lambda v: int(v.get("bandwidth") or 0))
            else:
                best = min(videos, key=lambda v: int(v.get("bandwidth") or 0))
            candidates = [best.get("baseUrl") or best.get("base_url")]
            candidates += list(best.get("backupUrl") or best.get("backup_url") or [])
            return [str(u) for u in candidates if u]
        durl = data.get("durl") or []
        if not durl:
            return []
        candidates = [durl[0].get("url")] + list(durl[0].get("backup_url") or [])
        return [str(u) for u in candidates if u]

    async def playurl(self, bvid: str, cid: int, *, qn: int = 32) -> list[str]:
        """取可下载的视频流地址：DASH → durl → 网页端 html5 三级回退。

        播放流接口的风控比普通接口严，且失败不该熔断全局读取，故关闭熔断。
        """
        referer = f"https://www.bilibili.com/video/{bvid}"
        base = {"bvid": bvid, "cid": int(cid), "qn": int(qn), "fourk": 0}
        last = "没有可用地址"
        for path, params, signed in (
            ("x/player/wbi/playurl", {**base, "fnval": 4048, "fnver": 0}, True),
            ("x/player/wbi/playurl", {**base, "fnval": 1}, True),
            ("x/player/playurl",
             {**base, "fnval": 1, "fnver": 0, "platform": "html5", "high_quality": 1},
             False),
        ):
            try:
                urls = await self._stream_urls(path, params, signed, referer)
            except BiliError as exc:
                last = str(exc)
                continue
            if urls:
                return urls
            last = "返回里没有视频流"
        raise BiliError(f"没有拿到可下载的视频流（{last}）")

    async def videoshot(self, bvid: str, cid: int) -> dict:
        """取B站官方的进度条缩略图：长视频不下载整片也能抽几帧看画面。"""
        data = await self._json("x/player/videoshot", {
            "bvid": str(bvid), "cid": int(cid), "index": 1,
        }, referer=f"https://www.bilibili.com/video/{bvid}")
        return data if isinstance(data, dict) else {}

    async def download(self, urls: list[str], path: str, *, limit_mb: float = 60,
                       referer: str = "https://www.bilibili.com/") -> int:
        """按候选地址依次尝试，写盘并做伪内容检查。"""
        limit = int(float(limit_mb) * 1024 * 1024)
        session = await self.session()
        last = ""
        for url in urls:
            if not url:
                continue
            try:
                async with session.get(
                    url, headers=self._headers(referer),
                    timeout=aiohttp.ClientTimeout(total=300, sock_read=60),
                ) as resp:
                    if resp.status != 200:
                        last = f"HTTP {resp.status}"
                        continue
                    total = 0
                    head = b""
                    with open(path, "wb") as handle:
                        async for chunk in resp.content.iter_chunked(1 << 16):
                            total += len(chunk)
                            if total > limit:
                                raise BiliError(f"视频超过 {limit_mb}MB 上限")
                            if not head:
                                head = chunk[:12]
                            handle.write(chunk)
                    if not head.lstrip()[:1] or head.lstrip()[:1] == b"<":
                        raise BiliError("下载到的内容不是视频")
                    return total
            except aiohttp.ClientError as exc:
                last = type(exc).__name__
            except asyncio.TimeoutError:
                last = "下载超时"
        raise BiliError(f"视频下载失败：{last or '没有可用地址'}")

    async def _ensure_mid(self):
        """写操作前补齐 DedeUserID。

        旧版扫码只存了 SESSDATA/bili_jct，配置里往往缺 DedeUserID；而
        bili-toolbox 的写请求是带着它的，缺了容易被判 -403。这里用 nav 补一次。
        """
        if self.credentials.dedeuserid:
            return
        try:
            data = await self._json("x/web-interface/nav", {},
                                    referer="https://www.bilibili.com/", allow=(-101,))
        except BiliBlocked:
            raise
        except BiliError:
            return
        mid = (data or {}).get("mid")
        if mid:
            self.credentials.dedeuserid = str(mid)

    async def _post_form(self, url: str, form: dict, *,
                         referer: str = "https://www.bilibili.com/",
                         allow: tuple = (), retry: bool = True,
                         json_body: dict | None = None) -> dict:
        """提交写请求（私信、评论、点赞投币收藏、发动态等统一走这里）。

        默认发 x-www-form-urlencoded 表单；传 json_body 时改发 application/json
        （新版发动态接口只认 JSON）。默认按 bili-toolbox 的口径带登录 Cookie
        （含 DedeUserID）+ 空的 buvid3/buvid4。
        """
        await self._before()
        await self._ensure_mid()
        session = await self.session()
        headers = self._headers(referer, bare=not self._use_fp)
        if json_body is None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = {k: str(v) for k, v in form.items()}
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body, ensure_ascii=False)
        try:
            async with session.post(url, data=data, headers=headers) as resp:
                if resp.status == 412:
                    await self._trip("HTTP 412")
                    raise BiliBlocked("B站拒绝了请求（412），已暂停发送")
                if resp.status != 200:
                    raise BiliError(f"B站接口 HTTP {resp.status}")
                text = await resp.text()
        except aiohttp.ClientError as exc:
            raise BiliError(f"网络请求失败：{type(exc).__name__}") from exc
        except asyncio.TimeoutError as exc:
            raise BiliError(f"请求超时（{self.timeout} 秒无响应）") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BiliError("B站返回了非 JSON 内容") from exc
        if not isinstance(payload, dict):
            raise BiliError("B站接口返回异常")
        code = payload.get("code")
        if code in (-412, -352):
            await self._trip(f"接口 code {code}")
            raise BiliBlocked("B站风控拦截，已暂停发送")
        if code == 403 and retry and not self._use_fp:
            # 空 buvid3 也被判 403 时，换成「spi 新签发 + Gaia 激活」的指纹再试一次，
            # 之后固定用这套，不再反复换身份（连续换指纹本身也会被风控盯上）。
            logger.warning("[bili_whale] 写操作被判 403，改用激活指纹重试")
            self._use_fp = True
            self._buvid = ""
            self._buvid4 = ""
            await self._ensure_buvid()
            return await self._post_form(url, form, referer=referer, allow=allow,
                                         retry=False, json_body=json_body)
        if code != 0 and code not in allow:
            message = str(payload.get("message") or "接口返回异常")
            err = BiliError(f"{message}（code={code}）")
            # 把业务码挂到异常上，调用方可以据此区分「这条内容不让评论」和真正的失败
            err.code = code if isinstance(code, int) else None
            raise err
        self._trips = 0
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def nav(self) -> dict:
        """取当前登录身份：mid / uname / isLogin / wbi_img。"""
        await self._ensure_buvid()
        data = await self._json("x/web-interface/nav", {},
                                referer="https://www.bilibili.com/", allow=(-101,))
        return data if isinstance(data, dict) else {}

    async def relation(self, fid: int) -> int:
        """查询与某用户的关注关系：6 表示互关（只有互关能发私信）。"""
        data = await self._json("x/relation", {"fid": int(fid)},
                                referer=f"https://space.bilibili.com/{int(fid)}")
        return int((data or {}).get("attribute") or 0)

    async def relation_modify(self, fid: int, act: int = 1) -> dict:
        """关注（act=1）或取关（act=2）一个 UP 主。"""
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法关注")
        form = {"fid": int(fid), "act": int(act), "re_src": 11,
                "csrf": self.credentials.bili_jct}
        return await self._post_form(
            f"{API}/x/relation/modify", form,
            referer=f"https://space.bilibili.com/{int(fid)}")

    async def fetch_session_msgs(self, talker_id: int, begin_seqno: int = 0,
                                 size: int = 30) -> dict:
        """拉取与 talker 的私信记录（WBI 签名，begin_seqno 用于往回翻页）。"""
        params = {"talker_id": int(talker_id), "session_type": 1,
                  "size": min(30, max(1, int(size)))}
        if begin_seqno:
            params["begin_seqno"] = int(begin_seqno)
        data = await self._json(
            "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs",
            params, signed=True, referer="https://message.bilibili.com/")
        return data if isinstance(data, dict) else {}

    async def send_msg(self, sender_uid: int, receiver_id: int, text: str,
                       dev_id: str) -> dict:
        """发送一条文本私信；dev_id 必须长期复用，换新会被 B 站拒绝。"""
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法发送私信")
        form = {
            "msg[sender_uid]": int(sender_uid),
            "msg[receiver_id]": int(receiver_id),
            "msg[receiver_type]": 1,
            "msg[msg_type]": 1,
            "msg[msg_status]": 0,
            "msg[timestamp]": int(time.time()),
            "msg[dev_id]": str(dev_id),
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "csrf": self.credentials.bili_jct,
        }
        return await self._post_form(
            "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg", form,
            referer="https://message.bilibili.com/")

    async def msgfeed(self, kind: str) -> dict:
        """拉取「回复我的」或「@我的」最新一页消息。"""
        if kind not in ("reply", "at"):
            raise BiliError(f"未知的消息类型：{kind}")
        params = {"platform": "web", "build": 0, "mobi_app": "web"}
        data = await self._json(f"x/msgfeed/{kind}", params,
                                referer="https://message.bilibili.com/")
        return data if isinstance(data, dict) else {}

    async def reply_add(self, oid: int, message: str, root: int = 0,
                        parent: int = 0, referer: str = "",
                        *, comment_type: int = 1) -> dict:
        """在评论区发表评论；comment_type 与 oid 配对决定评论区。

        type 取 1=视频（oid 为 aid）、12=专栏、17=动态（oid 为动态 id）等；
        root/parent 用于接在指定评论下面。
        """
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法发表评论")
        form = {
            "oid": int(oid),
            "type": int(comment_type),
            "message": message,
            "plat": 1,
            "csrf": self.credentials.bili_jct,
        }
        if root:
            form["root"] = int(root)
        if parent:
            form["parent"] = int(parent)
        return await self._post_form(
            "https://api.bilibili.com/x/v2/reply/add", form,
            referer=referer or "https://www.bilibili.com/")

    async def dynamic_thumb(self, dyn_id, up: int = 1) -> dict:
        """给一条动态点赞（up=1）或取消点赞（up=2）。"""
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法给动态点赞")
        dyn = str(dyn_id).strip()
        if not dyn.isdigit():
            raise BiliError("动态号不合法，无法给动态点赞")
        form = {
            "dyn_id": dyn,
            "up": int(up),
            "csrf": self.credentials.bili_jct,
        }
        return await self._post_form(
            f"{API}/x/dynamic/feed/dyn/thumb", form,
            referer=f"https://t.bilibili.com/{dyn}")

    async def archive_like(self, aid: int) -> dict:
        """给视频点赞（aid）。"""
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法点赞")
        form = {"aid": int(aid), "like": 1, "csrf": self.credentials.bili_jct}
        return await self._post_form(
            f"{API}/x/web-interface/archive/like", form,
            referer=f"https://www.bilibili.com/video/av{int(aid)}")

    async def coin_balance(self, ttl: float = 300.0) -> int | None:
        """查账号剩余硬币数（nav 的 money 字段），缓存 ttl 秒；查不到返回 None。

        nav 请求失败时返回 None，调用方据此跳过余额校验、交给 B 站自己判断，
        免得因为一次网络抖动就把投币动作整个挡掉。
        """
        now = time.time()
        if self._coin_at and now - self._coin_at < ttl:
            return self._coin_left
        try:
            data = await self.nav()
        except BiliError:
            return None
        raw = data.get("money")
        try:
            left = int(raw)
        except (TypeError, ValueError):
            return None
        self._coin_left = max(0, left)
        self._coin_at = now
        return self._coin_left

    async def coin_add(self, aid: int, multiply: int = 2, *,
                       bvid: str = "", select_like: bool = True) -> dict:
        """给视频投币，multiply 为 1（单币）或 2（双币）。

        实测只投币不点赞（select_like=0）会被风控直接判 -401「非法访问」，
        属于典型刷币特征，故默认 select_like=True 固定附带点赞。
        投币前先看账号还剩多少币：没币直接报错，只剩 1 枚就只投 1 枚。
        """
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法投币")
        left = await self.coin_balance()
        if left is not None:
            if left <= 0:
                raise BiliError("账号硬币已用完（剩 0 枚），这次不投币")
            if multiply > left:
                multiply = left
        form = {
            "aid": int(aid),
            "multiply": 2 if int(multiply) >= 2 else 1,
            "select_like": 1 if select_like else 0,
            "csrf": self.credentials.bili_jct,
        }
        if bvid:
            form["bvid"] = str(bvid)
        page = f"https://www.bilibili.com/video/{bvid}" if bvid \
            else f"https://www.bilibili.com/video/av{int(aid)}"
        data = await self._post_form(
            f"{API}/x/web-interface/coin/add", form, referer=page)
        if self._coin_at:  # 余额已知就顺手扣掉，下一次不用重新查
            self._coin_left = max(0, self._coin_left - form["multiply"])
            self._coin_at = time.time()
        # 硬币不够时上面会把 multiply 降下来，回填实际币数好让调用方如实汇报
        data["multiply"] = form["multiply"]
        return data

    async def share(self, aid: int, bvid: str = "") -> dict:
        """分享视频（等价于点网页「分享」按钮，不产生动态）。"""
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法分享")
        form = {"aid": int(aid), "csrf": self.credentials.bili_jct}
        if bvid:
            form["bvid"] = str(bvid)
        page = f"https://www.bilibili.com/video/{bvid}" if bvid \
            else f"https://www.bilibili.com/video/av{int(aid)}"
        return await self._post_form(
            f"{API}/x/web-interface/share/add", form, referer=page)

    async def triple(self, aid: int, bvid: str = "") -> dict:
        """一键三连（点赞 + 2币 + 收藏），收藏固定进默认收藏夹。

        返回 data：like / coin / fav 三个 bool 与 multiply 币数。
        """
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法一键三连")
        form = {"aid": int(aid), "csrf": self.credentials.bili_jct}
        if bvid:
            form["bvid"] = str(bvid)
        page = f"https://www.bilibili.com/video/{bvid}" if bvid \
            else f"https://www.bilibili.com/video/av{int(aid)}"
        return await self._post_form(
            f"{API}/x/web-interface/archive/like/triple", form, referer=page)

    async def fav_folder(self, up_mid: int) -> list:
        """列出该账号创建的收藏夹，默认收藏夹带 id=0 不进列表，这里取实际文件夹。"""
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法收藏")
        data = await self._json(
            "x/v3/fav/folder/created/list-all", {"up_mid": int(up_mid)},
            referer="https://www.bilibili.com/")
        if isinstance(data, dict):
            items = data.get("list")
        else:
            items = data
        return items if isinstance(items, list) else []

    async def fav_add(self, aid: int, folder_id: int) -> dict:
        """把视频（rid=aid、type=2）收进指定收藏夹。"""
        if not self.credentials.bili_jct:
            raise BiliError("缺少 bili_jct，无法收藏")
        form = {
            "rid": int(aid),
            "type": 2,
            "add_media_ids": int(folder_id),
            "csrf": self.credentials.bili_jct,
        }
        return await self._post_form(
            f"{API}/x/v3/fav/resource/deal", form,
            referer=f"https://www.bilibili.com/video/av{int(aid)}")

    async def own_mid(self) -> int:
        """取当前登录账号的 UID：收藏夹、观看历史这些接口都要用它。"""
        if self.credentials.dedeuserid:
            return int(self.credentials.dedeuserid)
        data = await self.nav()
        mid = int((data or {}).get("mid") or 0) if isinstance(data, dict) else 0
        if not mid:
            raise BiliError("没读到登录身份（Cookie 可能过期），收藏夹和观看历史需要登录")
        self.credentials.dedeuserid = str(mid)
        return mid

    async def fav_items(self, media_id: int, *, page: int = 1,
                        limit: int = 20) -> dict:
        """看某个收藏夹里的视频，返回 info / medias / has_more。"""
        data = await self._json("x/v3/fav/resource/list", {
            "media_id": int(media_id), "pn": max(1, int(page)),
            "ps": min(20, max(1, int(limit))), "keyword": "",
            "order": "mtime", "type": 0, "tid": 0, "platform": "web",
        }, referer=f"https://www.bilibili.com/medialist/detail/ml{int(media_id)}")
        return data if isinstance(data, dict) else {}

    async def watch_later(self, *, limit: int = 20) -> list[dict]:
        """稍后再看列表。"""
        data = await self._json("x/v2/history/toview", {"jsonp": "jsonp"},
                                referer="https://www.bilibili.com/watchlater/")
        items = (data or {}).get("list") if isinstance(data, dict) else []
        return [it for it in (items or []) if isinstance(it, dict)][:max(1, int(limit))]

    async def history(self, *, limit: int = 20) -> list[dict]:
        """观看历史，最近看过的在前。"""
        data = await self._json("x/web-interface/history/cursor", {
            "max": 0, "view_at": 0, "business": "",
            "ps": min(30, max(1, int(limit))), "type": "archive",
        }, referer="https://www.bilibili.com/account/history")
        items = (data or {}).get("list") if isinstance(data, dict) else []
        return [it for it in (items or []) if isinstance(it, dict)][:max(1, int(limit))]

    async def related(self, bvid: str, *, limit: int = 8) -> list[dict]:
        """相关推荐：视频页旁边那串「大家还在看」。"""
        data = await self._json("x/web-interface/archive/related",
                                {"bvid": str(bvid)},
                                referer=f"https://www.bilibili.com/video/{bvid}")
        items = data if isinstance(data, list) else []
        return [it for it in items if isinstance(it, dict)][:max(1, int(limit))]
