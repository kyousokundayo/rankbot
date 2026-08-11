"""Local simulation harness for the werewolf bot.

This runs the real GameCog / View logic with fake Discord objects so we can
exercise every configured game variant without touching a live server.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import tempfile
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from random import Random
from types import SimpleNamespace
from typing import Any, Callable, Optional

import discord

import config
import database
import room_runner as room_runner_module
import rating as rating_lib
from game import GameCog

# 本番の統計DBパス (import時点の既定値を控える)。
# シミュレーションは必ずテンポラリDBへ差し替えてから実行する。
PRODUCTION_DB_PATH = database.DB_PATH
from views import (
    RunoffVoteView, SeerView, SpeechDoneView, VoteView, WolfVoteView, GuardView,
    MorningReadyView,
    PrepReadyView,
)


_UNSET = object()


class FakePermissions:
    def __init__(self, *, manage_channels: bool = True, manage_guild: bool = True,
                 administrator: bool = False) -> None:
        self.manage_channels = manage_channels
        self.manage_guild = manage_guild
        self.administrator = administrator


class FakeRole:
    _next_id = 1

    def __init__(self, name: str, color: Any = None) -> None:
        self.id = FakeRole._next_id
        FakeRole._next_id += 1
        self.name = name
        self.color = color

    async def delete(self, reason: Optional[str] = None) -> None:
        return None

    async def edit(self, *, name: Optional[str] = None, reason: Optional[str] = None) -> None:
        if name is not None:
            self.name = name


class FakeCategory:
    _next_id = 10_000

    def __init__(
        self,
        guild: "FakeGuild",
        name: str,
        overwrites: Optional[dict[Any, discord.PermissionOverwrite]] = None,
    ) -> None:
        self.guild = guild
        self.id = FakeCategory._next_id
        FakeCategory._next_id += 1
        self.name = name
        self.channels: list[Any] = []
        self.permissions: dict[Any, dict[str, Any]] = {}
        self.overwrites: dict[Any, discord.PermissionOverwrite] = {
            getattr(target, "id", target): overwrite
            for target, overwrite in (overwrites or {}).items()
        }

    async def set_permissions(self, target: Any, **permissions: Any) -> None:
        key = getattr(target, "id", target)
        self.permissions[key] = permissions
        if "overwrite" in permissions:
            overwrite = permissions["overwrite"]
            if overwrite is None:
                self.overwrites.pop(key, None)
            else:
                self.overwrites[key] = overwrite

    def overwrites_for(self, target: Any) -> discord.PermissionOverwrite:
        overwrite = self.overwrites.get(getattr(target, "id", target))
        if overwrite is None:
            return discord.PermissionOverwrite()
        allow, deny = overwrite.pair()
        return discord.PermissionOverwrite.from_pair(allow, deny)

    async def edit(self, *, name: Optional[str] = None, reason: Optional[str] = None) -> None:
        if name is not None:
            self.name = name

    async def delete(self, reason: Optional[str] = None) -> None:
        if self in self.guild.categories:
            self.guild.categories.remove(self)


class FakeMessage:
    def __init__(
        self,
        *,
        author: Any,
        channel: Any,
        content: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
        embeds: Optional[list[discord.Embed]] = None,
        view: Optional[discord.ui.View] = None,
    ) -> None:
        self.author = author
        self.channel = channel
        self.content = content
        self.embed = embed
        self.embeds = embeds
        self.view = view

    async def edit(self, *, content: Any = _UNSET,
                   embed: Any = _UNSET,
                   embeds: Any = _UNSET,
                   view: Any = _UNSET) -> "FakeMessage":
        # discord.pyは「引数省略」と「明示的なNone (削除)」を区別する。
        if content is not _UNSET:
            self.content = content
        if embed is not _UNSET:
            self.embed = embed
        if embeds is not _UNSET:
            self.embeds = embeds
        if view is not _UNSET:
            self.view = view
        return self

    async def delete(self, *, delay: Optional[float] = None) -> None:
        return None


class FakeResponse:
    def __init__(self, interaction: "FakeInteraction") -> None:
        self.interaction = interaction
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, *_args, **kwargs) -> None:
        # エフェメラルの確認UI (占い・護衛の実行確認) を呼び出し側から辿れるようにする
        self._done = True
        self.interaction.sent_view = kwargs.get("view")
        self.interaction.original_content = kwargs.get("content")
        return None

    async def defer(self, *_args, **_kwargs) -> None:
        self._done = True
        return None

    async def edit_message(self, **kwargs) -> None:
        self._done = True
        if self.interaction.message is not None:
            await self.interaction.message.edit(**kwargs)


class FakeFollowup:
    def __init__(self, interaction: "FakeInteraction") -> None:
        self.interaction = interaction

    async def send(self, *_args, **kwargs) -> None:
        # defer後にfollowupで返される投票確認UIも辿れるようにする。
        if "view" in kwargs:
            self.interaction.sent_view = kwargs["view"]
        if _args:
            self.interaction.original_content = _args[0]
        elif "content" in kwargs:
            self.interaction.original_content = kwargs["content"]
        return None


class FakeInteraction:
    def __init__(
        self,
        *,
        user: Any,
        guild: Any = None,
        message: Optional[FakeMessage] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        self.user = user
        self.guild = guild
        self.message = message
        self.data = data or {}
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        # response.send_message(view=...) で返された確認UI
        self.sent_view: Optional[discord.ui.View] = None
        self.original_content: Optional[str] = None

    async def edit_original_response(self, **kwargs) -> Optional[FakeMessage]:
        """公開メッセージとephemeral応答の両方を最小限再現する。"""
        if "content" in kwargs:
            self.original_content = kwargs["content"]
        if "view" in kwargs:
            self.sent_view = kwargs["view"]
        if self.message is not None:
            return await self.message.edit(**kwargs)
        return None


class FakeTextChannel:
    _next_id = 20_000

    def __init__(self, guild: "FakeGuild", name: str, category: Optional[FakeCategory],
                 controller: "SimulationController",
                 overwrites: Optional[dict[Any, discord.PermissionOverwrite]] = None,
                 position: Optional[int] = None) -> None:
        self.id = FakeTextChannel._next_id
        FakeTextChannel._next_id += 1
        self.guild = guild
        self.name = name
        self.category = category
        self.position = len(guild.text_channels) if position is None else position
        self.controller = controller
        self.messages: list[FakeMessage] = []
        self.deleted = False
        self.permissions: dict[Any, dict[str, Any]] = {}
        self.overwrites: dict[Any, discord.PermissionOverwrite] = {
            getattr(target, "id", target): overwrite
            for target, overwrite in (overwrites or {}).items()
        }

    async def send(self, content: Optional[str] = None, *,
                   embed: Optional[discord.Embed] = None,
                   embeds: Optional[list[discord.Embed]] = None,
                   view: Optional[discord.ui.View] = None,
                   file: Optional[Any] = None) -> FakeMessage:
        # 進行ログはDiscordの2000字上限を超えるとファイル添付になる。
        # 本番と同じ経路を通せるよう、fakeでも file= を受ける。
        msg = FakeMessage(author=self.guild.me, channel=self, content=content, embed=embed, embeds=embeds, view=view)
        msg.file = file
        self.messages.append(msg)
        self.controller.on_channel_message(msg)
        return msg

    async def purge(self, limit: int, check) -> None:
        kept: list[FakeMessage] = []
        removed = 0
        for msg in reversed(self.messages):
            if removed < limit and check(msg):
                removed += 1
                continue
            kept.append(msg)
        self.messages = list(reversed(kept))

    async def delete(self, reason: Optional[str] = None) -> None:
        self.deleted = True
        if self in self.guild.text_channels:
            self.guild.text_channels.remove(self)
        if self.category is not None and self in self.category.channels:
            self.category.channels.remove(self)

    async def edit(
        self,
        *,
        name: Optional[str] = None,
        category: Optional[FakeCategory] = None,
        position: Optional[int] = None,
        overwrites: Optional[dict[Any, discord.PermissionOverwrite]] = None,
        sync_permissions: bool = False,
        reason: Optional[str] = None,
    ) -> None:
        if name is not None:
            self.name = name
        if self.category is not None and self in self.category.channels:
            self.category.channels.remove(self)
        self.category = category
        if category is not None and self not in category.channels:
            category.channels.append(self)
            if sync_permissions:
                # 本番と同じく、移動先カテゴリの権限へ揃える
                self.overwrites = dict(getattr(category, "overwrites", {}) or {})
        if position is not None:
            self.position = position
        if overwrites is not None:
            self.overwrites = {
                getattr(target, "id", target): overwrite
                for target, overwrite in overwrites.items()
            }

    async def set_permissions(self, target: Any, **permissions: Any) -> None:
        key = getattr(target, "id", target)
        self.permissions[key] = permissions
        if "overwrite" in permissions:
            overwrite = permissions["overwrite"]
            if overwrite is None:
                self.overwrites.pop(key, None)
            else:
                self.overwrites[key] = overwrite

    def overwrites_for(self, target: Any) -> discord.PermissionOverwrite:
        overwrite = self.overwrites.get(getattr(target, "id", target))
        if overwrite is None:
            return discord.PermissionOverwrite()
        allow, deny = overwrite.pair()
        return discord.PermissionOverwrite.from_pair(allow, deny)


class FakeVoiceChannel:
    _next_id = 30_000

    def __init__(self, guild: "FakeGuild", name: str, category: Optional[FakeCategory]) -> None:
        self.id = FakeVoiceChannel._next_id
        FakeVoiceChannel._next_id += 1
        self.guild = guild
        self.name = name
        self.category = category
        self.members: list[FakeMember] = []
        self.permissions: dict[Any, dict[str, Any]] = {}
        self.overwrites: dict[Any, discord.PermissionOverwrite] = {}

    def overwrites_for(self, target: Any) -> discord.PermissionOverwrite:
        ow = self.overwrites.get(getattr(target, "id", target))
        if ow is None:
            return discord.PermissionOverwrite()
        # 実Discordと同様にコピーを返す (呼び出し側の変更が直接反映されないように)
        allow, deny = ow.pair()
        return discord.PermissionOverwrite.from_pair(allow, deny)

    async def set_permissions(self, target: Any, **permissions: Any) -> None:
        key = getattr(target, "id", target)
        self.permissions[key] = permissions
        if "overwrite" in permissions:
            ow = permissions["overwrite"]
            if ow is None:
                self.overwrites.pop(key, None)
            else:
                self.overwrites[key] = ow

    async def delete(self, reason: Optional[str] = None) -> None:
        if self in self.guild.voice_channels:
            self.guild.voice_channels.remove(self)
        if self.category is not None and self in self.category.channels:
            self.category.channels.remove(self)

    async def edit(
        self,
        *,
        category: Optional[FakeCategory] = None,
        reason: Optional[str] = None,
    ) -> None:
        if self.category is not None and self in self.category.channels:
            self.category.channels.remove(self)
        self.category = category
        if category is not None and self not in category.channels:
            category.channels.append(self)


class FakeMember:
    def __init__(self, guild: "FakeGuild", member_id: int, name: str, *,
                 bot: bool = False, controller: Optional["SimulationController"] = None,
                 can_manage_channels: bool = False) -> None:
        self.guild = guild
        self.id = member_id
        self.name = name
        self.nick: Optional[str] = None
        self.bot = bot
        self.controller = controller
        self.mute_apply_delay = 0.0
        self.edit_failures: list[Exception] = []
        self.edit_calls: list[dict[str, Any]] = []
        self.add_role_calls: list[tuple[FakeRole, ...]] = []
        self.roles: list[FakeRole] = []
        self.voice = SimpleNamespace(channel=None, mute=False)
        self.guild_permissions = FakePermissions(
            manage_channels=can_manage_channels,
            manage_guild=can_manage_channels,
        )
        self.sent_messages: list[FakeMessage] = []

    @property
    def display_name(self) -> str:
        return self.nick or self.name

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    async def edit(self, *, nick: Optional[str] = None, mute: Optional[bool] = None,
                   roles: Optional[list[FakeRole]] = None,
                   reason: Optional[str] = None) -> None:
        self.edit_calls.append(
            {"nick": nick, "mute": mute, "roles": roles, "reason": reason}
        )
        if self.edit_failures:
            raise self.edit_failures.pop(0)
        if nick is not None:
            self.nick = nick
        if mute is not None:
            if self.mute_apply_delay > 0:
                async def apply_later() -> None:
                    await asyncio.sleep(self.mute_apply_delay)
                    self.voice.mute = mute

                asyncio.create_task(apply_later())
            else:
                self.voice.mute = mute
        if roles is not None:
            self.roles = list(roles)

    async def send(self, content: Optional[str] = None, *,
                   embed: Optional[discord.Embed] = None,
                   embeds: Optional[list[discord.Embed]] = None,
                   view: Optional[discord.ui.View] = None) -> FakeMessage:
        msg = FakeMessage(author=self.guild.me, channel=SimpleNamespace(name="dm"), content=content, embed=embed, embeds=embeds, view=view)
        self.sent_messages.append(msg)
        if self.controller is not None:
            self.controller.on_dm_message(self, msg)
        return msg

    async def add_roles(self, *roles: FakeRole, reason: Optional[str] = None) -> None:
        self.add_role_calls.append(roles)
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles: FakeRole, reason: Optional[str] = None) -> None:
        ids = {role.id for role in roles}
        self.roles = [role for role in self.roles if role.id not in ids]


class FakeGuild:
    def __init__(self, guild_id: int, name: str, controller: "SimulationController") -> None:
        self.id = guild_id
        self.name = name
        self.controller = controller
        self.categories: list[FakeCategory] = []
        self.text_channels: list[FakeTextChannel] = []
        self.voice_channels: list[FakeVoiceChannel] = []
        self.roles: list[FakeRole] = []
        self.default_role = FakeRole("@everyone")
        # validate_join のオーナー判定用。プレイヤーIDと衝突しない値にする
        self.owner_id = 0
        self.me = FakeMember(self, 999999, "Bot", bot=True, controller=controller, can_manage_channels=True)
        self._members: dict[int, FakeMember] = {self.me.id: self.me}

    def add_member(self, member: FakeMember) -> None:
        self._members[member.id] = member

    def get_member(self, member_id: int) -> Optional[FakeMember]:
        return self._members.get(member_id)

    async def fetch_member(self, member_id: int) -> FakeMember:
        """本番同様、在籍しなければ discord.NotFound を投げる。

        本番のフォールバック (キャッシュ未反映と退出済みの切り分け) を
        シミュレータでも通すために必要。無いと AttributeError になる。
        """
        member = self._members.get(member_id)
        if member is None:
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"),
                {"message": "Unknown Member", "code": 10007},
            )
        return member

    def get_channel(self, channel_id: int):
        return next(
            (
                channel
                for channel in [*self.categories, *self.text_channels, *self.voice_channels]
                if channel.id == channel_id
            ),
            None,
        )

    @property
    def members(self) -> list[FakeMember]:
        return list(self._members.values())

    async def create_category(
        self,
        name: str,
        *,
        overwrites: Optional[dict[Any, discord.PermissionOverwrite]] = None,
    ) -> FakeCategory:
        category = FakeCategory(self, name, overwrites=overwrites)
        self.categories.append(category)
        return category

    async def create_text_channel(self, name: str, *, category: Optional[FakeCategory] = None,
                                  overwrites: Optional[dict[Any, Any]] = None,
                                  position: Optional[int] = None) -> FakeTextChannel:
        channel = FakeTextChannel(
            self, name, category, self.controller,
            overwrites=overwrites, position=position,
        )
        self.text_channels.append(channel)
        if category is not None:
            category.channels.append(channel)
        return channel

    async def create_voice_channel(self, name: str, *, category: Optional[FakeCategory] = None) -> FakeVoiceChannel:
        channel = FakeVoiceChannel(self, name, category)
        self.voice_channels.append(channel)
        if category is not None:
            category.channels.append(channel)
        return channel

    async def create_role(self, *, name: str, color: Any = None, reason: Optional[str] = None) -> FakeRole:
        role = FakeRole(name, color)
        self.roles.append(role)
        return role


class FakeBot:
    def __init__(self, user: FakeMember) -> None:
        self.user = user


@dataclass
class SimulationResult:
    seed: int
    variant_id: str
    ladder_id: str
    winner: str
    days: int
    runoffs: int
    forced_end: Optional[str]
    role_counts: dict[str, int]
    game_run_id: str
    player_ids: list[int]
    alive_player_ids: set[int]
    action_totals: dict[str, int]
    rated: bool
    rank_lookup_failed: bool


@dataclass(frozen=True)
class SimulationScenario:
    """1試合ぶんの再現可能な実行条件。"""

    seed: int
    variant_id: str
    force_runoff: bool = False
    rated: bool = True
    fail_rank_lookup: bool = False


def build_simulation_scenarios(
    runs: int,
    variant_ids: Optional[list[str] | tuple[str, ...]] = None,
) -> list[SimulationScenario]:
    """全対象変種を最低1戦ずつ含む、決定的な実行順を返す。

    ``runs`` は従来どおり追加の通常戦数。これとは別に、先頭変種の
    強制再投票・残り変種の最低1戦・非レート1戦を必ず組み込む。
    """
    if runs < 0:
        raise ValueError("runs must be non-negative")

    selected = tuple(variant_ids or tuple(config.VARIANT_DEFINITIONS))
    if not selected:
        raise ValueError("at least one variant_id is required")
    if len(set(selected)) != len(selected):
        raise ValueError("variant_ids must not contain duplicates")
    unknown = [
        variant_id for variant_id in selected
        if variant_id not in config.VARIANT_DEFINITIONS
    ]
    if unknown:
        raise ValueError(f"unknown variant_id: {', '.join(unknown)}")

    scenarios = [
        SimulationScenario(
            seed=0,
            variant_id=selected[0],
            force_runoff=True,
            fail_rank_lookup=True,
        )
    ]
    scenarios.extend(
        SimulationScenario(seed=index, variant_id=variant_id)
        for index, variant_id in enumerate(selected[1:], 1)
    )
    next_seed = len(selected)
    scenarios.extend(
        SimulationScenario(
            seed=next_seed + offset,
            variant_id=selected[offset % len(selected)],
        )
        for offset in range(runs)
    )
    scenarios.append(
        SimulationScenario(
            seed=10_000,
            variant_id=selected[0],
            rated=False,
        )
    )
    return scenarios


class SimulationController:
    def __init__(self, cog: Any, guild: FakeGuild, players: list[FakeMember],
                 gm: FakeMember, rng: Random, *, force_runoff: bool = False) -> None:
        self.cog = cog
        self.guild = guild
        self.players = players
        self.gm = gm
        self.rng = rng
        self.force_runoff = force_runoff
        self.pending_tasks: set[asyncio.Task] = set()
        self.errors: list[BaseException] = []
        self.force_runoff_used = False

    def _schedule(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self.pending_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self.pending_tasks.discard(done_task)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                self.errors.append(exc)

        task.add_done_callback(_done)

    def on_channel_message(self, message: FakeMessage) -> None:
        view = message.view
        if isinstance(view, VoteView):
            self._schedule(self._handle_vote_view(message, view))
        elif isinstance(view, RunoffVoteView):
            self._schedule(self._handle_runoff_vote_view(message, view))
        elif isinstance(view, SpeechDoneView):
            self._schedule(self._handle_speech_done_view(message, view))
        elif isinstance(view, PrepReadyView):
            self._schedule(self._handle_prep_ready_view(message, view))
        elif isinstance(view, MorningReadyView):
            self._schedule(self._handle_morning_ready_view(message, view))

    def on_dm_message(self, member: FakeMember, message: FakeMessage) -> None:
        view = message.view
        if isinstance(view, WolfVoteView):
            self._schedule(self._handle_wolf_view(member, message, view))
        elif isinstance(view, SeerView):
            self._schedule(self._handle_seer_view(member, message, view))
        elif isinstance(view, GuardView):
            self._schedule(self._handle_guard_view(member, message, view))

    async def drain(self) -> None:
        """保留中のUI操作タスクを全て完了させる (エラーは投げずに保持)。

        高速化したカウントダウンでも、DMの役職行動と朝の宣言を
        フェーズ判定前に必ず処理し、結果を決定的にする。

        注意: 完了済みタスクは done コールバックが走るまで pending_tasks に
        残るため、集合の空判定でループするとイベントループへ制御が返らず
        ビジーループになる。未完了タスクだけを対象に、毎周 sleep(0) で
        コールバックを消化させる。
        """
        while True:
            await asyncio.sleep(0)
            pending = [task for task in self.pending_tasks if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def flush(self) -> None:
        await self.drain()
        if self.errors:
            raise self.errors[0]

    def _candidate_buttons(self, view: discord.ui.View) -> dict[int, discord.ui.Button]:
        """候補者ボタンだけを {user_id: ボタン} で取り出す。

        custom_id にアンダースコアが入っているかどうかで判定すると、
        候補以外のボタンを足したときに int() で落ちる。
        _BaseVoteView.button_prefix の一致でだけ候補と判定する。
        """
        prefix = f"{view.button_prefix}_"
        buttons: dict[int, discord.ui.Button] = {}
        for child in view.children:
            custom_id = getattr(child, "custom_id", "") or ""
            if custom_id.startswith(prefix):
                buttons[int(custom_id[len(prefix):])] = child
        return buttons

    def _candidate_ids_from_buttons(self, view: discord.ui.View) -> list[int]:
        return list(self._candidate_buttons(view))

    def _random_vote_mapping(self, voters: list[int], candidates: list[int]) -> dict[int, int]:
        mapping: dict[int, int] = {}
        for voter in voters:
            options = [candidate for candidate in candidates if candidate != voter]
            mapping[voter] = self.rng.choice(options)
        return mapping

    def _forced_tie_mapping(self, voters: list[int], candidates: list[int]) -> dict[int, int]:
        if len(candidates) < 3 or len(voters) < 3:
            return self._random_vote_mapping(voters, candidates)

        a, b, c = candidates[:3]
        quotient, remainder = divmod(len(voters), 3)
        if remainder == 0:
            counts = (quotient, quotient, quotient)
        elif remainder == 1:
            # 13人なら 5-5-3。最多票を必ず2人以上へ揃える。
            counts = (quotient + 1, quotient + 1, quotient - 1)
        else:
            counts = (quotient + 1, quotient + 1, quotient)
        desired = dict(zip((a, b, c), counts))
        voter_pool = voters[:]

        for _ in range(50):
            self.rng.shuffle(voter_pool)
            remaining = desired.copy()
            mapping: dict[int, int] = {}
            success = True
            for voter in voter_pool:
                choices = [target for target, count in sorted(remaining.items(), key=lambda item: -item[1])
                           if count > 0 and target != voter]
                if not choices:
                    success = False
                    break
                target = choices[0]
                remaining[target] -= 1
                mapping[voter] = target
            if success and all(count == 0 for count in remaining.values()):
                return mapping

        return self._random_vote_mapping(voters, candidates)

    async def _handle_vote_view(self, message: FakeMessage, view: VoteView) -> None:
        await asyncio.sleep(0)
        voters = list(view.voters)
        candidates = self._candidate_ids_from_buttons(view)
        if self.force_runoff and not self.force_runoff_used and self.cog.state.day_number == 1:
            mapping = self._forced_tie_mapping(voters, candidates)
            self.force_runoff_used = True
        else:
            mapping = self._random_vote_mapping(voters, candidates)

        buttons = self._candidate_buttons(view)
        for voter_id in voters:
            interaction = FakeInteraction(
                user=self.guild.get_member(voter_id),
                guild=self.guild,
                message=message,
            )
            await buttons[mapping[voter_id]].callback(interaction)
            await self._confirm_vote(interaction)

    async def _handle_runoff_vote_view(self, message: FakeMessage, view: RunoffVoteView) -> None:
        await asyncio.sleep(0)
        voters = list(view.voters)
        candidates = self._candidate_ids_from_buttons(view)
        mapping = self._random_vote_mapping(voters, candidates)
        buttons = self._candidate_buttons(view)
        for voter_id in voters:
            interaction = FakeInteraction(
                user=self.guild.get_member(voter_id),
                guild=self.guild,
                message=message,
            )
            await buttons[mapping[voter_id]].callback(interaction)
            await self._confirm_vote(interaction)

    async def _confirm_vote(self, selection: FakeInteraction) -> None:
        """候補選択後に本人用の投票確認を確定する。"""
        confirm_view = selection.sent_view
        if confirm_view is None:
            return
        confirm_btn = next(
            child
            for child in confirm_view.children
            if isinstance(child, discord.ui.Button)
            and child.label == "この人に投票"
        )
        await confirm_btn.callback(
            FakeInteraction(user=selection.user, guild=selection.guild, message=None)
        )

    async def _handle_speech_done_view(self, message: FakeMessage, view: SpeechDoneView) -> None:
        await asyncio.sleep(0)
        button = view.children[0]
        speaker = self.guild.get_member(view.speaker_id)
        interaction = FakeInteraction(user=speaker, guild=self.guild, message=message)
        await button.callback(interaction)

    async def _handle_wolf_view(self, member: FakeMember, message: FakeMessage, view: WolfVoteView) -> None:
        await asyncio.sleep(0)
        select = next(child for child in view.children if isinstance(child, discord.ui.Select))
        # 「噛みなし (-1)」は選ばない (従来の勝敗分布を維持するため)
        choices = [int(option.value) for option in select.options if int(option.value) != -1]
        target_id = self.rng.choice(choices)

        select_interaction = FakeInteraction(
            user=member,
            message=message,
            data={"values": [str(target_id)]},
        )
        await select.callback(select_interaction)

    async def _select_and_confirm(
        self, member: FakeMember, message: FakeMessage, view: discord.ui.View
    ) -> None:
        """セレクトで対象を選び、続く実行確認で「実行する」を押す。

        占い・護衛は誤タップ防止のため、選択だけでは確定しない。
        """
        select = next(child for child in view.children if isinstance(child, discord.ui.Select))
        choices = [int(option.value) for option in select.options]
        if not choices:
            return
        target_id = self.rng.choice(choices)
        interaction = FakeInteraction(
            user=member,
            message=message,
            data={"values": [str(target_id)]},
        )
        await select.callback(interaction)

        confirm_view = interaction.sent_view
        if confirm_view is None:
            return  # 選択が弾かれた (確認UIが出ていない)
        confirm_btn = next(
            child for child in confirm_view.children
            if isinstance(child, discord.ui.Button) and child.label == "実行する"
        )
        # 確認UIはエフェメラルなので、元のDMメッセージは編集させない
        await confirm_btn.callback(FakeInteraction(user=member, message=None))

    async def _handle_seer_view(self, member: FakeMember, message: FakeMessage, view: SeerView) -> None:
        await asyncio.sleep(0)
        await self._select_and_confirm(member, message, view)

    async def _handle_guard_view(self, member: FakeMember, message: FakeMessage, view: GuardView) -> None:
        await asyncio.sleep(0)
        await self._select_and_confirm(member, message, view)

    async def _handle_morning_ready_view(
        self, message: FakeMessage, view: MorningReadyView
    ) -> None:
        """生存者全員に #昼 の「朝を迎える」を押させる (未行動警告があれば2度押す)"""
        await asyncio.sleep(0)
        button = next(
            child for child in view.children
            if isinstance(child, discord.ui.Button) and child.label.endswith("朝を迎える")
        )
        state = self.cog.state
        for player in list(state.alive_players()):
            for _ in range(2):
                if player.user_id in state.morning_ready_ids:
                    break
                await button.callback(
                    FakeInteraction(user=player.member, message=message)
                )

    async def _handle_prep_ready_view(
        self, message: FakeMessage, view: PrepReadyView
    ) -> None:
        """参加者全員に「役職を確認した」を押させる"""
        await asyncio.sleep(0)
        button = next(
            child for child in view.children
            if isinstance(child, discord.ui.Button) and child.label.endswith("役職を確認した")
        )
        state = self.cog.state
        for player in list(state.alive_players()):
            if player.user_id in state.prep_ready_ids:
                continue
            await button.callback(FakeInteraction(user=player.member, message=message))


class _SeededSecrets:
    """room_runner の `secrets` を置き換えるシード可能なスタブ。

    本番の役職配布・番号割り当て・ランダム処刑は `secrets` (暗号論的乱数)
    を使うため、シードを与えても再現できない。シミュレーションでは
    シード付き Random に差し替えて、同じseedなら必ず同じ試合になるようにする。
    """

    def __init__(self, rng: Random) -> None:
        self._rng = rng

    def SystemRandom(self) -> Random:  # noqa: N802 (secretsのAPI名に合わせる)
        return self._rng

    def choice(self, seq):
        return self._rng.choice(seq)

    def token_hex(self, nbytes: int = 32) -> str:
        """secrets.token_hexと同形式の決定的な識別子を返す。"""
        return self._rng.getrandbits(nbytes * 8).to_bytes(nbytes, "big").hex()


@contextmanager
def _seeded_randomness(rng: Random):
    """room_runner の secrets / random をシード付きRNGへ差し替える"""
    original_secrets = room_runner_module.secrets
    original_random = room_runner_module.random
    room_runner_module.secrets = _SeededSecrets(rng)
    room_runner_module.random = rng
    try:
        yield
    finally:
        room_runner_module.secrets = original_secrets
        room_runner_module.random = original_random


def _make_fast_game_methods(cog: Any, controller: "SimulationController") -> None:
    async def fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    async def fast_wait_event(event: asyncio.Event, timeout: float) -> bool:
        if event.is_set():
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=0.1)
            return True
        except asyncio.TimeoutError:
            return False

    async def fast_countdown(message: Any, build_content: Callable[[float], str],
                             seconds: float, event: Optional[asyncio.Event] = None) -> bool:
        if message is not None:
            await message.edit(content=build_content(0))
        # 実時間で待つ代わりに、保留中のUI操作 (役職アクション・朝の宣言) を
        # 全て処理してから判定する。夜は「朝を迎える」宣言が揃うまで
        # 明けないため、イベント判定の前に必ずdrainする必要がある
        await controller.drain()
        if event is None:
            return False
        return await fast_wait_event(event, seconds)

    async def fast_wait_forever(event: asyncio.Event) -> None:
        # 「朝を迎える」「役職を確認した」の実際の完了条件を検証する。
        # テスト都合でeventを立てると、永久に進まない回帰を隠す。
        await controller.drain()
        if event.is_set():
            return
        state = cog.state
        if event is getattr(state, "prep_ready_event", None):
            label = "preparation gate"
            required = cog._prep_required_ids()
            ready = set(state.prep_ready_ids)
        else:
            label = "morning gate"
            required = cog._morning_required_ids()
            ready = set(state.morning_ready_ids)
        raise AssertionError(
            f"{label} did not open: "
            f"ready={len(ready & required)}/{len(required)}, "
            f"missing={sorted(required - ready)}"
        )

    async def fast_turn_segment_countdown(
        message: Any,
        speaker: Any,
        seconds: float,
        *,
        allow_interrupt: bool,
    ) -> tuple[str, float, Any]:
        """ターン順・ミュート・永続化は実経路のまま、実時間待ちだけ省く。"""
        del allow_interrupt
        if message is not None:
            await message.edit(
                content=cog._turn_segment_content(
                    speaker, 0, interrupt=bool(cog.state.turn_interrupt_active),
                )
            )
        await controller.drain()
        return "timeout", 0.0, message

    cog._pausable_sleep = fast_sleep  # type: ignore[assignment]
    cog._pausable_countdown = fast_countdown  # type: ignore[assignment]
    cog._pausable_wait_forever = fast_wait_forever  # type: ignore[assignment]
    if hasattr(cog, "_turn_segment_countdown"):
        cog._turn_segment_countdown = fast_turn_segment_countdown  # type: ignore[assignment]


async def simulate_one_game(
    *,
    seed: int,
    guild_id: int,
    force_runoff: bool,
    variant_id: str = config.DEFAULT_VARIANT_ID,
    rated: bool = True,
    fail_rank_lookup: bool = False,
) -> SimulationResult:
    variant = config.get_variant_definition(variant_id)
    player_ids = [20_000 + idx for idx in range(variant.player_count)]
    population_ids = player_ids
    return await simulate_selected_game(
        seed=seed,
        guild_id=guild_id,
        player_ids=player_ids,
        population_ids=population_ids,
        force_runoff=force_runoff,
        variant_id=variant_id,
        rated=rated,
        fail_rank_lookup=fail_rank_lookup,
    )


async def simulate_selected_game(
    *,
    seed: int,
    guild_id: int,
    player_ids: list[int],
    population_ids: list[int],
    force_runoff: bool,
    variant_id: str = config.DEFAULT_VARIANT_ID,
    rated: bool = True,
    fail_rank_lookup: bool = False,
) -> SimulationResult:
    _assert_sandbox_db()
    rng = Random(seed)
    variant = config.get_variant_definition(variant_id)
    if len(player_ids) != variant.player_count:
        raise ValueError(
            f"expected {variant.player_count} player_ids for {variant_id}, "
            f"got {len(player_ids)}"
        )

    # 役職配布などの secrets / random をシード付きに差し替えて再現性を担保する
    original_delete_delay = room_runner_module.CHANNEL_DELETE_DELAY
    try:
        with _seeded_randomness(rng):
            return await _simulate_selected_game_inner(
                seed=seed,
                guild_id=guild_id,
                player_ids=player_ids,
                population_ids=population_ids,
                rng=rng,
                force_runoff=force_runoff,
                variant_id=variant_id,
                rated=rated,
                fail_rank_lookup=fail_rank_lookup,
            )
    finally:
        room_runner_module.CHANNEL_DELETE_DELAY = original_delete_delay


async def _simulate_selected_game_inner(
    *,
    seed: int,
    guild_id: int,
    player_ids: list[int],
    population_ids: list[int],
    rng: Random,
    force_runoff: bool,
    variant_id: str,
    rated: bool,
    fail_rank_lookup: bool,
) -> SimulationResult:

    variant = config.get_variant_definition(variant_id)
    guild = FakeGuild(
        guild_id,
        f"SimGuild-{variant_id}-{seed}",
        controller=None,
    )  # type: ignore[arg-type]
    gm = FakeMember(guild, 10_000, "GM")
    population_members: dict[int, FakeMember] = {}
    for idx, player_id in enumerate(population_ids, 1):
        member = FakeMember(guild, player_id, f"P{idx}")
        population_members[player_id] = member

    for member in [gm, *population_members.values()]:
        guild.add_member(member)
    gm_role = FakeRole("GM")
    guild.roles.append(gm_role)
    gm.roles.append(gm_role)

    players = [population_members[player_id] for player_id in player_ids]

    fake_bot = FakeBot(guild.me)
    manager = GameCog(fake_bot)  # type: ignore[arg-type]
    # 実サーバー用の1.1秒ペーシングは、外部APIを呼ばないシミュレーションでは省く。
    manager.bulk_api_interval = 0.0
    if rated:
        candidates = [
            room_def
            for room_def in config.ROOM_DEFINITIONS
            if (
                room_def.variant_id == variant_id
                and room_def.room_id in config.RATED_ROOM_IDS
            )
        ]
        candidates.sort(
            key=lambda room_def: (
                room_def.room_id not in config.PUBLIC_ROOM_IDS,
                room_def.room_id != "open",
                room_def.room_id,
            )
        )
        if candidates:
            room_def = candidates[0]
        else:
            # enabled=False の変種も本物のレート精算まで検証する。ライブ用の
            # RATED_ROOM_IDS は無効卓を含めないため、ここだけ既存のレート卓ID
            # を借りたシミュレーション専用定義を使う。
            if "open" not in config.RATED_ROOM_IDS:
                raise AssertionError("simulation requires an active rated room ID")
            room_def = config.RoomDefinition(
                "open",
                f"検証用-{variant.label}",
                variant_id=variant_id,
            )
        cog = room_runner_module.RoomRunner(fake_bot, manager, room_def)
        manager.rooms[room_def.room_id] = cog
    else:
        room_def = config.RoomDefinition(
            "simulation-unrated",
            "検証用非レート卓",
            variant_id=variant_id,
        )
        cog = room_runner_module.RoomRunner(fake_bot, manager, room_def)
        manager.rooms[room_def.room_id] = cog

    # 厳格ロール限定の卓もシミュレーションできるよう、必要なロールがあれば
    # 参加者とGMへ明示的に付与する。標準の9人卓は一般公開なので不要。
    for role_name in sorted(cog.room_def.strict_access_role_names or ()):
        access_role = FakeRole(role_name)
        guild.roles.append(access_role)
        for member in [gm, *players]:
            member.roles.append(access_role)
    controller = SimulationController(cog, guild, players, gm, rng, force_runoff=force_runoff)
    guild.controller = controller
    guild.me.controller = controller
    gm.controller = controller
    for player in players:
        player.controller = controller

    _make_fast_game_methods(cog, controller)

    original_runoff = cog._runoff
    original_end_game = cog._end_game
    original_force_end = cog.force_end
    result_meta: dict[str, Any] = {
        "runoffs": 0, "winner": None, "days": None, "forced_end": None,
        "durability_stop": None, "alive_player_ids": set(), "action_totals": {},
        "game_run_id": None,
    }

    async def wrapped_runoff(candidate_ids: list[int]) -> int:
        result_meta["runoffs"] += 1
        return await original_runoff(candidate_ids)

    async def wrapped_end_game(winner: config.Team) -> None:
        result_meta["winner"] = winner.value
        result_meta["days"] = cog.state.day_number
        result_meta["game_run_id"] = cog.state.game_run_id
        result_meta["alive_player_ids"] = {
            player.user_id for player in cog.state.players.values() if player.alive
        }
        action_log = list(cog.state.action_log)
        result_meta["action_totals"] = {
            "peaceful_mornings": len({
                int(entry["day"]) for entry in action_log if entry.get("kind") == "平和"
            }),
            "guard_successes": len({
                int(entry["day"]) for entry in action_log if entry.get("kind") == "護衛成功"
            }),
            "guard_checks": None,
            "seer_checks": sum(entry.get("kind") == "占い" for entry in action_log),
            "seer_wolf_hits": sum(
                entry.get("kind") == "占い" and "結果=人狼" in str(entry.get("detail") or "")
                for entry in action_log
            ),
        }
        # 狩人が実際に選択した回数ではなく、狩人が生存状態で迎えた夜を
        # 分母にする。build_game_statsとは独立にログから手計算する。
        guard = next(
            player for player in cog.state.players.values()
            if player.role == config.Role.GUARD
        )
        night_days = {
            int(entry["day"])
            for entry in action_log
            if entry.get("phase") == config.Phase.NIGHT.name
            and int(entry.get("day", 0)) > 0
        }
        guard_death = next(
            (
                (int(entry["day"]), str(entry.get("detail") or "").split("/", 1)[0].strip(),
                 str(entry.get("phase") or ""))
                for entry in action_log
                if entry.get("kind") == "死亡"
                and entry.get("target_id") == guard.user_id
            ),
            None,
        )
        if guard_death is not None:
            death_day, death_cause, death_phase = guard_death
            night_days = {
                day for day in night_days
                if day < death_day
                or (
                    day == death_day
                    and (
                        death_cause == "襲撃"
                        or (death_cause == "除外" and death_phase == config.Phase.NIGHT.name)
                    )
                )
            }
        result_meta["action_totals"]["guard_checks"] = len(night_days)
        await original_end_game(winner)

    async def wrapped_force_end(reason: str = "廃村") -> None:
        result_meta["forced_end"] = reason
        await original_force_end(reason)

    # DB書き込み失敗による安全停止を捕まえる。_game_loop はこの経路を
    # StateDurabilityError として静かに握って return するため、記録しないと
    # 「winnerがNone」という無関係なメッセージに化けて原因が追えなくなる。
    original_stop_for_durability = cog._stop_for_durability_error

    async def wrapped_stop_for_durability(context: str, error: Exception) -> None:
        result_meta["durability_stop"] = f"{context}: {type(error).__name__}: {error}"
        await original_stop_for_durability(context, error)

    cog._runoff = wrapped_runoff  # type: ignore[assignment]
    cog._end_game = wrapped_end_game  # type: ignore[assignment]
    cog.force_end = wrapped_force_end  # type: ignore[assignment]
    cog._stop_for_durability_error = wrapped_stop_for_durability  # type: ignore[assignment]

    room_runner_module.CHANNEL_DELETE_DELAY = 0

    log_capture = _GameLoopLogCapture()
    room_runner_logger = logging.getLogger(room_runner_module.__name__)
    room_runner_logger.addHandler(log_capture)
    # _game_loop のキャンセル経路は log.info なので、ロガー側の閾値も
    # 一時的に下げないとハンドラまで届かない (終了時に元へ戻す)
    previous_log_level = room_runner_logger.level
    room_runner_logger.setLevel(logging.INFO)

    await cog.setup_channels(guild)

    lobby_view = cog.state.lobby_message.view
    join_button = next(child for child in lobby_view.children if getattr(child, "custom_id", None) == "join_game")
    gm_button = next(child for child in lobby_view.children if getattr(child, "custom_id", None) == "get_gm")
    start_button = next(child for child in lobby_view.children if getattr(child, "custom_id", None) == "start_game")

    for player in players:
        interaction = FakeInteraction(user=player, guild=guild, message=cog.state.lobby_message)
        await join_button.callback(interaction)

    gm_interaction = FakeInteraction(user=gm, guild=guild, message=cog.state.lobby_message)
    await gm_button.callback(gm_interaction)

    cog.state.voice_channel.members = [gm, *players]
    for member in [gm, *players]:
        member.voice.channel = cog.state.voice_channel

    await start_button.callback(gm_interaction)
    active_state = cog.state
    assert all(not player.add_role_calls for player in players), (
        "開始時の生存ロールがニックネームPATCHへ統合されていません"
    )
    assert all(len(player.edit_calls) == 1 for player in players), (
        "開始時のメンバー更新が1人1PATCHに収まっていません"
    )
    assert all(
        call["nick"] is not None
        and call["mute"] is True
        and call["roles"] is not None
        for player in players for call in player.edit_calls
    ), "開始PATCHにnick/mute/rolesが揃っていません"
    role_counts = Counter(player.role.value for player in active_state.players.values())
    expected_counts = {
        role.value: count for role, count in variant.role_distribution.items()
    }
    assert dict(role_counts) == expected_counts, (role_counts, expected_counts)
    assert all(player.sent_messages for player in players), "some player did not receive any DM"

    game_task = active_state.game_task
    original_rank_lookup = database.get_current_rank_map
    if fail_rank_lookup:
        async def failed_rank_lookup(
            _guild_id: int,
            ladder_id: str = config.DEFAULT_LADDER_ID,
        ):
            del ladder_id
            raise RuntimeError("simulated rank lookup failure")
        database.get_current_rank_map = failed_rank_lookup
    try:
        await _wait_for_game_task(game_task)
    finally:
        database.get_current_rank_map = original_rank_lookup
    await controller.flush()
    # 各ゲームで spawn_bg_task された処理 (終了後推薦の受付待ちなど) を
    # ここで畳む。待たずに次のゲームへ進むと、前のゲームのタスクが同じ
    # テンポラリDBへ書き込み続けてロック競合を起こし、後片付けで
    # DBが消えた後には disk I/O error になる。
    await _drain_manager_tasks(cog.manager)
    room_runner_logger.removeHandler(log_capture)
    room_runner_logger.setLevel(previous_log_level)

    if result_meta["forced_end"] is not None:
        raise AssertionError(f"game force-ended unexpectedly: {result_meta['forced_end']}")
    if result_meta["winner"] is None or result_meta["days"] is None:
        raise AssertionError(
            "game finished without recording winner/day "
            f"(durability_stop={result_meta['durability_stop']!r}, "
            f"cancelled={game_task.cancelled()}, "
            f"phase={active_state.phase.name}, "
            f"pending_winner={active_state.pending_winner}, "
            f"log={log_capture.tail()})"
        )

    return SimulationResult(
        seed=seed,
        variant_id=variant_id,
        ladder_id=variant.ladder_id,
        winner=result_meta["winner"],
        days=result_meta["days"],
        runoffs=result_meta["runoffs"],
        forced_end=result_meta["forced_end"],
        role_counts=dict(role_counts),
        game_run_id=str(result_meta["game_run_id"]),
        player_ids=list(player_ids),
        alive_player_ids=set(result_meta["alive_player_ids"]),
        action_totals=dict(result_meta["action_totals"]),
        rated=rated,
        rank_lookup_failed=fail_rank_lookup,
    )


class _GameLoopLogCapture(logging.Handler):
    """_game_loop が握り潰す終了理由をログから拾う。

    _game_loop は CancelledError / StateDurabilityError / Exception の
    3経路をどれも log を出すだけで return する。シミュレータからは
    「winnerが記録されなかった」ことしか分からず、本当の原因
    (DBロック競合など) が別のメッセージに化けてしまうため、
    room_runner のログを控えてアサーションに載せる。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:
            return
        if record.exc_info:
            text += f" | {record.exc_info[0].__name__}: {record.exc_info[1]}"
        self.records.append(f"{record.levelname}: {text}")

    def tail(self, n: int = 3) -> list[str]:
        return self.records[-n:]


async def _wait_for_game_task(
    game_task: asyncio.Task,
    *,
    timeout: float = 30.0,
) -> None:
    """CIの速度差を許容しつつ、タイムアウトを通常終了へ化けさせない。"""
    try:
        await asyncio.wait_for(asyncio.shield(game_task), timeout=timeout)
    except asyncio.TimeoutError as exc:
        game_task.cancel()
        await asyncio.gather(game_task, return_exceptions=True)
        raise AssertionError(
            f"simulation game task exceeded {timeout:.1f} seconds"
        ) from exc


async def _drain_manager_tasks(manager: GameCog, *, timeout: float = 1.0) -> None:
    """GameCog が spawn_bg_task した処理を畳んでから次のゲームへ進む。

    終了後推薦の受付は「全員が確定するか POSTGAME_RECOMMENDATION_TIMEOUT
    (3分) 経過」まで待つ。シミュレータでは誰も推薦を返さないため必ず
    3分待ちになり、1ゲームにつき1つずつタスクが積み残っていた。
    短時間で終わるものは待ち、待ち続けるものはキャンセルする。
    """
    pending = [t for t in getattr(manager, "_bg_tasks", ()) if not t.done()]
    if not pending:
        return
    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in still_pending:
        task.cancel()
    if still_pending:
        await asyncio.gather(*still_pending, return_exceptions=True)


def _assert_sandbox_db() -> None:
    """本番の統計DBへ書き込もうとしていないか確認する。

    シミュレーションは本物のゲームループを回すため、games / player_ratings /
    rating_history に実データと同じ形式で書き込む。`database.DB_PATH` を
    テンポラリへ差し替えずに実行すると、偽の試合とレートが本番DBへ
    そのまま入り、統計とランクが壊れる (実際に65試合・832レコードが
    混入した)。検証スクリプトからの直接呼び出しでも必ず止まるよう、
    ゲーム1試合ぶんの入口で確認する。

    テンポラリDBの用意には `sandbox_db()` を使うこと。
    """
    if Path(database.DB_PATH).resolve() == Path(PRODUCTION_DB_PATH).resolve():
        raise RuntimeError(
            "シミュレーションが本番DBを指しています: "
            f"{database.DB_PATH}\n"
            "simulate_games.sandbox_db() でテンポラリDBへ切り替えてから実行してください。"
        )


@asynccontextmanager
async def sandbox_db(prefix: str = "werewolf-sim-"):
    """テンポラリDBへ差し替えて init_db 済みの状態を提供する。

    シミュレーションを回す全てのコード (CLI・テスト・検証スクリプト) は
    必ずこれを通すこと。抜けると本番DBを汚染する。
    """
    temp_dir = tempfile.TemporaryDirectory(prefix=prefix)
    original_db_path = database.DB_PATH
    database.DB_PATH = str(Path(temp_dir.name) / "simulation.db")
    try:
        await database.init_db()
        yield database.DB_PATH
    finally:
        database.DB_PATH = original_db_path
        temp_dir.cleanup()


async def run_simulations(
    runs: int,
    variant_ids: Optional[list[str] | tuple[str, ...]] = None,
) -> None:
    # CLI経路も必ず共通ガードを通し、本番DBを指したまま下位処理へ入れない。
    async with sandbox_db(prefix="werewolf-cli-guard-"):
        await _run_simulations_with_private_temp(runs, variant_ids=variant_ids)


async def _run_simulations_with_private_temp(
    runs: int,
    *,
    variant_ids: Optional[list[str] | tuple[str, ...]] = None,
) -> None:
    temp_dir = tempfile.TemporaryDirectory(prefix="werewolf-sim-")
    tmp_dir = Path(temp_dir.name)
    original_db_path = database.DB_PATH
    database.DB_PATH = str(tmp_dir / "simulation.db")
    await database.init_db()

    try:
        scenarios = build_simulation_scenarios(runs, variant_ids)
        results = [
            await simulate_one_game(
                seed=scenario.seed,
                guild_id=777,
                force_runoff=scenario.force_runoff,
                variant_id=scenario.variant_id,
                rated=scenario.rated,
                fail_rank_lookup=scenario.fail_rank_lookup,
            )
            for scenario in scenarios
        ]
        # 最初の試合ではランク取得を意図的に壊す。勝敗精算とゲーム終了は
        # 成功し、ランク列だけNULLになることを実ゲームループで確認する。
        forced = results[0]

        total_games = len(results)
        winner_counts = Counter(result.winner for result in results)
        variant_counts = Counter(result.variant_id for result in results)
        total_runoffs = sum(result.runoffs for result in results)
        avg_days = sum(result.days for result in results) / total_games

        rows = await _read_game_count()
        assert rows == total_games, (rows, total_games)
        assert forced.runoffs >= 1, "forced runoff scenario did not reach runoff path"
        expected_variants = set(variant_ids or config.VARIANT_DEFINITIONS)
        assert expected_variants <= set(variant_counts), (
            expected_variants, variant_counts,
        )
        await _verify_recorded_game_stats(results)

        print(f"simulations: {total_games}")
        print(f"db_games: {rows}")
        print(f"variants: {dict(variant_counts)}")
        print(f"winners: {dict(winner_counts)}")
        print(f"total_runoffs: {total_runoffs}")
        print(f"average_days: {avg_days:.2f}")
        print("forced_runoff: OK")
        print("recorded_game_stats: OK")
        print("elo_delta_zero_sum: OK")
        print("rank_lookup_failure: OK")
        print("non_rated_rank_null: OK")
        print("simulation_status: OK")
    finally:
        database.DB_PATH = original_db_path
        temp_dir.cleanup()


async def _read_game_count() -> int:
    async with database.aiosqlite.connect(database.DB_PATH) as db:
        row = await db.execute_fetchall("SELECT COUNT(*) FROM games")
        return row[0][0]


async def _verify_recorded_game_stats(results: list[SimulationResult]) -> None:
    """実ゲームループの既知状態と、精算後DBを独立に突き合わせる。"""
    seen_rated_ladders: set[str] = set()
    async with database.connect_db() as db:
        for result in results:
            game_rows = await db.execute_fetchall(
                "SELECT g.game_id, g.gm_id, g.variant_id, g.ladder_id, "
                "gs.days, gs.peaceful_mornings, "
                "gs.guard_successes, gs.guard_checks, gs.seer_checks, gs.seer_wolf_hits, "
                "gs.executions_total, gs.executions_wolf, gs.wolf_alive_by_day, "
                "gs.rank_bucket FROM games g JOIN game_stats gs ON gs.game_id = g.game_id "
                "WHERE g.guild_id = ? AND g.game_run_id = ?",
                (777, result.game_run_id),
            )
            assert len(game_rows) == 1, (result.game_run_id, game_rows)
            (
                game_id, gm_id, variant_id, ladder_id, days, peaceful,
                guard_successes, guard_checks,
                seer_checks, seer_hits, executions_total, executions_wolf,
                wolf_json, rank_bucket,
            ) = game_rows[0]
            assert gm_id == 10_000, (gm_id, result.game_run_id)
            assert variant_id == result.variant_id, (
                variant_id, result.variant_id, result.game_run_id,
            )
            assert ladder_id == result.ladder_id, (
                ladder_id, result.ladder_id, result.game_run_id,
            )
            assert days == result.days, (days, result.days, result.game_run_id)
            assert peaceful == result.action_totals["peaceful_mornings"]
            assert guard_successes == result.action_totals["guard_successes"]
            assert guard_checks == result.action_totals["guard_checks"]
            assert seer_checks == result.action_totals["seer_checks"]
            assert seer_hits == result.action_totals["seer_wolf_hits"]
            assert executions_wolf <= executions_total

            wolf_alive = json.loads(wolf_json)
            assert len(wolf_alive) == days, (wolf_alive, days)
            assert all(
                later <= earlier
                for earlier, later in zip(wolf_alive, wolf_alive[1:])
            ), wolf_alive

            player_rows = await db.execute_fetchall(
                "SELECT player_id, died_on_day, rank_at_game FROM game_players "
                "WHERE game_id = ? ORDER BY player_id",
                (game_id,),
            )
            assert len(player_rows) == len(result.player_ids), (
                len(player_rows), len(result.player_ids), result.game_run_id,
            )
            stored_survivors = {
                int(player_id) for player_id, died_on_day, _rank in player_rows
                if died_on_day is None
            }
            assert stored_survivors == result.alive_player_ids, (
                stored_survivors, result.alive_player_ids, result.game_run_id,
            )

            rank_names = [rank for _pid, _day, rank in player_rows if rank is not None]
            expect_rank_names = (
                result.rated
                and not result.rank_lookup_failed
                and result.ladder_id in seen_rated_ladders
            )
            if expect_rank_names:
                assert len(rank_names) == len(result.player_ids), (
                    result.variant_id, result.game_run_id, rank_names,
                )
                ordered = sorted(rank_names, key=rating_lib.rank_order_value)
                expected_bucket = ordered[(len(ordered) - 1) // 2]
                assert rank_bucket == expected_bucket, (rank_bucket, expected_bucket)
            else:
                assert not rank_names, rank_names
                assert rank_bucket is None, rank_bucket

            rating_rows = await db.execute_fetchall(
                "SELECT variant_id, ladder_id, elo_delta FROM rating_history "
                "WHERE game_id = ? ORDER BY id",
                (game_id,),
            )
            if result.rated:
                assert len(rating_rows) == len(result.player_ids), (
                    len(rating_rows), len(result.player_ids), result.game_run_id,
                )
                assert {str(row[0]) for row in rating_rows} == {result.variant_id}
                assert {str(row[1]) for row in rating_rows} == {result.ladder_id}
                # 勝利参加・プレイ・推薦ボーナスを含む総deltaは非ゼロサム。
                # 固定プール本体のelo_deltaだけが必ず卓内で0になる。
                assert sum(int(row[2]) for row in rating_rows) == 0, (
                    result.game_run_id, rating_rows,
                )
            else:
                assert not rating_rows, (result.game_run_id, rating_rows)
            if result.rated:
                seen_rated_ladders.add(result.ladder_id)


def _select_balanced_players(
    *,
    rng: Random,
    player_ids: list[int],
    games_played: dict[int, int],
    player_count: int = config.MAX_PLAYERS,
) -> list[int]:
    if player_count <= 0:
        raise ValueError("player_count must be positive")
    if len(player_ids) < player_count:
        raise ValueError("not enough player_ids for player_count")
    remaining = player_count
    selected: list[int] = []
    current_level = min(games_played.values())

    while remaining > 0:
        bucket = [player_id for player_id in player_ids
                  if games_played[player_id] == current_level and player_id not in selected]
        if bucket:
            rng.shuffle(bucket)
            take = min(remaining, len(bucket))
            selected.extend(bucket[:take])
            remaining -= take
        current_level += 1

    return selected


async def _read_population_ratings(
    guild_id: int,
    ladder_id: str = config.DEFAULT_LADDER_ID,
) -> list[dict[str, int]]:
    async with database.aiosqlite.connect(database.DB_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT player_id, rating, peak_rating, games, wins "
            "FROM player_ratings WHERE guild_id = ? AND ladder_id = ? "
            "ORDER BY rating DESC",
            (guild_id, ladder_id),
        )
    return [
        {
            "player_id": row[0],
            "rating": row[1],
            "peak_rating": row[2],
            "games": row[3],
            "wins": row[4],
        }
        for row in rows
    ]


def _remainder_start(player_ids: list[int]) -> int:
    if not player_ids:
        return 0
    seed = sum(pid * 31 for pid in player_ids)
    return seed % len(player_ids)


def _split_pool_evenly(
    player_ids: list[int],
    total: int,
    *,
    negative: bool = False,
) -> dict[int, int]:
    if not player_ids:
        return {}

    count = len(player_ids)
    base, remainder = divmod(total, count)
    sign = -1 if negative else 1
    allocations = {pid: sign * base for pid in player_ids}

    if remainder:
        start = _remainder_start(player_ids)
        for offset in range(remainder):
            pid = player_ids[(start + offset) % count]
            allocations[pid] += sign

    return allocations


def build_fixed_pool_calculator(
    *,
    fixed_pool: int,
    win_bonus: int,
) -> Callable[..., list[dict[str, int]]]:
    def calculate_game_results(
        player_data: list[dict[str, int | bool]],
        *,
        winner_team: config.Team | str,
        **_rating_context: Any,
    ) -> list[dict[str, int]]:
        winners = [p for p in player_data if p["won"]]
        losers = [p for p in player_data if not p["won"]]

        if not winners or not losers:
            return [{
                "player_id": int(p["player_id"]),
                "rating_before": int(p["rating"]),
                "rating_after": int(p["rating"]),
                "delta": 0,
                "elo_delta": 0,
                "bonus": 0,
            } for p in player_data]

        winner_ids = [int(p["player_id"]) for p in winners]
        loser_ids = [int(p["player_id"]) for p in losers]
        winner_elo_map = _split_pool_evenly(winner_ids, fixed_pool)
        loser_elo_map = _split_pool_evenly(loser_ids, fixed_pool, negative=True)

        results = []
        for p in player_data:
            player_id = int(p["player_id"])
            rating_before = int(p["rating"])
            if p["won"]:
                elo_delta = winner_elo_map[player_id]
                bonus = win_bonus
            else:
                elo_delta = loser_elo_map[player_id]
                bonus = 0

            delta = elo_delta + bonus
            results.append({
                "player_id": player_id,
                "rating_before": rating_before,
                "rating_after": rating_before + delta,
                "delta": delta,
                "elo_delta": elo_delta,
                "bonus": bonus,
            })

        return results

    return calculate_game_results


@contextmanager
def patched_rating_calculator(
    calculator: Optional[Callable[..., list[dict[str, int]]]],
):
    if calculator is None:
        yield
        return

    original = rating_lib.calculate_game_results
    rating_lib.calculate_game_results = calculator
    try:
        yield
    finally:
        rating_lib.calculate_game_results = original


async def run_population_simulation(
    *,
    population_size: int,
    min_games: int,
    seed: int,
    variant_id: str = config.DEFAULT_VARIANT_ID,
    rating_label: str = "default",
    calculator: Optional[Callable[..., list[dict[str, int]]]] = None,
) -> None:
    # 長期シミュレーションも同じ本番DBガードを必ず通す。
    async with sandbox_db(prefix="werewolf-pop-guard-"):
        await _run_population_simulation_with_private_temp(
            population_size=population_size,
            min_games=min_games,
            seed=seed,
            variant_id=variant_id,
            rating_label=rating_label,
            calculator=calculator,
        )


async def _run_population_simulation_with_private_temp(
    *,
    population_size: int,
    min_games: int,
    seed: int,
    variant_id: str = config.DEFAULT_VARIANT_ID,
    rating_label: str = "default",
    calculator: Optional[Callable[..., list[dict[str, int]]]] = None,
) -> None:
    variant = config.get_variant_definition(variant_id)
    if population_size < variant.player_count:
        raise ValueError(
            f"population_size must be at least {variant.player_count} "
            f"for {variant_id}"
        )

    temp_dir = tempfile.TemporaryDirectory(prefix="werewolf-pop-")
    tmp_dir = Path(temp_dir.name)
    original_db_path = database.DB_PATH
    database.DB_PATH = str(tmp_dir / "population.db")
    await database.init_db()

    guild_id = 888
    player_ids = [20_000 + idx for idx in range(population_size)]
    games_played = {player_id: 0 for player_id in player_ids}
    rng = Random(seed)
    results: list[SimulationResult] = []

    try:
        with patched_rating_calculator(calculator):
            game_seed = seed
            while min(games_played.values()) < min_games:
                selected = _select_balanced_players(
                    rng=rng,
                    player_ids=player_ids,
                    games_played=games_played,
                    player_count=variant.player_count,
                )
                result = await simulate_selected_game(
                    seed=game_seed,
                    guild_id=guild_id,
                    player_ids=selected,
                    population_ids=player_ids,
                    force_runoff=(game_seed == seed),
                    variant_id=variant_id,
                )
                results.append(result)
                for player_id in selected:
                    games_played[player_id] += 1
                game_seed += 1

            ratings = await _read_population_ratings(guild_id, variant.ladder_id)
            if len(ratings) != population_size:
                raise AssertionError(f"expected {population_size} ratings, got {len(ratings)}")

            rank_map = await database.get_current_rank_map(
                guild_id, variant.ladder_id,
            )
            rank_counts = Counter(rank_map[row["player_id"]].rank_name for row in ratings)
            winner_counts = Counter(result.winner for result in results)
            total_games = len(results)
            total_runoffs = sum(result.runoffs for result in results)
            avg_days = sum(result.days for result in results) / total_games
            min_played = min(games_played.values())
            max_played = max(games_played.values())
            avg_rating = sum(row["rating"] for row in ratings) / len(ratings)

            print(f"rating_system: {rating_label}")
            print(f"variant: {variant_id}")
            print(f"ladder: {variant.ladder_id}")
            print(f"population_size: {population_size}")
            print(f"target_min_games: {min_games}")
            print(f"simulated_games: {total_games}")
            print(f"games_per_player_min: {min_played}")
            print(f"games_per_player_max: {max_played}")
            print(f"average_days: {avg_days:.2f}")
            print(f"total_runoffs: {total_runoffs}")
            print(f"winner_counts: {dict(winner_counts)}")
            print(f"average_rating: {avg_rating:.2f}")
            print("rank_distribution:")
            for rank_name, _emoji, _color in config.RANK_SPECS:
                count = rank_counts.get(rank_name, 0)
                pct = count / population_size * 100
                print(f"  {rank_name}: {count} ({pct:.1f}%)")

            print("top_10:")
            for idx, row in enumerate(ratings[:10], 1):
                rank_ctx = rank_map[row["player_id"]]
                winrate = row["wins"] / row["games"] * 100 if row["games"] else 0
                top_txt = f" top={rank_ctx.percentile:.1f}%" if rank_ctx.percentile is not None else " top=provisional"
                print(
                    f"  {idx}. P{row['player_id'] - 19_999}: "
                    f"rating={row['rating']} peak={row['peak_rating']} "
                    f"games={row['games']} wins={row['wins']} winrate={winrate:.1f}% rank={rank_ctx.rank_name}{top_txt}"
                )

            print("bottom_10:")
            for idx, row in enumerate(ratings[-10:], 1):
                rank_ctx = rank_map[row["player_id"]]
                winrate = row["wins"] / row["games"] * 100 if row["games"] else 0
                top_txt = f" top={rank_ctx.percentile:.1f}%" if rank_ctx.percentile is not None else " top=provisional"
                print(
                    f"  {idx}. P{row['player_id'] - 19_999}: "
                    f"rating={row['rating']} peak={row['peak_rating']} "
                    f"games={row['games']} wins={row['wins']} winrate={winrate:.1f}% rank={rank_ctx.rank_name}{top_txt}"
                )

            print("population_simulation_status: OK")
    finally:
        database.DB_PATH = original_db_path
        temp_dir.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local werewolf bot simulations.")
    parser.add_argument("--runs", type=int, default=30, help="number of additional random games to simulate")
    parser.add_argument(
        "--mode",
        choices=("single", "population"),
        default="single",
        help="single: standalone games, population: persistent player pool",
    )
    parser.add_argument(
        "--variant",
        choices=("all", *tuple(config.VARIANT_DEFINITIONS)),
        default="all",
        help=(
            "single mode defaults to every variant; population mode uses "
            f"{config.DEFAULT_VARIANT_ID} when 'all' is selected"
        ),
    )
    parser.add_argument("--population-size", type=int, default=200, help="population size for population mode")
    parser.add_argument("--min-games", type=int, default=200, help="minimum games per player in population mode")
    parser.add_argument("--seed", type=int, default=0, help="base RNG seed")
    parser.add_argument(
        "--rating-mode",
        choices=("default", "fixed-pool"),
        default="default",
        help="default: production rating logic, fixed-pool: simulation-only fixed zero-sum pool",
    )
    parser.add_argument("--fixed-pool", type=int, default=108, help="fixed zero-sum pool size for fixed-pool mode")
    parser.add_argument("--win-bonus", type=int, default=3, help="winner bonus for fixed-pool mode")
    args = parser.parse_args()

    calculator = None
    rating_label = "default"
    if args.rating_mode == "fixed-pool":
        calculator = build_fixed_pool_calculator(
            fixed_pool=args.fixed_pool,
            win_bonus=args.win_bonus,
        )
        rating_label = f"fixed-pool:{args.fixed_pool}+bonus:{args.win_bonus}"

    if args.mode == "single":
        variant_ids = (
            tuple(config.VARIANT_DEFINITIONS)
            if args.variant == "all"
            else (args.variant,)
        )
        asyncio.run(run_simulations(args.runs, variant_ids=variant_ids))
    else:
        population_variant = (
            config.DEFAULT_VARIANT_ID
            if args.variant == "all"
            else args.variant
        )
        asyncio.run(
            run_population_simulation(
                population_size=args.population_size,
                min_games=args.min_games,
                seed=args.seed,
                variant_id=population_variant,
                rating_label=rating_label,
                calculator=calculator,
            )
        )


if __name__ == "__main__":
    main()
