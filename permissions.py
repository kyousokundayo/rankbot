"""チャンネル・カテゴリの権限管理 (GameCog用ミックスイン)"""
from __future__ import annotations

import logging
from typing import Optional

import discord

import rating as rating_lib
from config import ADMIN_ONLY_ROOM_IDS, CH_SPIRIT, CH_VILLAGE, PUBLIC_ROOM_IDS

log = logging.getLogger(__name__)


class RoomVisibilityError(RuntimeError):
    """非公開境界の権限を安全に適用できなかった。"""


class RoomPermissionMixin:
    """部屋の表示権限・公開カテゴリの権限を管理するミックスイン。

    利用側 (GameCog) に discord_api_sem (asyncio.Semaphore) があることを前提とする。
    paced_discord_api_call があれば、複数の権限差分を適用する際のバーストも抑える。
    """

    def _rank_role_by_name(self, guild: discord.Guild) -> dict[str, discord.Role]:
        return {role.name: role for role in guild.roles}

    def _validate_room_access_roles(self, guild: discord.Guild, room_def) -> None:
        """ローカル卓の閲覧ロールをDiscord副作用より前に検証する。"""
        required = frozenset(getattr(room_def, "access_role_names", None) or ())
        if not required:
            return
        existing = self._rank_role_by_name(guild)
        missing = sorted(name for name in required if name not in existing)
        if missing:
            raise RoomVisibilityError(
                f"{room_def.name} の許可ロールが見つかりません: "
                + " / ".join(missing)
            )

    def _has_permission_overwrite(self, channel, target) -> bool:
        overwrites = getattr(channel, "overwrites", None)
        if isinstance(overwrites, dict):
            return target in overwrites
        permissions = getattr(channel, "permissions", None)
        if isinstance(permissions, dict):
            return getattr(target, "id", target) in permissions
        return True

    def _permission_overwrite_matches(self, channel, target, overwrite: discord.PermissionOverwrite) -> bool:
        overwrites_for = getattr(channel, "overwrites_for", None)
        if not callable(overwrites_for):
            return False
        return overwrites_for(target) == overwrite

    async def _set_permission_if_changed(
        self,
        channel,
        target,
        overwrite: discord.PermissionOverwrite | None,
        *,
        reason: str,
    ) -> None:
        if overwrite is None:
            if not self._has_permission_overwrite(channel, target):
                return
        elif self._permission_overwrite_matches(channel, target, overwrite):
            return
        paced_call = getattr(self, "paced_discord_api_call", None)
        if callable(paced_call):
            await paced_call(
                channel.set_permissions,
                target,
                overwrite=overwrite,
                reason=reason,
            )
        else:
            async with self.discord_api_sem:
                await channel.set_permissions(target, overwrite=overwrite, reason=reason)

    def _stale_visibility_targets(
        self,
        guild: discord.Guild,
        channel,
        allowed_targets: set,
    ) -> list:
        """許可リスト外で閲覧を明示allowしているoverwrite対象を返す。"""
        mapping = getattr(channel, "overwrites", None)
        if not isinstance(mapping, dict):
            return []
        allowed_ids = {getattr(target, "id", target) for target in allowed_targets}
        stale: list = []
        for raw_target, raw_overwrite in list(mapping.items()):
            target_id = getattr(raw_target, "id", raw_target)
            if target_id in allowed_ids:
                continue
            target = raw_target
            if not hasattr(raw_target, "id"):
                target = next(
                    (role for role in guild.roles if role.id == target_id),
                    None,
                )
                if target is None:
                    get_member = getattr(guild, "get_member", None)
                    target = get_member(target_id) if callable(get_member) else None
                if target is None:
                    # Discord実体ではtarget objectが返る。解決不能なFake/破損値は
                    # API対象にできないためここでは扱わない。
                    continue
            overwrite = raw_overwrite
            if not isinstance(overwrite, discord.PermissionOverwrite):
                overwrites_for = getattr(channel, "overwrites_for", None)
                if not callable(overwrites_for):
                    continue
                overwrite = overwrites_for(target)
            if any(
                getattr(overwrite, permission, None) is True
                for permission in ("view_channel", "read_messages", "connect")
            ):
                stale.append(target)
        return stale

    async def _remove_stale_visibility_allows(
        self,
        guild: discord.Guild,
        channel,
        allowed_targets: set,
        *,
        label: str,
    ) -> None:
        for target in self._stale_visibility_targets(guild, channel, allowed_targets):
            try:
                await self._set_permission_if_changed(
                    channel, target, None, reason="村表示権限の残骸解除"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                raise RoomVisibilityError(
                    f"{label}の未許可閲覧権限を解除できません "
                    f"({getattr(target, 'name', target)}): {e}"
                ) from e

    def _build_room_overwrites(
        self,
        guild: discord.Guild,
        room_def,
        *,
        send_messages: Optional[bool] = None,
    ) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        private_room = room_def.private_owner_id is not None and room_def.private_role_name is not None
        admin_only = room_def.room_id in ADMIN_ONLY_ROOM_IDS and not private_room
        access_role_names = frozenset(
            getattr(room_def, "access_role_names", None) or ()
        )
        public_room = (
            room_def.room_id in PUBLIC_ROOM_IDS
            and not private_room
            and not admin_only
            and not access_role_names
        )
        default_overwrite = discord.PermissionOverwrite(
            view_channel=public_room,
            read_messages=public_room,
            connect=public_room,
        )
        if send_messages is not None:
            default_overwrite.send_messages = send_messages if public_room else False

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: default_overwrite,
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                read_messages=True,
                send_messages=True,
                connect=True,
                # ゲーム中は@everyoneのspeakを拒否するため、Bot自身には明示的に
                # speakを許可しておく (シーン切替SEの再生に必要。管理者権限を
                # 外した運用でも無音にならないようにする)
                speak=True,
                manage_channels=True,
            ),
        }

        # 管理者限定カテゴリでは、Bot以外のロールへ明示allowを付けない。
        # Administrator権限保持者とサーバー所有者だけがDiscord側で拒否を
        # バイパスできる。manage_guildだけのロールも閲覧不可にする。
        if admin_only:
            return overwrites

        if access_role_names:
            roles_by_name = self._rank_role_by_name(guild)
            for role_name in access_role_names:
                role = roles_by_name.get(role_name)
                if role is None:
                    log.warning(
                        "卓の閲覧ロールが見つかりません (%s/%s)",
                        room_def.name,
                        role_name,
                    )
                    continue
                overwrite = discord.PermissionOverwrite(
                    view_channel=True,
                    read_messages=True,
                    connect=True,
                )
                if send_messages is not None:
                    overwrite.send_messages = send_messages
                overwrites[role] = overwrite
            for role in guild.roles:
                permissions = getattr(role, "permissions", None)
                if role == guild.default_role or not getattr(
                    permissions, "manage_guild", False
                ):
                    continue
                overwrite = discord.PermissionOverwrite(
                    view_channel=True,
                    read_messages=True,
                    connect=True,
                )
                if send_messages is not None:
                    overwrite.send_messages = send_messages
                overwrites[role] = overwrite
            return overwrites

        if private_room:
            role = discord.utils.get(guild.roles, name=room_def.private_role_name)
            if role is not None:
                overwrite = discord.PermissionOverwrite(
                    view_channel=True,
                    read_messages=True,
                    connect=True,
                )
                if send_messages is not None:
                    overwrite.send_messages = send_messages
                overwrites[role] = overwrite
            return overwrites

        if room_def.allowed_ranks:
            roles_by_name = self._rank_role_by_name(guild)
            for rank_name in room_def.allowed_ranks:
                role = roles_by_name.get(rating_lib.get_rank_role_name(rank_name))
                if role is None:
                    continue
                overwrite = discord.PermissionOverwrite(
                    view_channel=True,
                    read_messages=True,
                    connect=True,
                )
                if send_messages is not None:
                    overwrite.send_messages = send_messages
                overwrites[role] = overwrite

        return overwrites

    async def _apply_room_visibility(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        room_def,
    ) -> None:
        overwrites = self._build_room_overwrites(guild, room_def)
        admin_only = (
            room_def.room_id in ADMIN_ONLY_ROOM_IDS
            and room_def.private_owner_id is None
        )
        restricted = overwrites[guild.default_role].view_channel is False
        managed_rank_role_names = set(rating_lib.all_rank_role_names())
        stale_rank_roles = [
            role for role in guild.roles
            if role.name in managed_rank_role_names and role not in overwrites
        ]
        for target, overwrite in overwrites.items():
            try:
                await self._set_permission_if_changed(
                    category, target, overwrite, reason="村表示権限更新"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                raise RoomVisibilityError(
                    f"カテゴリ権限更新失敗 ({room_def.name}/{getattr(target, 'name', target)}): {e}"
                ) from e
        for role in stale_rank_roles:
            try:
                await self._set_permission_if_changed(
                    category, role, None, reason="村表示権限更新"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                raise RoomVisibilityError(
                    f"カテゴリ対象外ランク権限解除失敗 ({room_def.name}/{role.name}): {e}"
                ) from e
        if restricted:
            await self._remove_stale_visibility_allows(
                guild,
                category,
                set(overwrites),
                label=f"カテゴリ {room_def.name}",
            )

        # ゲーム中チャンネル (#昼/#霊界) は通常RoomRunnerに任せるが、
        # 管理者限定カテゴリでは例外なく全子チャンネルを非公開に揃える。
        children = [
            ch for ch in [*guild.text_channels, *guild.voice_channels]
            if ch.category and ch.category.id == category.id
            and (admin_only or ch.name not in (CH_VILLAGE, CH_SPIRIT))
        ]
        for ch in children:
            for target, overwrite in overwrites.items():
                try:
                    await self._set_permission_if_changed(
                        ch, target, overwrite, reason="村表示権限更新"
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    raise RoomVisibilityError(
                        f"チャンネル権限更新失敗 ({room_def.name}/{ch.name}): {e}"
                    ) from e
            for role in stale_rank_roles:
                try:
                    await self._set_permission_if_changed(
                        ch, role, None, reason="村表示権限更新"
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    raise RoomVisibilityError(
                        f"チャンネル対象外ランク権限解除失敗 ({room_def.name}/{ch.name}/{role.name}): {e}"
                    ) from e
            if restricted:
                await self._remove_stale_visibility_allows(
                    guild,
                    ch,
                    set(overwrites),
                    label=f"チャンネル {room_def.name}/{ch.name}",
                )
