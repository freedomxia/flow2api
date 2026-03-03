"""Proxy management module"""
import asyncio
import contextvars
import re
import time
from typing import List, Optional, Tuple

from ..core.database import Database
from ..core.models import ProxyConfig


_BOUND_PROXY_UNSET = object()

class ProxyManager:
    """Proxy configuration manager"""

    def __init__(self, db: Database):
        self.db = db
        self._rotation_lock = asyncio.Lock()
        self._rotation_index = 0
        self._rotation_request_counter = 0
        self._rotation_failure_counter = 0
        self._rotation_window_started_at = 0.0
        self._last_selected_proxy: Optional[str] = None
        self._last_selected_at = 0.0
        self._bound_proxy_ctx: contextvars.ContextVar[object] = contextvars.ContextVar(
            "flow_bound_proxy_url",
            default=_BOUND_PROXY_UNSET
        )

    def _parse_proxy_line(self, line: str) -> Optional[str]:
        """将用户输入代理转换为标准 URL 格式。

        支持格式：
        - http://user:pass@host:port
        - https://user:pass@host:port
        - socks5://user:pass@host:port
        - socks5h://user:pass@host:port
        - socks5://host:port:user:pass
        - st5 host:port:user:pass
        - host:port
        - host:port:user:pass
        """
        if not line:
            return None

        line = line.strip()
        if not line:
            return None

        # st5 host:port:user:pass
        st5_match = re.match(r"^st5\s+(.+)$", line, re.IGNORECASE)
        if st5_match:
            rest = st5_match.group(1).strip()
            if "@" in rest:
                return f"socks5://{rest}"
            parts = rest.split(":")
            if len(parts) >= 4 and parts[1].isdigit():
                host = parts[0]
                port = parts[1]
                username = parts[2]
                password = ":".join(parts[3:])
                return f"socks5://{username}:{password}@{host}:{port}"
            return None

        # 协议前缀格式
        if line.startswith(("http://", "https://", "socks5://", "socks5h://")):
            # socks5h 统一转 socks5，便于后续处理
            if line.startswith("socks5h://"):
                line = "socks5://" + line[len("socks5h://"):]

            # 已是标准 user:pass@host:port（或 host:port）
            if "@" in line:
                return line

            # 兼容 protocol://host:port:user:pass
            try:
                protocol_end = line.index("://") + 3
                protocol = line[:protocol_end]
                rest = line[protocol_end:]
                parts = rest.split(":")
                if len(parts) >= 4 and parts[1].isdigit():
                    host = parts[0]
                    port = parts[1]
                    username = parts[2]
                    password = ":".join(parts[3:])
                    return f"{protocol}{username}:{password}@{host}:{port}"
                if len(parts) == 2 and parts[1].isdigit():
                    return line
            except Exception:
                return None
            return None

        # 无协议，带 @：默认按 http 处理
        if "@" in line:
            return f"http://{line}"

        # 无协议，按冒号数量判断
        parts = line.split(":")
        if len(parts) == 2 and parts[1].isdigit():
            # host:port
            return f"http://{parts[0]}:{parts[1]}"

        if len(parts) >= 4 and parts[1].isdigit():
            # host:port:user:pass
            host = parts[0]
            port = parts[1]
            username = parts[2]
            password = ":".join(parts[3:])
            return f"http://{username}:{password}@{host}:{port}"

        return None

    def normalize_proxy_url(self, proxy_url: Optional[str]) -> Optional[str]:
        """标准化代理地址，空值返回 None，非法格式抛 ValueError。"""
        if proxy_url is None:
            return None

        raw = proxy_url.strip()
        if not raw:
            return None

        parsed = self._parse_proxy_line(raw)
        if not parsed:
            raise ValueError(
                "代理地址格式错误，支持示例："
                "http://user:pass@host:port / "
                "socks5://user:pass@host:port / "
                "host:port:user:pass / st5 host:port:user:pass"
            )
        return parsed

    def normalize_proxy_pool(self, proxy_pool: Optional[str]) -> Optional[str]:
        """标准化多行代理池，返回去重后的多行文本。"""
        if proxy_pool is None:
            return None

        normalized_lines: List[str] = []
        seen = set()
        for line in proxy_pool.splitlines():
            raw_line = line.strip()
            if not raw_line or raw_line.startswith("#"):
                continue
            normalized = self.normalize_proxy_url(raw_line)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            normalized_lines.append(normalized)

        return "\n".join(normalized_lines) if normalized_lines else None

    def _normalize_rotation_mode(self, rotation_mode: Optional[str]) -> str:
        mode = (rotation_mode or "fixed").strip().lower()
        if mode not in {"fixed", "by_request_count", "by_time_window", "by_consecutive_failures"}:
            return "fixed"
        return mode

    def _build_request_proxy_candidates(self, config: Optional[ProxyConfig]) -> List[str]:
        if not config or not config.enabled:
            return []

        candidates: List[str] = []
        seen = set()

        if config.proxy_pool_enabled and config.proxy_pool:
            for line in (config.proxy_pool or "").splitlines():
                proxy = line.strip()
                if not proxy or proxy in seen:
                    continue
                seen.add(proxy)
                candidates.append(proxy)
            if candidates:
                return candidates

        if config.proxy_url and config.proxy_url not in seen:
            candidates.append(config.proxy_url)

        return candidates

    def _get_bound_proxy_state(self) -> Tuple[bool, Optional[str]]:
        bound = self._bound_proxy_ctx.get()
        if bound is _BOUND_PROXY_UNSET:
            return False, None
        return True, bound

    def clear_bound_proxy(self):
        """清理当前请求链路绑定的代理（恢复未绑定状态）。"""
        self._bound_proxy_ctx.set(_BOUND_PROXY_UNSET)

    def get_bound_proxy_url(self) -> Optional[str]:
        """获取当前请求链路已绑定的代理；未绑定时返回 None。"""
        has_bound, bound_proxy = self._get_bound_proxy_state()
        if not has_bound:
            return None
        return bound_proxy

    async def _advance_time_window_if_needed(self, config: ProxyConfig, candidate_count: int):
        if candidate_count <= 1:
            self._rotation_index = 0
            self._rotation_window_started_at = time.monotonic()
            return

        rotate_every_seconds = max(1, int(config.rotate_every_seconds or 300))
        now = time.monotonic()
        if self._rotation_window_started_at <= 0:
            self._rotation_window_started_at = now
            return

        elapsed = now - self._rotation_window_started_at
        if elapsed < rotate_every_seconds:
            return

        steps = max(1, int(elapsed // rotate_every_seconds))
        self._rotation_index = (self._rotation_index + steps) % candidate_count
        self._rotation_window_started_at += steps * rotate_every_seconds

    async def _peek_request_proxy_from_config(self, config: Optional[ProxyConfig]) -> Optional[str]:
        candidates = self._build_request_proxy_candidates(config)
        if not candidates:
            return None

        mode = self._normalize_rotation_mode(config.rotation_mode if config else None)
        async with self._rotation_lock:
            if mode == "by_time_window":
                await self._advance_time_window_if_needed(config, len(candidates))
            return candidates[self._rotation_index % len(candidates)]

    async def _select_request_proxy_for_new_binding(self, config: Optional[ProxyConfig]) -> Optional[str]:
        candidates = self._build_request_proxy_candidates(config)
        if not candidates:
            return None

        mode = self._normalize_rotation_mode(config.rotation_mode if config else None)
        async with self._rotation_lock:
            if mode == "by_time_window":
                await self._advance_time_window_if_needed(config, len(candidates))
                return candidates[self._rotation_index % len(candidates)]

            if mode == "by_request_count":
                index = self._rotation_index % len(candidates)
                selected = candidates[index]
                threshold = max(1, int(config.rotate_every_requests or 1))
                self._rotation_request_counter += 1
                if self._rotation_request_counter >= threshold:
                    self._rotation_request_counter = 0
                    self._rotation_index = (self._rotation_index + 1) % len(candidates)
                return selected

            if mode == "by_consecutive_failures":
                return candidates[self._rotation_index % len(candidates)]

            self._rotation_index = 0
            self._rotation_request_counter = 0
            self._rotation_failure_counter = 0
            self._rotation_window_started_at = time.monotonic()
            return candidates[0]

    async def bind_request_proxy(self) -> Optional[str]:
        """为当前请求链路锁定一个请求代理；同链路内后续调用保持一致。"""
        has_bound, bound_proxy = self._get_bound_proxy_state()
        if has_bound:
            return bound_proxy

        config = await self.get_proxy_config()
        selected_proxy = await self._select_request_proxy_for_new_binding(config)
        self._bound_proxy_ctx.set(selected_proxy)
        self._last_selected_proxy = selected_proxy
        self._last_selected_at = time.time() if selected_proxy else 0.0
        return selected_proxy

    async def record_request_result(self, success: bool):
        """记录一次生成链路的结果，用于按连续失败次数轮换代理。"""
        config = await self.get_proxy_config()
        candidates = self._build_request_proxy_candidates(config)
        if not candidates:
            return

        mode = self._normalize_rotation_mode(config.rotation_mode if config else None)
        async with self._rotation_lock:
            if success:
                self._rotation_failure_counter = 0
                return

            if mode != "by_consecutive_failures":
                return

            threshold = max(1, int(config.rotate_every_failures or 3))
            self._rotation_failure_counter += 1
            if self._rotation_failure_counter < threshold:
                return

            self._rotation_failure_counter = 0
            if len(candidates) > 1:
                self._rotation_index = (self._rotation_index + 1) % len(candidates)

    async def get_rotation_status(self) -> dict:
        """返回当前代理轮询状态，便于管理页展示。"""
        config = await self.get_proxy_config()
        candidates = self._build_request_proxy_candidates(config)
        has_bound, bound_proxy = self._get_bound_proxy_state()

        if not candidates:
            return {
                "enabled": False,
                "candidate_count": 0,
                "rotation_mode": self._normalize_rotation_mode(config.rotation_mode if config else None),
                "current_proxy": bound_proxy if has_bound else None,
                "current_index": None,
                "request_counter": 0,
                "failure_counter": 0,
                "rotate_every_requests": max(1, int(config.rotate_every_requests or 1)) if config else 1,
                "seconds_until_rotate": None,
                "rotate_every_seconds": max(1, int(config.rotate_every_seconds or 300)) if config else 300,
                "rotate_every_failures": max(1, int(config.rotate_every_failures or 3)) if config else 3,
                "last_selected_proxy": self._last_selected_proxy,
                "last_selected_at": self._last_selected_at or None,
                "source": "none",
            }

        mode = self._normalize_rotation_mode(config.rotation_mode if config else None)
        async with self._rotation_lock:
            if mode == "by_time_window":
                await self._advance_time_window_if_needed(config, len(candidates))

            current_index = self._rotation_index % len(candidates)
            current_proxy = candidates[current_index]

            if has_bound and bound_proxy:
                current_proxy = bound_proxy
                if bound_proxy in candidates:
                    current_index = candidates.index(bound_proxy)

            seconds_until_rotate = None
            if mode == "by_time_window":
                rotate_every_seconds = max(1, int(config.rotate_every_seconds or 300))
                if self._rotation_window_started_at <= 0:
                    seconds_until_rotate = rotate_every_seconds
                else:
                    elapsed = max(0.0, time.monotonic() - self._rotation_window_started_at)
                    seconds_until_rotate = max(0, int(round(rotate_every_seconds - elapsed)))

            return {
                "enabled": bool(config and config.enabled),
                "candidate_count": len(candidates),
                "rotation_mode": mode,
                "current_proxy": current_proxy,
                "current_index": current_index,
                "request_counter": self._rotation_request_counter,
                "failure_counter": self._rotation_failure_counter,
                "rotate_every_requests": max(1, int(config.rotate_every_requests or 1)),
                "seconds_until_rotate": seconds_until_rotate,
                "rotate_every_seconds": max(1, int(config.rotate_every_seconds or 300)),
                "rotate_every_failures": max(1, int(config.rotate_every_failures or 3)),
                "last_selected_proxy": self._last_selected_proxy,
                "last_selected_at": self._last_selected_at or None,
                "source": "pool" if config and config.proxy_pool_enabled and len(candidates) > 1 else "single",
            }

    async def get_proxy_url(self) -> Optional[str]:
        """兼容旧调用：返回请求代理地址"""
        return await self.get_request_proxy_url()

    async def get_request_proxy_url(self) -> Optional[str]:
        """Get request proxy URL if enabled, otherwise return None"""
        has_bound, bound_proxy = self._get_bound_proxy_state()
        if has_bound:
            return bound_proxy

        config = await self.get_proxy_config()
        return await self._peek_request_proxy_from_config(config)

    async def get_browser_proxy_url(self, bind_if_missing: bool = False) -> Optional[str]:
        """获取浏览器打码代理。

        开启“验证码代理跟随请求代理”时，优先返回当前请求绑定代理；未绑定且要求绑定时，
        会为当前请求链路锁定一个请求代理。
        """
        has_bound, bound_proxy = self._get_bound_proxy_state()
        if has_bound:
            return bound_proxy

        proxy_config = await self.get_proxy_config()
        if proxy_config and proxy_config.sync_browser_proxy and proxy_config.enabled:
            if bind_if_missing:
                return await self.bind_request_proxy()
            return await self.get_request_proxy_url()

        captcha_config = await self.db.get_captcha_config()
        if captcha_config and captcha_config.browser_proxy_enabled and captcha_config.browser_proxy_url:
            return captcha_config.browser_proxy_url
        return None

    async def get_media_proxy_url(self) -> Optional[str]:
        """Get media upload/download proxy URL, fallback to request proxy"""
        config = await self.db.get_proxy_config()
        if config and config.media_proxy_enabled and config.media_proxy_url:
            return config.media_proxy_url
        return await self.get_request_proxy_url()

    async def update_proxy_config(
        self,
        enabled: bool,
        proxy_url: Optional[str],
        proxy_pool_enabled: Optional[bool] = None,
        proxy_pool: Optional[str] = None,
        rotation_mode: Optional[str] = None,
        rotate_every_requests: Optional[int] = None,
        rotate_every_seconds: Optional[int] = None,
        rotate_every_failures: Optional[int] = None,
        sync_browser_proxy: Optional[bool] = None,
        media_proxy_enabled: Optional[bool] = None,
        media_proxy_url: Optional[str] = None
    ):
        """Update proxy configuration"""
        normalized_proxy_url = self.normalize_proxy_url(proxy_url)
        normalized_proxy_pool = self.normalize_proxy_pool(proxy_pool)
        normalized_rotation_mode = self._normalize_rotation_mode(rotation_mode)
        normalized_rotate_every_requests = (
            max(1, int(rotate_every_requests or 1))
            if rotate_every_requests is not None
            else None
        )
        normalized_rotate_every_seconds = (
            max(1, int(rotate_every_seconds or 300))
            if rotate_every_seconds is not None
            else None
        )
        normalized_rotate_every_failures = (
            max(1, int(rotate_every_failures or 3))
            if rotate_every_failures is not None
            else None
        )
        normalized_media_proxy_url = self.normalize_proxy_url(media_proxy_url)

        await self.db.update_proxy_config(
            enabled=enabled,
            proxy_url=normalized_proxy_url,
            proxy_pool_enabled=proxy_pool_enabled,
            proxy_pool=normalized_proxy_pool,
            rotation_mode=normalized_rotation_mode if rotation_mode is not None else None,
            rotate_every_requests=normalized_rotate_every_requests,
            rotate_every_seconds=normalized_rotate_every_seconds,
            rotate_every_failures=normalized_rotate_every_failures,
            sync_browser_proxy=sync_browser_proxy,
            media_proxy_enabled=media_proxy_enabled,
            media_proxy_url=normalized_media_proxy_url
        )
        async with self._rotation_lock:
            self._rotation_index = 0
            self._rotation_request_counter = 0
            self._rotation_failure_counter = 0
            self._rotation_window_started_at = 0.0
            self._last_selected_proxy = None
            self._last_selected_at = 0.0

    async def get_proxy_config(self) -> ProxyConfig:
        """Get proxy configuration"""
        config = await self.db.get_proxy_config()
        return config or ProxyConfig()
