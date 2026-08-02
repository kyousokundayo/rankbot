"""実Discord VCへ接続し、指定SEを1回再生して切断する運用確認。"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))

from config import ROOM_DEFINITIONS, VC_GAME  # noqa: E402
import sounds  # noqa: E402


class PlaybackClient(discord.Client):
    def __init__(
        self, *, guild_id: int | None, channel_id: int | None, scene: str
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.scene = scene
        self.exit_code = 1
        self._checked = False

    def _select_channel(self, guild: discord.Guild) -> discord.VoiceChannel | None:
        if self.channel_id is not None:
            channel = guild.get_channel(self.channel_id)
            return channel if isinstance(channel, discord.VoiceChannel) else None

        room_order = {
            room.name: index for index, room in enumerate(ROOM_DEFINITIONS)
        }
        candidates = [channel for channel in guild.voice_channels if channel.name == VC_GAME]
        candidates.sort(
            key=lambda channel: (
                not any(not member.bot for member in channel.members),
                room_order.get(getattr(channel.category, "name", ""), 999),
                channel.id,
            )
        )
        return candidates[0] if candidates else None

    async def on_ready(self) -> None:
        if self._checked:
            return
        self._checked = True
        try:
            if self.guild_id is None:
                if len(self.guilds) != 1:
                    raise RuntimeError(
                        f"参加サーバーが{len(self.guilds)}件あります。"
                        "DISCORD_GUILD_IDを.envへ設定してください"
                    )
                guild = self.guilds[0]
            else:
                guild = self.get_guild(self.guild_id)
            if guild is None:
                raise RuntimeError(f"管理対象サーバーが見つかりません: {self.guild_id}")
            channel = self._select_channel(guild)
            if channel is None:
                raise RuntimeError(
                    f"再生先VCが見つかりません。--channel-id または {VC_GAME} を確認してください"
                )
            played = await sounds.SoundPlayer().play(channel, self.scene)
            if not played:
                raise RuntimeError("VC接続またはSE再生に失敗しました")
            print(
                f"playback_ok scene={self.scene} channel={channel.name} channel_id={channel.id}"
            )
            self.exit_code = 0
        except Exception as e:
            logging.getLogger("se-check").exception("SE実再生チェック失敗: %s", e)
        finally:
            await self.close()


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Discord VCでSEを1回実再生する")
    parser.add_argument("--scene", choices=sorted(sounds._SCENES), default="morning")
    parser.add_argument("--channel-id", type=int)
    args = parser.parse_args()

    load_dotenv(BOT_DIR / ".env")
    token = os.getenv("DISCORD_TOKEN")
    guild_raw = os.getenv("DISCORD_GUILD_ID")
    if not token:
        print("DISCORD_TOKENを.envへ設定してください", file=sys.stderr)
        return 2
    if guild_raw and not guild_raw.isdecimal():
        print("DISCORD_GUILD_IDは整数で指定してください", file=sys.stderr)
        return 2

    sounds.require_voice_ready()
    client = PlaybackClient(
        guild_id=int(guild_raw) if guild_raw else None,
        channel_id=args.channel_id,
        scene=args.scene,
    )
    await client.start(token, reconnect=False)
    return client.exit_code


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(_main()))
