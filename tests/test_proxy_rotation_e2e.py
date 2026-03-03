import asyncio
import tempfile
import unittest
from pathlib import Path

from src.core.database import Database
from src.services.proxy_manager import ProxyManager


class ProxyRotationE2ETest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp_dir.name) / "test_flow.db")
        self.db = Database(self.db_path)
        await self.db.init_db()
        await self.db.init_config_from_toml({}, is_first_startup=True)
        self.proxy_manager = ProxyManager(self.db)

    async def asyncTearDown(self):
        self.proxy_manager.clear_bound_proxy()
        self._tmp_dir.cleanup()

    async def test_request_count_rotation_keeps_browser_and_request_proxy_aligned(self):
        await self.proxy_manager.update_proxy_config(
            enabled=True,
            proxy_url="http://solo-user:solo-pass@10.0.0.9:8009",
            proxy_pool_enabled=True,
            proxy_pool="\n".join([
                "10.0.0.1:8001:user1:pass1",
                "10.0.0.2:8002:user2:pass2",
            ]),
            rotation_mode="by_request_count",
            rotate_every_requests=2,
            rotate_every_seconds=300,
            sync_browser_proxy=True,
            media_proxy_enabled=False,
            media_proxy_url=None,
        )

        selections = []
        browser_selections = []
        for _ in range(5):
            request_proxy = await self.proxy_manager.bind_request_proxy()
            browser_proxy = await self.proxy_manager.get_browser_proxy_url()
            selections.append(request_proxy)
            browser_selections.append(browser_proxy)
            self.proxy_manager.clear_bound_proxy()

        expected = [
            "http://user1:pass1@10.0.0.1:8001",
            "http://user1:pass1@10.0.0.1:8001",
            "http://user2:pass2@10.0.0.2:8002",
            "http://user2:pass2@10.0.0.2:8002",
            "http://user1:pass1@10.0.0.1:8001",
        ]

        self.assertEqual(selections, expected)
        self.assertEqual(browser_selections, expected)

    async def test_time_window_rotation_switches_after_threshold(self):
        await self.proxy_manager.update_proxy_config(
            enabled=True,
            proxy_url=None,
            proxy_pool_enabled=True,
            proxy_pool="\n".join([
                "http://user1:pass1@10.0.0.1:8001",
                "http://user2:pass2@10.0.0.2:8002",
            ]),
            rotation_mode="by_time_window",
            rotate_every_requests=1,
            rotate_every_seconds=1,
            sync_browser_proxy=True,
            media_proxy_enabled=False,
            media_proxy_url=None,
        )

        first = await self.proxy_manager.bind_request_proxy()
        self.proxy_manager.clear_bound_proxy()
        second = await self.proxy_manager.bind_request_proxy()
        self.proxy_manager.clear_bound_proxy()
        await asyncio.sleep(1.05)
        third = await self.proxy_manager.bind_request_proxy()
        self.proxy_manager.clear_bound_proxy()

        self.assertEqual(first, "http://user1:pass1@10.0.0.1:8001")
        self.assertEqual(second, "http://user1:pass1@10.0.0.1:8001")
        self.assertEqual(third, "http://user2:pass2@10.0.0.2:8002")

    async def test_rotation_status_reports_current_and_last_proxy(self):
        await self.proxy_manager.update_proxy_config(
            enabled=True,
            proxy_url=None,
            proxy_pool_enabled=True,
            proxy_pool="\n".join([
                "http://user1:pass1@10.0.0.1:8001",
                "http://user2:pass2@10.0.0.2:8002",
            ]),
            rotation_mode="by_request_count",
            rotate_every_requests=2,
            rotate_every_seconds=300,
            sync_browser_proxy=True,
            media_proxy_enabled=False,
            media_proxy_url=None,
        )

        before = await self.proxy_manager.get_rotation_status()
        selected = await self.proxy_manager.bind_request_proxy()
        after = await self.proxy_manager.get_rotation_status()
        self.proxy_manager.clear_bound_proxy()

        self.assertEqual(before["current_proxy"], "http://user1:pass1@10.0.0.1:8001")
        self.assertEqual(before["current_index"], 0)
        self.assertEqual(before["candidate_count"], 2)
        self.assertEqual(after["current_proxy"], selected)
        self.assertEqual(after["last_selected_proxy"], selected)
        self.assertEqual(after["source"], "pool")

    async def test_consecutive_failure_rotation_only_switches_after_streak(self):
        await self.proxy_manager.update_proxy_config(
            enabled=True,
            proxy_url=None,
            proxy_pool_enabled=True,
            proxy_pool="\n".join([
                "http://user1:pass1@10.0.0.1:8001",
                "http://user2:pass2@10.0.0.2:8002",
            ]),
            rotation_mode="by_consecutive_failures",
            rotate_every_requests=1,
            rotate_every_seconds=300,
            rotate_every_failures=2,
            sync_browser_proxy=True,
            media_proxy_enabled=False,
            media_proxy_url=None,
        )

        first = await self.proxy_manager.bind_request_proxy()
        self.proxy_manager.clear_bound_proxy()
        await self.proxy_manager.record_request_result(False)

        second = await self.proxy_manager.bind_request_proxy()
        self.proxy_manager.clear_bound_proxy()
        await self.proxy_manager.record_request_result(True)

        third = await self.proxy_manager.bind_request_proxy()
        self.proxy_manager.clear_bound_proxy()
        await self.proxy_manager.record_request_result(False)

        fourth = await self.proxy_manager.bind_request_proxy()
        self.proxy_manager.clear_bound_proxy()
        await self.proxy_manager.record_request_result(False)

        fifth = await self.proxy_manager.bind_request_proxy()
        self.proxy_manager.clear_bound_proxy()

        status = await self.proxy_manager.get_rotation_status()

        self.assertEqual(first, "http://user1:pass1@10.0.0.1:8001")
        self.assertEqual(second, "http://user1:pass1@10.0.0.1:8001")
        self.assertEqual(third, "http://user1:pass1@10.0.0.1:8001")
        self.assertEqual(fourth, "http://user1:pass1@10.0.0.1:8001")
        self.assertEqual(fifth, "http://user2:pass2@10.0.0.2:8002")
        self.assertEqual(status["failure_counter"], 0)
        self.assertEqual(status["current_proxy"], "http://user2:pass2@10.0.0.2:8002")

    async def test_browser_proxy_falls_back_to_captcha_proxy_when_sync_disabled(self):
        await self.proxy_manager.update_proxy_config(
            enabled=True,
            proxy_url="10.0.0.9:8009:apiuser:apipass",
            proxy_pool_enabled=False,
            proxy_pool=None,
            rotation_mode="fixed",
            rotate_every_requests=1,
            rotate_every_seconds=300,
            sync_browser_proxy=False,
            media_proxy_enabled=False,
            media_proxy_url=None,
        )
        await self.db.update_captcha_config(
            captcha_method="browser",
            yescaptcha_api_key="",
            yescaptcha_base_url="https://api.yescaptcha.com",
            capmonster_api_key="",
            capmonster_base_url="https://api.capmonster.cloud",
            ezcaptcha_api_key="",
            ezcaptcha_base_url="https://api.ez-captcha.com",
            capsolver_api_key="",
            capsolver_base_url="https://api.capsolver.com",
            browser_proxy_enabled=True,
            browser_proxy_url="http://browser-user:browser-pass@20.0.0.1:9001",
            browser_count=1,
        )

        request_proxy = await self.proxy_manager.get_request_proxy_url()
        browser_proxy = await self.proxy_manager.get_browser_proxy_url(bind_if_missing=True)

        self.assertEqual(request_proxy, "http://apiuser:apipass@10.0.0.9:8009")
        self.assertEqual(browser_proxy, "http://browser-user:browser-pass@20.0.0.1:9001")
        self.assertIsNone(self.proxy_manager.get_bound_proxy_url())


if __name__ == "__main__":
    unittest.main()
