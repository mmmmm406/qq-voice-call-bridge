"""QQ 原生通话的独立语音对话运行时。

本模块把电话媒体、SenseVoice、全局声纹、OneBot 文本模型和 TTS 组合成一条
电话房间流水线。TTS 播放期间可以继续采集对端插话，但它不发送 QQ 文本或
``record`` 消息；TTS PCM 只写入配置指定的电话输出设备。所有重量级实现都采用
惰性加载，测试和后续替换可以注入假服务。
"""

from __future__ import annotations

import asyncio
import array
import copy
import ctypes
from datetime import datetime
import inspect
import json
import math
import os
import re
import threading
import time
import uuid
import wave
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable

from onebot.logger import logger
from utils.gpt_model.file_lock import cross_process_lock
from utils.voice_input import (
    SenseVoiceTranscription,
    build_speech_perception_prompt,
    parse_sensevoice_transcription,
)

from .runtime import normalize_dialogue_config
from .voiceprint_registry import QQVoiceprintRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RECORDING_ROOT = PROJECT_ROOT / "OneBot" / "data" / "qq_voice_call" / "recordings"
DEFAULT_TRANSCRIPT_ROOT = PROJECT_ROOT / "OneBot" / "data" / "qq_voice_call" / "transcripts"


def _is_transient_replace_error(error: OSError) -> bool:
    """判断是否值得重试 Windows 的文件替换失败。"""

    # Python 在 Windows 上通常映射为 PermissionError；部分包装层只保留
    # ``winerror``，因此同时覆盖共享冲突和文件占用的常见错误码。
    return isinstance(error, PermissionError) or getattr(error, "winerror", None) in {5, 32, 33}


def _atomic_write_json(
    path: Path,
    value: Any,
    *,
    replace_retries: int = 8,
    retry_delay: float = 0.05,
) -> None:
    """完整写入 JSON 后原子替换；短暂占用时退避重试并提供直接写回兜底。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    # 同一进程的重复回调不能共用固定临时名，否则旧句柄/另一个线程会让
    # os.replace 误报 WinError 5；UUID 也能隔离不同进程的残留临时文件。
    temp_path = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    replace_error: OSError | None = None
    retries = max(1, int(replace_retries))
    try:
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())

        for attempt in range(retries):
            try:
                os.replace(temp_path, path)
                return
            except OSError as error:
                if not _is_transient_replace_error(error):
                    raise
                replace_error = error
                if attempt + 1 < retries:
                    # 退避上限 200ms，避免 Defender/编辑器短暂扫描时忙等，
                    # 同时不让一次诊断保存阻塞电话循环太久。
                    delay = min(max(0.0, float(retry_delay)) * (2**attempt), 0.2)
                    time.sleep(delay)

        # 仍然优先保留完整数据。目标文件允许写入时，直接写回可以绕过
        # 某些只拦截 replace 的 Windows 过滤器；若目标仍被占用则交给上层
        # 保存对象保留内存快照，并在后续阶段再次尝试。
        try:
            with path.open("w", encoding="utf-8") as file:
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            return
        except OSError:
            if replace_error is not None:
                raise replace_error
            raise
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _project_path(value: Any, default: Path) -> Path:
    """把配置中的项目相对路径转换为绝对路径。"""

    text = str(value or "").strip()
    if not text:
        return default
    path = Path(text).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


async def _maybe_await(value: Any) -> Any:
    """兼容同步和异步的可注入服务。"""

    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class AudioUtterance:
    """一段已经由电话输入设备切出的 PCM 语音。"""

    pcm: bytes
    sample_rate: int
    channels: int = 1
    sample_width: int = 2
    duration_seconds: float | None = None
    wav_path: str | None = None

    @property
    def duration(self) -> float:
        """返回 PCM 时长；显式时长优先。"""

        if self.duration_seconds is not None:
            return max(0.0, float(self.duration_seconds))
        frame_width = max(1, int(self.channels) * int(self.sample_width))
        return len(self.pcm) / max(1, int(self.sample_rate) * frame_width)


@dataclass(frozen=True)
class PcmChunk:
    """TTS 输出的一块 PCM 数据。"""

    pcm: bytes
    sample_rate: int
    channels: int = 1
    sample_width: int = 2


@dataclass(frozen=True)
class PreparedCallTurn:
    """已经完成 ASR 与声纹观察、尚未进入模型请求的一轮电话输入。"""

    utterance: AudioUtterance
    wav_path: str
    transcription: SenseVoiceTranscription
    speaker: dict[str, Any]
    is_interruption: bool = False
    hard_interrupt: bool = False
    # 保留语音分段完成后的各阶段时间，使电话房间的新循环继续提供第二阶段
    # 已有的首响可观测性；这些时间只用于状态/日志，不写入对话内容。
    segment_ready_at: float = 0.0
    asr_started_at: float = 0.0
    asr_finished_at: float = 0.0
    voiceprint_started_at: float = 0.0
    voiceprint_finished_at: float = 0.0


@dataclass(frozen=True)
class PendingPrivateText:
    """通话期间由同一 QQ 联系人发来的普通文字。"""

    text: str
    caller_uin: str
    caller_name: str
    identity_context: dict[str, Any]
    world_task: Any = None


def _coerce_call_timestamp(value: Any, fallback: float | None = None) -> float:
    """将 NapCat 的秒/毫秒时间戳规范为 Unix 秒，无法解析时使用回退值。"""

    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return float(fallback if fallback is not None else time.time())
    # JavaScript Date.now() 是毫秒；QQ AVSDK 事件可能使用它，也可能使用秒。
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return timestamp if timestamp > 0 else float(fallback if fallback is not None else time.time())


def _format_call_time(value: Any) -> str:
    """返回电话提示词使用的本地时间文本。"""

    return datetime.fromtimestamp(_coerce_call_timestamp(value)).strftime("%Y-%m-%d %H点%M分")


def _format_elapsed(seconds: float | None) -> str:
    """将一段时长转换为电话上下文中的简短中文描述。"""

    try:
        total = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        return "未知"
    if total < 60:
        return f"{total}秒"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    if seconds and minutes < 2:
        return f"{minutes}分钟{seconds}秒"
    return f"{minutes}分钟"


class _TemplateValues(dict):
    """保留用户模板中的未知占位符，避免结束流程因为单个模板拼写中断。"""

    def __missing__(self, key: str) -> str:
        return "{" + str(key) + "}"


def _fade_in_pcm16(
    pcm: bytes,
    *,
    sample_rate: int,
    channels: int,
    fade_in_ms: int,
) -> bytes:
    """对一轮电话 TTS 的首块 PCM16 做短线性淡入。

    参数:
        pcm: 小端有符号 PCM16 字节。
        sample_rate: 每秒采样帧数。
        channels: 每帧声道数。
        fade_in_ms: 淡入时长；0 表示关闭。

    返回:
        长度不变的 PCM；参数无效或没有完整采样帧时原样返回。
    """

    rate = int(sample_rate or 0)
    channel_count = int(channels or 0)
    fade_ms = max(0, int(fade_in_ms or 0))
    if not pcm or rate <= 0 or channel_count <= 0 or fade_ms <= 0:
        return pcm
    frame_width = 2 * channel_count
    usable_length = len(pcm) - (len(pcm) % frame_width)
    if usable_length <= 0:
        return pcm

    samples = array.array("h")
    samples.frombytes(pcm[:usable_length])
    frame_count = len(samples) // channel_count
    fade_frames = min(frame_count, max(1, round(rate * fade_ms / 1000.0)))
    for frame in range(fade_frames):
        gain = 0.0 if fade_frames <= 1 else frame / float(fade_frames - 1)
        offset = frame * channel_count
        for index in range(offset, offset + channel_count):
            samples[index] = int(round(samples[index] * gain))
    return samples.tobytes() + pcm[usable_length:]


class _VoicemeeterRemote:
    """Voicemeeter Remote DLL 的最小标准库封装。"""

    def __init__(self, dll: Any) -> None:
        self._dll = dll
        dll.VBVMR_Login.restype = ctypes.c_long
        dll.VBVMR_Logout.restype = ctypes.c_long
        dll.VBVMR_GetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_float)]
        dll.VBVMR_GetParameterFloat.restype = ctypes.c_long
        dll.VBVMR_SetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.c_float]
        dll.VBVMR_SetParameterFloat.restype = ctypes.c_long

    @classmethod
    def load(cls) -> "_VoicemeeterRemote":
        """从 Voicemeeter 官方安装目录加载与当前 Python 位数匹配的 DLL。"""

        if os.name != "nt":
            raise RuntimeError("Voicemeeter 路由隔离只支持 Windows")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise RuntimeError("当前 Python 不支持 WinDLL")
        dll_names = (
            ("VoicemeeterRemote64.dll", "VoicemeeterRemote.dll")
            if ctypes.sizeof(ctypes.c_void_p) == 8
            else ("VoicemeeterRemote.dll", "VoicemeeterRemote64.dll")
        )
        roots = [
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramFiles"),
            r"C:\Program Files (x86)",
            r"C:\Program Files",
        ]
        candidates: list[Path] = []
        for root in roots:
            if not root:
                continue
            for dll_name in dll_names:
                path = Path(root) / "VB" / "Voicemeeter" / dll_name
                if path not in candidates:
                    candidates.append(path)
        for path in candidates:
            if path.is_file():
                return cls(loader(str(path)))
        raise FileNotFoundError("未找到 VoicemeeterRemote DLL")

    def login(self) -> None:
        """连接正在运行的 Voicemeeter；负数返回码表示失败。"""

        code = int(self._dll.VBVMR_Login())
        if code < 0:
            raise RuntimeError(f"VBVMR_Login 返回 {code}")

    def get(self, name: str) -> float:
        """读取一个浮点路由参数。"""

        value = ctypes.c_float()
        code = int(self._dll.VBVMR_GetParameterFloat(name.encode("ascii"), ctypes.byref(value)))
        if code != 0:
            raise RuntimeError(f"读取 {name} 失败，返回码 {code}")
        return float(value.value)

    def set(self, name: str, value: float) -> None:
        """设置一个浮点路由参数。"""

        code = int(self._dll.VBVMR_SetParameterFloat(name.encode("ascii"), float(value)))
        if code != 0:
            raise RuntimeError(f"设置 {name} 失败，返回码 {code}")

    def logout(self) -> None:
        """释放 Remote API 登录。"""

        self._dll.VBVMR_Logout()


class VoicemeeterCallRoute:
    """通话期间临时隔离 TTS 虚拟输入条带，并在结束后恢复原值。"""

    _ROUTE_VALUES = {"A1": 0.0, "A2": 0.0, "A3": 0.0, "B1": 1.0, "B2": 0.0}

    def __init__(
        self,
        config: dict[str, Any],
        *,
        device_name_resolver: Callable[[int | None], str] | None = None,
        remote_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self._device_name_resolver = device_name_resolver
        self._remote_factory = remote_factory or _VoicemeeterRemote.load
        self._remote: Any = None
        self._snapshot: dict[str, float] | None = None
        self._strip_index: int | None = None
        self._owner_id: str | None = None
        self._lock = threading.RLock()

    @property
    def active(self) -> bool:
        """返回当前是否持有一份待恢复的 Voicemeeter 路由快照。"""

        with self._lock:
            return self._snapshot is not None

    def _resolve_strip(self) -> tuple[int, str] | None:
        """仅把明确的 Voicemeeter 主虚拟输出映射到 Banana Strip[3]。"""

        audio_cfg = self.config.get("audio") or {}
        if not bool(audio_cfg.get("isolate_voicemeeter_route", True)):
            return None
        resolver = self._device_name_resolver
        if not callable(resolver):
            return None
        device_index = audio_cfg.get("output_device_index")
        name = str(resolver(device_index) or "").strip()
        lowered = name.casefold()
        if "voicemeeter input" in lowered and "aux" not in lowered and "vaio3" not in lowered:
            return 3, name
        logger.warning(
            "[qq_voice_call] 电话 TTS 输出不是受支持的 Voicemeeter Input，"
            f"跳过自动路由隔离: device={device_index} name={name or 'unknown'}"
        )
        return None

    def activate(self, owner_id: str) -> bool:
        """保存当前条带路由并切换为只送 B1；重复调用保持幂等。"""

        owner_id = str(owner_id or "").strip()
        if not owner_id:
            return False
        with self._lock:
            if self._snapshot is not None and self._owner_id == owner_id:
                return True
            if self._snapshot is not None:
                self._restore_locked()
            remote = None
            snapshot: dict[str, float] = {}
            strip_index = None
            try:
                resolved = self._resolve_strip()
                if resolved is None:
                    return False
                strip_index, device_name = resolved
                remote = self._remote_factory()
                remote.login()
                for route in self._ROUTE_VALUES:
                    snapshot[route] = remote.get(f"Strip[{strip_index}].{route}")
                for route, value in self._ROUTE_VALUES.items():
                    remote.set(f"Strip[{strip_index}].{route}", value)
                self._remote = remote
                self._snapshot = snapshot
                self._strip_index = strip_index
                self._owner_id = owner_id
                remote = None  # 句柄所有权已经转交给当前路由快照。
                logger.info(
                    "[qq_voice_call] 已隔离电话 TTS 的 Voicemeeter 路由: "
                    f"device={device_name} strip={strip_index} A1/A2/A3=0 B1=1 B2=0"
                )
                return True
            except Exception as error:  # noqa: BLE001
                if remote is not None:
                    if strip_index is not None:
                        for route, value in snapshot.items():
                            try:
                                remote.set(f"Strip[{strip_index}].{route}", value)
                            except Exception:
                                pass
                    try:
                        remote.logout()
                    except Exception:
                        pass
                logger.warning(f"[qq_voice_call] Voicemeeter 电话路由隔离失败，继续接听：{error}")
                return False

    def _restore_locked(self) -> bool:
        """在持锁状态下恢复快照，失败也释放 Remote API 句柄。"""

        remote = self._remote
        snapshot = self._snapshot
        strip_index = self._strip_index
        if remote is None or snapshot is None or strip_index is None:
            self._remote = None
            self._snapshot = None
            self._strip_index = None
            self._owner_id = None
            return False
        restored = True
        try:
            for route, value in snapshot.items():
                try:
                    remote.set(f"Strip[{strip_index}].{route}", value)
                except Exception as error:  # noqa: BLE001
                    restored = False
                    logger.warning(
                        f"[qq_voice_call] 恢复 Voicemeeter 路由参数 {route} 失败：{error}"
                    )
        finally:
            try:
                remote.logout()
            except Exception:
                pass
            self._remote = None
            self._snapshot = None
            self._strip_index = None
            self._owner_id = None
        if restored:
            logger.info("[qq_voice_call] 已恢复通话前的 Voicemeeter 路由")
        return restored

    def restore(self, owner_id: str | None = None) -> bool:
        """恢复指定通话持有的路由；旧通话不能覆盖新通话的快照。"""

        with self._lock:
            if owner_id is not None and self._owner_id not in {None, str(owner_id)}:
                return False
            return self._restore_locked()


class PyAudioMedia:
    """通过本机虚拟音频设备完成电话输入、输出和可选监听。

    输入使用一个简单能量 VAD 切段；TTS 播放时上层不会调用
    ``capture_utterance``，因此第一版天然是半双工。监听设备故障只会禁用监听，
    不会抛出到电话对话主循环。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._pyaudio = None
        self._audio = None
        self._output_stream = None
        self._monitor_stream = None
        self._output_format: tuple[int, int] | None = None
        self._monitor_format: tuple[int, int] | None = None
        self._input_log_key: tuple[int | None, int, int, int] | None = None
        self._input_signal_verified = False
        self._lock = threading.RLock()
        self.monitor_failed = False

    def _ensure_audio(self) -> Any:
        """惰性导入并创建 PyAudio 宿主；没有设备时由调用方处理异常。"""

        if self._audio is not None:
            return self._audio
        import pyaudio

        self._pyaudio = pyaudio
        self._audio = pyaudio.PyAudio()
        return self._audio

    def get_output_device_name(self, device_index: int | None) -> str:
        """返回电话输出设备名，供安全映射 Voicemeeter 虚拟输入条带。"""

        with self._lock:
            audio = self._ensure_audio()
            info = (
                audio.get_device_info_by_index(device_index)
                if device_index is not None
                else audio.get_default_output_device_info()
            )
            return str(info.get("name") or "")

    @staticmethod
    def _energy(pcm: bytes) -> float:
        """计算 int16 PCM 的 RMS 能量。"""

        if not pcm:
            return 0.0
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if not samples:
            return 0.0
        return math.sqrt(sum(float(value) * value for value in samples) / len(samples))

    def probe_input_level(self, duration_seconds: float = 1.0) -> dict[str, Any]:
        """短暂读取电话输入设备的电平，不保存、转发或处理音频。

        参数 ``duration_seconds`` 限制在 0.1 到 5 秒，返回实际打开的设备、
        采样格式、RMS、峰值和是否读到非静音 PCM。WebUI 用它确认虚拟音频
        路由，不会触发 ASR、声纹、模型或 TTS。
        """

        audio_cfg = self.config.get("audio") or {}
        vad_cfg = self.config.get("vad") or {}
        rate = max(1, int(audio_cfg.get("rate", 16000)))
        channels = max(1, int(audio_cfg.get("channels", 1)))
        chunk_size = max(256, int(vad_cfg.get("chunk_size", 1024)))
        input_index = audio_cfg.get("input_device_index")
        requested_duration = max(0.1, min(5.0, float(duration_seconds)))
        chunk_count = max(1, math.ceil(rate * requested_duration / chunk_size))
        stream = None
        audio = self._ensure_audio()
        total_squares = 0.0
        sample_count = 0
        peak = 0
        try:
            stream = audio.open(
                format=self._pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=input_index,
                frames_per_buffer=chunk_size,
            )
            try:
                device_info = (
                    audio.get_device_info_by_index(input_index)
                    if input_index is not None
                    else audio.get_default_input_device_info()
                )
                device_name = str(device_info.get("name") or "未知输入设备")
            except Exception:
                device_name = "未知输入设备"

            for _ in range(chunk_count):
                pcm = stream.read(chunk_size, exception_on_overflow=False)
                samples = array.array("h")
                samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
                if not samples:
                    continue
                total_squares += sum(float(value) * value for value in samples)
                sample_count += len(samples)
                peak = max(peak, max(abs(int(value)) for value in samples))

            rms = math.sqrt(total_squares / sample_count) if sample_count else 0.0
            result = {
                "device_index": input_index,
                "device_name": device_name,
                "rate": rate,
                "channels": channels,
                "duration_seconds": sample_count / max(1, rate * channels),
                "rms": rms,
                "peak": peak,
                "has_signal": peak > 0,
            }
            logger.info(
                "[qq_voice_call] 电话输入电平检测: "
                f"device={input_index if input_index is not None else 'default'} "
                f"name={device_name} rms={rms:.0f} peak={peak}"
            )
            return result
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    def capture_utterance(
        self,
        stop_event: threading.Event,
        on_remote_audio: Callable[[bytes, int, int], None] | None = None,
    ) -> AudioUtterance | None:
        """阻塞采集一段语音，供对话任务通过 ``asyncio.to_thread`` 调用。"""

        audio_cfg = self.config.get("audio") or {}
        vad_cfg = self.config.get("vad") or {}
        rate = int(audio_cfg.get("rate", 16000))
        channels = int(audio_cfg.get("channels", 1))
        chunk_size = max(256, int(vad_cfg.get("chunk_size", 1024)))
        vad_enabled = bool(vad_cfg.get("enable", True))
        sensitivity = str(vad_cfg.get("sensitivity") or "high").lower()
        threshold = {"high": 280.0, "medium": 500.0, "low": 900.0}.get(sensitivity, 280.0)
        if not vad_enabled:
            # 关闭 VAD 时从接通后的第一帧开始收集，直到达到最大时长；
            # 仍保留 stop_event，结束电话时不会永久卡在录音线程。
            threshold = 0.0
        max_silence = max(0.1, float(vad_cfg.get("max_end_silence", 1500)) / 1000.0)
        min_duration = max(0.0, float(vad_cfg.get("min_speech_duration", 200)) / 1000.0)
        max_duration = max(2.0, float(vad_cfg.get("max_speech_duration", 60.0)))
        input_index = audio_cfg.get("input_device_index")
        stream = None
        frames: list[bytes] = []
        speech_started = False
        speech_started_at = 0.0
        last_voice_at = 0.0
        listen_started_at = time.monotonic()
        next_silence_report_at = listen_started_at + 15.0
        max_observed_energy = 0.0
        audio = self._ensure_audio()
        try:
            stream = audio.open(
                format=self._pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=input_index,
                frames_per_buffer=chunk_size,
            )
            try:
                device_info = (
                    audio.get_device_info_by_index(input_index)
                    if input_index is not None
                    else audio.get_default_input_device_info()
                )
                device_name = str(device_info.get("name") or "未知输入设备")
            except Exception:
                device_name = "未知输入设备"
            input_log_key = (input_index, rate, channels, chunk_size)
            if self._input_log_key != input_log_key:
                logger.info(
                    "[qq_voice_call] 电话音频输入已打开: "
                    f"device={input_index if input_index is not None else 'default'} "
                    f"name={device_name} rate={rate} channels={channels} "
                    f"vad_threshold={threshold:.0f}"
                )
                self._input_log_key = input_log_key
            while not stop_event.is_set():
                pcm = stream.read(chunk_size, exception_on_overflow=False)
                if on_remote_audio is not None:
                    on_remote_audio(pcm, rate, channels)
                energy = self._energy(pcm)
                max_observed_energy = max(max_observed_energy, energy)
                now = time.monotonic()
                if not speech_started:
                    if energy >= threshold:
                        speech_started = True
                        self._input_signal_verified = True
                        speech_started_at = now
                        last_voice_at = now
                        frames.append(pcm)
                        logger.info(
                            "[qq_voice_call] 检测到电话语音: "
                            f"energy={energy:.0f} threshold={threshold:.0f}"
                        )
                    elif not self._input_signal_verified and now >= next_silence_report_at:
                        # 尚未有人开口无法判断是正常沉默还是路由故障；只给一次
                        # 中性提示。成功收音后不再把电话里的自然停顿当成告警。
                        logger.info(
                            "[qq_voice_call] 电话仍在等待对端开口: "
                            f"device={input_index if input_index is not None else 'default'} "
                            f"max_energy={max_observed_energy:.0f} threshold={threshold:.0f}；"
                            "若对端已经说话，请在 QQ 语音通话页检测输入电平"
                        )
                        next_silence_report_at = float("inf")
                    continue

                frames.append(pcm)
                if energy >= threshold:
                    last_voice_at = now
                elapsed = now - speech_started_at
                if elapsed >= max_duration or (
                    now - last_voice_at >= max_silence and elapsed >= min_duration
                ):
                    break
            if stop_event.is_set() or not frames:
                return None
            pcm = b"".join(frames)
            duration = len(pcm) / max(1, rate * channels * 2)
            if duration < min_duration:
                return None
            logger.info(f"[qq_voice_call] 电话语音分段完成: duration={duration:.2f}s")
            return AudioUtterance(pcm, rate, channels, 2, duration)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    def _open_output(self, *, monitor: bool, sample_rate: int, channels: int) -> Any:
        """打开或复用一条指定格式的输出流。"""

        audio_cfg = self.config.get("audio") or {}
        monitor_cfg = self.config.get("monitor") or {}
        device_index = (
            monitor_cfg.get("output_device_index")
            if monitor
            else audio_cfg.get("output_device_index")
        )
        with self._lock:
            attr = "_monitor_stream" if monitor else "_output_stream"
            current = getattr(self, attr)
            format_attr = "_monitor_format" if monitor else "_output_format"
            current_format = getattr(self, format_attr)
            requested_format = (max(1, sample_rate), max(1, channels))
            if current is not None and current_format == requested_format:
                return current
            if current is not None:
                try:
                    current.stop_stream()
                    current.close()
                except Exception:
                    pass
                setattr(self, attr, None)
            stream = self._ensure_audio().open(
                format=self._pyaudio.paInt16,
                channels=requested_format[1],
                rate=requested_format[0],
                output=True,
                output_device_index=device_index,
                frames_per_buffer=1024,
            )
            setattr(self, attr, stream)
            setattr(self, format_attr, requested_format)
            return stream

    def write_output(self, pcm: bytes, sample_rate: int, channels: int) -> None:
        """把 TTS PCM 写入 QQ 通话的虚拟输出设备。"""

        if pcm:
            self._open_output(monitor=False, sample_rate=sample_rate, channels=channels).write(pcm)

    def write_monitor(self, pcm: bytes, sample_rate: int, channels: int, volume: float = 1.0) -> None:
        """把对端或 TTS 的副本写入本地监听设备；失败后自动停用监听。"""

        if self.monitor_failed or not pcm:
            return
        try:
            if volume != 1.0:
                values = array.array("h")
                values.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
                factor = max(0.0, min(1.0, float(volume)))
                for index, value in enumerate(values):
                    values[index] = max(-32768, min(32767, int(value * factor)))
                pcm = values.tobytes()
            self._open_output(monitor=True, sample_rate=sample_rate, channels=channels).write(pcm)
        except Exception as error:  # noqa: BLE001
            self.monitor_failed = True
            logger.warning(f"[qq_voice_call] 本地监听设备不可用，已停用监听：{error}")

    def close(self) -> None:
        """关闭输出流和 PyAudio 宿主。"""

        with self._lock:
            for attr in ("_output_stream", "_monitor_stream"):
                stream = getattr(self, attr)
                if stream is None:
                    continue
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
                setattr(self, attr, None)
                setattr(self, "_monitor_format" if attr == "_monitor_stream" else "_output_format", None)
            if self._audio is not None:
                try:
                    self._audio.terminate()
                except Exception:
                    pass
                self._audio = None
            self._input_log_key = None
            self._input_signal_verified = False


class SenseVoiceASR:
    """QQ 电话专用 SenseVoice 适配器；优先在振铃准备期加载模型。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._model = None
        self._kwargs: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _ensure_model(self) -> None:
        """核对模型目录后惰性加载，不下载或补齐缺失模型。"""

        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            cfg = self.config.get("sensevoice") or {}
            asr_path = _project_path(cfg.get("asr_model_path"), PROJECT_ROOT / "models" / "iic" / "SenseVoiceSmall")
            if not asr_path.is_dir():
                raise FileNotFoundError(f"SenseVoice ASR 模型目录不存在：{asr_path}")
            vad_path = _project_path(
                cfg.get("vad_model_path"),
                PROJECT_ROOT / "models" / "iic" / "speech_fsmn_vad_zh-cn-16k-common-pytorch",
            )
            import torch

            # rotary_embedding_torch 旧版装饰器会在导入时输出与本次推理无关的
            # PyTorch 弃用提示；只在 FunASR 导入/构造范围内定向屏蔽该警告。
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    category=FutureWarning,
                    module=r"rotary_embedding_torch(?:\..*)?",
                )
                from funasr import AutoModel

            device = str(cfg.get("device") or "cuda:0")
            if device.startswith("cuda") and not torch.cuda.is_available():
                device = "cpu"
            kwargs: dict[str, Any] = {
                "model": str(asr_path),
                # SenseVoice 已由当前 FunASR 注册；本地目录没有 model.py，
                # 开启 remote code 只会触发失败回退和误导性控制台输出。
                "trust_remote_code": False,
                "device": device,
                "disable_update": True,
                "disable_pbar": True,
            }
            # 保留 SenseVoice 自身的 FSMN VAD：虽然输入侧已经完成粗切段，
            # 这里仍负责模型侧的空片段过滤，不能为几十毫秒收益改变识别边界。
            if vad_path.is_dir() and bool((self.config.get("vad") or {}).get("enable", True)):
                kwargs["vad_model"] = str(vad_path)
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    category=FutureWarning,
                    module=r"rotary_embedding_torch(?:\..*)?",
                )
                self._model = AutoModel(**kwargs)
            self._kwargs = {
                "language": cfg.get("language") or "auto",
                "use_itn": str(cfg.get("text_norm") or "withitn").lower() == "withitn",
                "merge_vad": bool(kwargs.get("vad_model")),
                "merge_length_s": 15,
                "enable_rich_transcription": True,
                "ban_emo_unk": False,
            }

    def warm_up(self) -> None:
        """在待机期加载模型，避免第一句电话语音触发冷启动。"""

        self._ensure_model()

    def transcribe(self, wav_path: str) -> SenseVoiceTranscription:
        """识别一条 WAV 并解析 SenseVoice 富文本标签。"""

        self._ensure_model()
        result = self._model.generate(input=wav_path, **self._kwargs)
        if isinstance(result, list):
            result = result[0] if result else {}
        raw = result.get("text", "") if isinstance(result, dict) else str(result or "")
        return parse_sensevoice_transcription(raw)


class LocalTTSService:
    """复用根项目 ``MY_TTS`` 的电话专用适配器。

    ``synthesize`` 返回 WAV 路径或 CosyVoice3 的 PCM 流对象；输出设备和播放
    生命周期仍由 ``QQVoiceDialogueRuntime`` 独立管理，不进入普通播放器队列。
    """

    def __init__(self, config: dict[str, Any], root_config_path: str | os.PathLike[str] | None = None) -> None:
        self.config = config
        self.root_config_path = Path(root_config_path or PROJECT_ROOT / "config.json")
        self._tts = None
        self._audio_helper = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        """惰性加载底层 TTS 适配器。"""

        if self._tts is not None:
            return
        with self._lock:
            if self._tts is not None:
                return
            from utils.audio_handle.my_tts import MY_TTS

            self._tts = MY_TTS(str(self.root_config_path))

    def _root_config(self) -> Any:
        """返回根配置读取器，保持与主项目相同的配置解析方式。"""

        from utils.config import Config

        return Config(str(self.root_config_path))

    async def warm_up(self) -> None:
        """预热当前电话 TTS；生成的测试 PCM 不会写入任何声卡。"""

        self._ensure()
        root = self._root_config()
        tts_cfg = self.config.get("tts") or {}
        profile = str(tts_cfg.get("profile") or "inherit_main").strip()
        engine = str(root.get("audio_synthesis_type") or "gpt_sovits") if profile == "inherit_main" else profile
        engine = engine.strip().lower()
        if engine == "cosyvoice3":
            data = copy.deepcopy(root.get("cosyvoice3") or {})
            if not bool(tts_cfg.get("streaming", True)) or not data.get("streaming_enable"):
                return
            data["content"] = "嗯。"
            source = None
            try:
                source = await self._tts.cosyvoice3_stream_api(data)
                if source is None:
                    raise RuntimeError("CosyVoice3 流式预热没有返回 PCM")
            finally:
                if source is not None:
                    cancel = getattr(source, "cancel", None)
                    if callable(cancel):
                        cancel("qq_call_warmup_complete")
            return
        if engine == "omnivoice":
            warmup = getattr(self._tts, "warmup_omnivoice", None)
            if callable(warmup):
                if await _maybe_await(warmup()) is False:
                    raise RuntimeError("OmniVoice 预热未完成")

    async def synthesize(self, text: str, *, streaming: bool = True) -> Any:
        """合成一条电话回复，返回流对象或 WAV 文件路径。"""

        self._ensure()
        root = self._root_config()
        tts_cfg = self.config.get("tts") or {}
        profile = str(tts_cfg.get("profile") or "inherit_main").strip()
        engine = str(root.get("audio_synthesis_type") or "gpt_sovits") if profile == "inherit_main" else profile
        engine = engine.strip().lower()
        if engine == "cosyvoice3":
            data = copy.deepcopy(root.get("cosyvoice3") or {})
            data["content"] = text
            if streaming and bool(tts_cfg.get("streaming", True)) and data.get("streaming_enable"):
                source = await self._tts.cosyvoice3_stream_api(data)
                if source is not None:
                    return source
            return await self._tts.cosyvoice3_api(data)
        if engine == "omnivoice":
            data = copy.deepcopy(root.get("omnivoice") or {})
            data["content"] = text
            return await self._tts.omnivoice_api(data)
        if engine == "none":
            return None
        data = copy.deepcopy(root.get("gpt_sovits") or {})
        data["content"] = text
        return await self._tts.gpt_sovits_api(data)


class QQVoiceprintService:
    """把全局声纹登记适配层接到电话运行时。"""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config.get("voiceprint") or {}
        path = _project_path(cfg.get("enroll_path"), PROJECT_ROOT / "OneBot" / "data" / "qq_voice_call" / "voiceprints")
        self.config = cfg
        self.registry = QQVoiceprintRegistry(path, other_name_template=str(cfg.get("default_name_prefix") or "旁边的人"))
        self._pipeline = None
        self._pipeline_error: str | None = None
        self._lock = threading.Lock()

    def _ensure_pipeline(self) -> Any:
        """核对 CAM++ 权重路径后惰性加载；缺失时返回 None。"""

        if self._pipeline is not None or self._pipeline_error is not None:
            return self._pipeline
        with self._lock:
            if self._pipeline is not None or self._pipeline_error is not None:
                return self._pipeline
            model_path = _project_path(self.config.get("model_path"), PROJECT_ROOT / "CAM++" / "campplus_cn_common.bin")
            if not model_path.is_file():
                self._pipeline_error = f"CAM++ 模型文件不存在：{model_path}"
                logger.warning(f"[qq_voice_call] {self._pipeline_error}")
                return None
            try:
                from utils.sv_3dspeaker import SV_3DSpeaker

                self._pipeline = SV_3DSpeaker(str(model_path))
            except Exception as error:  # noqa: BLE001
                self._pipeline_error = str(error)
                logger.warning(f"[qq_voice_call] CAM++ 加载失败，暂不做声纹匹配：{error}")
            return self._pipeline

    def warm_up(self) -> None:
        """加载 CAM++ 声纹模型；不登记或修改任何说话人。"""

        if bool(self.config.get("enable", True)):
            self._ensure_pipeline()

    def resolve(
        self,
        wav_path: str,
        *,
        caller_uin: str,
        caller_name: str,
        duration: float,
        first_utterance: bool,
        is_active: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """匹配全局声纹；未知声音仅在允许时按联系人语境登记。"""

        def cancelled() -> dict[str, Any]:
            return {
                "speaker_id": None,
                "name": caller_name or "电话参与者",
                "role": "participant",
                "matched": False,
                "enrolled": False,
                "reason": "call_ended",
            }

        if is_active is not None and not is_active():
            return cancelled()
        if not bool(self.config.get("enable", True)):
            return {"speaker_id": None, "name": caller_name or "电话参与者", "role": "participant", "matched": False, "enrolled": False, "reason": "disabled"}
        pipeline = self._ensure_pipeline()
        if is_active is not None and not is_active():
            return cancelled()
        existing = self.registry.get_all_speakers()
        # 没有 CAM++ 时允许首次建立主声纹，但不把每个后续片段都登记成新的人。
        auto_enroll = bool(self.config.get("auto_enroll_unknown", True)) and (
            pipeline is not None or not existing
        )
        if is_active is not None and not is_active():
            return cancelled()
        return self.registry.resolve_utterance(
            wav_path,
            caller_uin=caller_uin,
            caller_name=caller_name,
            threshold=float(self.config.get("threshold", 0.35)),
            sv_pipeline=pipeline,
            duration_seconds=duration,
            auto_enroll_unknown=auto_enroll,
            min_enroll_duration=float(self.config.get("minimum_enroll_duration", 3.0)),
            first_utterance=first_utterance,
            is_active=is_active,
        )


class QQVoiceCallTranscript:
    """电话独立上下文和可审计文字记录。"""

    def __init__(self, call_id: str, caller_uin: str, caller_name: str, enabled: bool = True) -> None:
        self.call_id = call_id
        self.caller_uin = caller_uin
        self.caller_name = caller_name
        self.enabled = enabled
        self.path = DEFAULT_TRANSCRIPT_ROOT / f"{call_id}.json"
        # 文字记录可能同时由状态回调、语音循环和结束流程触发保存。
        # RLock 只保护当前 transcript，不改变全局跨进程锁的职责。
        self._save_lock = threading.RLock()
        self._pending_payload: dict[str, Any] | None = None
        self.entries: list[dict[str, Any]] = []
        self.recent_reference: list[dict[str, Any]] = []
        self.metadata: dict[str, Any] = {}
        self._last_save_error: str | None = None

    def add(self, kind: str, text: str, **metadata: Any) -> None:
        """追加一条电话上下文记录。"""

        text = str(text or "").strip()
        if not text:
            return
        with self._save_lock:
            self.entries.append({"at": time.time(), "kind": kind, "text": text, **metadata})

    def update_last(self, kind: str, **metadata: Any) -> bool:
        """更新最后一条指定类型记录，供播放中断等后续状态补充使用。"""

        with self._save_lock:
            for entry in reversed(self.entries):
                if entry.get("kind") != kind:
                    continue
                entry.update(metadata)
                return True
        return False

    def set_metadata(self, **metadata: Any) -> None:
        """写入本通电话的稳定房间信息。"""

        with self._save_lock:
            self.metadata.update(copy.deepcopy(metadata))

    def save(self, phase: str | None = None) -> bool:
        """保存电话上下文；文件被外部占用时不影响通话状态机。"""

        if not self.enabled:
            return True
        with self._save_lock:
            try:
                payload = {
                    "schema_version": 2,
                    "call_id": self.call_id,
                    "caller_uin": self.caller_uin,
                    "caller_name": self.caller_name,
                    "phase": phase,
                    "recent_reference": copy.deepcopy(self.recent_reference),
                    "metadata": copy.deepcopy(self.metadata),
                    "entries": copy.deepcopy(self.entries),
                    "updated_at": time.time(),
                }
                # 失败时保留最近一次完整快照；下一次 save（包括结束阶段）会继续
                # 尝试写入，且不会因为诊断文件被占用改变电话状态机。
                self._pending_payload = copy.deepcopy(payload)
                with cross_process_lock(
                    str(self.path.parent),
                    f".qq_voice_transcript_{self.call_id}.lock",
                ):
                    _atomic_write_json(self.path, payload)
            except Exception as error:  # noqa: BLE001
                # transcript 是诊断/排查数据，任何写盘异常都不能升级为电话错误；
                # 这里也覆盖 JSON 序列化或第三方锁实现抛出的非 OSError 异常。
                message = str(error)
                if message != self._last_save_error:
                    logger.warning(
                        f"[qq_voice_call] 电话文字记录暂未保存，将在后续阶段重试：{message}"
                    )
                self._last_save_error = message
                return False
            self._pending_payload = None
            self._last_save_error = None
            return True


class QQVoiceCallHistory:
    """按 QQ 联系人保存上一通电话的最小连续性信息。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (DEFAULT_TRANSCRIPT_ROOT.parent / "call_history.json")

    def get_previous(self, caller_uin: str) -> dict[str, Any] | None:
        """读取当前 QQ 联系人的上一通电话；不存在时返回 ``None``。"""

        caller = str(caller_uin or "").strip()
        if not caller:
            return None
        with cross_process_lock(str(self.path.parent), ".qq_voice_call_history.lock"):
            try:
                with self.path.open("r", encoding="utf-8") as file:
                    payload = json.load(file)
            except FileNotFoundError:
                return None
            except (OSError, json.JSONDecodeError):
                return None
        contacts = payload.get("contacts") if isinstance(payload, dict) else {}
        value = contacts.get(caller) if isinstance(contacts, dict) else None
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def record_end(
        self,
        *,
        caller_uin: str,
        caller_name: str,
        call_id: str,
        started_at: float,
        ended_at: float,
        ended_by: str,
    ) -> None:
        """原子更新联系人最后一次通话；未知挂断方必须显式保留。"""

        caller = str(caller_uin or "").strip()
        if not caller:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "call_id": str(call_id or ""),
            "caller_name": str(caller_name or caller),
            "started_at": float(started_at or ended_at),
            "ended_at": float(ended_at),
            "duration_seconds": max(0, round(float(ended_at) - float(started_at or ended_at), 3)),
            "ended_by": str(ended_by or "unknown/system"),
        }
        with cross_process_lock(str(self.path.parent), ".qq_voice_call_history.lock"):
            payload: dict[str, Any]
            try:
                with self.path.open("r", encoding="utf-8") as file:
                    loaded = json.load(file)
                payload = loaded if isinstance(loaded, dict) else {}
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                payload = {}
            contacts = payload.setdefault("contacts", {})
            if not isinstance(contacts, dict):
                contacts = {}
                payload["contacts"] = contacts
            contacts[caller] = record
            payload["schema_version"] = 1
            payload["updated_at"] = time.time()
            _atomic_write_json(self.path, payload)


class QQVoiceDialogueRuntime:
    """单通 QQ 电话房间的媒体、模型和上下文编排器。"""

    # 振铃阶段是严格的二选一动作。这里集中声明名称，避免提示词、工具
    # 白名单检查和后续回归测试各自维护一套容易漂移的列表。
    ADMISSION_TOOL_NAMES = (
        "qq_voice_call_accept",
        "qq_voice_call_reject",
    )
    # NapCat 的结束快照在部分版本中会把身份字段作为空字符串上报。
    # 这些字段一旦覆盖已确认的来电者，结束摘要就无法可靠归因。
    _CALL_IDENTITY_FIELDS = ("caller_uin", "caller_uid", "caller_name", "bot_self_id")

    def __init__(
        self,
        config: dict[str, Any],
        *,
        ai_service: Any = None,
        asr: Any = None,
        tts: Any = None,
        media: Any = None,
        voiceprint: Any = None,
        call_control: Callable[[str], Any] | None = None,
        route_guard: Any = None,
        call_history: QQVoiceCallHistory | None = None,
    ) -> None:
        self.config = normalize_dialogue_config(config)
        self.ai_service = ai_service
        self.asr = asr or SenseVoiceASR(self.config)
        self.tts = tts or LocalTTSService(self.config)
        self.media = media or PyAudioMedia(self.config)
        self.call_control = call_control
        self.route_guard = route_guard or VoicemeeterCallRoute(
            self.config,
            device_name_resolver=getattr(self.media, "get_output_device_name", None),
        )
        # 对话关闭时不创建声纹目录或写入全局登记 schema；真正接通电话后
        # 仍可通过构造参数注入测试替身或自定义声纹服务。
        self.voiceprint = voiceprint
        if self.voiceprint is None and self.config.get("enabled"):
            self.voiceprint = QQVoiceprintService(self.config)
        self.call_history = call_history or QQVoiceCallHistory()
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread_stop_event = threading.Event()
        self._capture_stop_event: threading.Event | None = None
        self._generation = 0
        self._call: dict[str, Any] = {}
        self._transcript: QQVoiceCallTranscript | None = None
        self._listeners: list[Callable[[dict[str, Any]], Any]] = []
        self._active_tts_source = None
        self._preparation_task: asyncio.Task | None = None
        self._preparation_call_id: str | None = None
        self._prepared_call_id: str | None = None
        self._accept_requested_call_id: str | None = None
        self._prepared_opening: dict[str, Any] | None = None
        self._admission_task: asyncio.Task | None = None
        self._admission_call_id: str | None = None
        self._admission_decision: str | None = None
        self._admission_decision_event: asyncio.Event | None = None
        # 已决定拒接或准备失败的 call_id 在远端明确 ended 前不能重新进入媒体链。
        # 这能挡住 QQ/AVSDK 迟到上报的 connected 状态。
        self._declined_call_ids: set[str] = set()
        self._summary_task: asyncio.Task | None = None
        self._room_call_id: str | None = None
        self._room_state = "idle"
        self._room_started_at: float | None = None
        self._connected_at: float | None = None
        self._last_reply_at: float | None = None
        self._turn_index = 0
        self._active_reply_text = ""
        self._active_reply_scope = ""
        # 模型工具在生成阶段执行，而电话挂断必须等同一回复的 PCM 播完。
        # 令牌把工具意图绑定到一轮回复，避免下一轮回复误触发迟到挂断。
        self._active_reply_request: dict[str, Any] | None = None
        self._pending_hangup: dict[str, Any] | None = None
        self._hangup_control_token: str | None = None
        # 仅表示当前正在等待桥响应的控制请求；收到失败结果后必须清掉，
        # 不能把“请求中”误当成已经提交成功。
        self._hangup_control_inflight_token: str | None = None
        self._interrupt_event: asyncio.Event | None = None
        self._private_text_queue: asyncio.Queue[PendingPrivateText] = asyncio.Queue()
        self._status: dict[str, Any] = {
            "enabled": bool(self.config.get("enabled")),
            "phase": "idle",
            "room_state": "idle",
            "call_id": None,
            "session_scope": None,
            "speaker": None,
            "last_text": None,
            "last_reply": None,
            "last_latency_ms": None,
            "turn_count": 0,
            "monitor_enabled": bool((self.config.get("monitor") or {}).get("enabled", False)),
            "route_isolated": False,
            "opening_ready": False,
            "admission_decision": None,
            "admission_decision_source": None,
            "hangup_pending": False,
            "preparation_warnings": [],
            "last_error": None,
            "updated_at": time.time(),
        }

    def get_status(self) -> dict[str, Any]:
        """返回脱敏的电话对话状态。"""

        return copy.deepcopy(self._status)

    def add_status_listener(self, listener: Callable[[dict[str, Any]], Any]) -> None:
        """订阅对话阶段变化。"""

        if callable(listener) and listener not in self._listeners:
            self._listeners.append(listener)

    def _set_room_state(self, state: str, **extra: Any) -> None:
        """更新电话房间的占用状态，不用媒体细节替代房间事实。"""

        previous = self._room_state
        self._room_state = str(state or "idle")
        self._status.update({"room_state": self._room_state, **extra})
        if self._room_state != previous:
            logger.info(
                f"[qq_voice_call] 电话房间状态: {previous} -> {self._room_state}"
            )

    def _room_is_active(self) -> bool:
        """判断当前实例是否仍占有唯一电话房间。"""

        return self._room_state in {
            "deciding",
            "preparing",
            "connected",
            "ending",
            "summarizing",
        }

    def _source_session_scope(self, caller_uin: str | None = None) -> str:
        """返回当前来电者的普通 QQ 私聊会话作用域。"""

        caller = str(caller_uin or self._call.get("caller_uin") or "unknown").strip() or "unknown"
        return f"qq:private:{caller}"

    def _bot_self_id(self) -> str:
        """返回承载当前电话桥的机器人 QQ 号，用于固定 OneBot 发送连接。"""

        return str(self._call.get("bot_self_id") or "").strip()

    def _identity_context(
        self,
        *,
        text: str = "",
        caller_uin: str | None = None,
        caller_name: str | None = None,
        base: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构造与普通 QQ 私聊相同的可信身份/世界上下文。"""

        caller = str(caller_uin or self._call.get("caller_uin") or "").strip()
        name = str(caller_name or self._call.get("caller_name") or caller or "QQ联系人").strip()
        identity = dict(base or {})
        # 电话只是私聊的表达媒介，不能把 source 改成孤立的 qq_voice_call。
        identity.update(
            {
                "user_id": f"qq:{caller}",
                "identity_agent_available": bool(caller),
                "display_name": name,
                "platform": "qq",
                "source": "qq_private",
                "context_id": caller,
                "context_name": "",
                "visibility": "private",
                "call_id": str(self._call.get("call_id") or ""),
            }
        )
        if text:
            identity["memory_query"] = str(text)
        return identity

    def _telephone_system_context(self, *, connected: bool | None = None) -> str:
        """构建每轮重建的电话稳定 system 信息，避免写入持久对话历史。"""

        caller = str(self._call.get("caller_uin") or "")
        caller_name = str(self._call.get("caller_name") or caller or "QQ联系人")
        previous = self.call_history.get_previous(caller) if caller else None
        is_connected = self._connected_at is not None if connected is None else bool(connected)
        if is_connected:
            connected_at = _format_call_time(self._connected_at)
        else:
            connected_at = "尚未接通，正在完成接听准备"
        if previous:
            previous_end = _coerce_call_timestamp(previous.get("ended_at"), time.time())
            interval = _format_elapsed(time.time() - previous_end)
            previous_text = (
                f"距离与该联系人的上一次语音通话：{interval}；"
                f"上一次时长：{_format_elapsed(previous.get('duration_seconds'))}；"
                f"上一次挂断方：{previous.get('ended_by') or 'unknown/system'}。"
            )
        else:
            previous_text = "当前联系人没有可用的上一通语音通话记录。"
        return "\n".join(
            [
                "<qq_voice_call>",
                "当前场景是 QQ 私聊语音通话房间，电话音频只在通话中播放，"
                "不得把正常电话回复额外发送为 QQ 文字或普通语音消息。",
                f"当前联系人：{caller_name}（QQ：{caller or '未知'}）。",
                f"本次电话接通时间：{connected_at}。",
                previous_text,
                "</qq_voice_call>",
            ]
        )

    def _current_ai_service(self) -> Any:
        """优先使用注入服务，兼容插件加载顺序下的运行时注册。"""

        if self.ai_service is not None:
            return self.ai_service
        try:
            from onebot.runtime import get_qq_ai_service

            return get_qq_ai_service()
        except Exception:
            return None

    async def _build_request_options(
        self,
        ai: Any,
        *,
        message_kind: str,
        tool_names: tuple[str, ...] | list[str] | None = None,
        disable_tools: bool = False,
        include_telephone_context: bool = True,
    ) -> dict[str, Any]:
        """向宿主索取当前 QQ 私聊可信工具闭包并附加电话 system context。"""

        options: dict[str, Any] = {}
        builder = getattr(ai, "build_voice_options", None)
        if callable(builder):
            try:
                built = await _maybe_await(
                    builder(
                        caller_uin=str(self._call.get("caller_uin") or ""),
                        caller_name=str(self._call.get("caller_name") or ""),
                        bot_self_id=self._bot_self_id(),
                        session_scope=self._session_scope(str(self._call.get("call_id") or "")),
                        message_kind=message_kind,
                        tool_names=tool_names,
                    )
                )
                if isinstance(built, dict):
                    # AstrBot 工具描述符中包含绑定当前事件的回调；该回调可能捕获
                    # aiohttp WebSocket/socket，深拷贝会触发 pickle 失败。这里只复制
                    # 外层选项字典，后续不会修改 tools 内部对象。
                    options.update(dict(built))
            except Exception as error:  # noqa: BLE001
                logger.warning(f"[qq_voice_call] 构建电话可信工具闭包失败，按无工具继续：{error}")
        if disable_tools or not options:
            options.update(
                {
                    "tools": [],
                    "allowed_tool_capabilities": [],
                    "allowed_tool_names": [],
                    "allowed_tool_prefixes": [],
                }
            )
        if include_telephone_context:
            contexts = list(options.get("contexts") or [])
            contexts.append({"content": self._telephone_system_context()})
            options["contexts"] = contexts
        options["message_kind"] = message_kind
        return options

    @classmethod
    def _request_option_tool_names(cls, options: dict[str, Any]) -> set[str]:
        """提取请求选项中实际可见的工具名称。

        不同宿主版本的工具描述可能把名称放在 ``allowed_tool_names``、
        ``function.name`` 或顶层 ``name``。来电准入只在两个动作都可见时
        才允许请求模型；无法确认工具完整性时必须闭合到系统拒接。

        Args:
            options: ``build_voice_options`` 返回的请求选项。

        Returns:
            当前请求中可识别的工具名称集合。
        """

        allowed_names: set[str] = set()
        for value in options.get("allowed_tool_names") or ():
            name = str(value or "").strip()
            if name:
                allowed_names.add(name)
        tool_names: set[str] = set()
        for tool in options.get("tools") or ():
            if isinstance(tool, dict):
                function = tool.get("function")
                if isinstance(function, dict):
                    name = function.get("name")
                else:
                    name = tool.get("name")
            else:
                function = getattr(tool, "function", None)
                name = getattr(function, "name", None) if function is not None else None
                if not name:
                    name = getattr(tool, "name", None)
            name = str(name or "").strip()
            if name:
                tool_names.add(name)

        # 请求门控和 API 工具描述同时存在时，只有两者的交集才是真正可执行
        # 的集合；任一侧明确为空都不能被另一侧的旧数据“补齐”。
        if "tools" in options:
            if "allowed_tool_names" in options:
                return allowed_names & tool_names
            return tool_names
        return allowed_names

    async def _send_private_flow_text(
        self,
        text: str,
        *,
        reason: str,
        call: dict[str, Any] | None = None,
        record_context: bool = True,
    ) -> bool:
        """发送并记录非电话音频的流程文本，例如忙线和异常拒接。"""

        message = str(text or "").strip()
        active_call = call or self._call
        caller = str(active_call.get("caller_uin") or "").strip()
        if not caller or not message:
            return False
        ai = self._current_ai_service()
        event_text = (
            "<qq_voice_call>"
            f"当前时间：{_format_call_time(time.time())}。"
            f"与 {str(active_call.get('caller_name') or caller)} 的语音通话未建立，"
            f"原因：{reason}。"
            "</qq_voice_call>"
        )
        append = getattr(ai, "append_session_message", None)
        if record_context and callable(append):
            await _maybe_await(
                append(
                    self._source_session_scope(caller),
                    "user",
                    event_text,
                    {"message_kind": "qq_voice_call_admission", "reason": reason},
                )
            )
        sender = getattr(ai, "send_private_text", None)
        if not callable(sender):
            return False
        submitted = bool(
            await _maybe_await(
                sender(
                    caller,
                    message,
                    reason=reason,
                    bot_self_id=self._bot_self_id(),
                )
            )
        )
        if not submitted:
            # 未能定位到电话所属机器人的唯一 OneBot 连接时，不能把未发出的
            # 拒接/忙线文本伪装成已经说过的 assistant 历史。
            logger.warning(
                "[qq_voice_call] 电话流程文本未提交，不写入 assistant 会话历史: "
                f"user={caller} reason={reason}"
            )
            return False
        if record_context and callable(append):
            await _maybe_await(
                append(
                    self._source_session_scope(caller),
                    "assistant",
                    message,
                    {"message_kind": "qq_voice_call_admission_reply", "reason": reason},
                )
            )
        return True

    async def _warm_component(self, label: str, service: Any, *, worker: bool) -> str | None:
        """运行一个可选预热入口；失败返回警告文本而不阻断接听。"""

        method = getattr(service, "warm_up", None)
        if not callable(method):
            return None
        logger.info(f"[qq_voice_call] 正在准备{label}")
        try:
            result = await asyncio.to_thread(method) if worker else method()
            await _maybe_await(result)
        except Exception as error:  # noqa: BLE001
            message = f"{label}准备失败：{error}"
            logger.warning(f"[qq_voice_call] {message}；本次仍继续接听")
            return message
        logger.info(f"[qq_voice_call] {label}准备完成")
        return None

    async def _restore_route(self, call_id: str | None) -> None:
        """在线程中恢复通话前路由；重复调用和未隔离状态都保持安全。"""

        restore = getattr(self.route_guard, "restore", None)
        if callable(restore):
            try:
                await asyncio.to_thread(restore, call_id)
            except Exception as error:  # noqa: BLE001
                logger.warning(f"[qq_voice_call] 恢复电话音频路由失败：{error}")
        self._status["route_isolated"] = bool(getattr(self.route_guard, "active", False))

    async def _prepare_and_accept(self, call_id: str, generation: int, request_accept: bool) -> None:
        """隔离路由并并行预热模型，完成后由 OneBot 主动接听。"""

        if self.config.get("enabled"):
            activate = getattr(self.route_guard, "activate", None)
            if callable(activate):
                try:
                    isolated = await asyncio.to_thread(activate, call_id)
                    self._status["route_isolated"] = bool(isolated)
                except Exception as error:  # noqa: BLE001
                    logger.warning(f"[qq_voice_call] 电话音频路由准备失败，继续接听：{error}")
        if not self._is_current_generation(generation):
            await self._restore_route(call_id)
            return

        warnings: list[str] = []
        if self.config.get("enabled"):
            preparation = self.config.get("preparation") or {}
            tasks = []
            if preparation.get("warm_up_asr", True):
                tasks.append(self._warm_component("电话 SenseVoice", self.asr, worker=True))
            if preparation.get("warm_up_voiceprint", True) and bool((self.config.get("voiceprint") or {}).get("enable", True)):
                tasks.append(self._warm_component("电话 CAM++ 声纹", self.voiceprint, worker=True))
            if preparation.get("warm_up_tts", True):
                tasks.append(self._warm_component("电话 TTS", self.tts, worker=False))
            if tasks:
                warnings = [item for item in await asyncio.gather(*tasks) if item]

        if not self._is_current_generation(generation):
            await self._restore_route(call_id)
            return
        self._prepared_call_id = call_id
        self._set_phase("ready", preparation_warnings=warnings)
        if not request_accept or self._accept_requested_call_id == call_id:
            return

        await self._submit_accept(call_id, generation)

    async def _submit_accept(self, call_id: str, generation: int) -> bool:
        """在所有电话准备完成后提交真实 AVSDK 接听命令。"""

        control = self.call_control
        if not callable(control):
            message = "电话准备完成，但没有可用的接听控制入口"
            logger.warning(f"[qq_voice_call] {message}")
            self._status["last_error"] = message
            await self._restore_route(call_id)
            return False
        self._accept_requested_call_id = call_id
        try:
            result = await _maybe_await(control("accept"))
        except Exception as error:  # noqa: BLE001
            result = {"success": False, "message": str(error)}
        if not self._is_current_generation(generation):
            return False
        if not isinstance(result, dict) or not result.get("success"):
            safe_result = result if isinstance(result, dict) else {}
            message = str(safe_result.get("message") or safe_result.get("status") or "接听命令提交失败")
            logger.warning(f"[qq_voice_call] 电话准备完成但接听失败：{message}")
            self._set_phase("ready", last_error=message)
            await self._restore_route(call_id)
            return False
        logger.info("[qq_voice_call] 电话准备完成，已提交接听命令")
        self._set_phase("waiting_connect", last_error=None)
        return True

    async def prepare_call(self, call: dict[str, Any], *, request_accept: bool) -> None:
        """为一通振铃或已接通电话建立唯一准备任务。"""

        call_id = str(call.get("call_id") or "").strip()
        if not call_id:
            return
        current_id = str(self._status.get("call_id") or "")
        if current_id and current_id != call_id:
            await self.stop("replaced")
        if self._prepared_call_id == call_id:
            return
        existing = self._preparation_task
        if existing is not None and not existing.done() and self._preparation_call_id == call_id:
            await existing
            return

        self._generation += 1
        generation = self._generation
        self._call = copy.deepcopy(call)
        self._pending_hangup = None
        self._active_reply_request = None
        self._hangup_control_token = None
        self._hangup_control_inflight_token = None
        self._transcript = None
        self._thread_stop_event = threading.Event()
        self._capture_stop_event = None
        self._stop_event = asyncio.Event()
        self._turn_index = 0
        self._private_text_queue = asyncio.Queue()
        self._status.update(
            {
                "call_id": call_id,
                "last_error": None,
                "preparation_warnings": [],
                "turn_count": 0,
                "speaker": None,
                "last_text": None,
                "last_reply": None,
                "last_latency_ms": None,
                "hangup_pending": False,
            }
        )
        self._set_phase("preparing")
        task = asyncio.create_task(
            self._prepare_and_accept(call_id, generation, request_accept),
            name=f"QQVoiceDialogue-Prepare-{call_id}",
        )
        self._preparation_task = task
        self._preparation_call_id = call_id
        try:
            await task
        finally:
            if self._preparation_task is task:
                self._preparation_task = None
                self._preparation_call_id = None

    async def _branch_phone_session(self, call: dict[str, Any]) -> bool:
        """从当前 QQ 私聊复制本通电话的独立会话分支。"""

        call_id = str(call.get("call_id") or "").strip()
        caller = str(call.get("caller_uin") or "").strip()
        if not call_id or not caller:
            return False
        ai = self._current_ai_service()
        branch = getattr(ai, "branch_session", None)
        if not callable(branch):
            # 正式 OneBotAIService 必须提供分支接口。这里仅为旧宿主/测试替身
            # 保留可运行降级，真实运行日志会明确标出没有继承私聊历史的风险。
            self._status["session_scope"] = self._session_scope(call_id)
            logger.warning("[qq_voice_call] OneBot AI 服务未提供电话会话分支接口，按空电话分支降级")
            return True
        source_scope = self._source_session_scope(caller)
        target_scope = self._session_scope(call_id)
        success = bool(await _maybe_await(branch(source_scope, target_scope)))
        if success:
            self._status["session_scope"] = target_scope
            logger.info(
                "[qq_voice_call] 已创建电话上下文分支: "
                f"source={source_scope} target={target_scope}"
            )
        return success

    async def _prepare_opening(self, call: dict[str, Any], generation: int) -> bool:
        """在真正接听前生成并预合成一句电话开场白。"""

        if not self._is_current_generation(generation):
            return False
        ai = self._current_ai_service()
        generate = getattr(ai, "generate_text", None)
        if not callable(generate):
            return False
        caller = str(call.get("caller_uin") or "")
        name = str(call.get("caller_name") or caller or "QQ联系人")
        prompt = "\n".join(
            [
                "<qq_voice_call>",
                f"当前时间：{_format_call_time(time.time())}。",
                f"你已选择接听 {name} 发起的 QQ 私聊语音通话。",
                "对方是拨打方，你是接听方；不要把电话说成是你拨出去的，"
                "也不要说对方终于肯接你的电话。",
                "请说一句适合接通瞬间直接播放的简短自然开场白。"
                "这句话只会在电话里播放，不要发送 QQ 文字，不要解释系统标签。",
                "</qq_voice_call>",
            ]
        )
        options = await self._build_request_options(
            ai,
            message_kind="qq_voice_call_opening",
            disable_tools=True,
        )
        reply = await _maybe_await(
            generate(
                username=name,
                prompt=prompt,
                identity_context=self._identity_context(text=prompt, caller_uin=caller, caller_name=name),
                session_scope=self._session_scope(str(call.get("call_id") or "")),
                request_options=options,
                timeout=float((self.config.get("llm") or {}).get("timeout", 60)),
            )
        )
        if not self._is_current_generation(generation):
            return False
        text = str(reply or "").strip()
        max_chars = int((self.config.get("llm") or {}).get("max_reply_chars", 1000))
        text = text[:max_chars]
        if not text:
            logger.warning("[qq_voice_call] 来电开场白模型未返回可播放文本")
            return False
        try:
            source = await _maybe_await(
                self.tts.synthesize(
                    text,
                    streaming=bool((self.config.get("tts") or {}).get("streaming", True)),
                )
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[qq_voice_call] 来电开场白预合成失败：{error}")
            return False
        if source is None or not self._is_current_generation(generation):
            return False
        self._prepared_opening = {"text": text, "source": source}
        self._status["opening_ready"] = True
        logger.info("[qq_voice_call] 电话开场白已生成并完成预合成准备")
        return True

    async def _release_unaccepted_room(self, call_id: str, *, phase: str = "ended") -> None:
        """释放尚未接通的来电房间并恢复可能已应用的电话路由。"""

        await self._restore_route(call_id)
        if call_id:
            self._declined_call_ids.add(call_id)
        if self._room_call_id == call_id:
            self._prepared_call_id = None
            self._prepared_opening = None
            self._status["opening_ready"] = False
            self._set_room_state("ended")
            self._set_phase(phase)

    async def _reject_before_connect(
        self,
        call: dict[str, Any],
        *,
        reason: str,
        text: str,
        release_room: bool = True,
        record_context: bool = True,
    ) -> None:
        """尝试拒接来电，并在无法验证 AVSDK 动作时仍如实发送流程文本。"""

        call_id = str(call.get("call_id") or "").strip()
        control = self.call_control
        if callable(control):
            try:
                result = await _maybe_await(control("reject"))
                logger.info(
                    "[qq_voice_call] 来电拒接控制结果: "
                    f"reason={reason} status={str((result or {}).get('status') if isinstance(result, dict) else result)}"
                )
            except Exception as error:  # noqa: BLE001
                logger.warning(f"[qq_voice_call] 来电拒接控制失败（不伪造成功）：{error}")
        await self._send_private_flow_text(
            text,
            reason=reason,
            call=call,
            record_context=record_context,
        )
        if release_room and call_id:
            await self._release_unaccepted_room(call_id)

    async def record_admission_decision(self, action: str, call: dict[str, Any]) -> dict[str, Any]:
        """接收模型工具表达的接听/拒接意图，实际 AVSDK 动作留给准备完成后。"""

        decision = str(action or "").strip().lower()
        call_id = str(call.get("call_id") or "").strip()
        if decision not in {"accept", "reject"}:
            return {"success": False, "status": "invalid_action", "action": decision}
        if not call_id or self._room_call_id != call_id or self._room_state != "deciding":
            return {
                "success": False,
                "status": "admission_not_pending",
                "action": decision,
                "message": "当前没有等待模型决定的来电",
            }
        if self._admission_decision is not None:
            logger.warning(
                "[qq_voice_call] 忽略同一来电的后续模型决定: "
                f"kept={self._admission_decision} ignored={decision} call_id={call_id}"
            )
            return {
                "success": True,
                "status": "decision_already_recorded",
                "action": self._admission_decision,
                "message": "当前来电已经记录首个有效决定，后续决定不会覆盖。",
            }
        self._admission_decision = decision
        if self._admission_decision_event is not None:
            self._admission_decision_event.set()
        self._status["admission_decision"] = decision
        self._status["admission_decision_source"] = "tool"
        logger.info(f"[qq_voice_call] 已记录模型来电意图: action={decision} call_id={call_id}")
        return {
            "success": True,
            "status": "decision_recorded",
            "action": decision,
            "message": "已记录接听意图，正在继续电话准备流程",
        }

    async def _run_admission(self, call: dict[str, Any]) -> None:
        """在普通私聊中请求模型决定本次来电，并按结果准备或拒接。"""

        call_id = str(call.get("call_id") or "").strip()
        caller = str(call.get("caller_uin") or "").strip()
        name = str(call.get("caller_name") or caller or "QQ联系人")
        admission_cfg = self.config.get("admission") or {}
        ai = self._current_ai_service()
        try:
            is_live = getattr(ai, "is_live_active", None)
            if callable(is_live) and bool(is_live()):
                await self._reject_before_connect(
                    call,
                    reason="live",
                    text=str(admission_cfg.get("live_reject_message") or ""),
                )
                return
            generate = getattr(ai, "generate_text", None)
            if not callable(generate):
                raise RuntimeError("OneBot 文本模型服务尚未注册")
            self._admission_decision = None
            self._status["admission_decision"] = None
            self._status["admission_decision_source"] = None
            self._admission_decision_event = asyncio.Event()
            prompt = "\n".join(
                [
                    "<qq_voice_call>",
                    f"当前时间：{_format_call_time(time.time())}。",
                    f"{name} 向你发起了 QQ 私聊语音通话。",
                    "这是一次强制二选一的真实电话动作，不存在第三种回答。",
                    "现在必须且只能调用一个工具：接听调用 qq_voice_call_accept；"
                    "不接听调用 qq_voice_call_reject。",
                    "禁止输出解释、开场白、自然语言决定、等待或沉默；"
                    "工具调用本身是唯一有效回答。",
                    "</qq_voice_call>",
                ]
            )
            options = await self._build_request_options(
                ai,
                message_kind="qq_voice_call_admission",
                tool_names=self.ADMISSION_TOOL_NAMES,
                include_telephone_context=False,
            )
            available_tools = self._request_option_tool_names(options)
            missing_tools = [
                name for name in self.ADMISSION_TOOL_NAMES if name not in available_tools
            ]
            if missing_tools:
                # ``tool_choice=required`` 在没有完整工具集合时无法表达二选一。
                # 直接失败闭环，避免把一个没有可执行动作的请求交给模型。
                raise RuntimeError(
                    "来电二选一工具未完整注册，缺少：" + "、".join(missing_tools)
                )
            options["tool_choice"] = "required"
            # 接听的唯一自然语言开场白由电话分支在真正接通前生成。接听工具
            # 成功后必须结束本次“是否接听”的请求，不能让通用工具循环再追加
            # 一条普通私聊回复并污染随后复制的电话上下文。
            options["terminal_tool_names"] = ("qq_voice_call_accept",)
            options["terminal_tool_allow_empty_response"] = True
            # 接听的同轮自然语言也不能作为开场白复用；只保留工具事实，
            # 由 _prepare_opening 在电话分支中生成唯一、方向正确的开场。
            options["terminal_tool_response_policy"] = "discard"
            try:
                timeout = max(0.1, float(admission_cfg.get("decision_timeout", 20.0)))
            except (TypeError, ValueError):
                timeout = 20.0
            deadline = time.monotonic() + timeout

            async def request_decision(request_prompt: str, request_options: dict[str, Any]) -> str:
                """在同一来电决策总时限内发起一次模型请求。"""

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("来电 AI 决策超时")
                wait_seconds = max(0.1, remaining)
                return await asyncio.wait_for(
                    _maybe_await(
                        generate(
                            username=name,
                            prompt=request_prompt,
                            identity_context=self._identity_context(
                                text=request_prompt,
                                caller_uin=caller,
                                caller_name=name,
                            ),
                            session_scope=self._source_session_scope(caller),
                            request_options=request_options,
                            timeout=wait_seconds,
                        )
                    ),
                    timeout=wait_seconds + 0.2,
                )

            async def wait_for_tool_dispatch() -> None:
                """给异步 AstrBot 工具回调极短机会，不引入固定接听延迟。"""

                if self._admission_decision is not None or self._admission_decision_event is None:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    await asyncio.wait_for(
                        self._admission_decision_event.wait(),
                        timeout=min(0.2, remaining),
                    )
                except asyncio.TimeoutError:
                    pass

            reply = await request_decision(prompt, options)
            reply_persisted = True
            if self._room_call_id != call_id or self._room_state != "deciding":
                return
            await wait_for_tool_dispatch()
            if self._admission_decision is None:
                logger.warning(
                    "[qq_voice_call] 来电模型首轮未调用工具，进行一次强制二选一纠正: "
                    f"call_id={call_id}"
                )
                # 纠正轮仍然必须返回结构化工具调用；不能退回 ``auto``，否则
                # 模型再次输出自然语言时会把振铃阶段变成第三种状态。
                retry_options = dict(options)
                retry_options.update(
                    {
                        "tool_choice": "required",
                        "persist_session": False,
                        "persist_user_message": False,
                    }
                )
                retry_prompt = "\n".join(
                    [
                        "<qq_voice_call>",
                        "上一轮只输出了文字，尚未执行来电动作。",
                        "现在必须且只能调用 qq_voice_call_accept 或 qq_voice_call_reject 之一。",
                        "不要输出解释、开场白或普通回复；请立即返回一次工具调用。",
                        "</qq_voice_call>",
                    ]
                )
                reply = await request_decision(retry_prompt, retry_options)
                reply_persisted = False
                if self._room_call_id != call_id or self._room_state != "deciding":
                    return
                await wait_for_tool_dispatch()
            decision = self._admission_decision
            if decision == "reject":
                # 模型生成的文字本来已经进入普通私聊会话；只把它提交给 QQ，
                # 纠正轮不持久化，因此它的拒接文本需要由流程入口补记一次。
                await self._reject_before_connect(
                    call,
                    reason="ai_reject",
                    text=str(reply or "").strip(),
                    record_context=not reply_persisted,
                )
                return
            if decision != "accept":
                # 模型两轮都没有返回结构化动作时，不把自然语言猜测成“接听”。
                # 二选一没有第三种结果，因此按安全策略明确拒接并记录来源。
                self._status["admission_decision"] = "reject"
                self._status["admission_decision_source"] = "system_fallback"
                logger.error(
                    "[qq_voice_call] 来电模型两轮均未调用接听/拒接工具，"
                    f"按系统二选一兜底自动拒接: call_id={call_id}"
                )
                await self._reject_before_connect(
                    call,
                    reason="ai_no_tool",
                    text=str(admission_cfg.get("error_reject_message") or ""),
                    # 首轮自然语言不是有效决定，必须补写明确的未建立/拒接事实，
                    # 不能把那段文字当成已经完成的模型动作。
                    record_context=True,
                )
                return

            self._set_room_state("preparing")
            await self.prepare_call(call, request_accept=False)
            generation = self._generation
            if self._prepared_call_id != call_id or not self._is_current_generation(generation):
                raise RuntimeError("电话准备在来电结束后失效")
            if not await self._branch_phone_session(call):
                raise RuntimeError("无法创建电话上下文分支")
            if not await self._prepare_opening(call, generation):
                raise RuntimeError("开场白生成或预合成失败")
            if not await self._submit_accept(call_id, generation):
                raise RuntimeError("接听命令未成功提交")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[qq_voice_call] 来电 AI 决策或准备失败：{error}")
            if self._room_call_id == call_id:
                if self._admission_decision is None:
                    self._status["admission_decision"] = "reject"
                    self._status["admission_decision_source"] = "system_fallback"
                    logger.error(
                        "[qq_voice_call] 来电未取得有效工具决定，"
                        f"按系统二选一兜底自动拒接: call_id={call_id}"
                    )
                await self._reject_before_connect(
                    call,
                    reason="admission_error",
                    text=str(admission_cfg.get("error_reject_message") or ""),
                )
        finally:
            if self._admission_call_id == call_id:
                self._admission_task = None
                self._admission_call_id = None
                self._admission_decision_event = None

    async def _begin_admission(self, call: dict[str, Any]) -> None:
        """声明唯一房间所有权，并启动不阻塞信令事件循环的来电决策任务。"""

        call_id = str(call.get("call_id") or "").strip()
        if not call_id:
            return
        if call_id in self._declined_call_ids:
            logger.info(f"[qq_voice_call] 忽略已拒接来电的重复振铃: call_id={call_id}")
            return
        if self._room_is_active() and self._room_call_id != call_id:
            admission_cfg = self.config.get("admission") or {}
            logger.info(
                "[qq_voice_call] 第二通来电进入忙线处理: "
                f"active_call_id={self._room_call_id} incoming_call_id={call_id}"
            )
            await self._reject_before_connect(
                call,
                reason="busy",
                text=str(admission_cfg.get("busy_reject_message") or ""),
                release_room=False,
            )
            return
        if self._admission_task is not None and not self._admission_task.done() and self._admission_call_id == call_id:
            return
        self._call = copy.deepcopy(call)
        self._room_call_id = call_id
        self._room_started_at = time.time()
        self._connected_at = None
        self._last_reply_at = None
        self._status.update(
            {
                "call_id": call_id,
                "last_error": None,
                "opening_ready": False,
                "admission_decision": None,
                "admission_decision_source": None,
            }
        )
        self._set_room_state("deciding")
        self._set_phase("deciding")
        task = asyncio.create_task(self._run_admission(copy.deepcopy(call)), name=f"QQVoiceAdmission-{call_id}")
        self._admission_task = task
        self._admission_call_id = call_id

    def _set_phase(self, phase: str, **extra: Any) -> None:
        """更新状态并隔离外部状态回调异常。"""

        previous_phase = str(self._status.get("phase") or "idle")
        self._status.update({"phase": phase, "updated_at": time.time(), **extra})
        if phase != previous_phase:
            logger.info(f"[qq_voice_call] 电话对话状态: {previous_phase} -> {phase}")
        payload = self.get_status()
        for listener in tuple(self._listeners):
            try:
                result = listener(payload)
                if inspect.isawaitable(result):
                    asyncio.get_running_loop().create_task(_maybe_await(result))
            except Exception as error:  # noqa: BLE001
                logger.warning(f"[qq_voice_call] 对话状态回调失败：{error}")
        if self._transcript is not None:
            try:
                self._transcript.save(phase)
            except Exception as error:  # noqa: BLE001
                logger.warning(f"[qq_voice_call] 电话文字记录保存失败：{error}")

    @classmethod
    def _merge_call_snapshot(
        cls,
        existing: dict[str, Any] | None,
        incoming: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """合并远程快照，避免空身份字段覆盖已确认的来电者。"""

        merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        if not isinstance(incoming, dict):
            return merged
        for key, value in incoming.items():
            if key in cls._CALL_IDENTITY_FIELDS and not str(value or "").strip():
                continue
            merged[key] = copy.deepcopy(value)
        return merged

    def _caller_identity(self) -> tuple[str, str]:
        """返回当前通话的来电者身份，并以后续 transcript 作为稳定回退。"""

        caller_uin = str(self._call.get("caller_uin") or "").strip()
        caller_name = str(self._call.get("caller_name") or "").strip()
        if self._transcript is not None:
            caller_uin = caller_uin or str(self._transcript.caller_uin or "").strip()
            caller_name = caller_name or str(self._transcript.caller_name or "").strip()
        caller_name = caller_name or caller_uin or "QQ联系人"
        return caller_uin, caller_name

    async def handle_call_event(self, event: dict[str, Any]) -> None:
        """接收通话桥的 ringing/connected/ended 生命周期事件。"""

        name = str((event or {}).get("event") or "")
        call = (event or {}).get("call") if isinstance((event or {}).get("call"), dict) else {}
        event_call_id = str(call.get("call_id") or (event or {}).get("call_id") or "").strip()
        active_call_id = str(self._status.get("call_id") or "").strip()
        if name in {"ended", "transport_lost", "error"} and active_call_id and event_call_id and event_call_id != active_call_id:
            logger.info(
                "[qq_voice_call] 忽略旧通话的结束事件: "
                f"event_call_id={event_call_id} active_call_id={active_call_id}"
            )
            return
        if name == "ringing":
            admission = self.config.get("admission") or {}
            if bool(admission.get("enabled", True)):
                await self._begin_admission(call)
            else:
                # 配置显式关闭 AI 来电感知时保留第二阶段兼容行为。
                self._room_call_id = event_call_id or str(call.get("call_id") or "")
                self._set_room_state("preparing")
                await self.prepare_call(call, request_accept=True)
        elif name == "connected":
            if event_call_id and event_call_id in self._declined_call_ids:
                logger.warning(
                    "[qq_voice_call] 已拒接来电仍迟到上报 connected，"
                    f"不会重启电话房间: call_id={event_call_id}"
                )
                control = self.call_control
                if callable(control):
                    try:
                        result = await _maybe_await(control("hangup"))
                        logger.info(
                            "[qq_voice_call] 对迟到已接通来电提交挂断请求: "
                            f"status={str((result or {}).get('status') if isinstance(result, dict) else result)}"
                        )
                    except Exception as error:  # noqa: BLE001
                        logger.warning(f"[qq_voice_call] 迟到已接通来电的挂断请求失败：{error}")
                return
            await self.start(call)
        elif name in {"ended", "transport_lost", "error"}:
            if event_call_id and event_call_id == str(self._call.get("call_id") or ""):
                self._call = self._merge_call_snapshot(self._call, call)
            await self.stop(name)
            if event_call_id:
                self._declined_call_ids.discard(event_call_id)

    async def start(self, call: dict[str, Any]) -> None:
        """为一通已接通的电话启动独立对话任务。"""

        call_id = str(call.get("call_id") or "").strip()
        if not call_id:
            return
        if self._room_is_active() and self._room_call_id not in {None, call_id}:
            logger.info(
                "[qq_voice_call] 忽略不属于当前房间的 connected 事件: "
                f"event_call_id={call_id} active_call_id={self._room_call_id}"
            )
            return
        await self.prepare_call(call, request_accept=False)
        if not self.config.get("enabled") or self._prepared_call_id != call_id:
            return
        if self._task is not None and not self._task.done() and self._status.get("call_id") == call_id:
            return
        if self._task is not None and not self._task.done():
            await self.stop("replaced")
        generation = self._generation
        self._call.update(copy.deepcopy(call))
        caller_uin = str(call.get("caller_uin") or "")
        caller_name = str(call.get("caller_name") or caller_uin or "QQ联系人")
        if not self._status.get("session_scope"):
            if not await self._branch_phone_session(self._call):
                logger.warning("[qq_voice_call] 已接通但无法创建电话会话分支，结束本次电话对话")
                return
        audio_cfg = self.config.get("audio") or {}
        logger.info(
            "[qq_voice_call] 准备启动电话模型对话: "
            f"call_id={call_id} input_device={audio_cfg.get('input_device_index')} "
            f"output_device={audio_cfg.get('output_device_index')} "
            f"mode={self.config.get('mode') or 'full_duplex'}"
        )
        self._transcript = QQVoiceCallTranscript(
            call_id,
            caller_uin,
            caller_name,
            bool((self.config.get("session") or {}).get("persist_transcript", True)),
        )
        self._connected_at = _coerce_call_timestamp(call.get("connected_at"), time.time())
        self._room_started_at = self._connected_at
        previous = self.call_history.get_previous(caller_uin)
        self._transcript.set_metadata(
            session_scope=self._session_scope(call_id),
            connected_at=self._connected_at,
            previous_call=previous,
            room_state="connected",
        )
        if generation != self._generation:
            return
        self._status.update(
            {"call_id": call_id, "last_error": None, "turn_count": 0, "last_latency_ms": None}
        )
        self._room_call_id = call_id
        self._set_room_state("connected")
        self._set_phase("listening")
        self._task = asyncio.create_task(self._run(generation), name=f"QQVoiceDialogue-{call_id}")

    async def enqueue_private_text(
        self,
        *,
        message: str,
        caller_uin: str,
        caller_name: str,
        identity_context: dict[str, Any],
        world_task: Any = None,
    ) -> bool:
        """把同一联系人通话期间的普通 QQ 文字转入电话分支。"""

        text = str(message or "").strip()
        caller = str(caller_uin or "").strip()
        active_caller = str(self._call.get("caller_uin") or "").strip()
        if (
            not text
            or not caller
            or caller != active_caller
            or self._room_state != "connected"
            or self._stop_event is None
            or self._stop_event.is_set()
        ):
            return False
        active_reply = self._active_reply_request
        active_reply_token = (
            str(active_reply.get("reply_token") or "").strip()
            if isinstance(active_reply, dict)
            else ""
        )
        if self._has_pending_hangup_for_reply(active_reply_token):
            # 挂断工具已经把本轮回复指定为收口。此时对端继续发送文字不应
            # 取消结束语或把通话重新拉回普通对话流程。返回 True 使上游
            # 不会把同一条文字再送入普通私聊模型；世界事件也要明确收尾，
            # 否则会遗留电话结束阶段无法消费的私聊处理权。
            await self._finish_world_task(
                self._current_ai_service(),
                world_task,
                result="skipped",
                reason="电话已进入模型挂断收口",
            )
            logger.info(f"[qq_voice_call] 已忽略挂断收口期间的私聊文字: user={caller}")
            return True
        await self._private_text_queue.put(
            PendingPrivateText(
                text=text,
                caller_uin=caller,
                caller_name=str(caller_name or active_caller),
                identity_context=copy.deepcopy(identity_context or {}),
                world_task=world_task,
            )
        )
        # 正在收音时唤醒当前 capture；正在播放时同步取消未播放 PCM。
        if self._capture_stop_event is not None:
            self._capture_stop_event.set()
        if self._interrupt_event is not None:
            self._interrupt_event.set()
        source = self._active_tts_source
        cancel = getattr(source, "cancel", None)
        if callable(cancel):
            try:
                cancel("private_text")
            except Exception:
                pass
        logger.info(f"[qq_voice_call] 已把私聊文字加入电话房间队列: user={caller}")
        return True

    async def queue_hangup_after_reply(
        self,
        call: dict[str, Any],
        *,
        caller_uin: str | None = None,
    ) -> dict[str, Any]:
        """登记当前模型回复播放完成后的挂断意图，不立即控制 QQ。

        Args:
            call: 通话桥当前的脱敏通话状态。
            caller_uin: 发起工具调用的私聊 QQ 号，用于再次校验作用域。

        Returns:
            ``queued_after_reply`` 表示已登记；``hangup_already_queued`` 或
            ``hangup_already_submitted`` 表示同一轮重复调用；其他状态表示
            当前没有可安全绑定的电话回复。
        """

        current_call_id = str(self._call.get("call_id") or "").strip()
        requested_call_id = str((call or {}).get("call_id") or "").strip()
        active_caller = str(self._call.get("caller_uin") or "").strip()
        requested_caller = str((call or {}).get("caller_uin") or "").strip()
        sender_caller = str(caller_uin or "").strip()
        if self._room_state != "connected" or not current_call_id:
            return {
                "success": False,
                "status": "call_not_connected",
                "action": "hangup",
                "message": "当前电话尚未处于已接通状态",
            }
        if requested_call_id and requested_call_id != current_call_id:
            return {
                "success": False,
                "status": "call_scope_mismatch",
                "action": "hangup",
                "message": "控制请求不属于当前电话房间",
            }
        if (requested_caller and active_caller and requested_caller != active_caller) or (
            sender_caller and active_caller and sender_caller != active_caller
        ):
            return {
                "success": False,
                "status": "caller_scope_mismatch",
                "action": "hangup",
                "message": "控制请求不属于当前来电者",
            }

        active = self._active_reply_request
        if not isinstance(active, dict):
            return {
                "success": False,
                "status": "hangup_not_during_phone_reply",
                "action": "hangup",
                "message": "当前没有正在生成的电话回复可绑定挂断请求",
            }
        token = str(active.get("reply_token") or "").strip()
        if not token or active.get("call_id") != current_call_id or active.get("generation") != self._generation:
            return {
                "success": False,
                "status": "hangup_reply_expired",
                "action": "hangup",
                "message": "当前电话回复已失效，未登记挂断",
            }
        if self._hangup_control_token == token:
            return {
                "success": True,
                "status": "hangup_already_submitted",
                "action": "hangup",
                "message": "本轮回复已经提交过挂断请求",
            }
        if self._hangup_control_inflight_token == token:
            return {
                "success": True,
                "status": "hangup_control_inflight",
                "action": "hangup",
                "message": "本轮挂断请求正在等待 QQ 通话桥返回结果",
            }
        pending = self._pending_hangup
        if isinstance(pending, dict) and pending.get("reply_token") == token:
            return {
                "success": True,
                "status": "hangup_already_queued",
                "action": "hangup",
                "message": "已登记本轮回复播放完成后的挂断请求",
            }

        self._pending_hangup = {
            "call_id": current_call_id,
            "caller_uin": active_caller,
            "generation": self._generation,
            "reply_token": token,
            "requested_at": time.time(),
        }
        self._status["hangup_pending"] = True
        logger.info(
            "[qq_voice_call] 已登记本轮回复播放完成后的挂断请求: "
            f"call_id={current_call_id} reply_token={token[:8]}"
        )
        return {
            "success": True,
            "status": "queued_after_reply",
            "action": "hangup",
            "message": "已登记本轮电话回复播放完成后的挂断请求，尚未向 QQ 提交挂断",
        }

    def _discard_hangup_for_reply(self, reply_token: str | None, reason: str) -> None:
        """作废指定回复的迟到挂断意图，避免后续轮次误触发。"""

        token = str(reply_token or "").strip()
        pending = self._pending_hangup
        if isinstance(pending, dict) and (not token or pending.get("reply_token") == token):
            self._pending_hangup = None
            self._status["hangup_pending"] = False
            logger.info(
                "[qq_voice_call] 已作废延后挂断请求: "
                f"reason={reason} reply_token={str(pending.get('reply_token') or '')[:8]}"
            )

    def _has_pending_hangup_for_reply(self, reply_token: str | None) -> bool:
        """判断当前电话回复是否已经登记为结束通话的收口回复。"""

        token = str(reply_token or "").strip()
        pending = self._pending_hangup
        return bool(
            token
            and isinstance(pending, dict)
            and pending.get("reply_token") == token
            and pending.get("call_id") == str(self._call.get("call_id") or "").strip()
            and pending.get("generation") == self._generation
        )

    def _clear_reply_request_for_token(self, reply_token: str | None, reason: str) -> None:
        """清理本轮模型回复和挂断意图，供 TTS 失败等播放前路径复用。"""

        token = str(reply_token or "").strip()
        self._discard_hangup_for_reply(token, reason)
        active = self._active_reply_request
        if isinstance(active, dict) and active.get("reply_token") == token:
            self._active_reply_request = None

    async def _submit_queued_hangup_after_reply(
        self,
        *,
        generation: int,
        reply_token: str | None,
        playback_interrupted: bool,
        wrote_output: bool,
    ) -> None:
        """在本轮 PCM 写出后执行一次挂断收口；当前实现为 OneBot 本地结束。"""

        token = str(reply_token or "").strip()
        pending = self._pending_hangup
        if not token or not isinstance(pending, dict):
            return
        if (
            pending.get("reply_token") != token
            or pending.get("call_id") != str(self._call.get("call_id") or "").strip()
            or pending.get("generation") != generation
            or generation != self._generation
            or self._room_state != "connected"
        ):
            self._discard_hangup_for_reply(token, "电话已失效或挂断请求不再属于当前回复")
            return

        if playback_interrupted or not wrote_output:
            # 模型已经明确调用挂断工具时，后续语音、文字或 TTS 的局部异常
            # 不能把电话重新拉回普通对话。仍执行本地收口，避免继续响应。
            logger.warning(
                "[qq_voice_call] 挂断收口回复未完整播放，仍按模型已登记意图提交挂断: "
                f"interrupted={playback_interrupted} wrote_output={wrote_output}"
            )

        # 先取出意图再等待桥响应，保证异常重入也不会重复提交。
        self._pending_hangup = None
        self._status["hangup_pending"] = False
        control = self.call_control
        if not callable(control):
            logger.warning("[qq_voice_call] 回复已播放完，但没有可用的挂断控制入口")
            return
        self._hangup_control_inflight_token = token
        try:
            result = await _maybe_await(control("hangup"))
        except Exception as error:  # noqa: BLE001
            if self._hangup_control_inflight_token == token:
                self._hangup_control_inflight_token = None
            self._call["last_control_action"] = "hangup"
            self._call["last_control_status"] = "failed"
            logger.warning(f"[qq_voice_call] 回复播放完成后的挂断提交失败：{error}")
            return
        status = str(result.get("status") or "failed").strip().casefold() if isinstance(result, dict) else "failed"
        submitted = bool(
            isinstance(result, dict)
            and result.get("success")
            and status in {"submitted", "succeeded"}
        )
        locally_ended = bool(
            isinstance(result, dict)
            and result.get("success")
            and status in {"locally_ended", "already_locally_ended"}
        )
        self._call["last_control_action"] = "hangup"
        self._call["last_control_status"] = status
        if self._hangup_control_inflight_token == token:
            self._hangup_control_inflight_token = None
        if not submitted and not locally_ended:
            logger.warning(
                "[qq_voice_call] 回复已播放完，但 QQ 挂断未提交："
                f"status={str(result.get('status') if isinstance(result, dict) else result)}"
            )
            return
        if locally_ended:
            self._hangup_control_token = token
            logger.info(
                "[qq_voice_call] 电话回复已完整播放，OneBot 已结束本地电话房间；"
                "远端 QQ 状态仅保留为通话页提示"
            )
            return
        # 只有桥明确返回 submitted/succeeded 时，才记录成功 token；失败后
        # 下一次模型回复仍可显式重试，不会收到虚假的 already_submitted。
        self._hangup_control_token = token
        self._call["last_control_status"] = "submitted"
        if playback_interrupted or not wrote_output:
            logger.info(
                "[qq_voice_call] 电话收口结束语未完整播放，仍已提交 QQ 挂断请求，"
                "等待 ended 信令"
            )
        else:
            logger.info("[qq_voice_call] 电话回复已完整播放，已提交 QQ 挂断请求，等待 ended 信令")

    def _resolve_end_actor(self, reason: str) -> tuple[str, str]:
        """归因挂断方，并同时保留结论的可审计依据。

        当前 NapCat 桥不会稳定上报结束者。对于正常结束的入站电话，只有在
        没有本机成功提交挂断命令时，才按来电方结束推断；桥断开和异常不能冒充
        任一方主动挂断。
        """

        if str(reason or "").strip().casefold() == "local_hangup":
            return "本地助手", "onebot_local_hangup"

        caller_uin, caller_name = self._caller_identity()
        explicit_actor = str(
            self._call.get("end_actor")
            or self._call.get("ended_by")
            or self._call.get("endActor")
            or self._call.get("endedBy")
            or ""
        ).strip().casefold()
        end_reason = str(self._call.get("end_reason") or reason or "").strip().casefold()
        local_markers = {"ai", "local", "self", "hangup_by_self", "bot", "assistant", "local_assistant"}
        remote_markers = {"remote", "caller", "peer", "hangup_by_peer"}

        if explicit_actor in local_markers:
            return "本地助手", "explicit_end_actor"
        if explicit_actor in remote_markers or explicit_actor in {
            caller_uin.casefold(),
            caller_name.casefold(),
        }:
            return caller_name, "explicit_end_actor"
        if end_reason in local_markers:
            return "本地助手", "explicit_end_reason"
        if end_reason in remote_markers:
            return caller_name, "explicit_end_reason"

        last_action = str(self._call.get("last_control_action") or "").strip().casefold()
        last_status = str(self._call.get("last_control_status") or "").strip().casefold()
        if last_action == "hangup" and last_status == "submitted":
            return "本地助手", "local_hangup_control_submitted"

        system_end_reasons = {
            "bridge_disconnected",
            "deactivated",
            "error",
            "runtime_stopped",
            "shutdown",
            "transport_lost",
        }
        has_caller_identity = bool(
            caller_uin or caller_name not in {"", "对方", "QQ联系人"}
        )
        if (
            str(reason or "").strip().casefold() == "ended"
            and has_caller_identity
            and end_reason not in system_end_reasons
        ):
            return caller_name, "inferred_from_inbound_terminal_event"
        return "unknown/system", "unresolved"

    def _ended_by(self, reason: str) -> str:
        """返回兼容旧调用方的挂断方文本。"""

        return self._resolve_end_actor(reason)[0]

    def _summary_transcript_text(self) -> str:
        """将本通 transcript 转为摘要模型可消费的文本，避免直接写入长期私聊。"""

        if self._transcript is None:
            return ""
        lines = []
        for entry in self._transcript.entries:
            kind = str(entry.get("kind") or "")
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            if kind == "assistant":
                who = "本地助手"
            else:
                who = str(entry.get("speaker_name") or self._call.get("caller_name") or "对方")
            suffix = "（播放被打断）" if entry.get("playback_interrupted") else ""
            suffix += "（硬性打断，不回复）" if entry.get("hard_interrupt") else ""
            lines.append(f"{who}：{text}{suffix}")
        return "\n".join(lines)[-12000:]

    async def _run_summary_after_end(
        self,
        *,
        call_id: str,
        caller_uin: str,
        caller_name: str,
        started_at: float,
        ended_at: float,
        ended_by: str,
        transcript_text: str,
    ) -> None:
        """在媒体清理后总结电话并把摘要事件回流普通 QQ 私聊。"""

        summary_cfg = self.config.get("summary") or {}
        try:
            if not bool(summary_cfg.get("enabled", True)) or not transcript_text:
                return
            ai = self._current_ai_service()
            generate = getattr(ai, "generate_text", None)
            if not callable(generate):
                logger.warning("[qq_voice_call] 电话总结跳过：OneBot 文本模型服务未注册")
                return
            summary_prompt = "\n".join(
                [
                    "<qq_voice_call>",
                    f"这是一通已结束的 QQ 私聊语音通话。联系人：{caller_name}。",
                    f"通话时间：{_format_call_time(started_at)} 到 {_format_call_time(ended_at)}。",
                    f"挂断方：{ended_by}。请基于以下真实 transcript 总结本次通话。",
                    "</qq_voice_call>",
                    transcript_text,
                ]
            )
            options = await self._build_request_options(
                ai,
                message_kind="qq_voice_call_summary",
                disable_tools=True,
            )
            options.update(
                {
                    "stable_system_prompt": str(summary_cfg.get("system_prompt") or ""),
                    "persist_session": False,
                    "persist_user_message": False,
                }
            )
            summary = await _maybe_await(
                generate(
                    username=caller_name,
                    prompt=summary_prompt,
                    identity_context=self._identity_context(
                        text=summary_prompt,
                        caller_uin=caller_uin,
                        caller_name=caller_name,
                    ),
                    session_scope=self._session_scope(call_id),
                    request_options=options,
                    timeout=float((self.config.get("llm") or {}).get("timeout", 60)),
                )
            )
            summary = str(summary or "").strip()
            if not summary:
                logger.warning("[qq_voice_call] 电话总结模型未返回内容，跳过私聊回流")
                return
            template = str(summary_cfg.get("writeback_template") or "")
            values = _TemplateValues(
                started_at=_format_call_time(started_at),
                ended_at=_format_call_time(ended_at),
                caller_name=caller_name,
                caller_uin=caller_uin,
                duration_seconds=_format_elapsed(ended_at - started_at),
                ended_by=ended_by,
                summary=summary,
            )
            writeback = template.format_map(values).strip() if template else summary
            if not writeback:
                return
            private_options = await self._build_request_options(
                ai,
                message_kind="qq_voice_call_summary_writeback",
                include_telephone_context=False,
            )
            final_text = await _maybe_await(
                generate(
                    username=caller_name,
                    prompt=writeback,
                    identity_context=self._identity_context(
                        text=writeback,
                        caller_uin=caller_uin,
                        caller_name=caller_name,
                    ),
                    session_scope=self._source_session_scope(caller_uin),
                    request_options=private_options,
                    timeout=float((self.config.get("llm") or {}).get("timeout", 60)),
                )
            )
            final_text = str(final_text or "").strip()
            if final_text:
                sender = getattr(ai, "send_private_text", None)
                if callable(sender):
                    submitted = bool(
                        await _maybe_await(
                            sender(
                                caller_uin,
                                final_text,
                                reason="call_summary",
                                bot_self_id=self._bot_self_id(),
                            )
                        )
                    )
                    logger.info(
                        "[qq_voice_call] 电话总结后的私聊结束语"
                        f"{'已提交' if submitted else '未提交'}: user={caller_uin}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[qq_voice_call] 电话结束总结失败，不阻塞房间释放：{error}")
        finally:
            if self._room_call_id == call_id:
                self._set_room_state("ended")
                self._room_call_id = None
            self._summary_task = None

    async def stop(self, reason: str = "ended") -> None:
        """停止媒体、记录电话连续性，并在媒体释放后异步执行结束总结。"""

        call_id = str(self._status.get("call_id") or self._call.get("call_id") or "").strip() or None
        if not call_id:
            return
        if self._room_state == "summarizing" and self._room_call_id == call_id:
            return
        connected_once = self._connected_at is not None
        task = self._task
        admission_task = self._admission_task
        self._set_room_state("ending")
        self._generation += 1
        self._discard_hangup_for_reply(None, "电话已结束")
        self._active_reply_request = None
        self._hangup_control_token = None
        self._hangup_control_inflight_token = None
        self._thread_stop_event.set()
        if self._capture_stop_event is not None:
            self._capture_stop_event.set()
        if self._stop_event is not None:
            self._stop_event.set()
        if self._interrupt_event is not None:
            self._interrupt_event.set()
        source = self._active_tts_source
        cancel = getattr(source, "cancel", None)
        if callable(cancel):
            try:
                cancel(reason)
            except Exception:
                pass
        if admission_task is not None and not admission_task.done() and admission_task is not asyncio.current_task():
            admission_task.cancel()
            await asyncio.gather(admission_task, return_exceptions=True)
        preparing = self._preparation_task is not None and not self._preparation_task.done()
        if preparing or task is not None and not task.done():
            self._set_phase("stopping")
        if task is not None and not task.done() and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._task = None
        await self._finish_queued_private_text_world_events("电话已结束，未处理的通话文字已跳过")
        try:
            self.media.close()
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[qq_voice_call] 关闭电话媒体失败：{error}")
        await self._restore_route(call_id)
        self._prepared_call_id = None
        self._prepared_opening = None
        self._accept_requested_call_id = None
        self._status["opening_ready"] = False
        ended_at = time.time()
        started_at = self._connected_at or self._room_started_at or ended_at
        caller_uin, caller_name = self._caller_identity()
        ended_by, ended_by_evidence = self._resolve_end_actor(reason)
        logger.info(
            "[qq_voice_call] 挂断方归因: "
            f"actor={ended_by} evidence={ended_by_evidence} reason={reason}"
        )
        if connected_once and caller_uin:
            try:
                self.call_history.record_end(
                    caller_uin=caller_uin,
                    caller_name=caller_name,
                    call_id=call_id,
                    started_at=started_at,
                    ended_at=ended_at,
                    ended_by=ended_by,
                )
            except Exception as error:  # noqa: BLE001
                logger.warning(f"[qq_voice_call] 写入电话历史失败：{error}")
        if self._transcript is not None:
            self._transcript.set_metadata(
                ended_at=ended_at,
                ended_by=ended_by,
                ended_by_evidence=ended_by_evidence,
                end_reason=reason,
            )
            self._transcript.save("ended")
        self._set_phase("error" if reason == "error" else "ended")
        transcript_text = self._summary_transcript_text()
        should_summarize = connected_once and reason in {"ended", "transport_lost", "local_hangup"}
        if should_summarize:
            self._set_room_state("summarizing")
            self._summary_task = asyncio.create_task(
                self._run_summary_after_end(
                    call_id=call_id,
                    caller_uin=caller_uin,
                    caller_name=caller_name,
                    started_at=started_at,
                    ended_at=ended_at,
                    ended_by=ended_by,
                    transcript_text=transcript_text,
                ),
                name=f"QQVoiceSummary-{call_id}",
            )
        else:
            self._set_room_state("ended")
            if self._room_call_id == call_id:
                self._room_call_id = None

    def _write_utterance(self, utterance: AudioUtterance, call_id: str, index: int) -> str:
        """保存本轮 WAV，供 SenseVoice 和声纹共用同一文件。"""

        if utterance.wav_path and os.path.isfile(utterance.wav_path):
            return utterance.wav_path
        root = DEFAULT_RECORDING_ROOT / call_id
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"utterance_{index:05d}.wav"
        with wave.open(str(path), "wb") as file:
            file.setnchannels(int(utterance.channels))
            file.setsampwidth(int(utterance.sample_width))
            file.setframerate(int(utterance.sample_rate))
            file.writeframes(utterance.pcm)
        return str(path)

    def _build_speech_perception(
        self,
        transcription: SenseVoiceTranscription,
        *,
        is_interruption: bool,
    ) -> str:
        """把感知标签与本轮插话状态收敛为单个可读的 speech_perception 块。"""

        speech_cfg = self.config.get("speech_perception") or {}
        raw = build_speech_perception_prompt(transcription.perception(speech_cfg), speech_cfg).strip()
        prefix = "<speech_perception>"
        suffix = "</speech_perception>"
        if raw.startswith(prefix) and raw.endswith(suffix):
            body = raw[len(prefix):-len(suffix)].strip()
        else:
            body = raw or "本次语音未提供额外的情绪或声音事件标签。"
        body = body.replace(
            "这是本次语音输入的感知结果，只用于理解说话人，不代表助手自身情绪；自然参考即可，不要朗读或解释这些标签。",
            "",
        ).strip()
        if self._last_reply_at is None:
            elapsed_text = "距离上次回复：本通电话尚未有上一轮回复"
        else:
            elapsed_text = f"距离上次回复已过去{_format_elapsed(time.monotonic() - self._last_reply_at)}"
        body = " ".join(
            part.rstrip("。")
            for part in (
                body,
                elapsed_text,
                f"当前对方是否属于插话：{'是' if is_interruption else '否'}",
                "这是本次语音输入的感知结果，只用于理解说话人，不代表助手自身情绪；自然参考即可，不要朗读或解释这些标签",
            )
            if str(part or "").strip()
        )
        return f"{prefix}{body}。</speech_perception>"

    def _build_prompt(
        self,
        transcription: SenseVoiceTranscription,
        speaker: dict[str, Any],
        *,
        is_interruption: bool = False,
    ) -> str:
        """包装当前电话语音，使模型看到私密场景、说话人和感知边界。"""

        speaker_name = str(speaker.get("name") or "电话参与者")
        return "\n".join(
            [
                "<qq_voice_call>",
                "当前是已经接通的 QQ 私聊语音通话；"
                f"当前时间：{_format_call_time(time.time())}；当前说话人：{speaker_name}。",
                "正常回复只会通过电话音频播放，不要额外发送 QQ 文字或普通语音消息。",
                "</qq_voice_call>",
                f"听到{speaker_name}在电话中说：{transcription.text}",
                self._build_speech_perception(transcription, is_interruption=is_interruption),
            ]
        )

    def _session_scope(self, call_id: str) -> str:
        """生成按 call_id 隔离的电话上下文作用域，不回写普通 QQ 私聊窗口。"""

        caller_uin = str(self._call.get("caller_uin") or "unknown")
        return f"qq:voice_call:{caller_uin}:{str(call_id or 'unknown')}"

    async def _resolve_voiceprint(
        self,
        wav_path: str,
        duration: float,
        first: bool,
        generation: int,
    ) -> dict[str, Any]:
        """在工作线程中执行 CAM++ 匹配或全局声纹登记。"""

        caller_uin = str(self._call.get("caller_uin") or "")
        caller_name = str(self._call.get("caller_name") or caller_uin or "QQ联系人")
        resolver = self.voiceprint
        if resolver is None:
            return {"speaker_id": None, "name": caller_name, "role": "participant", "matched": False, "enrolled": False}
        method = getattr(resolver, "resolve", None)
        if not callable(method):
            return {"speaker_id": None, "name": caller_name, "role": "participant", "matched": False, "enrolled": False}
        return await asyncio.to_thread(
            method,
            wav_path,
            caller_uin=caller_uin,
            caller_name=caller_name,
            duration=duration,
            first_utterance=first,
            is_active=lambda: self._is_current_generation(generation),
        )

    def _is_current_generation(self, generation: int) -> bool:
        """确认当前结果仍属于未结束的这通电话。"""

        return (
            generation == self._generation
            and self._stop_event is not None
            and not self._stop_event.is_set()
        )

    async def _transcribe(self, wav_path: str) -> Any:
        """在线程中运行同步 ASR，避免模型冷启动阻塞电话状态事件。"""

        method = getattr(self.asr, "transcribe", None)
        if not callable(method):
            raise RuntimeError("QQ 电话 ASR 未提供 transcribe 方法")
        result = await asyncio.to_thread(method, wav_path)
        return await _maybe_await(result)

    async def _iter_tts_chunks(self, source: Any) -> AsyncIterator[PcmChunk]:
        """统一 WAV 路径、异步迭代器、CosyVoice PCM 源和测试列表。"""

        if source is None:
            return
        if isinstance(source, (str, os.PathLike)):
            with wave.open(os.fspath(source), "rb") as file:
                rate = file.getframerate()
                channels = file.getnchannels()
                width = file.getsampwidth()
                while True:
                    pcm = file.readframes(2048)
                    if not pcm:
                        break
                    yield PcmChunk(pcm, rate, channels, width)
            return
        if hasattr(source, "__aiter__"):
            async for item in source:
                yield self._coerce_chunk(item)
            return
        if hasattr(source, "read_frames"):
            self._active_tts_source = source
            rate = int(getattr(source, "sample_rate", 24000))
            channels = int(getattr(source, "channels", 1))
            try:
                while True:
                    pcm = await asyncio.to_thread(source.read_frames, 2048, 0.25)
                    if pcm:
                        yield PcmChunk(pcm, rate, channels, int(getattr(source, "sample_width", 2)))
                    buffered_frames = int(getattr(source, "buffered_frames", 0) or 0)
                    # 生产端结束并不代表本地缓冲已经播放完；两者都满足时
                    # 才退出，否则长回复会只留下开头几字。
                    if source.is_done() and buffered_frames <= 0:
                        break
                    if not pcm:
                        await asyncio.sleep(0)
            finally:
                if self._active_tts_source is source:
                    self._active_tts_source = None
            return
        if isinstance(source, Iterable) and not isinstance(source, (bytes, bytearray)):
            for item in source:
                yield self._coerce_chunk(item)
            return
        if isinstance(source, (bytes, bytearray)):
            yield PcmChunk(bytes(source), 24000, 1, 2)

    @staticmethod
    def _coerce_chunk(item: Any) -> PcmChunk:
        """把测试服务或 TTS 适配器返回的常见结构转为 PCM 块。"""

        if isinstance(item, PcmChunk):
            return item
        if isinstance(item, dict):
            return PcmChunk(
                bytes(item.get("pcm") or item.get("data") or b""),
                int(item.get("sample_rate", 24000)),
                int(item.get("channels", 1)),
                int(item.get("sample_width", 2)),
            )
        if isinstance(item, tuple):
            pcm = bytes(item[0] if item else b"")
            return PcmChunk(pcm, int(item[1]) if len(item) > 1 else 24000, int(item[2]) if len(item) > 2 else 1, 2)
        return PcmChunk(bytes(item or b""), 24000, 1, 2)

    @staticmethod
    def _coerce_utterance(value: Any) -> AudioUtterance | None:
        """将媒体替身或真实采集器的常见返回值统一为 AudioUtterance。"""

        if value is None:
            return None
        if isinstance(value, AudioUtterance):
            return value
        if isinstance(value, dict):
            raw_pcm = value.get("pcm") or value.get("data") or b""
            rate = value.get("sample_rate", 16000)
            channels = value.get("channels", 1)
            width = value.get("sample_width", 2)
        else:
            raw_pcm = getattr(value, "pcm", b"")
            rate = getattr(value, "sample_rate", 16000)
            channels = getattr(value, "channels", 1)
            width = getattr(value, "sample_width", 2)
        if not raw_pcm:
            return None
        return AudioUtterance(bytes(raw_pcm), int(rate), int(channels), int(width))

    def _remote_monitor_callback(self) -> Callable[[bytes, int, int], None] | None:
        """在用户显式开启本地监听时生成对端 PCM 副本回调。"""

        monitor_cfg = self.config.get("monitor") or {}
        if not (monitor_cfg.get("enabled") and monitor_cfg.get("remote_audio", True)):
            return None
        return lambda pcm, rate, channels: self.media.write_monitor(
            pcm,
            rate,
            channels,
            float(monitor_cfg.get("volume", 1.0)),
        )

    async def _capture_once(self, generation: int) -> AudioUtterance | None:
        """进行一次可单独取消的电话输入采集，不改变整通电话的 stop event。"""

        if not self._is_current_generation(generation):
            return None
        stop_event = threading.Event()
        self._capture_stop_event = stop_event
        try:
            raw = await asyncio.to_thread(
                self.media.capture_utterance,
                stop_event,
                self._remote_monitor_callback(),
            )
        finally:
            if self._capture_stop_event is stop_event:
                self._capture_stop_event = None
        if not self._is_current_generation(generation):
            return None
        return self._coerce_utterance(raw)

    async def _prepare_audio_turn(
        self,
        utterance: AudioUtterance,
        generation: int,
        *,
        is_interruption: bool = False,
    ) -> PreparedCallTurn | None:
        """完成 ASR/声纹观察，但把是否调用模型留给上层媒体仲裁。"""

        call_id = str(self._call.get("call_id") or "")
        if not call_id or not utterance or not self._is_current_generation(generation):
            return None
        segment_ready_at = time.monotonic()
        self._turn_index += 1
        wav_path = await asyncio.to_thread(
            self._write_utterance,
            utterance,
            call_id,
            self._turn_index,
        )
        if not self._is_current_generation(generation):
            return None
        self._set_phase("recognizing")
        asr_started_at = time.monotonic()
        transcription = await self._transcribe(wav_path)
        asr_finished_at = time.monotonic()
        if not self._is_current_generation(generation):
            logger.info("[qq_voice_call] 通话已结束，丢弃迟到的识别结果")
            return None
        if not isinstance(transcription, SenseVoiceTranscription):
            transcription = parse_sensevoice_transcription(
                transcription.get("text", "") if isinstance(transcription, dict) else transcription
            )
        if not transcription.text:
            logger.info("[qq_voice_call] 电话语音识别结果为空，继续监听")
            return None
        preview = " ".join(str(transcription.text).split())
        logger.info(f"[qq_voice_call] 电话语音识别结果: {preview[:160]}{'...' if len(preview) > 160 else ''}")
        first_user_turn = not bool(
            self._transcript and any(item.get("kind") == "user" for item in self._transcript.entries)
        )
        voiceprint_started_at = time.monotonic()
        speaker = await self._resolve_voiceprint(
            wav_path,
            utterance.duration,
            first=first_user_turn,
            generation=generation,
        )
        voiceprint_finished_at = time.monotonic()
        if not self._is_current_generation(generation):
            logger.info("[qq_voice_call] 通话已结束，丢弃迟到的声纹结果")
            return None
        self._status["speaker"] = copy.deepcopy(speaker)
        self._status["last_text"] = transcription.text
        return PreparedCallTurn(
            utterance=utterance,
            wav_path=wav_path,
            transcription=transcription,
            speaker=copy.deepcopy(speaker),
            is_interruption=is_interruption,
            segment_ready_at=segment_ready_at,
            asr_started_at=asr_started_at,
            asr_finished_at=asr_finished_at,
            voiceprint_started_at=voiceprint_started_at,
            voiceprint_finished_at=voiceprint_finished_at,
        )

    def _record_reply_latency(
        self,
        timing: dict[str, Any] | None,
        output_written_at: float,
    ) -> None:
        """记录电话首个 PCM 已写入后的分段时长，保持历史状态字段兼容。"""

        if not isinstance(timing, dict):
            return

        def elapsed(start: Any, end: Any) -> int | None:
            try:
                start_value = float(start)
                end_value = float(end)
            except (TypeError, ValueError):
                return None
            if start_value <= 0 or end_value < start_value:
                return None
            return round((end_value - start_value) * 1000)

        segment_ready_at = timing.get("segment_ready_at")
        llm_started_at = timing.get("llm_started_at")
        tts_started_at = timing.get("tts_started_at")
        latency: dict[str, Any] = {
            "input_to_output": elapsed(segment_ready_at, output_written_at),
            "llm": elapsed(llm_started_at, timing.get("llm_finished_at")),
            "tts_and_output": elapsed(tts_started_at, output_written_at),
            "input_kind": str(timing.get("input_kind") or "voice"),
        }
        if latency["input_kind"] == "voice":
            # 这两个字段名保持第二阶段 WebUI/日志与已有排障习惯兼容。
            latency["segment_to_output"] = latency["input_to_output"]
            latency["asr"] = elapsed(timing.get("asr_started_at"), timing.get("asr_finished_at"))
            latency["voiceprint"] = elapsed(
                timing.get("voiceprint_started_at"),
                timing.get("voiceprint_finished_at"),
            )
            latency["vad_end_silence_config"] = int(
                (self.config.get("vad") or {}).get("max_end_silence", 700)
            )
        self._status["last_latency_ms"] = latency
        if latency["input_kind"] == "voice":
            logger.info(
                "[qq_voice_call] 电话回复首响耗时: "
                f"分段后到输出={latency['segment_to_output']}ms "
                f"ASR={latency['asr']}ms 声纹={latency['voiceprint']}ms "
                f"模型={latency['llm']}ms TTS首包与写入={latency['tts_and_output']}ms "
                f"VAD结束静默配置={latency['vad_end_silence_config']}ms"
            )

    def _is_meaningful_interruption(self, turn: PreparedCallTurn) -> bool:
        """过滤短噪声和单独语气词，避免无意义声音反复打断电话回复。"""

        interaction = self.config.get("interaction") or {}
        if turn.utterance.duration < float(interaction.get("minimum_interrupt_duration", 0.5)):
            return False
        normalized = "".join(str(turn.transcription.text or "").split())
        if len(normalized) < int(interaction.get("minimum_interrupt_chars", 2)):
            return False
        cleaned = re.sub(r"[，。！？!?、~～…\-]+", "", normalized).strip()
        fillers = {
            re.sub(r"[，。！？!?、~～…\-]+", "", str(value or "")).strip()
            for value in (interaction.get("filler_words") or [])
        }
        return bool(cleaned and cleaned not in fillers)

    def _match_hard_interrupt(self, text: str) -> str | None:
        """按电话 Tab 配置识别硬性打断词。"""

        interaction = self.config.get("interaction") or {}
        content = str(text or "").strip()
        if not content:
            return None
        exact = str(interaction.get("hard_interrupt_match_mode") or "contains") == "exact"
        folded = content.casefold()
        for value in interaction.get("hard_interrupt_keywords") or []:
            keyword = str(value or "").strip()
            if not keyword:
                continue
            if (folded == keyword.casefold()) if exact else (keyword.casefold() in folded):
                return keyword
        return None

    def _record_user_turn(self, turn: PreparedCallTurn, *, source: str, hard_keyword: str | None = None) -> None:
        """将真实电话输入写入诊断 transcript，硬打断也不能丢失。"""

        if self._transcript is None:
            return
        self._transcript.add(
            "user",
            turn.transcription.text,
            speaker_id=turn.speaker.get("speaker_id"),
            speaker_name=turn.speaker.get("name"),
            perception=turn.transcription.perception(self.config.get("speech_perception") or {}),
            source=source,
            interruption=bool(turn.is_interruption),
            hard_interrupt=bool(hard_keyword),
            hard_interrupt_keyword=hard_keyword,
        )
        self._transcript.save("recognizing")

    def _record_text_turn(self, item: PendingPrivateText) -> None:
        """把通话期间的普通私聊文字保留在同一电话 transcript。"""

        if self._transcript is None:
            return
        self._transcript.add(
            "user",
            item.text,
            speaker_name=item.caller_name,
            source="qq_private_text",
        )
        self._transcript.save("thinking")

    async def _finish_world_task(
        self,
        ai: Any,
        world_task: Any,
        *,
        result: str,
        reply: str = "",
        reason: str = "",
    ) -> None:
        """结束电话输入关联的世界事件；异常不能打断媒体生命周期。"""

        if world_task is None:
            return
        finish = getattr(ai, "finish_world_event", None)
        if not callable(finish):
            return
        try:
            await _maybe_await(finish(world_task, result=result, reply=reply, reason=reason))
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[qq_voice_call] 电话世界事件收尾失败：{error}")

    async def _finish_queued_private_text_world_events(self, reason: str) -> None:
        """释放挂断时尚未消费的电话文字事件，避免它们占住私聊世界队列。"""

        ai = self._current_ai_service()
        while True:
            try:
                item = self._private_text_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await self._finish_world_task(
                ai,
                item.world_task,
                result="skipped",
                reason=reason,
            )

    async def _generate_phone_reply(
        self,
        *,
        prompt: str,
        identity_context: dict[str, Any],
        message_kind: str,
        world_task: Any = None,
    ) -> str:
        """按普通 QQ 私聊的身份/世界上下文生成电话回复，不发送平台文字。"""

        ai = self._current_ai_service()
        generate = getattr(ai, "generate_text", None)
        if not callable(generate):
            raise RuntimeError("OneBot 文本模型服务尚未注册")
        request_generation = self._generation
        reply_token: str | None = None
        world_task_finished = False

        async def finish_once(*, result: str, reply: str = "", reason: str = "") -> None:
            """确保一个电话输入只释放一次世界事件处理权。"""

            nonlocal world_task_finished
            if world_task_finished:
                return
            await self._finish_world_task(
                ai,
                world_task,
                result=result,
                reply=reply,
                reason=reason,
            )
            world_task_finished = True

        try:
            if not self._is_current_generation(request_generation):
                await finish_once(
                    result="skipped",
                    reason="电话已结束，未开始的模型回复已跳过",
                )
                return ""
            if world_task is not None:
                wait = getattr(ai, "wait_world_turn", None)
                if callable(wait):
                    claimed = await _maybe_await(wait(world_task))
                    if claimed is None:
                        await finish_once(
                            result="skipped",
                            reason="电话输入未取得世界事件处理权",
                        )
                        return ""
            if not self._is_current_generation(request_generation):
                await finish_once(
                    result="skipped",
                    reason="电话已结束，等待世界事件期间的模型回复已跳过",
                )
                return ""
            event_id = str(world_task.get("event_id") or "") if isinstance(world_task, dict) else ""
            options = await self._build_request_options(ai, message_kind=message_kind)
            # 电话挂断是本轮回复的终止动作：工具结果确认“已登记”且模型已经
            # 给出结束语时，Chatgpt 工具循环应直接收口，避免再生成一段解释文本。
            if message_kind in {"qq_voice_call_audio", "qq_voice_call_private_text"}:
                options["terminal_tool_names"] = ("qq_voice_call_hangup",)
            # 工具调用发生在 generate() 内；令牌必须在调用前建立，才能把
            # qq_voice_call_hangup 精确绑定到随后要播放的完整回复。
            reply_token = uuid.uuid4().hex
            self._active_reply_request = {
                "call_id": str(self._call.get("call_id") or "").strip(),
                "caller_uin": str(self._call.get("caller_uin") or "").strip(),
                "generation": request_generation,
                "reply_token": reply_token,
                "message_kind": message_kind,
                "requested_at": time.time(),
            }
            reply = await _maybe_await(
                generate(
                    username=str(self._call.get("caller_name") or self._call.get("caller_uin") or "QQ联系人"),
                    prompt=prompt,
                    identity_context=identity_context,
                    session_scope=self._session_scope(str(self._call.get("call_id") or "")),
                    world_event_id=event_id or None,
                    request_options=options,
                    timeout=float((self.config.get("llm") or {}).get("timeout", 60)),
                )
            )
            if not self._is_current_generation(request_generation):
                # 底层同步/HTTP 模型请求无法总是立即取消。即使它在挂断后返回，
                # 也不能把这轮电话输入当成已经正常处理完成。
                await finish_once(
                    result="skipped",
                    reason="电话已结束，丢弃迟到的模型回复",
                )
                self._discard_hangup_for_reply(reply_token, "模型回复返回时电话已失效")
                if self._active_reply_request and self._active_reply_request.get("reply_token") == reply_token:
                    self._active_reply_request = None
                return ""
            text = str(reply or "").strip()
            text = text[:int((self.config.get("llm") or {}).get("max_reply_chars", 1000))]
            if not text:
                self._discard_hangup_for_reply(reply_token, "模型未返回可播放文本")
                if self._active_reply_request and self._active_reply_request.get("reply_token") == reply_token:
                    self._active_reply_request = None
            await finish_once(
                result="completed" if text else "failed",
                reply=text,
                reason="电话模型未返回回复" if not text else "",
            )
            return text
        except asyncio.CancelledError:
            self._discard_hangup_for_reply(reply_token, "模型回复任务已取消")
            if self._active_reply_request and self._active_reply_request.get("reply_token") == reply_token:
                self._active_reply_request = None
            await finish_once(
                result="skipped",
                reason="电话已结束，取消未完成的模型回复",
            )
            raise
        except Exception as error:  # noqa: BLE001
            self._discard_hangup_for_reply(reply_token, "模型回复生成失败")
            if self._active_reply_request and self._active_reply_request.get("reply_token") == reply_token:
                self._active_reply_request = None
            await finish_once(result="failed", reason=str(error))
            raise

    async def _synthesize_phone_reply(self, text: str) -> Any:
        """使用电话专用 TTS 配置生成待播放 PCM 源。"""

        return await _maybe_await(
            self.tts.synthesize(
                text,
                streaming=bool((self.config.get("tts") or {}).get("streaming", True)),
            )
        )

    async def _mark_reply_interrupted(
        self,
        *,
        reply: str,
        reason: str,
        hard_interrupt: bool = False,
    ) -> None:
        """标记已进入电话分支但未完整播放的 assistant 回复。"""

        marker = "<qq_voice_call>该回复在电话播放期间被对方打断，未完整播放。</qq_voice_call>"
        ai = self._current_ai_service()
        mark = getattr(ai, "mark_assistant_message", None)
        if callable(mark):
            await _maybe_await(
                mark(
                    self._active_reply_scope or self._session_scope(str(self._call.get("call_id") or "")),
                    marker,
                    expected_content=reply,
                    metadata={"playback_interrupted": True, "interrupt_reason": reason},
                )
            )
        if self._transcript is not None:
            self._transcript.update_last(
                "assistant",
                playback_interrupted=True,
                interrupt_reason=reason,
                hard_interrupt=hard_interrupt,
            )
            self._transcript.save("speaking")

    async def _stop_capture_task(self, task: asyncio.Task | None, stop_event: threading.Event | None) -> None:
        """停止播放期的并行采集任务，底层线程会在下一块音频后自行退出。"""

        if stop_event is not None:
            stop_event.set()
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        except Exception:
            pass

    async def _play_reply(
        self,
        *,
        reply: str,
        source: Any,
        generation: int,
        reply_token: str | None = None,
        opening: bool = False,
        timing: dict[str, Any] | None = None,
    ) -> PreparedCallTurn | None:
        """播放一轮电话 TTS，并在播放期并行侦听可验证的对端插话。"""

        active_request = self._active_reply_request
        bound_reply_token = str(
            reply_token
            or (active_request.get("reply_token") if isinstance(active_request, dict) else "")
            or ""
        ).strip() or None
        if source is None:
            if self._has_pending_hangup_for_reply(bound_reply_token):
                # 挂断工具已经被模型确认。没有 PCM 时没有可等待的结束语，
                # 仍必须把真实挂断控制交给桥端，而不能静默遗留通话。
                await self._submit_queued_hangup_after_reply(
                    generation=generation,
                    reply_token=bound_reply_token,
                    playback_interrupted=False,
                    wrote_output=False,
                )
            else:
                self._discard_hangup_for_reply(bound_reply_token, "TTS 没有产生可播放音频")
            if isinstance(active_request, dict) and active_request.get("reply_token") == bound_reply_token:
                self._active_reply_request = None
            return None
        interaction = self.config.get("interaction") or {}
        monitor_cfg = self.config.get("monitor") or {}
        hangup_protected = self._has_pending_hangup_for_reply(bound_reply_token)
        listen_while_speaking = bool(interaction.get("listen_while_speaking", True)) and not hangup_protected
        if hangup_protected:
            logger.info("[qq_voice_call] 挂断收口回复已登记，本轮不再接受插话打断")
        monitor_tts = bool(monitor_cfg.get("enabled") and monitor_cfg.get("tts_audio", True))
        fade_in_ms = int((self.config.get("audio") or {}).get("tts_fade_in_ms", 10))
        first_output_chunk = True
        interrupted_reason = ""
        hard_interrupt = False
        capture_stop: threading.Event | None = None
        capture_task: asyncio.Task | None = None
        prepare_task: asyncio.Task | None = None
        prepared_turn: PreparedCallTurn | None = None
        wrote_output = False
        playback_failed = False
        written_chunks = 0
        written_frames = 0
        hangup_submitted_on_playback_failure = False
        self._active_tts_source = source
        self._active_reply_text = reply
        self._active_reply_scope = self._session_scope(str(self._call.get("call_id") or ""))
        self._interrupt_event = asyncio.Event()

        def is_hangup_protected() -> bool:
            """允许工具登记发生在播放启动前后的任意时刻。"""

            return self._has_pending_hangup_for_reply(bound_reply_token)

        def start_capture() -> tuple[threading.Event, asyncio.Task]:
            event = threading.Event()
            self._capture_stop_event = event
            task = asyncio.create_task(
                asyncio.to_thread(self.media.capture_utterance, event, self._remote_monitor_callback()),
                name="QQVoiceDialogue-InterruptCapture",
            )
            return event, task

        async def observe_capture() -> None:
            nonlocal capture_task, capture_stop, prepare_task, prepared_turn, interrupted_reason, hard_interrupt
            if is_hangup_protected():
                # 已登记挂断时，后续说话不再具备取消结束语的权限；capture
                # 任务会在 finally 统一停止，避免占用音频输入设备。
                return
            if capture_task is not None and capture_task.done() and prepare_task is None:
                try:
                    utterance = self._coerce_utterance(capture_task.result())
                except Exception as error:  # noqa: BLE001
                    logger.warning(f"[qq_voice_call] 播放期电话输入读取失败：{error}")
                    utterance = None
                capture_task = None
                if utterance is not None and self._is_current_generation(generation):
                    prepare_task = asyncio.create_task(
                        self._prepare_audio_turn(utterance, generation, is_interruption=True),
                        name="QQVoiceDialogue-InterruptRecognize",
                    )
                elif listen_while_speaking and self._is_current_generation(generation):
                    capture_stop, capture_task = start_capture()
            if prepare_task is not None and prepare_task.done():
                try:
                    candidate = prepare_task.result()
                except Exception as error:  # noqa: BLE001
                    logger.warning(f"[qq_voice_call] 播放期插话识别失败：{error}")
                    candidate = None
                prepare_task = None
                if candidate is not None and self._is_meaningful_interruption(candidate):
                    keyword = self._match_hard_interrupt(candidate.transcription.text)
                    prepared_turn = replace(
                        candidate,
                        is_interruption=True,
                        hard_interrupt=bool(keyword),
                    )
                    interrupted_reason = "hard_keyword" if keyword else "voice_interjection"
                    hard_interrupt = bool(keyword)
                    cancel = getattr(source, "cancel", None)
                    if callable(cancel):
                        try:
                            cancel(interrupted_reason)
                        except Exception:
                            pass
                elif listen_while_speaking and self._is_current_generation(generation):
                    capture_stop, capture_task = start_capture()

        self._set_phase("speaking")
        try:
            if listen_while_speaking:
                capture_stop, capture_task = start_capture()
            async for chunk in self._iter_tts_chunks(source):
                if not self._is_current_generation(generation):
                    return None
                if self._interrupt_event is not None and self._interrupt_event.is_set():
                    if is_hangup_protected():
                        # enqueue_private_text 已在登记后拒绝新消息；这里仍保留
                        # 竞态保护，不能让先到的事件截断已确认的挂断收口。
                        self._interrupt_event.clear()
                    else:
                        interrupted_reason = "private_text"
                        break
                await observe_capture()
                if prepared_turn is not None and not is_hangup_protected():
                    break
                if not chunk.pcm:
                    continue
                if int(chunk.sample_width) != 2:
                    raise RuntimeError(f"QQ 电话 TTS 只支持 PCM16，当前 sample_width={chunk.sample_width}")
                output_pcm = chunk.pcm
                if first_output_chunk:
                    output_pcm = _fade_in_pcm16(
                        output_pcm,
                        sample_rate=chunk.sample_rate,
                        channels=chunk.channels,
                        fade_in_ms=fade_in_ms,
                    )
                await asyncio.to_thread(self.media.write_output, output_pcm, chunk.sample_rate, chunk.channels)
                wrote_output = True
                written_chunks += 1
                frame_width = max(1, int(chunk.channels) * int(chunk.sample_width))
                written_frames += len(output_pcm) // frame_width
                if first_output_chunk:
                    self._record_reply_latency(timing, time.monotonic())
                if monitor_tts:
                    await asyncio.to_thread(
                        self.media.write_monitor,
                        output_pcm,
                        chunk.sample_rate,
                        chunk.channels,
                        float(monitor_cfg.get("volume", 1.0)),
                    )
                first_output_chunk = False
                await observe_capture()
                if prepared_turn is not None and not is_hangup_protected():
                    break
            stream_cancelled = bool(getattr(source, "cancelled", False))
            stream_error = getattr(source, "error", None)
            if stream_cancelled or stream_error:
                playback_failed = True
                logger.warning(
                    "[qq_voice_call] 电话 TTS 流异常结束: "
                    f"cancelled={stream_cancelled} "
                    f"cancel_reason={str(getattr(source, 'cancel_reason', '') or '')} "
                    f"error_stage={str(getattr(source, 'error_stage', '') or '')} "
                    f"error={str(stream_error or '')} "
                    f"written_chunks={written_chunks} written_frames={written_frames}"
                )
            # 流结束前已经采集到的完整一句话也应作为下一轮输入，避免丢掉尾音插话。
            if not is_hangup_protected():
                await observe_capture()
            if (
                not is_hangup_protected()
                and prepare_task is not None
                and not prepare_task.done()
                and not interrupted_reason
            ):
                try:
                    candidate = await asyncio.wait_for(asyncio.shield(prepare_task), timeout=0.8)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    candidate = None
                if candidate is not None and self._is_meaningful_interruption(candidate):
                    prepared_turn = replace(
                        candidate,
                        is_interruption=True,
                        hard_interrupt=bool(self._match_hard_interrupt(candidate.transcription.text)),
                    )
                    interrupted_reason = "voice_interjection"
                    hard_interrupt = prepared_turn.hard_interrupt
        except Exception as error:  # noqa: BLE001
            playback_failed = True
            if is_hangup_protected():
                logger.warning(
                    "[qq_voice_call] 挂断收口回复播放异常，仍将在清理后提交挂断："
                    f"{error}"
                )
            raise
        finally:
            await self._stop_capture_task(capture_task, capture_stop)
            if self._capture_stop_event is capture_stop:
                self._capture_stop_event = None
            if prepare_task is not None and not prepare_task.done():
                prepare_task.cancel()
            self._active_tts_source = None
            self._interrupt_event = None
            if not self._is_current_generation(generation):
                self._discard_hangup_for_reply(bound_reply_token, "电话 TTS 播放未完整完成")
            elif (playback_failed or not wrote_output) and not is_hangup_protected():
                self._discard_hangup_for_reply(bound_reply_token, "电话 TTS 播放未完整完成")
            if (
                isinstance(self._active_reply_request, dict)
                and self._active_reply_request.get("reply_token") == bound_reply_token
            ):
                self._active_reply_request = None
            if playback_failed and is_hangup_protected():
                hangup_submitted_on_playback_failure = True
                await self._submit_queued_hangup_after_reply(
                    generation=generation,
                    reply_token=bound_reply_token,
                    playback_interrupted=True,
                    wrote_output=wrote_output,
                )

        if interrupted_reason or prepared_turn is not None:
            await self._mark_reply_interrupted(
                reply=reply,
                reason=interrupted_reason or "voice_interjection",
                hard_interrupt=hard_interrupt,
            )
        if not hangup_submitted_on_playback_failure:
            await self._submit_queued_hangup_after_reply(
                generation=generation,
                reply_token=bound_reply_token,
                playback_interrupted=bool(
                    playback_failed or interrupted_reason or prepared_turn is not None
                ),
                wrote_output=wrote_output,
            )
        self._last_reply_at = time.monotonic()
        if opening:
            logger.info("[qq_voice_call] 电话开场白播放完成")
        return prepared_turn

    async def _process_audio_turn(
        self,
        turn: PreparedCallTurn,
        generation: int,
    ) -> PreparedCallTurn | None:
        """将已识别的电话语音写入分支、调用模型并播放回复。"""

        keyword = self._match_hard_interrupt(turn.transcription.text) if turn.hard_interrupt else None
        self._record_user_turn(turn, source="voice", hard_keyword=keyword)
        ai = self._current_ai_service()
        identity = self._identity_context(text=turn.transcription.text)
        identity.update(
            {
                "speaker_id": turn.speaker.get("speaker_id"),
                "speaker_name": turn.speaker.get("name"),
            }
        )
        begin = getattr(ai, "begin_world_event", None)
        world_task = await _maybe_await(begin(identity, turn.transcription.text)) if callable(begin) else None
        if keyword:
            await self._finish_world_task(
                ai,
                world_task,
                result="skipped",
                reason=f"电话硬性打断词：{keyword}",
            )
            logger.info(f"[qq_voice_call] 命中硬性打断词，停止播放且不调用模型: {keyword}")
            return None
        self._set_phase("thinking")
        prompt = self._build_prompt(
            turn.transcription,
            turn.speaker,
            is_interruption=turn.is_interruption,
        )
        llm_started_at = time.monotonic()
        reply = await self._generate_phone_reply(
            prompt=prompt,
            identity_context=identity,
            message_kind="qq_voice_call_audio",
            world_task=world_task,
        )
        reply_token = str(
            (self._active_reply_request or {}).get("reply_token") or ""
        ).strip() or None
        llm_finished_at = time.monotonic()
        if not self._is_current_generation(generation) or not reply:
            return None
        self._status["last_reply"] = reply
        self._status["turn_count"] = int(self._status.get("turn_count") or 0) + 1
        if self._transcript is not None:
            self._transcript.add("assistant", reply, source="voice")
            self._transcript.save("thinking")
        tts_started_at = time.monotonic()
        try:
            source = await self._synthesize_phone_reply(reply)
        except asyncio.CancelledError:
            self._clear_reply_request_for_token(reply_token, "电话 TTS 合成任务已取消")
            raise
        except Exception:
            self._clear_reply_request_for_token(reply_token, "电话 TTS 合成失败")
            raise
        if not self._is_current_generation(generation):
            self._clear_reply_request_for_token(reply_token, "电话 TTS 合成完成时电话已失效")
            return None
        return await self._play_reply(
            reply=reply,
            source=source,
            generation=generation,
            reply_token=reply_token,
            timing={
                "input_kind": "voice",
                "segment_ready_at": turn.segment_ready_at,
                "asr_started_at": turn.asr_started_at,
                "asr_finished_at": turn.asr_finished_at,
                "voiceprint_started_at": turn.voiceprint_started_at,
                "voiceprint_finished_at": turn.voiceprint_finished_at,
                "llm_started_at": llm_started_at,
                "llm_finished_at": llm_finished_at,
                "tts_started_at": tts_started_at,
            },
        )

    async def _process_private_text(
        self,
        item: PendingPrivateText,
        generation: int,
    ) -> PreparedCallTurn | None:
        """处理已分流到电话房间的同联系人普通 QQ 文字。"""

        input_received_at = time.monotonic()
        self._record_text_turn(item)
        prompt = "\n".join(
            [
                "<qq_voice_call>",
                f"当前是已经接通的 QQ 私聊语音通话；当前时间：{_format_call_time(time.time())}。",
                f"当前说话人：{item.caller_name}。以下内容是对方在通话期间发送的普通 QQ 文字，"
                "回复只在电话中播放，不要额外发送 QQ 文字。",
                "</qq_voice_call>",
                f"收到{item.caller_name}发送的文字：{item.text}",
            ]
        )
        identity = self._identity_context(
            text=item.text,
            caller_uin=item.caller_uin,
            caller_name=item.caller_name,
            base=item.identity_context,
        )
        self._set_phase("thinking")
        llm_started_at = time.monotonic()
        reply = await self._generate_phone_reply(
            prompt=prompt,
            identity_context=identity,
            message_kind="qq_voice_call_private_text",
            world_task=item.world_task,
        )
        reply_token = str(
            (self._active_reply_request or {}).get("reply_token") or ""
        ).strip() or None
        llm_finished_at = time.monotonic()
        if not self._is_current_generation(generation) or not reply:
            return None
        self._status["last_reply"] = reply
        self._status["turn_count"] = int(self._status.get("turn_count") or 0) + 1
        if self._transcript is not None:
            self._transcript.add("assistant", reply, source="qq_private_text")
            self._transcript.save("thinking")
        tts_started_at = time.monotonic()
        try:
            source = await self._synthesize_phone_reply(reply)
        except asyncio.CancelledError:
            self._clear_reply_request_for_token(reply_token, "电话文字回复 TTS 合成任务已取消")
            raise
        except Exception:
            self._clear_reply_request_for_token(reply_token, "电话文字回复 TTS 合成失败")
            raise
        if not self._is_current_generation(generation):
            self._clear_reply_request_for_token(reply_token, "电话文字回复 TTS 合成完成时电话已失效")
            return None
        return await self._play_reply(
            reply=reply,
            source=source,
            generation=generation,
            reply_token=reply_token,
            timing={
                "input_kind": "private_text",
                "segment_ready_at": input_received_at,
                "llm_started_at": llm_started_at,
                "llm_finished_at": llm_finished_at,
                "tts_started_at": tts_started_at,
            },
        )

    def _pop_private_text(self) -> PendingPrivateText | None:
        """无等待地取出已到达的电话文字，优先于下一轮语音采集。"""

        try:
            return self._private_text_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _run(self, generation: int) -> None:
        """执行电话房间的语音/文字统一输入循环，并支持 TTS 期间插话。"""

        call_id = str(self._call.get("call_id") or "")
        pending_turn: PreparedCallTurn | None = None
        try:
            opening = self._prepared_opening
            self._prepared_opening = None
            if opening and self._is_current_generation(generation):
                opening_text = str(opening.get("text") or "").strip()
                if opening_text and self._transcript is not None:
                    self._transcript.add("assistant", opening_text, source="opening")
                    self._transcript.save("speaking")
                if opening_text:
                    pending_turn = await self._play_reply(
                        reply=opening_text,
                        source=opening.get("source"),
                        generation=generation,
                        opening=True,
                    )
            while self._is_current_generation(generation):
                item = self._pop_private_text()
                if item is not None:
                    pending_turn = await self._process_private_text(item, generation)
                    continue
                if pending_turn is not None:
                    turn = pending_turn
                    pending_turn = await self._process_audio_turn(turn, generation)
                    continue
                self._set_phase("listening")
                utterance = await self._capture_once(generation)
                if not self._is_current_generation(generation):
                    return
                # enqueue_private_text 会唤醒 capture；先给文字队列优先权。
                if self._private_text_queue.qsize():
                    continue
                if utterance is None:
                    continue
                pending_turn = await self._prepare_audio_turn(utterance, generation)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            self._status["last_error"] = str(error)
            logger.error(f"[qq_voice_call] 电话对话运行时失败：{error}")
            self._set_phase("error", last_error=str(error))
        finally:
            self._active_tts_source = None
            self._capture_stop_event = None
            if self._status.get("phase") == "error":
                try:
                    self.media.close()
                except Exception:
                    pass
                await self._restore_route(call_id)


__all__ = [
    "AudioUtterance",
    "LocalTTSService",
    "PcmChunk",
    "PyAudioMedia",
    "QQVoiceCallTranscript",
    "QQVoiceDialogueRuntime",
    "QQVoiceprintService",
    "SenseVoiceASR",
    "VoicemeeterCallRoute",
]
