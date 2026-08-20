"""シーン切替SE: 生成トーンをVCで再生する

音源ファイルは使わず、起動時にメモリ上でサイン波トーンを合成する。

依存: davey (DAVE E2EE) + PyNaCl (音声送信) + libopus (エンコード)。
SE_ENABLED=TrueではBot起動前に検証し、欠けた環境をreadyにしない。
再生時の一時的な接続失敗はSEだけをスキップし、ゲーム進行には影響しない。
  - pip install davey PyNaCl
  - macOS: brew install opus

同一ギルドでBotが同時に接続できるVCは1つのため、ギルド単位のロックで
「接続 → 再生 (1秒前後) → 切断」を直列化する。複数卓の同時フェーズ切替では
後の卓のSEが数秒待たされることがあるが、ゲーム進行はブロックしない。
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import struct
from importlib import metadata
from pathlib import Path
from typing import Optional

import discord

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000  # discord.PCMAudio の要求形式 (48kHz 16bit ステレオ)
VOICE_OPERATION_TIMEOUT = 5.0
VOICE_FORCE_DISCONNECT_TIMEOUT = 2.0
# 複数卓のフェーズ切替が重なっても、数秒前の古い合図を後から鳴らさない。
VOICE_QUEUE_MAX_WAIT = 1.5

# シーン名 -> (周波数列Hz, 1音の長さ秒, 音量0-1)
_SCENES: dict[str, tuple[list[float], float, float]] = {
    "morning":   ([523.25, 659.25, 783.99], 0.18, 0.5),  # C5→E5→G5 上昇 (朝)
    "prep_end":  ([523.25, 659.25], 0.15, 0.5),          # C5→E5 (役職確認タイム終了)
    "discussion": ([659.25, 880.0], 0.14, 0.55),          # E5→A5 (議論開始の合図)
    "discussion_end": ([880.0, 659.25], 0.16, 0.5),      # A5→E5 下降 (議論終了)
    "reveal":    ([659.25, 523.25], 0.20, 0.5),          # E5→C5 (投票開示)
    "speech":    ([698.46, 880.0], 0.14, 0.5),           # F5→A5 (決戦弁明の開始)
    "speech_end": ([880.0, 698.46], 0.14, 0.5),          # A5→F5 (投票発言の終了)
    "lastwill":  ([392.0, 329.63], 0.25, 0.45),          # G4→E4 (遺言)
    "execution": ([196.0], 0.60, 0.5),                   # G3 低音 (処刑)
    "night":     ([392.0, 261.63], 0.25, 0.45),          # G4→C4 下降 (夜)
}

# libopus の明示ロード候補。環境探索より先に試し、Intel Homebrewなどの
# 残存ライブラリを誤って選ばないようプロジェクト同梱版を最優先する。
_OPUS_PATHS = (
    str(Path(__file__).parent / "data" / "lib" / "libopus.dylib"),
    "/opt/homebrew/lib/libopus.dylib",
    "/opt/homebrew/lib/libopus.0.dylib",
    "/usr/local/lib/libopus.dylib",
)


class VoiceDependencyError(RuntimeError):
    """SEを有効にした起動環境がdiscord.pyの音声要件を満たしていない。"""


def _render_tone(freqs: list[float], duration: float, volume: float) -> bytes:
    """サイン波の連続音を 48kHz 16bit ステレオPCM で合成する"""
    frames = bytearray()
    n = int(SAMPLE_RATE * duration)
    attack = int(SAMPLE_RATE * 0.01)   # 10ms
    release = int(SAMPLE_RATE * 0.05)  # 50ms
    gap = b"\x00" * (4 * int(SAMPLE_RATE * 0.03))  # 音間の無音 30ms
    for freq in freqs:
        for i in range(n):
            env = min(1.0, i / attack if attack else 1.0, (n - i) / release if release else 1.0)
            v = int(32767 * volume * env * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
            frames += struct.pack("<hh", v, v)
        frames += gap
    return bytes(frames)


def _ensure_opus_loaded() -> Optional[str]:
    """libopusをロードし、失敗時だけ説明文を返す。"""
    if discord.opus.is_loaded():
        return None
    path_errors: list[str] = []
    for path in _OPUS_PATHS:
        if not Path(path).exists():
            continue
        try:
            discord.opus.load_opus(path)
            log.info(f"libopus をロードしました: {path}")
            return None
        except Exception as e:
            path_errors.append(f"{path}: {e}")
    default_error: Optional[Exception] = None
    try:
        discord.opus._load_default()
    except Exception as e:
        default_error = e
    if discord.opus.is_loaded():
        return None
    detail = "; ".join(path_errors)
    if not detail and default_error is not None:
        detail = str(default_error)
    suffix = f" ({detail})" if detail else ""
    return f"libopusをロードできません。`brew install opus` を実行してください{suffix}"


def _voice_dependency_error() -> Optional[str]:
    """discord.py 2.7系の音声依存を実際に初期化して検査する。"""
    try:
        import davey
    except ImportError as e:
        return f"daveyが見つかりません: {e}"

    # Python本体とwheelのCPUアーキテクチャが不一致でも、importだけではなく
    # ネイティブDAVEセッションの生成まで通ることを起動前に確認する。
    try:
        dave_session = davey.DaveSession(
            davey.DAVE_PROTOCOL_VERSION, 1, 1
        )
        if not dave_session.get_serialized_key_package():
            return "daveyのDAVEキーパッケージを生成できません"
    except Exception as e:
        return f"daveyを初期化できません: {e}"

    # discord.py 2.7.1はvoice_stateとVoiceClientの各モジュールで
    # import時の可用性を保持する。両方を検査し、実接続時だけ落ちる状態を防ぐ。
    from discord import voice_client, voice_state

    if not voice_state.has_dave or not voice_client.has_dave:
        return (
            "discord.pyがdaveyを認識していません。依存導入後にBotを完全再起動してください"
        )

    try:
        __import__("nacl")
    except ImportError as e:
        return f"PyNaClが見つかりません: {e}"
    if not voice_client.has_nacl:
        return (
            "discord.pyがPyNaClを認識していません。依存導入後にBotを完全再起動してください"
        )

    return _ensure_opus_loaded()


def require_voice_ready() -> None:
    """SE有効時の起動前検証。欠落があれば不完全な状態で起動させない。"""
    error = _voice_dependency_error()
    if error is not None:
        raise VoiceDependencyError(
            f"{error}。`./scripts/setup_venv.sh` を再実行してください。"
        )
    versions = []
    for package in ("discord.py", "davey", "PyNaCl"):
        try:
            versions.append(f"{package} {metadata.version(package)}")
        except metadata.PackageNotFoundError:
            versions.append(f"{package} unknown")
    log.info("SE音声依存チェック完了: %s / libopus loaded", " / ".join(versions))


def _ensure_voice_ready() -> bool:
    """再生直前の防御的チェック。失敗時はSEだけを安全に無効化する。"""
    try:
        require_voice_ready()
        return True
    except VoiceDependencyError as e:
        log.warning("SE無効: %s", e)
        return False


class SoundPlayer:
    """ギルド単位でSE再生を直列化するプレイヤー (失敗は全て無音スキップ)"""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._pcm_cache: dict[str, bytes] = {}
        self._available: bool | None = None

    def _get_pcm(self, scene: str) -> bytes:
        pcm = self._pcm_cache.get(scene)
        if pcm is None:
            freqs, duration, volume = _SCENES[scene]
            pcm = _render_tone(freqs, duration, volume)
            self._pcm_cache[scene] = pcm
        return pcm

    async def play(self, voice_channel, scene: str) -> bool:
        if voice_channel is None or scene not in _SCENES:
            return False
        # シミュレータ等のフェイクVCは connect を持たない
        if not hasattr(voice_channel, "connect"):
            return False
        if self._available is None:
            self._available = _ensure_voice_ready()
        if not self._available:
            return False

        guild = voice_channel.guild
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=VOICE_QUEUE_MAX_WAIT)
        except asyncio.TimeoutError:
            log.info("SEを破棄しました (待機上限超過): %s", scene)
            return False
        played = False
        try:
            vc_client = None
            try:
                loop = asyncio.get_running_loop()
                if guild.voice_client is not None:
                    vc_client = guild.voice_client
                    if vc_client.channel is None or vc_client.channel.id != voice_channel.id:
                        await asyncio.wait_for(
                            vc_client.move_to(voice_channel), timeout=VOICE_OPERATION_TIMEOUT
                        )
                else:
                    vc_client = await asyncio.wait_for(
                        voice_channel.connect(), timeout=VOICE_OPERATION_TIMEOUT
                    )

                done = asyncio.Event()
                playback_errors: list[Exception] = []

                def _after(err: Exception | None) -> None:
                    # 再生スレッドから呼ばれるためスレッドセーフに通知する
                    if err is not None:
                        playback_errors.append(err)
                    loop.call_soon_threadsafe(done.set)

                vc_client.play(
                    discord.PCMAudio(io.BytesIO(self._get_pcm(scene))), after=_after
                )
                await asyncio.wait_for(done.wait(), timeout=VOICE_OPERATION_TIMEOUT)
                if playback_errors:
                    log.warning(f"SE再生エラー ({scene}): {playback_errors[0]}")
                else:
                    played = True
                    # 正常再生は1ゲームで数十回出るため既定では残さない。
                    # 失敗は上のWARNINGで残るので、切り分けには困らない。
                    log.debug(
                        "SE再生完了 (%s / VC:%s)",
                        scene,
                        getattr(voice_channel, "name", voice_channel.id),
                    )
            except Exception as e:
                log.warning(f"SE再生失敗 ({scene}): {e}")
            finally:
                try:
                    if vc_client is not None and vc_client.is_connected():
                        await asyncio.wait_for(
                            vc_client.disconnect(force=False),
                            timeout=VOICE_OPERATION_TIMEOUT,
                        )
                except Exception as e:
                    log.warning(f"SE用VC切断失敗 ({scene}): {e}")
                    try:
                        if vc_client is not None and vc_client.is_connected():
                            await asyncio.wait_for(
                                vc_client.disconnect(force=True),
                                timeout=VOICE_FORCE_DISCONNECT_TIMEOUT,
                            )
                    except Exception as force_error:
                        log.warning(f"SE用VC強制切断失敗 ({scene}): {force_error}")
        finally:
            lock.release()
        return played
