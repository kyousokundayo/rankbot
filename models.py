"""データモデル定義"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable, Optional

import discord

from config import Phase, Role, Team, ROLE_TEAM


def by_number(players: Iterable["Player"]) -> list["Player"]:
    """プレイヤーを番号昇順に並べた**コピー**を返す。

    参加者を選ぶUI (投票ボタン・占い/護衛/襲撃セレクト・GMの除外) と、
    参加者を並べる表示は全てこれを通す。プレイヤーは毎回ランダムな番号を
    受け取るので、参加順のまま出すと「01, 07, 03…」と読めない並びになる。

    **state.players の dict 自体は絶対に並べ替えないこと。** 番号割り当てと
    役職配布がその反復順に依存し (room_runner の _start_game / _assign_roles)、
    スナップショットも同じ順で保存・復元される。必ずコピーを渡す。

    ロビー中は全員 number=0 だが、sorted は安定ソートなので参加順が保たれる。
    """
    return sorted(players, key=lambda p: p.number)


def parse_select_id(raw: object) -> Optional[int]:
    """セレクトUIから返った値をIDとして安全に解釈する。

    インタラクションの values はクライアントが送る値なので、Discord公式
    クライアント以外からは任意の文字列が届きうる。そのまま int() すると
    ValueError でコールバックが落ち、押した人にはDiscordの
    「インタラクションに失敗しました」しか出ない。解釈できない値は None を
    返し、呼び出し側が理由を本人へ返せるようにする。

    IDは常にASCII数字なので、それ以外は受け付けない。int() は全角やアラビア
    数字などのUnicode数字も解釈してしまう (int("１２３") や int("١٢٣") が通る)
    ため、提示した選択肢以外の表記が別のIDへ化けないよう明示的に弾く。

    (views.py と recruitment.py の両方から使うため、双方が既に依存している
     このモジュールへ置く。遅延importを増やさないための配置)
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw[1:] if raw.startswith("-") else raw  # 「噛みなし」の -1 を許す
    if not text.isascii() or not text.isdigit():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass
class Player:
    user_id: int
    member: discord.Member
    role: Optional[Role] = None
    alive: bool = True
    number: int = 0
    original_nickname: Optional[str] = None
    # ゲーム開始時 (改名前) に確定する素の表示名。
    # member.display_name は改名後「番号.名前」になるため、
    # フォールバックに使うと番号が二重になる (復元時のニックネーム未設定者など)
    base_name: Optional[str] = None

    @property
    def team(self) -> Optional[Team]:
        if self.role is None:
            return None
        return ROLE_TEAM[self.role]

    @property
    def display_name(self) -> str:
        base = self.base_name or self.original_nickname or self.member.display_name
        # 番号は2桁0埋め (01, 02, ... 13) で表示を揃える
        return f"{self.number:02d}.{base}"

    @property
    def is_wolf(self) -> bool:
        return self.role == Role.WEREWOLF


class GameState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.room_id: str = ""
        self.room_name: str = ""
        self.phase: Phase = Phase.LOBBY
        self.day_number: int = 0
        self.players: dict[int, Player] = {}
        self.gm_id: Optional[int] = None
        self.guild: Optional[discord.Guild] = None
        self.recovery_phase: Optional[Phase] = None
        self.recovered_from_restart: bool = False

        # 1ゲームを一意に識別するIDと、昼/夜UIの世代番号。
        # 古いDiscordメッセージのViewが次ゲーム・次の夜へ作用するのを防ぎ、
        # 再起動後の精算/フェーズ処理を冪等にするためスナップショットへ保存する。
        self.game_run_id: str = ""
        # 募集経由で組まれたゲームだけ設定。直接ロビー参加はNone。
        self.recruitment_id: Optional[int] = None
        # 開始時にカテゴリ/VCの閲覧境界が一般公開だったかを表す。
        # 設定変更後の再起動中もゲーム中のアクセス境界を保つためsnapshotへ保存する。
        # 終了ログはこの値に関係なく、全村で共通の公開ログへ退避する。
        self.public_log_archive_allowed: bool = False
        # 静的権限を手動管理する卓では、ゲーム中だけVCの発言/内蔵チャットを
        # 閉じ、終了時に開始前の三値(True/False/未設定)へ正確に戻す。
        self.vc_default_permissions_captured: bool = False
        self.vc_default_speak_before_game: Optional[bool] = None
        self.vc_default_send_before_game: Optional[bool] = None
        # 専任GMへゲーム中だけ付けるVC発言許可も、手動overwriteの他項目を
        # 壊さず、終了時に開始前の三値へ戻す。再起動をまたぐためIDも保存する。
        self.vc_gm_speak_captured: bool = False
        self.vc_gm_speak_user_id: Optional[int] = None
        self.vc_gm_speak_before_game: Optional[bool] = None
        # Botが作成した #昼/#霊界 の所有ID。手動管理カテゴリでは名前だけを
        # 根拠に既存チャンネルを削除しないため、ロビーへ戻った後も保持する。
        self.managed_game_channel_ids: set[int] = set()
        self.day_generation: int = 0
        self.night_generation: int = 0

        # フェーズ結果の処理済みチェックポイント。
        # 死亡状態と同じスナップショットに保存し、処刑/襲撃適用後の再起動で
        # 同じ昼・夜をもう一度解決しないようにする。
        self.day_execution_resolved: bool = False
        self.day_executed_target: Optional[int] = None
        # 投票集計後〜遺言中のクラッシュで、確定した
        # 処刑対象(再同票ランダムを含む)を失わないための予定値。
        self.pending_execution_target: Optional[int] = None
        self.night_resolved: bool = False
        # 勝敗確定後、DBへの精算stageに失敗して安全停止している場合の再試行用。
        self.pending_winner: Optional[Team] = None
        # 終了処理開始後の古いView/除外/一時停止との競合を防ぐ。
        self.ending: bool = False

        # PREPARATIONのdurable saga。役職・番号・初日白を先に
        # 保存し、外部Discord副作用は送信済み記録で再開可能にする。
        self.preparation_dm_sent_ids: set[int] = set()
        self.initial_seer_target: Optional[int] = None
        self.initial_seer_result_sent: bool = False

        # 死亡/除外のDiscord副作用outbox。aliveと処理済み印を
        # 先に保存し、クラッシュ後は通知・権限を再適用する。
        self.pending_death_effects: list[dict] = []

        # 終了後推薦の投票権判定用。朝ログで消える一時表示とは分けて、
        # 初日の処刑者と初夜の襲撃死者をゲーム終了まで保持する。
        self.day1_executed_id: Optional[int] = None
        self.night1_killed_id: Optional[int] = None

        # プレイボーナスの材料。精算キューへ載せるのでスナップショットにも保存する。
        # decisive_executions: 投票で決まった処刑だけを積む
        #   ({"day", "target", "voters"})。0票・再同票のランダム処刑は入れない
        # wolf_guesses: 人狼予想。死亡確定時に凍結した「死亡者ID -> 選んだ相手」
        # spirit_hold_ids: 人狼予想のため霊界の閲覧解放を保留している死亡者。
        #   保留中だけ提出でき、提出か時間切れで解放する。霊界に入ってから
        #   先住者に答えを聞けてしまうと提出の意味が無くなるための仕組み
        # spirit_hold_events: 保留を開始した死亡イベントID。古いDMが同じ試合や
        #   次の試合の受付へ作用しないよう、提出時にgame_run_idと併せて照合する
        self.decisive_executions: list[dict] = []
        self.wolf_guesses: dict[int, list[int]] = {}
        self.spirit_hold_ids: set[int] = set()
        self.spirit_hold_events: dict[int, str] = {}

        # チャンネル
        self.village_channel: Optional[discord.TextChannel] = None
        self.spirit_channel: Optional[discord.TextChannel] = None  # 霊界 (死亡者+観戦者)
        self.lobby_channel: Optional[discord.TextChannel] = None
        self.voice_channel: Optional[discord.VoiceChannel] = None
        self.stats_channel: Optional[discord.TextChannel] = None
        self.category: Optional[discord.CategoryChannel] = None

        # 投票
        self.votes: dict[int, int] = {}          # voter_id -> target_id
        self.runoff_candidates: list[int] = []
        self.runoff_speech_index: int = 0

        # 通常投票のdurableな発言順とcursor。順番は「投票」ボタンを押した順で、
        # 押した人から1人ずつ20秒だけ発言し、確定投票か時間切れで次へ進む。
        # 列が空になったら次に押した人が来るまで待つ。進行中に落ちた場合は
        # 完了済みcursorを維持し、未完了だった枠だけを満額でやり直す。
        self.vote_day_generation: int = 0
        self.vote_order: list[int] = []
        self.vote_slot_index: int = 0
        self.vote_slot_token: int = 0
        self.vote_slot_active: bool = False
        # GMが待機を打ち切って締め切った状態。未発言者は棄権として集計する。
        self.vote_closed: bool = False
        # 自分の発言枠の中で投票先が除外され、票だけ失った人。cursorを
        # 進めた後でないと列を組み替えられないので、印だけ置いて
        # _day_vote 側で末尾へ積み直す。
        self.vote_requeue_ids: set[int] = set()
        # 通常投票の公開パネルを再利用するためのメッセージID。
        self.vote_panel_message_id: Optional[int] = None
        # 現在枠の残り時間は表示更新用のプロセス内状態。復元時は同じ投票者の
        # 20秒を満額でやり直すためsnapshotへは保存しない。
        self.vote_remaining_seconds: float = 0.0

        # 決戦弁明・遺言・ターン制議論の現在話者。
        # ターン制では turn_slot_token も併用し、同じ人が2巡目に回ったときに
        # 1巡目の古いボタンが現在の発言を終了させないようにする。
        self.current_speaker_id: Optional[int] = None

        # ターン制議論のdurable cursor。完了したslotだけをcheckpointし、
        # 再起動時は進行中だったslotだけを満額でやり直す。
        self.turn_day_generation: int = 0
        self.turn_anchor_number: Optional[int] = None
        self.next_turn_anchor_number: Optional[int] = None
        self.turn_order: list[int] = []
        self.turn_round_index: int = 0
        self.turn_slot_index: int = 0
        self.turn_slot_token: int = 0
        self.turn_slot_active: bool = False
        self.turn_original_speaker_id: Optional[int] = None
        self.turn_interrupt_active: bool = False
        self.turn_interrupt_pending_id: Optional[int] = None
        self.turn_interrupts_used: int = 0
        # ターン制の公開CO。役職・内容は持たず、押下した人の当日一覧だけを
        # 保存する。退出・除外後も表示を保てるよう、当時の表示名も記録する。
        self.turn_co_declarations: list[dict[str, object]] = []
        # 1日1枚の話者パネル。再起動後に同じメッセージから再開し、
        # 古いボタンだけが残るパネルを増殖させない。
        self.turn_panel_message_id: Optional[int] = None
        # ボタン受付窓と残り時間はプロセス内だけの状態。クラッシュ復旧では
        # slotの規定時間を満額で再実行するため、snapshotへは保存しない。
        self.turn_window_open: bool = False
        self.turn_remaining_seconds: float = 0.0

        # 夜アクション
        self.wolf_target: Optional[int] = None
        self.wolf_voters: dict[int, int] = {}    # wolf_id -> target_id
        self.seer_target: Optional[int] = None
        self.guard_target: Optional[int] = None
        self.guard_previous: Optional[int] = None

        # 進行ログ: 役職の行動・投票・死亡を発生順に積む。
        # 終了時に #昼 へ貼って、二重実行などの不具合を追えるようにする。
        # 拒否された操作もここへ残す (弾けていることの証跡になる)。
        self.action_log: list[dict] = []

        # 役職確認後・1日目開始前の「0日目初夜」。人狼の挨拶DM中継だけを
        # 30秒開き、能力行使は一切行わない。完了状態を保存し、再起動後に
        # 挨拶時間を二重実行して1日目の開始を遅らせない。
        self.initial_night_completed: bool = False

        # 生存中の実人狼によるサレンダー同意。全員同意で村陣営勝利として
        # 通常精算する。途中経過はDMだけに留め、成立後だけ公開する。
        self.surrender_ids: set[int] = set()
        self.surrender_confirmed: bool = False
        self.surrender_announced: bool = False

        # 夜の目安時間が終わってから、朝を迎える宣言の受付を開く。
        # 生存者全員が押すまで明けず、押下は一方向で取り消せない。
        self.morning_ready_open: bool = False
        self.morning_ready_ids: set[int] = set()
        # 未行動のまま「朝を迎える」を押して警告を受けた人 (2度目の押下で確定)
        self.morning_warned_ids: set[int] = set()
        # 全員宣言またはGM強制によって夜明けが確定済みか。
        # asyncio.Event自体は永続化できないため、復元用のboolを別に持つ。
        self.morning_confirmed: bool = False

        # 役職確認の宣言 (初日はここが揃うまで議論に入らない)
        self.prep_ready_ids: set[int] = set()
        # 全員宣言またはGMの確認付き締切で役職確認が終了済みか (復元用bool)
        self.prep_confirmed: bool = False

        # 一時停止
        self.paused: bool = False
        self.phase_before_pause: Optional[Phase] = None

        # 復帰待ちのプレイヤー (VC切断/サーバー退出で自動一時停止した対象)
        self.disconnected_players: set[int] = set()

        # botがサーバーミュートしたメンバー (終了時の解除漏れ防止用。
        # 手動ミュートと区別するために記録し、スナップショットへ永続化する)
        self.bot_muted_ids: set[int] = set()
        # PREPARATIONでmute PATCHを出す前の永続意図。実際の
        # 成功所有(bot_muted_ids)と分離し、API失敗者の後付け
        # 手動muteをBotが解除しないようにする。
        self.bot_mute_intent_ids: set[int] = set()
        # muteと同一Member.edit PATCHで専用マーカーロールを
        # 付け外しする方式が有効か。checkpoint前に落ちても
        # Discord側のマーカーからBot所有muteを一意に復元できる。
        self.mute_marker_enabled: bool = False

        # ニックネーム復元用
        self.original_nicknames: dict[int, Optional[str]] = {}  # 全メンバー

        # ゲームタスク
        self.game_task: Optional[asyncio.Task] = None
        self.pause_event: asyncio.Event = asyncio.Event()
        self.pause_event.set()  # 初期状態: 非停止

        # 投票完了イベント
        self.vote_complete_event: asyncio.Event = asyncio.Event()

        # 通常投票の列に誰かが並んだ (またはGMが締め切った) 合図。
        # 列が空の待機中だけ待ち、起こされたら条件を見直す。
        self.vote_queue_event: asyncio.Event = asyncio.Event()

        # 夜アクション完了イベント (未行動者への警告DMを省くかの判定に使う)
        self.night_complete_event: asyncio.Event = asyncio.Event()

        # 0日目初夜をGMが早送りするためのイベント (完了boolは上で永続化)。
        self.initial_night_skip_event: asyncio.Event = asyncio.Event()

        # 朝を迎えるイベント (生存者全員が宣言 or GMの強制で立つ)
        self.morning_ready_event: asyncio.Event = asyncio.Event()

        # 役職確認完了イベント (参加者全員が宣言 or GMの強制で立つ)
        self.prep_ready_event: asyncio.Event = asyncio.Event()

        # 弁明終了イベント
        self.speech_done_event: asyncio.Event = asyncio.Event()

        # ターン制の現在発言終了・割り込み要求。turn_signal_eventへ集約して
        # タイマー側が0.5秒ごとに複数Taskを作らず即座に起きられるようにする。
        self.turn_done_event: asyncio.Event = asyncio.Event()
        self.turn_interrupt_event: asyncio.Event = asyncio.Event()
        self.turn_signal_event: asyncio.Event = asyncio.Event()

        # 朝ログ追跡
        self._last_executed: Optional[object] = None
        self._last_killed: Optional[object] = None
        self._last_guarded: bool = False

        # ロビーメッセージ (永続化用)
        self.lobby_message: Optional[discord.Message] = None

        # GMコントロールパネル (#昼 の末尾に掲示するための参照。非永続)
        self.gm_panel_message: Optional[discord.Message] = None
        self.gm_panel_view: Optional[object] = None

        # 各人狼のDM襲撃UIメッセージ (現在の襲撃先を全狼へ反映するため。非永続)
        self.wolf_dm_messages: dict[int, discord.Message] = {}
        # 現在の夜の制限時間 (人狼DM本文の再生成に使う。非永続)
        self.night_duration: float = 0.0
        # 人狼DMの中継を受け付ける窓が開いているか (非永続)。
        # 夜の制限時間で開閉し、「朝を迎える」の宣言状況とは切り離す。
        # 復元時は同じ夜のUIを制限時間ごと出し直すので、保存せず作り直す
        self.wolf_relay_window_open: bool = False

        # 「朝を迎える」パネル (夜の間 #昼 に1枚だけ掲示。非永続)
        self.morning_panel_message: Optional[discord.Message] = None
        # 掲示中パネルのメッセージID (永続)。再起動するとViewが復元されず
        # 押しても「インタラクションに失敗しました」になるだけなので、
        # 次の掲示より前にこのIDで消す。
        self.morning_panel_message_id: Optional[int] = None

        # 「役職を確認した」パネル (役職確認タイム中 #昼 に掲示。非永続)
        self.prep_panel_message: Optional[discord.Message] = None

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def alive_wolves(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive and p.is_wolf]

    def alive_villagers(self) -> list[Player]:
        """生存中の非人狼プレイヤー (狂人を含む / 勝利条件判定用)"""
        return [p for p in self.players.values()
                if p.alive and not p.is_wolf]

    def get_player(self, user_id: int) -> Optional[Player]:
        return self.players.get(user_id)

    def get_day_discussion_time(
        self,
        discussion_seconds: tuple[int, int, int],
    ) -> int:
        """選択変種のクロストーク昼時間を当日ぶん返す。"""
        base, decrease, minimum = discussion_seconds
        t = base - (self.day_number - 1) * decrease
        return max(t, minimum)

    def get_night_time(self) -> int:
        from config import NIGHT_BASE, NIGHT_DECREASE, NIGHT_MIN
        t = NIGHT_BASE - (self.day_number - 1) * NIGHT_DECREASE
        return max(t, NIGHT_MIN)

    def check_win(self) -> Optional[Team]:
        wolves = len(self.alive_wolves())
        non_wolves = len(self.alive_villagers())
        if wolves == 0:
            return Team.VILLAGE
        if wolves >= non_wolves:
            return Team.WOLF
        return None
