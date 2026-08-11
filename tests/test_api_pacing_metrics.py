"""API待ち時間の計測が、内訳を正しく切り分けて記録することを検証する。

「遅い」の原因は 順番待ち(単一ロック) / 間隔(こちらの設定) / 実行(Discord側の
バケット待ち) の3つに分かれ、どれが支配的かで打つ手が変わる。取り違えると
間隔だけ詰めて何も速くならない、という結論になるため区別を固定しておく。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from game import GameCog


class ApiPacingMetricsTest(unittest.IsolatedAsyncioTestCase):
    def _manager(self, *, interval: float = 0.0) -> GameCog:
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = interval
        return manager

    async def test_route_label_uses_target_type_and_method(self) -> None:
        manager = self._manager()

        class _Member:
            async def add_roles(self, *roles, reason=None) -> str:
                return "ok"

        await manager.paced_discord_api_call(_Member().add_roles)

        self.assertIn("_Member.add_roles", manager._api_call_stats)

    async def test_interval_wait_is_recorded_separately_from_execution(self) -> None:
        manager = self._manager(interval=0.05)

        async def _instant() -> None:
            return None

        # 1回目は間隔待ちが無く、2回目だけ直前の呼び出しぶん待たされる。
        await manager.paced_discord_api_call(_instant)
        await manager.paced_discord_api_call(_instant)

        stat = manager._api_call_stats["_instant"]
        self.assertEqual(int(stat["count"]), 2)
        self.assertGreater(stat["interval_wait"], 0.0)
        self.assertLess(stat["exec"], stat["interval_wait"])

    async def test_slow_api_is_recorded_as_execution_not_interval(self) -> None:
        """Discord側のバケット待ちは実行時間として出る (間隔を詰めても縮まない)。"""
        manager = self._manager()

        async def _slow() -> None:
            await asyncio.sleep(0.05)

        await manager.paced_discord_api_call(_slow)

        stat = manager._api_call_stats["_slow"]
        self.assertGreater(stat["exec"], 0.0)
        self.assertGreaterEqual(stat["max_exec"], stat["exec"])
        self.assertEqual(stat["interval_wait"], 0.0)

    async def test_queue_wait_captures_time_blocked_by_another_call(self) -> None:
        """別処理がロックを握っている間の待ちは順番待ちとして出る。"""
        manager = self._manager()

        async def _slow() -> None:
            await asyncio.sleep(0.05)

        async def _instant() -> None:
            return None

        blocker = asyncio.create_task(manager.paced_discord_api_call(_slow))
        await asyncio.sleep(0)  # blockerにロックを取らせる
        await manager.paced_discord_api_call(_instant)
        await blocker

        blocked_wait = manager._api_call_stats["_instant"]["queue_wait"]
        blocker_wait = manager._api_call_stats["_slow"]["queue_wait"]
        # 待たされた側は_slowの所要時間ぶん、待たせた側はロック取得のみ。
        self.assertGreater(blocked_wait, 0.04)
        self.assertGreater(blocked_wait, blocker_wait * 100)

    async def test_failed_call_is_still_recorded(self) -> None:
        manager = self._manager()

        async def _boom() -> None:
            raise RuntimeError("API失敗")

        with self.assertRaises(RuntimeError):
            await manager.paced_discord_api_call(_boom)

        self.assertEqual(int(manager._api_call_stats["_boom"]["count"]), 1)

    def test_summary_resets_so_each_window_is_independent(self) -> None:
        manager = self._manager()
        manager._record_api_call(
            "Member.add_roles", queue_wait=1.0, interval_wait=2.0, exec_seconds=3.0
        )

        manager.log_api_pacing_summary("テスト")

        self.assertEqual(manager._api_call_stats, {})

    def test_summary_can_keep_stats_when_not_resetting(self) -> None:
        manager = self._manager()
        manager._record_api_call(
            "Member.add_roles", queue_wait=1.0, interval_wait=2.0, exec_seconds=3.0
        )

        manager.log_api_pacing_summary("テスト", reset=False)

        self.assertEqual(int(manager._api_call_stats["Member.add_roles"]["count"]), 1)

if __name__ == "__main__":
    unittest.main()
