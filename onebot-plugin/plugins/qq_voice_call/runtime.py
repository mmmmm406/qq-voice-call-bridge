"""OneBot 侧 QQ 语音通话运行时。

本模块只负责远程 NapCat 桥的连接、配置下发、状态同步和退出清理；
QQ 私聊来电的真实接听动作由虚拟机中的 AVSDK 插件完成。
"""

from __future__ import annotations

import asyncio
import copy
import json
import inspect
import re
import time
import uuid
from typing import Any, Callable

from aiohttp import WSMsgType

from onebot.logger import logger


QQ_NUMBER_PATTERN = re.compile(r"^[1-9]\d{4,19}$")
CALL_PHASES = {
    "idle",
    "ringing",
    "accepting",
    "accepted",
    "connected",
    "ended",
    "error",
}

# 控制动作先在 OneBot 层固定下来，NapCat 端可以按 QQ/AVSDK 版本逐步实现。
CALL_CONTROL_ACTIONS = {"accept", "reject", "hangup", "mute", "unmute"}
CALL_CONTROL_PHASES = {
    "accept": {"ringing"},
    "reject": {"ringing"},
    "hangup": {"ringing", "accepting", "accepted", "connected"},
    "mute": {"accepted", "connected"},
    "unmute": {"accepted", "connected"},
}

MANUAL_HANGUP_CAPTURE_CAPABILITY = "manual_hangup_capture_v1"
AVSDK_VISIBLE_SURFACE_DIAGNOSTIC_CAPABILITY = "avsdk_visible_surface_diagnostic_v1"
AVSDK_VISIBLE_SURFACE_SCHEMA = "qq_voice_call.avsdk_visible_surface.v1"
AVSDK_SERVICE_SURFACE_DIAGNOSTIC_CAPABILITY = "avsdk_service_surface_diagnostic_v1"
AVSDK_SERVICE_SURFACE_SCHEMA = "qq_voice_call.avsdk_service_surface.v1"
AVSDK_STATIC_ARTIFACTS_DIAGNOSTIC_CAPABILITY = "avsdk_static_artifacts_diagnostic_v1"
AVSDK_STATIC_ARTIFACTS_SCHEMA = "qq_voice_call.avsdk_static_artifacts.v1"
AVSDK_VISIBLE_SURFACE_NAME_PATTERN = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]{0,127}$")
AVSDK_VISIBLE_SURFACE_CONTROL_PATTERN = re.compile(
    r"(?:accept|answer|reject|decline|hang(?:up)?|leave|close|quit|end|disconnect|terminate|cancel|room|call|invite)",
    re.IGNORECASE,
)
AVSDK_VISIBLE_SURFACE_ERROR_OPERATIONS = {
    "own_property_names_failed",
    "property_descriptor_failed",
    "prototype_lookup_failed",
}
AVSDK_STATIC_ARTIFACT_IDS = ("avsdk_plugin", "napcat_default_loader")
AVSDK_STATIC_ARTIFACT_STATUSES = {"scanned", "missing", "not_regular_file", "read_failed"}
AVSDK_STATIC_ARTIFACT_ERROR_CODES = {
    "not_found",
    "permission_denied",
    "not_regular_file",
    "read_failed",
    "byte_limit_reached",
}
AVSDK_STATIC_ARTIFACT_KEYWORD_IDS = (
    "on_action_to_avsdk",
    "on_invite_action_to_avsdk",
    "action_type",
    "hangup",
    "reject",
    "leave_room",
    "clear_room",
    "call_terminated",
    "avsdk",
    "cmd_20001",
)

# 电话对话的配置只在本机 OneBot 使用，绝不能混入 ``call_config`` 后
# 下发给虚拟机 NapCat。这里保留完整的嵌套结构，方便后续接入 ASR、
# 声纹、模型和 TTS 时继续扩展而不需要再次改变配置边界。
DIALOGUE_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    # 这里只表示电话 TTS 播放期间仍可监听插话；实际行为由
    # interaction.listen_while_speaking 控制，并不承诺回声消除。
    "mode": "full_duplex",
    "admission": {
        "enabled": True,
        "decision_timeout": 20.0,
        "live_reject_message": "我现在正在直播，暂时不方便接电话，晚点再聊吧。",
        "busy_reject_message": "我现在正在通话，晚点再联系你。",
        "error_reject_message": "我这边暂时没法接通电话，晚点再试吧。",
    },
    "session": {
        "scope": "per_call",
        "recent_qq_messages": 20,
        "persist_transcript": True,
    },
    "interaction": {
        "listen_while_speaking": True,
        "minimum_interrupt_duration": 0.5,
        "minimum_interrupt_chars": 2,
        "hard_interrupt_keywords": ["停", "不要再说了"],
        "hard_interrupt_match_mode": "contains",
        "filler_words": ["嗯", "啊", "呃", "哦", "噢", "唔"],
    },
    "audio": {
        "input_device_index": None,
        "output_device_index": None,
        "rate": 16000,
        "channels": 1,
        "pause_asr_while_speaking": True,
        "isolate_voicemeeter_route": True,
        "tts_fade_in_ms": 10,
    },
    "vad": {
        "enable": True,
        "sensitivity": "high",
        "chunk_size": 1024,
        "max_end_silence": 700,
        "min_speech_duration": 200,
        "max_speech_duration": 60,
    },
    "sensevoice": {
        "asr_model_path": "models/iic/SenseVoiceSmall",
        "vad_model_path": "models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "device": "cuda:0",
        "language": "auto",
        "text_norm": "withitn",
    },
    "preparation": {
        "warm_up_asr": True,
        "warm_up_voiceprint": True,
        "warm_up_tts": True,
    },
    "monitor": {
        "enabled": False,
        "output_device_index": None,
        "remote_audio": True,
        "tts_audio": True,
        "volume": 1.0,
    },
    "voiceprint": {
        "enable": True,
        "enroll_path": "OneBot/data/qq_voice_call/voiceprints",
        "model_path": "CAM++/campplus_cn_common.bin",
        "threshold": 0.35,
        "auto_enroll_unknown": True,
        "minimum_enroll_duration": 3.0,
        "default_name_prefix": "旁边的人",
    },
    "speech_perception": {
        "enable": True,
        "comment_template": "通过QQ语音通话识别出{username}的话：{comment}",
        "include_language": False,
        "include_emotion": True,
        "include_event": True,
        "ignore_events": ["SPEECH"],
        "detail_separator": "；",
    },
    "llm": {
        "model_profile": "inherit_onebot",
        "timeout": 60,
        "max_reply_chars": 1000,
    },
    "tts": {
        "profile": "inherit_main",
        "streaming": True,
    },
    "summary": {
        "enabled": True,
        "system_prompt": "请简洁、准确地总结这次 QQ 私聊语音通话的要点、关系状态、承诺和待跟进事项。只输出总结正文，不要编造没有出现的内容。",
        "writeback_template": "<qq_voice_call>时间：{started_at} 到 {ended_at} 期间与 {caller_name} 进行了语音通话：{summary}。刚刚是 {ended_by} 挂断电话，请向对方回复一句简短自然的结束语。</qq_voice_call>",
    },
}


def _merge_config(target: dict[str, Any], source: Any) -> None:
    """递归合并配置，同时复制未知字段供后续版本使用。"""
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if isinstance(target.get(key), dict) and isinstance(value, dict):
            _merge_config(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _normalize_bool(value: Any, field: str, default: bool) -> bool:
    """只接受真正的布尔值，避免 JSON 字符串 ``"false"`` 被当成 True。"""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是布尔值")
    return value


def _normalize_device_index(value: Any, field: str) -> int | None:
    """规范化可选音频设备索引；空字符串代表使用底层默认设备。"""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是非负整数或空值")
    try:
        index = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} 必须是非负整数或空值") from error
    if index < 0:
        raise ValueError(f"{field} 必须是非负整数或空值")
    return index


def normalize_dialogue_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """校验并规范化 QQ 电话对话配置。

    ``dialogue`` 是本机适配层的配置，不会进入发送给 NapCat 的
    ``activate.config``。已知字段会被转换为稳定类型，未知字段则保留，
    以便以后接入更多感知、模型和音频选项。

    Args:
        config: 插件完整配置，或直接传入 ``dialogue`` 配置对象。

    Returns:
        独立的、可安全修改的对话配置副本。

    Raises:
        ValueError: 已知字段的类型或范围不正确。
    """
    source = config if isinstance(config, dict) else {}
    # 调用方通常传入插件完整配置；直接传入 dialogue 对象也保持可用。
    dialogue_source = source.get("dialogue") if "dialogue" in source or "call" in source else source
    if not isinstance(dialogue_source, dict):
        dialogue_source = {}
    normalized = copy.deepcopy(DIALOGUE_DEFAULT_CONFIG)
    _merge_config(normalized, dialogue_source)

    normalized["enabled"] = _normalize_bool(normalized.get("enabled"), "dialogue.enabled", False)
    mode = str(normalized.get("mode") or "full_duplex").strip().lower()
    if mode not in {"half_duplex", "full_duplex"}:
        raise ValueError("dialogue.mode 必须是 half_duplex 或 full_duplex")
    normalized["mode"] = mode

    admission = normalized.get("admission")
    if not isinstance(admission, dict):
        raise ValueError("dialogue.admission 必须是对象")
    admission["enabled"] = _normalize_bool(
        admission.get("enabled"), "dialogue.admission.enabled", True
    )
    try:
        decision_timeout = float(admission.get("decision_timeout", 20.0))
    except (TypeError, ValueError) as error:
        raise ValueError("dialogue.admission.decision_timeout 必须是数字") from error
    if not 3.0 <= decision_timeout <= 300.0:
        raise ValueError("dialogue.admission.decision_timeout 必须在 3 到 300 秒之间")
    admission["decision_timeout"] = decision_timeout
    for field in ("live_reject_message", "busy_reject_message", "error_reject_message"):
        admission[field] = str(admission.get(field) or "").strip()

    interaction = normalized.get("interaction")
    if not isinstance(interaction, dict):
        raise ValueError("dialogue.interaction 必须是对象")
    interaction["listen_while_speaking"] = _normalize_bool(
        interaction.get("listen_while_speaking"),
        "dialogue.interaction.listen_while_speaking",
        True,
    )
    try:
        minimum_interrupt_duration = float(interaction.get("minimum_interrupt_duration", 0.5))
        minimum_interrupt_chars = int(interaction.get("minimum_interrupt_chars", 2))
    except (TypeError, ValueError) as error:
        raise ValueError("dialogue.interaction 的最小时长和最少字符数类型不正确") from error
    if not 0.0 <= minimum_interrupt_duration <= 30.0:
        raise ValueError("dialogue.interaction.minimum_interrupt_duration 必须在 0 到 30 秒之间")
    if not 1 <= minimum_interrupt_chars <= 100:
        raise ValueError("dialogue.interaction.minimum_interrupt_chars 必须在 1 到 100 之间")
    interaction["minimum_interrupt_duration"] = minimum_interrupt_duration
    interaction["minimum_interrupt_chars"] = minimum_interrupt_chars
    match_mode = str(interaction.get("hard_interrupt_match_mode") or "contains").strip().lower()
    if match_mode not in {"exact", "contains"}:
        raise ValueError("dialogue.interaction.hard_interrupt_match_mode 必须是 exact 或 contains")
    interaction["hard_interrupt_match_mode"] = match_mode
    for field in ("hard_interrupt_keywords", "filler_words"):
        values = interaction.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"dialogue.interaction.{field} 必须是列表")
        normalized_values = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in normalized_values:
                normalized_values.append(text)
        interaction[field] = normalized_values

    session = normalized.get("session")
    if not isinstance(session, dict):
        raise ValueError("dialogue.session 必须是对象")
    scope = str(session.get("scope") or "per_call").strip().lower()
    if scope not in {"per_call", "per_contact"}:
        raise ValueError("dialogue.session.scope 必须是 per_call 或 per_contact")
    session["scope"] = scope
    recent = session.get("recent_qq_messages", 20)
    if isinstance(recent, bool):
        raise ValueError("dialogue.session.recent_qq_messages 必须是整数")
    try:
        recent = int(recent)
    except (TypeError, ValueError) as error:
        raise ValueError("dialogue.session.recent_qq_messages 必须是整数") from error
    if not 0 <= recent <= 200:
        raise ValueError("dialogue.session.recent_qq_messages 必须在 0 到 200 之间")
    session["recent_qq_messages"] = recent
    session["persist_transcript"] = _normalize_bool(
        session.get("persist_transcript"),
        "dialogue.session.persist_transcript",
        True,
    )

    audio = normalized.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("dialogue.audio 必须是对象")
    for field in ("input_device_index", "output_device_index"):
        audio[field] = _normalize_device_index(audio.get(field), f"dialogue.audio.{field}")
    rate = audio.get("rate", 16000)
    channels = audio.get("channels", 1)
    if isinstance(rate, bool) or isinstance(channels, bool):
        raise ValueError("dialogue.audio.rate 和 channels 必须是整数")
    try:
        rate = int(rate)
        channels = int(channels)
    except (TypeError, ValueError) as error:
        raise ValueError("dialogue.audio.rate 和 channels 必须是整数") from error
    if not 8000 <= rate <= 96000:
        raise ValueError("dialogue.audio.rate 必须在 8000 到 96000 之间")
    if not 1 <= channels <= 8:
        raise ValueError("dialogue.audio.channels 必须在 1 到 8 之间")
    audio["rate"] = rate
    audio["channels"] = channels
    audio["pause_asr_while_speaking"] = _normalize_bool(
        audio.get("pause_asr_while_speaking"),
        "dialogue.audio.pause_asr_while_speaking",
        True,
    )
    audio["isolate_voicemeeter_route"] = _normalize_bool(
        audio.get("isolate_voicemeeter_route"),
        "dialogue.audio.isolate_voicemeeter_route",
        True,
    )
    try:
        tts_fade_in_ms = int(audio.get("tts_fade_in_ms", 10))
    except (TypeError, ValueError) as error:
        raise ValueError("dialogue.audio.tts_fade_in_ms 必须是整数") from error
    if not 0 <= tts_fade_in_ms <= 50:
        raise ValueError("dialogue.audio.tts_fade_in_ms 必须在 0 到 50 之间")
    audio["tts_fade_in_ms"] = tts_fade_in_ms

    vad = normalized.get("vad")
    if not isinstance(vad, dict):
        raise ValueError("dialogue.vad 必须是对象")
    vad["enable"] = _normalize_bool(vad.get("enable"), "dialogue.vad.enable", True)
    vad["sensitivity"] = str(vad.get("sensitivity") or "high").strip().lower()
    if vad["sensitivity"] not in {"high", "medium", "low"}:
        raise ValueError("dialogue.vad.sensitivity 必须是 high、medium 或 low")
    for field, default, minimum, maximum in (
        ("chunk_size", 1024, 128, 8192),
        ("max_end_silence", 700, 100, 10000),
        ("min_speech_duration", 200, 0, 60000),
        ("max_speech_duration", 60, 1, 3600),
    ):
        value = vad.get(field, default)
        if isinstance(value, bool):
            raise ValueError(f"dialogue.vad.{field} 必须是数字")
        try:
            value = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"dialogue.vad.{field} 必须是数字") from error
        if not minimum <= value <= maximum:
            raise ValueError(f"dialogue.vad.{field} 必须在 {minimum} 到 {maximum} 之间")
        vad[field] = value

    sensevoice = normalized.get("sensevoice")
    if not isinstance(sensevoice, dict):
        raise ValueError("dialogue.sensevoice 必须是对象")
    for field, default in (
        ("asr_model_path", "models/iic/SenseVoiceSmall"),
        ("vad_model_path", "models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"),
        ("device", "cuda:0"),
        ("language", "auto"),
        ("text_norm", "withitn"),
    ):
        sensevoice[field] = str(sensevoice.get(field) or default).strip()

    preparation = normalized.get("preparation")
    if not isinstance(preparation, dict):
        raise ValueError("dialogue.preparation 必须是对象")
    for field in ("warm_up_asr", "warm_up_voiceprint", "warm_up_tts"):
        preparation[field] = _normalize_bool(
            preparation.get(field),
            f"dialogue.preparation.{field}",
            True,
        )

    monitor = normalized.get("monitor")
    if not isinstance(monitor, dict):
        raise ValueError("dialogue.monitor 必须是对象")
    monitor["enabled"] = _normalize_bool(monitor.get("enabled"), "dialogue.monitor.enabled", False)
    monitor["output_device_index"] = _normalize_device_index(
        monitor.get("output_device_index"),
        "dialogue.monitor.output_device_index",
    )
    for field, default in (("remote_audio", True), ("tts_audio", True)):
        monitor[field] = _normalize_bool(monitor.get(field), f"dialogue.monitor.{field}", default)
    try:
        volume = float(monitor.get("volume", 1.0))
    except (TypeError, ValueError) as error:
        raise ValueError("dialogue.monitor.volume 必须是数字") from error
    if not 0.0 <= volume <= 1.0:
        raise ValueError("dialogue.monitor.volume 必须在 0 到 1 之间")
    monitor["volume"] = volume

    voiceprint = normalized.get("voiceprint")
    if not isinstance(voiceprint, dict):
        raise ValueError("dialogue.voiceprint 必须是对象")
    voiceprint["enable"] = _normalize_bool(voiceprint.get("enable"), "dialogue.voiceprint.enable", True)
    voiceprint["auto_enroll_unknown"] = _normalize_bool(
        voiceprint.get("auto_enroll_unknown"),
        "dialogue.voiceprint.auto_enroll_unknown",
        True,
    )
    # 旧字段会把来电人旁边已登记的人误绑定为账号主人，当前规则固定为：
    # 只有首次未知声音按联系人昵称登记时才建立主声纹绑定。
    voiceprint.pop("use_caller_nickname_for_first_match", None)
    for field in ("enroll_path", "model_path", "default_name_prefix"):
        voiceprint[field] = str(voiceprint.get(field) or "").strip()
    try:
        threshold = float(voiceprint.get("threshold", 0.35))
        minimum_duration = float(voiceprint.get("minimum_enroll_duration", 3.0))
    except (TypeError, ValueError) as error:
        raise ValueError("dialogue.voiceprint.threshold 和 minimum_enroll_duration 必须是数字") from error
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("dialogue.voiceprint.threshold 必须在 0 到 1 之间")
    if not 0.0 <= minimum_duration <= 3600.0:
        raise ValueError("dialogue.voiceprint.minimum_enroll_duration 必须在 0 到 3600 秒之间")
    voiceprint["threshold"] = threshold
    voiceprint["minimum_enroll_duration"] = minimum_duration

    speech = normalized.get("speech_perception")
    if not isinstance(speech, dict):
        raise ValueError("dialogue.speech_perception 必须是对象")
    speech["enable"] = _normalize_bool(speech.get("enable"), "dialogue.speech_perception.enable", True)
    for field, default in (("include_language", False), ("include_emotion", True), ("include_event", True)):
        speech[field] = _normalize_bool(speech.get(field), f"dialogue.speech_perception.{field}", default)
    speech["comment_template"] = str(speech.get("comment_template") or "").strip()
    speech["detail_separator"] = str(speech.get("detail_separator") or "；")
    ignore_events = speech.get("ignore_events", [])
    if not isinstance(ignore_events, list):
        raise ValueError("dialogue.speech_perception.ignore_events 必须是列表")
    speech["ignore_events"] = [str(item).strip() for item in ignore_events if str(item).strip()]

    llm = normalized.get("llm")
    if not isinstance(llm, dict):
        raise ValueError("dialogue.llm 必须是对象")
    llm["model_profile"] = str(llm.get("model_profile") or "inherit_onebot").strip()
    try:
        timeout = float(llm.get("timeout", 60))
        max_reply_chars = int(llm.get("max_reply_chars", 1000))
    except (TypeError, ValueError) as error:
        raise ValueError("dialogue.llm.timeout 和 max_reply_chars 类型不正确") from error
    if not 1.0 <= timeout <= 600.0:
        raise ValueError("dialogue.llm.timeout 必须在 1 到 600 秒之间")
    if not 1 <= max_reply_chars <= 100000:
        raise ValueError("dialogue.llm.max_reply_chars 必须在 1 到 100000 之间")
    llm["timeout"] = timeout
    llm["max_reply_chars"] = max_reply_chars

    tts = normalized.get("tts")
    if not isinstance(tts, dict):
        raise ValueError("dialogue.tts 必须是对象")
    tts["profile"] = str(tts.get("profile") or "inherit_main").strip()
    tts["streaming"] = _normalize_bool(tts.get("streaming"), "dialogue.tts.streaming", True)

    summary = normalized.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("dialogue.summary 必须是对象")
    summary["enabled"] = _normalize_bool(summary.get("enabled"), "dialogue.summary.enabled", True)
    for field in ("system_prompt", "writeback_template"):
        summary[field] = str(summary.get(field) or "").strip()
    return normalized


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化插件配置。

    Args:
        config: ``OneBot/plugins/qq_voice_call/config.json`` 的内容。

    Returns:
        可安全下发给虚拟机 NapCat 插件的配置副本。

    Raises:
        ValueError: 字段类型、号码或数值范围不正确。
    """
    source = config if isinstance(config, dict) else {}
    call = source.get("call") if isinstance(source.get("call"), dict) else {}
    def numbers(name: str) -> list[str]:
        values = call.get(name, [])
        if not isinstance(values, list):
            raise ValueError(f"call.{name} 必须是列表")
        normalized = []
        for value in values:
            number = str(value or "").strip()
            if not QQ_NUMBER_PATTERN.fullmatch(number):
                raise ValueError(f"call.{name} 包含无效 QQ 号：{number or '<空>'}")
            if number not in normalized:
                normalized.append(number)
        return normalized

    return {
        "auto_accept_private": bool(call.get("auto_accept_private", True)),
        "allow_users": numbers("allow_users"),
        "deny_users": numbers("deny_users"),
    }


def _bounded_remote_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """读取远程数值时只接受有界整数，避免状态接口变成任意数据透传。"""

    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _sanitize_visible_surface_method(value: Any) -> dict[str, Any] | None:
    """把一条 Host 方法元数据缩减为 WebUI 可展示的固定字段。"""

    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if not isinstance(name, str) or not AVSDK_VISIBLE_SURFACE_NAME_PATTERN.fullmatch(name):
        return None
    arity_value = value.get("arity")
    arity = None if arity_value is None else _bounded_remote_int(arity_value, 0, 0, 64)
    return {
        "name": name,
        "owner": "own" if value.get("owner") == "own" else "prototype",
        "depth": _bounded_remote_int(value.get("depth"), 0, 0, 12),
        "arity": arity,
        "enumerable": bool(value.get("enumerable")),
        "configurable": bool(value.get("configurable")),
        "writable": bool(value.get("writable")),
    }


def _sanitize_visible_surface_report(value: Any) -> dict[str, Any] | None:
    """白名单化 Host 可见面报告，绝不把 AVSDK 原始对象或环境数据带入 OneBot。"""

    if not isinstance(value, dict) or value.get("schema") != AVSDK_VISIBLE_SURFACE_SCHEMA:
        return None
    callable_methods: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    source_methods = value.get("callableMethods")
    if not isinstance(source_methods, list):
        source_methods = []
    for item in source_methods[:64]:
        method = _sanitize_visible_surface_method(item)
        if method is not None and method["name"] not in seen_names:
            seen_names.add(method["name"])
            callable_methods.append(method)

    accessors: list[dict[str, Any]] = []
    seen_accessors: set[str] = set()
    source_accessors = value.get("controlAccessors")
    if not isinstance(source_accessors, list):
        source_accessors = []
    for item in source_accessors[:16]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if (
            not isinstance(name, str)
            or not AVSDK_VISIBLE_SURFACE_NAME_PATTERN.fullmatch(name)
            or not AVSDK_VISIBLE_SURFACE_CONTROL_PATTERN.search(name)
            or name in seen_accessors
        ):
            continue
        seen_accessors.add(name)
        accessors.append(
            {
                "name": name,
                "owner": "own" if item.get("owner") == "own" else "prototype",
                "depth": _bounded_remote_int(item.get("depth"), 0, 0, 12),
                "has_getter": bool(item.get("has_getter")),
                "has_setter": bool(item.get("has_setter")),
            }
        )

    layers: list[dict[str, Any]] = []
    source_layers = value.get("layers")
    if not isinstance(source_layers, list):
        source_layers = []
    for item in source_layers[:16]:
        if not isinstance(item, dict):
            continue
        layers.append(
            {
                "depth": _bounded_remote_int(item.get("depth"), 0, 0, 12),
                "owner": "own" if item.get("owner") == "own" else "prototype",
                "property_count": _bounded_remote_int(item.get("propertyCount"), 0, 0, 4096),
                "truncated": bool(item.get("truncated")),
            }
        )

    errors: list[dict[str, Any]] = []
    source_errors = value.get("reflectionErrors")
    if not isinstance(source_errors, list):
        source_errors = []
    for item in source_errors[:8]:
        if not isinstance(item, dict) or item.get("operation") not in AVSDK_VISIBLE_SURFACE_ERROR_OPERATIONS:
            continue
        errors.append(
            {
                "operation": item["operation"],
                "depth": _bounded_remote_int(item.get("depth"), 0, 0, 12),
            }
        )

    status = str(value.get("status") or "partial")
    if status not in {"complete", "partial", "unavailable"}:
        status = "partial"
    return {
        "schema": AVSDK_VISIBLE_SURFACE_SCHEMA,
        "status": status,
        "plugin_found": bool(value.get("pluginFound")),
        "callable_methods": callable_methods,
        "control_candidates": [
            copy.deepcopy(method)
            for method in callable_methods
            if AVSDK_VISIBLE_SURFACE_CONTROL_PATTERN.search(method["name"])
        ],
        "control_accessors": accessors,
        "layers": layers,
        "truncated": bool(value.get("truncated")),
        # 原型扫描到达上限并不必然意味着遗漏 QQ 能力；只有未能确认普通
        # embed 基线尾部时，Host 才会同时把 truncated 设为 true。
        "prototype_depth_limited": bool(value.get("prototypeDepthLimited")),
        "skipped_baseline_prototype_tail": bool(value.get("skippedBaselinePrototypeTail")),
        "reflection_errors": errors,
    }


def _sanitize_visible_surface_diagnostic(value: Any) -> dict[str, Any] | None:
    """读取 NapCat 的一次性诊断状态，并限制原因、时间和报告字段。"""

    if not isinstance(value, dict):
        return None
    request_id = str(value.get("requestId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request_id):
        return None
    status = str(value.get("status") or "failed").strip().lower()
    if status not in {"running", "completed", "failed", "rejected"}:
        status = "failed"
    reason = str(value.get("reason") or "").strip().lower()
    if reason not in {
        "",
        "host_not_ready",
        "host_request_failed",
        "host_lifecycle_limit",
        "bridge_disconnected",
    }:
        reason = ""
    return {
        "kind": "avsdk_visible_surface",
        "request_id": request_id,
        "status": status,
        "reason": reason or None,
        "started_at": str(value.get("startedAt") or "").strip()[:64] or None,
        "finished_at": str(value.get("finishedAt") or "").strip()[:64] or None,
        "report": _sanitize_visible_surface_report(value.get("report")),
    }


def _sanitize_service_surface_candidate(value: Any) -> dict[str, Any] | None:
    """仅保留 Service 控制候选的描述符元数据，不接收对象或参数。"""

    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if (
        not isinstance(name, str)
        or not AVSDK_VISIBLE_SURFACE_NAME_PATTERN.fullmatch(name)
        or not AVSDK_VISIBLE_SURFACE_CONTROL_PATTERN.search(name)
    ):
        return None
    kind = str(value.get("kind") or "")
    if kind not in {"method", "accessor"}:
        return None
    return {
        "name": name,
        "kind": kind,
        "owner": "own" if value.get("owner") == "own" else "prototype",
        "depth": _bounded_remote_int(value.get("depth"), 0, 0, 8),
        "enumerable": bool(value.get("enumerable")),
        "configurable": bool(value.get("configurable")),
        "writable": bool(value.get("writable")) if kind == "method" else None,
        "has_getter": bool(value.get("hasGetter")) if kind == "accessor" else False,
        "has_setter": bool(value.get("hasSetter")) if kind == "accessor" else False,
    }


def _sanitize_service_surface_report(value: Any) -> dict[str, Any] | None:
    """白名单化当前 AVSDK Service 的受限反射报告。"""

    if not isinstance(value, dict) or value.get("schema") != AVSDK_SERVICE_SURFACE_SCHEMA:
        return None
    candidates: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    source_candidates = value.get("controlCandidates")
    if not isinstance(source_candidates, list):
        source_candidates = []
    for item in source_candidates[:16]:
        candidate = _sanitize_service_surface_candidate(item)
        if candidate is not None and candidate["name"] not in seen_names:
            seen_names.add(candidate["name"])
            candidates.append(candidate)

    layers: list[dict[str, Any]] = []
    source_layers = value.get("layers")
    if not isinstance(source_layers, list):
        source_layers = []
    for item in source_layers[:8]:
        if not isinstance(item, dict):
            continue
        layers.append(
            {
                "depth": _bounded_remote_int(item.get("depth"), 0, 0, 8),
                "owner": "own" if item.get("owner") == "own" else "prototype",
                "property_count": _bounded_remote_int(item.get("propertyCount"), 0, 0, 512),
                "truncated": bool(item.get("truncated")),
            }
        )

    errors: list[dict[str, Any]] = []
    source_errors = value.get("reflectionErrors")
    if not isinstance(source_errors, list):
        source_errors = []
    for item in source_errors[:8]:
        if not isinstance(item, dict) or item.get("operation") not in AVSDK_VISIBLE_SURFACE_ERROR_OPERATIONS:
            continue
        errors.append(
            {
                "operation": item["operation"],
                "depth": _bounded_remote_int(item.get("depth"), 0, 0, 8),
            }
        )

    status = str(value.get("status") or "partial")
    if status not in {"complete", "partial", "unavailable"}:
        status = "partial"
    return {
        "schema": AVSDK_SERVICE_SURFACE_SCHEMA,
        "status": status,
        "service_available": bool(value.get("serviceAvailable")),
        "control_candidates": candidates,
        "layers": layers,
        "truncated": bool(value.get("truncated")),
        "prototype_depth_limited": bool(value.get("prototypeDepthLimited")),
        "reflection_errors": errors,
    }


def _sanitize_service_surface_diagnostic(value: Any) -> dict[str, Any] | None:
    """读取一次性 Service 诊断状态，并丢弃远端的对象、值和异常正文。"""

    if not isinstance(value, dict):
        return None
    request_id = str(value.get("requestId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request_id):
        return None
    status = str(value.get("status") or "failed").strip().lower()
    if status not in {"running", "completed", "failed", "rejected"}:
        status = "failed"
    reason = str(value.get("reason") or "").strip().lower()
    if reason not in {
        "",
        "runtime_not_active",
        "service_unavailable",
        "service_runtime_limit",
        "reflection_failed",
        "bridge_disconnected",
    }:
        reason = ""
    return {
        "kind": "avsdk_service_surface",
        "request_id": request_id,
        "status": status,
        "reason": reason or None,
        "started_at": str(value.get("startedAt") or "").strip()[:64] or None,
        "finished_at": str(value.get("finishedAt") or "").strip()[:64] or None,
        "report": _sanitize_service_surface_report(value.get("report")),
    }


def _empty_service_surface_diagnostic() -> dict[str, Any]:
    """提供稳定的空状态，避免旧插件状态覆盖新诊断等待态。"""

    return {
        "kind": "avsdk_service_surface",
        "request_id": None,
        "status": "idle",
        "reason": None,
        "started_at": None,
        "finished_at": None,
        "report": None,
    }


def _sanitize_static_artifact_keyword_hits(value: Any) -> list[dict[str, Any]]:
    """仅保留静态扫描预定义词表的有界计数，不接受回传的任意搜索词。"""

    source = value if isinstance(value, list) else []
    counts: dict[str, int] = {}
    for item in source[: len(AVSDK_STATIC_ARTIFACT_KEYWORD_IDS)]:
        if not isinstance(item, dict):
            continue
        keyword_id = item.get("id")
        if keyword_id not in AVSDK_STATIC_ARTIFACT_KEYWORD_IDS or keyword_id in counts:
            continue
        counts[keyword_id] = _bounded_remote_int(item.get("count"), 0, 0, 1_000_000)
    return [
        {"id": keyword_id, "count": counts.get(keyword_id, 0)}
        for keyword_id in AVSDK_STATIC_ARTIFACT_KEYWORD_IDS
    ]


def _empty_static_artifact(artifact_id: str) -> dict[str, Any]:
    """生成缺失固定目标的脱敏占位，避免远端省略项目时误报检查完整。"""

    return {
        "id": artifact_id,
        "status": "missing",
        "byte_length": 0,
        "scanned_bytes": 0,
        "scan_truncated": False,
        "sha256": None,
        "sha256_scope": None,
        "keyword_hits": _sanitize_static_artifact_keyword_hits([]),
        "error_code": None,
    }


def _sanitize_static_artifact(value: Any) -> dict[str, Any] | None:
    """过滤单个固定静态目标，拒绝路径、正文、异常详情和未知目标。"""

    if not isinstance(value, dict):
        return None
    artifact_id = value.get("id")
    if artifact_id not in AVSDK_STATIC_ARTIFACT_IDS:
        return None
    status = str(value.get("status") or "read_failed")
    if status not in AVSDK_STATIC_ARTIFACT_STATUSES:
        status = "read_failed"
    byte_length = _bounded_remote_int(value.get("byteLength"), 0, 0, 128 * 1024 * 1024)
    scanned_bytes = min(
        byte_length,
        _bounded_remote_int(value.get("scannedBytes"), 0, 0, 128 * 1024 * 1024),
    )
    sha256 = str(value.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        sha256 = ""
    sha256_scope = str(value.get("sha256Scope") or "").strip().lower()
    if not sha256 or sha256_scope not in {"full", "prefix"}:
        sha256_scope = None
    error_code = str(value.get("errorCode") or "").strip().lower()
    if error_code not in AVSDK_STATIC_ARTIFACT_ERROR_CODES:
        error_code = None
    return {
        "id": artifact_id,
        "status": status,
        "byte_length": byte_length,
        "scanned_bytes": scanned_bytes,
        "scan_truncated": bool(value.get("scanTruncated")),
        "sha256": sha256 or None,
        "sha256_scope": sha256_scope,
        "keyword_hits": _sanitize_static_artifact_keyword_hits(value.get("keywordHits")),
        "error_code": error_code,
    }


def _sanitize_static_artifacts_report(value: Any) -> dict[str, Any] | None:
    """白名单化 VM 内静态扫描摘要，不向 OneBot 扩散路径或文件正文。"""

    if not isinstance(value, dict) or value.get("schema") != AVSDK_STATIC_ARTIFACTS_SCHEMA:
        return None
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    source = value.get("artifacts")
    if not isinstance(source, list):
        source = []
    for item in source[: len(AVSDK_STATIC_ARTIFACT_IDS) + 2]:
        artifact = _sanitize_static_artifact(item)
        if artifact is not None and artifact["id"] not in artifacts_by_id:
            artifacts_by_id[artifact["id"]] = artifact
    artifacts = [
        artifacts_by_id.get(artifact_id, _empty_static_artifact(artifact_id))
        for artifact_id in AVSDK_STATIC_ARTIFACT_IDS
    ]
    scanned_count = sum(artifact["status"] == "scanned" for artifact in artifacts)
    complete = scanned_count == len(artifacts) and all(
        not artifact["scan_truncated"] for artifact in artifacts
    )
    return {
        "schema": AVSDK_STATIC_ARTIFACTS_SCHEMA,
        "status": "unavailable" if scanned_count == 0 else "complete" if complete else "partial",
        "artifacts": artifacts,
    }


def _sanitize_static_artifacts_diagnostic(value: Any) -> dict[str, Any] | None:
    """读取一次性静态资源诊断状态，并仅保留固定原因与脱敏报告。"""

    if not isinstance(value, dict):
        return None
    request_id = str(value.get("requestId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request_id):
        return None
    status = str(value.get("status") or "failed").strip().lower()
    if status not in {"running", "completed", "failed", "rejected"}:
        status = "failed"
    reason = str(value.get("reason") or "").strip().lower()
    if reason not in {
        "",
        "runtime_not_active",
        "plugin_lifecycle_limit",
        "scan_failed",
        "bridge_disconnected",
    }:
        reason = ""
    return {
        "kind": "avsdk_static_artifacts",
        "request_id": request_id,
        "status": status,
        "reason": reason or None,
        "started_at": str(value.get("startedAt") or "").strip()[:64] or None,
        "finished_at": str(value.get("finishedAt") or "").strip()[:64] or None,
        "report": _sanitize_static_artifacts_report(value.get("report")),
    }


class QQVoiceCallRuntime:
    """维护一条虚拟机 NapCat 到 OneBot 的通话控制连接。"""

    protocol_version = 1

    def __init__(self, config: dict[str, Any]) -> None:
        self.call_config = normalize_config(config)
        # 对话配置只留在本机运行时，后续媒体/模型适配器从这里读取。
        self.dialogue_config = normalize_dialogue_config(config)
        self._accepting = True
        self._websocket = None
        self._send_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._activated_connection = None
        self._pending_controls: dict[str, asyncio.Future] = {}
        self._pending_diagnostic_arms: dict[str, asyncio.Future] = {}
        self._pending_visible_surface_diagnostics: dict[str, asyncio.Future] = {}
        self._pending_service_surface_diagnostics: dict[str, asyncio.Future] = {}
        self._pending_static_artifacts_diagnostics: dict[str, asyncio.Future] = {}
        self._last_phase = "idle"
        self._call_id: str | None = None
        self._ringing_event_call_id: str | None = None
        # 当前房间已有通话时，第二通来电只作为忙线事件暂存，不能覆盖主通话状态。
        self._busy_ringing_call: dict[str, Any] | None = None
        self._status_listeners: list[Callable[[dict[str, Any]], Any]] = []
        self._last_error_event_key: tuple[str | None, str, str] | None = None
        self._connection_generation = 0
        self._transport_lost_generation: int | None = None
        self._dialogue_service = None
        self._admission_service = None
        # QQ/AVSDK 当前没有可验证的主动挂断协议。模型选择挂断时，OneBot
        # 可以先结束自己的电话房间；该覆盖层用于隔离仍由 NapCat 上报的旧通话，
        # 防止本地媒体和总结流程被迟到的 connected/ended 状态重新触发。
        self._locally_ended_call_id: str | None = None
        self._status: dict[str, Any] = {
            "onebot_online": True,
            "plugin_available": True,
            "bridge_connected": False,
            "runtime_active": False,
            "connected_at": None,
            "remote": {},
            "avsdk": {},
            "call": {"phase": "idle", "call_id": None},
            "local_termination": {
                "status": "idle",
                "call_id": None,
                "ended_at": None,
                "remote_phase": None,
                "remote_ended_at": None,
            },
            "diagnostic_capture": {
                "kind": "manual_hangup",
                "mode": "raw",
                "status": "idle",
                "capture_id": None,
                "call_id": None,
                "event_count": 0,
                "byte_count": 0,
                "file_path": None,
                "file_sha256": None,
                "reason": None,
                "started_at": None,
                "finished_at": None,
            },
            "diagnostic_visible_surface": {
                "kind": "avsdk_visible_surface",
                "request_id": None,
                "status": "idle",
                "reason": None,
                "started_at": None,
                "finished_at": None,
                "report": None,
            },
            "diagnostic_service_surface": _empty_service_surface_diagnostic(),
            "diagnostic_static_artifacts": {
                "kind": "avsdk_static_artifacts",
                "request_id": None,
                "status": "idle",
                "reason": None,
                "started_at": None,
                "finished_at": None,
                "report": None,
            },
            "last_error": None,
            "updated_at": time.time(),
        }

    def get_status(self) -> dict[str, Any]:
        """返回可交给 WebUI 的脱敏状态副本。"""
        status = copy.deepcopy(self._status)
        service = self._dialogue_service
        getter = getattr(service, "get_status", None)
        if callable(getter):
            try:
                status["dialogue"] = copy.deepcopy(getter())
            except Exception as error:  # noqa: BLE001
                status["dialogue"] = {"phase": "error", "last_error": str(error)}
        return status

    def get_manual_hangup_capture_status(self) -> dict[str, Any]:
        """返回不含原始 QQ payload 的单次挂断诊断摘要。"""

        capture = self._status.get("diagnostic_capture")
        return copy.deepcopy(capture) if isinstance(capture, dict) else {"status": "idle"}

    def get_visible_surface_diagnostic_status(self) -> dict[str, Any]:
        """返回不含 AVSDK 原生对象的可见方法表面诊断摘要。"""

        diagnostic = self._status.get("diagnostic_visible_surface")
        return copy.deepcopy(diagnostic) if isinstance(diagnostic, dict) else {"status": "idle"}

    def get_service_surface_diagnostic_status(self) -> dict[str, Any]:
        """返回不含 Service 实例、属性值或参数的受限候选摘要。"""

        diagnostic = self._status.get("diagnostic_service_surface")
        return copy.deepcopy(diagnostic) if isinstance(diagnostic, dict) else _empty_service_surface_diagnostic()

    def get_static_artifacts_diagnostic_status(self) -> dict[str, Any]:
        """返回不含路径和原文的 AVSDK 静态资源诊断摘要。"""

        diagnostic = self._status.get("diagnostic_static_artifacts")
        return copy.deepcopy(diagnostic) if isinstance(diagnostic, dict) else {"status": "idle"}

    def set_dialogue_service(self, service: Any) -> None:
        """挂接电话对话服务，使管理接口能同时展示媒体/模型阶段。"""

        self._dialogue_service = service

    def set_admission_service(self, service: Any) -> None:
        """注册来电 AI 决策协调器；低层 control 仍只负责真实 AVSDK 指令。"""

        self._admission_service = service

    def _clear_local_termination(self) -> None:
        """新电话开始时清除上一通电话的 OneBot 本地结束提示。"""

        self._locally_ended_call_id = None
        self._status["local_termination"] = {
            "status": "idle",
            "call_id": None,
            "ended_at": None,
            "remote_phase": None,
            "remote_ended_at": None,
        }

    def _mark_remote_finished_after_local_termination(self, call_id: str, phase: str) -> None:
        """记录旧电话的远端终态，但不把它重新派发给本地电话房间。"""

        if not call_id or self._locally_ended_call_id != call_id:
            return
        current = self._status.get("local_termination")
        current = copy.deepcopy(current) if isinstance(current, dict) else {}
        current.update(
            {
                "status": "remote_ended",
                "call_id": call_id,
                "remote_phase": phase,
                "remote_ended_at": time.time(),
            }
        )
        self._status["local_termination"] = current

    async def _end_call_locally(self, call: dict[str, Any], phase: str) -> dict[str, Any]:
        """结束 OneBot 自身电话房间，不向 NapCat/AVSDK 发送未知挂断协议。

        该方法只在电话已接通且本地对话服务仍属于同一 ``call_id`` 时生效。
        对端 QQ 是否随后结束不参与本地收口判断，远端状态仅供 QQ语音通话 Tab 提示。
        """

        call_id = str((call or {}).get("call_id") or "").strip()
        if not call_id:
            return {
                "success": False,
                "status": "no_active_call",
                "action": "hangup",
                "phase": phase,
                "message": "当前通话没有可用于本地结束的 call_id",
            }
        if self._locally_ended_call_id == call_id:
            return {
                "success": True,
                "status": "already_locally_ended",
                "action": "hangup",
                "phase": "locally_ended",
                "call_id": call_id,
                "local_only": True,
                "native_supported": False,
                "message": "OneBot 已结束本地电话房间，正在等待远端自行结束",
            }

        service = self._dialogue_service
        stop = getattr(service, "stop", None)
        get_dialogue_status = getattr(service, "get_status", None)
        if not callable(stop):
            return {
                "success": False,
                "status": "dialogue_unavailable",
                "action": "hangup",
                "phase": phase,
                "message": "电话对话服务没有可用的本地结束入口",
            }
        if callable(get_dialogue_status):
            try:
                dialogue_status = get_dialogue_status()
                dialogue_call_id = str(
                    (dialogue_status or {}).get("call_id")
                    if isinstance(dialogue_status, dict)
                    else ""
                ).strip()
            except Exception as error:  # noqa: BLE001
                logger.warning(f"[qq_voice_call] 读取本地电话房间状态失败：{error}")
                dialogue_call_id = ""
            if dialogue_call_id and dialogue_call_id != call_id:
                return {
                    "success": False,
                    "status": "dialogue_scope_mismatch",
                    "action": "hangup",
                    "phase": phase,
                    "message": "当前本地电话房间不属于要结束的通话",
                }

        self._locally_ended_call_id = call_id
        self._status["local_termination"] = {
            "status": "ended_waiting_remote",
            "call_id": call_id,
            "ended_at": time.time(),
            "remote_phase": phase,
            "remote_ended_at": None,
        }
        self._status["updated_at"] = time.time()
        try:
            result = stop("local_hangup")
            if inspect.isawaitable(result):
                await result
        except Exception as error:  # noqa: BLE001
            # 本地停止没有完成时不能吞掉原通话状态，否则会让电话既未结束又无法继续。
            self._clear_local_termination()
            self._status["updated_at"] = time.time()
            logger.warning(f"[qq_voice_call] OneBot 本地挂断失败：{error}")
            return {
                "success": False,
                "status": "failed",
                "action": "hangup",
                "phase": phase,
                "message": "OneBot 本地结束电话房间失败",
            }

        logger.info(
            "[qq_voice_call] OneBot 已单方面结束电话房间："
            f"call_id={call_id} remote_phase={phase}"
        )
        return {
            "success": True,
            "status": "locally_ended",
            "action": "hangup",
            "phase": "locally_ended",
            "call_id": call_id,
            "local_only": True,
            "native_supported": False,
            "message": "OneBot 已结束本地电话房间，等待远端自行结束",
        }

    async def record_admission_decision(
        self,
        action: str,
        *,
        caller_uin: str | None = None,
    ) -> dict[str, Any] | None:
        """记录模型在振铃期作出的接听/拒接意图，不直接提交底层 accept。"""

        call = self._status.get("call") if isinstance(self._status.get("call"), dict) else {}
        if str(call.get("phase") or "") != "ringing":
            return None
        current_uin = str(call.get("caller_uin") or "").strip()
        expected_uin = str(caller_uin or "").strip()
        if expected_uin and current_uin and expected_uin != current_uin:
            return {
                "success": False,
                "status": "caller_scope_mismatch",
                "action": str(action or ""),
                "message": "当前私聊用户不是活动来电者",
            }
        service = self._admission_service
        handler = getattr(service, "record_admission_decision", None)
        if not callable(handler):
            return None
        try:
            result = handler(str(action or "").strip().lower(), copy.deepcopy(call))
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, dict) else None
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[qq_voice_call] 记录 AI 来电决策失败：{error}")
            return {
                "success": False,
                "status": "failed",
                "action": str(action or ""),
                "message": str(error),
            }

    async def record_hangup_after_reply(
        self,
        *,
        caller_uin: str | None = None,
    ) -> dict[str, Any]:
        """把模型的挂断意图交给电话对话层，等待本轮语音播放完成。

        Args:
            caller_uin: 当前私聊事件的 QQ 号，用于防止跨联系人控制电话。

        Returns:
            对话层的结构化登记结果；此方法不会直接调用 ``control``，因此
            不会在模型结束语播放前切断 QQ 通话。
        """

        status = self.get_status()
        call = status.get("call") if isinstance(status.get("call"), dict) else {}
        phase = str(call.get("phase") or "idle")
        expected_uin = str(caller_uin or "").strip()
        current_uin = str(call.get("caller_uin") or "").strip()
        if phase != "connected":
            return {
                "success": False,
                "status": "call_not_connected" if phase in {"idle", "ended", "error"} else "invalid_state",
                "action": "hangup",
                "phase": phase,
                "message": "当前没有已接通且可延后挂断的电话",
            }
        if expected_uin and current_uin and expected_uin != current_uin:
            return {
                "success": False,
                "status": "caller_scope_mismatch",
                "action": "hangup",
                "phase": phase,
                "message": "当前私聊用户不是活动通话的来电者",
            }
        service = self._dialogue_service
        handler = getattr(service, "queue_hangup_after_reply", None)
        if not callable(handler):
            return {
                "success": False,
                "status": "dialogue_unavailable",
                "action": "hangup",
                "phase": phase,
                "message": "电话对话服务没有可用的延后挂断入口",
            }
        try:
            result = handler(copy.deepcopy(call), caller_uin=expected_uin or current_uin)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                return result
            return {
                "success": False,
                "status": "failed",
                "action": "hangup",
                "phase": phase,
                "message": "电话对话服务返回了无效的挂断登记结果",
            }
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[qq_voice_call] 登记回复完成后挂断失败：{error}")
            return {
                "success": False,
                "status": "failed",
                "action": "hangup",
                "phase": phase,
                "message": str(error),
            }

    def add_status_listener(self, listener: Callable[[dict[str, Any]], Any]) -> None:
        """订阅电话生命周期事件；重复注册同一个回调不会产生重复通知。"""
        if not callable(listener):
            raise ValueError("状态监听器必须是可调用对象")
        if listener not in self._status_listeners:
            self._status_listeners.append(listener)

    def remove_status_listener(self, listener: Callable[[dict[str, Any]], Any]) -> None:
        """移除状态监听器；监听器不存在时保持幂等。"""
        try:
            self._status_listeners.remove(listener)
        except ValueError:
            return

    async def _await_status_listener(self, result: Any) -> None:
        """隔离异步监听器异常，避免回调影响通话状态机。"""
        try:
            await result
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[qq_voice_call] 状态监听器异步回调失败: {error}")

    def _notify_status_event(self, event: str, *, call: dict[str, Any] | None = None) -> None:
        """向监听器广播一份脱敏状态快照。"""

        event_call = copy.deepcopy(call if isinstance(call, dict) else self._status.get("call") or {})
        payload = {
            "event": event,
            "call_id": str(event_call.get("call_id") or self._call_id or "").strip() or None,
            "call": event_call,
        }
        for listener in tuple(self._status_listeners):
            try:
                result = listener(copy.deepcopy(payload))
                if not inspect.isawaitable(result):
                    continue
                try:
                    asyncio.get_running_loop().create_task(self._await_status_listener(result))
                except RuntimeError:
                    # 同步环境没有事件循环时不能安全执行异步回调，主动关闭
                    # 协程，避免产生 RuntimeWarning。
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
            except Exception as error:  # noqa: BLE001
                logger.warning(f"[qq_voice_call] 状态监听器回调失败: {error}")

    def _emit_error_event(self, message: str) -> None:
        """按通话、阶段和错误文本去重错误事件。"""
        phase = str((self._status.get("call") or {}).get("phase") or "idle")
        key = (self._call_id, phase, str(message))
        if key == self._last_error_event_key:
            return
        self._last_error_event_key = key
        self._notify_status_event("error")

    @staticmethod
    def _remote_identity_is_distinct(
        active_remote_call_id: str,
        active_caller_uin: str,
        incoming_remote_call_id: str,
        incoming_caller_uin: str,
    ) -> bool:
        """仅在两边都具备同类远端标识且值不同的时候确认是另一通电话。"""

        return bool(
            (active_remote_call_id and incoming_remote_call_id and active_remote_call_id != incoming_remote_call_id)
            or (active_caller_uin and incoming_caller_uin and active_caller_uin != incoming_caller_uin)
        )

    @staticmethod
    def _remote_identity_matches(
        call: dict[str, Any],
        remote_call_id: str,
        caller_uin: str,
    ) -> bool:
        """判断状态是否仍属于一份已暂存的忙线来电。"""

        known_call_id = str(call.get("remote_call_id") or "").strip()
        known_caller_uin = str(call.get("caller_uin") or "").strip()
        call_id_compared = bool(known_call_id and remote_call_id)
        caller_compared = bool(known_caller_uin and caller_uin)
        if call_id_compared and known_call_id != remote_call_id:
            return False
        if caller_compared and known_caller_uin != caller_uin:
            return False
        return bool(
            (call_id_compared and known_call_id == remote_call_id)
            or (caller_compared and known_caller_uin == caller_uin)
        )

    async def handle_connection(
        self,
        websocket: Any,
        *,
        remote_address: str,
        self_id: str,
    ) -> None:
        """接管一个已鉴权的 NapCat WebSocket 直到它断开。

        Args:
            websocket: aiohttp 已完成握手的 WebSocketResponse。
            remote_address: 连接来源地址，仅用于状态展示。
            self_id: NapCat 通过 ``X-Self-ID`` 提供的机器人 QQ 号。
        """
        if not self._accepting:
            await websocket.close(code=1013, message=b"OneBot is shutting down")
            return

        async with self._connection_lock:
            previous = self._websocket
            self._websocket = websocket
            self._activated_connection = None
            self._connection_generation += 1
            connection_generation = self._connection_generation
            self._transport_lost_generation = None
            self._status.update(
                {
                    "bridge_connected": True,
                    "runtime_active": False,
                    "connected_at": time.time(),
                    "remote": {
                        "address": remote_address,
                        "self_id": self_id,
                    },
                    "last_error": None,
                    "updated_at": time.time(),
                }
            )
        if previous is not None and previous is not websocket and not previous.closed:
            await previous.close(code=1012, message=b"Replaced by a new bridge connection")

        logger.info(f"[qq_voice_call] 虚拟机 NapCat 通话桥已连接: {remote_address or 'unknown'}")
        try:
            await self._send({"type": "hello_request", "protocolVersion": self.protocol_version}, websocket)
            async for message in websocket:
                if message.type == WSMsgType.TEXT:
                    await self._handle_message(websocket, message.data)
                elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
                    break
                elif message.type == WSMsgType.ERROR:
                    error = websocket.exception()
                    self._set_error(f"远程通话桥连接异常：{error}" if error else "远程通话桥连接异常")
                    break
        finally:
            should_emit_transport_lost = False
            async with self._connection_lock:
                is_current = self._websocket is websocket
                if is_current:
                    for request_id, future in list(self._pending_controls.items()):
                        if not future.done():
                            future.set_result(
                                {
                                    "requestId": request_id,
                                    "status": "bridge_disconnected",
                                    "message": "虚拟机 NapCat 通话桥已断开",
                                }
                            )
                    self._pending_controls.clear()
                    for request_id, future in list(self._pending_diagnostic_arms.items()):
                        if not future.done():
                            future.set_result(
                                {
                                    "requestId": request_id,
                                    "status": "rejected",
                                    "reason": "bridge_disconnected",
                                    "message": "虚拟机 NapCat 通话桥已断开",
                                }
                            )
                    self._pending_diagnostic_arms.clear()
                    for request_id, future in list(self._pending_visible_surface_diagnostics.items()):
                        if not future.done():
                            future.set_result(
                                {
                                    "requestId": request_id,
                                    "status": "failed",
                                    "reason": "bridge_disconnected",
                                }
                            )
                    self._pending_visible_surface_diagnostics.clear()
                    for request_id, future in list(self._pending_service_surface_diagnostics.items()):
                        if not future.done():
                            future.set_result(
                                {
                                    "requestId": request_id,
                                    "status": "failed",
                                    "reason": "bridge_disconnected",
                                }
                            )
                    self._pending_service_surface_diagnostics.clear()
                    for request_id, future in list(self._pending_static_artifacts_diagnostics.items()):
                        if not future.done():
                            future.set_result(
                                {
                                    "requestId": request_id,
                                    "status": "failed",
                                    "reason": "bridge_disconnected",
                                }
                            )
                    self._pending_static_artifacts_diagnostics.clear()
                    capture = self._status.get("diagnostic_capture")
                    if isinstance(capture, dict) and capture.get("status") in {"arming", "armed"}:
                        capture.update(
                            {
                                "status": "cancelled",
                                "reason": "bridge_disconnected",
                                "finished_at": time.time(),
                            }
                        )
                    visible_surface = self._status.get("diagnostic_visible_surface")
                    if isinstance(visible_surface, dict) and visible_surface.get("status") in {
                        "requesting",
                        "running",
                    }:
                        visible_surface.update(
                            {
                                "status": "failed",
                                "reason": "bridge_disconnected",
                                "finished_at": time.time(),
                            }
                        )
                    service_surface = self._status.get("diagnostic_service_surface")
                    if isinstance(service_surface, dict) and service_surface.get("status") in {
                        "requesting",
                        "running",
                    }:
                        service_surface.update(
                            {
                                "status": "failed",
                                "reason": "bridge_disconnected",
                                "finished_at": time.time(),
                            }
                        )
                    static_artifacts = self._status.get("diagnostic_static_artifacts")
                    if isinstance(static_artifacts, dict) and static_artifacts.get("status") in {
                        "requesting",
                        "running",
                    }:
                        static_artifacts.update(
                            {
                                "status": "failed",
                                "reason": "bridge_disconnected",
                                "finished_at": time.time(),
                            }
                        )
                    phase = str((self._status.get("call") or {}).get("phase") or "idle")
                    if (
                        phase in {"ringing", "accepting", "accepted", "connected"}
                        and self._transport_lost_generation != connection_generation
                    ):
                        self._transport_lost_generation = connection_generation
                        should_emit_transport_lost = True
                    self._websocket = None
                    self._activated_connection = None
                    self._status.update(
                        {
                            "bridge_connected": False,
                            "runtime_active": False,
                            "connected_at": None,
                            "updated_at": time.time(),
                        }
                    )
            if should_emit_transport_lost:
                self._notify_status_event("transport_lost")
            logger.info("[qq_voice_call] 虚拟机 NapCat 通话桥已断开")

    async def _handle_message(self, websocket: Any, raw_message: str) -> None:
        """处理来自虚拟机插件的一条结构化状态消息。"""
        try:
            payload = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            self._set_error("远程通话桥发送了无效 JSON")
            return
        if not isinstance(payload, dict):
            self._set_error("远程通话桥消息必须是 JSON 对象")
            return

        message_type = str(payload.get("type", ""))
        if message_type == "hello":
            protocol = int(payload.get("protocolVersion", 0) or 0)
            if protocol != self.protocol_version:
                self._set_error(f"QQ 通话桥协议版本不兼容：{protocol}")
                await websocket.close(code=1002, message=b"Protocol version mismatch")
                return
            remote = self._status.setdefault("remote", {})
            capabilities = payload.get("capabilities")
            safe_capabilities = []
            if isinstance(capabilities, list):
                safe_capabilities = [
                    str(item).strip()
                    for item in capabilities[:32]
                    if isinstance(item, str) and str(item).strip()
                ]
            remote.update(
                {
                    "self_id": str(payload.get("selfId") or remote.get("self_id") or ""),
                    "plugin_version": str(payload.get("pluginVersion") or ""),
                    "platform": str(payload.get("platform") or ""),
                    "qq_version": str(payload.get("qqVersion") or ""),
                    "capabilities": safe_capabilities,
                }
            )
            self._status["updated_at"] = time.time()
            if self._activated_connection is not websocket:
                self._activated_connection = websocket
                await self._send(
                    {
                        "type": "activate",
                        "protocolVersion": self.protocol_version,
                        "config": {
                            "autoAcceptPrivate": self.call_config["auto_accept_private"],
                            # 启用电话模型对话时，NapCat 只完成本地名单判断并保留
                            # 振铃，接听意图由当前 QQ 私聊上下文中的模型工具给出。
                            "admissionByOneBot": bool(
                                self.dialogue_config.get("enabled")
                                and (self.dialogue_config.get("admission") or {}).get("enabled", True)
                            ),
                            # 新版插件保持振铃，直到本机模型和音频链准备完成后
                            # 主动发送 accept；旧版插件会忽略这个扩展字段。
                            "acceptWhenReady": True,
                            "allowUsers": self.call_config["allow_users"],
                            "denyUsers": self.call_config["deny_users"],
                        },
                    },
                    websocket,
                )
            return

        if message_type == "status":
            self._apply_remote_status(payload.get("data"))
            return
        if message_type == "control_result":
            self._handle_control_result(payload)
            return
        if message_type == "diagnostic_capture_status":
            self._handle_diagnostic_capture_status(payload)
            return
        if message_type == "diagnostic_capture_result":
            self._handle_diagnostic_capture_result(payload)
            return
        if message_type == "diagnostic_visible_surface_result":
            self._handle_visible_surface_diagnostic_result(payload)
            return
        if message_type == "diagnostic_avsdk_service_surface_result":
            self._handle_service_surface_diagnostic_result(payload)
            return
        if message_type == "diagnostic_static_artifacts_result":
            self._handle_static_artifacts_diagnostic_result(payload)
            return
        if message_type == "pong":
            self._status["updated_at"] = time.time()

    def _handle_control_result(self, payload: dict[str, Any]) -> None:
        """把 NapCat 对控制请求的真实执行结果交给等待中的调用方。"""
        request_id = str(payload.get("requestId") or "").strip()
        if not request_id:
            self._set_error("远程通话桥返回了没有 requestId 的控制结果")
            return
        future = self._pending_controls.pop(request_id, None)
        if future is None or future.done():
            return
        logger.info(
            "[qq_voice_call] 通话控制结果: "
            f"action={str(payload.get('action') or 'unknown')} "
            f"status={str(payload.get('status') or 'unknown')}"
        )
        future.set_result(copy.deepcopy(payload))

    def _handle_diagnostic_capture_status(self, payload: dict[str, Any]) -> None:
        """接收 NapCat 对一次性诊断武装请求的确认或拒绝。"""

        request_id = str(payload.get("requestId") or "").strip()
        capture_id = str(payload.get("captureId") or "").strip()
        call_id = str(payload.get("callId") or "").strip()
        status = str(payload.get("status") or "rejected").strip().lower()
        current = self._status.get("diagnostic_capture")
        if not isinstance(current, dict):
            return
        if (
            not request_id
            or request_id != str(current.get("request_id") or "")
            or capture_id != str(current.get("capture_id") or "")
            or call_id != str(current.get("call_id") or "")
        ):
            return
        if status not in {"armed", "rejected"}:
            status = "rejected"
        current.update(
            {
                "status": status,
                "reason": str(payload.get("reason") or "").strip() or None,
                "started_at": str(payload.get("startedAt") or "").strip()
                or current.get("started_at"),
            }
        )
        self._status["updated_at"] = time.time()
        future = self._pending_diagnostic_arms.get(request_id)
        if future is not None and not future.done():
            future.set_result(copy.deepcopy(payload))
        logger.info(f"[qq_voice_call] 手动挂断协议诊断武装结果: status={status}")

    def _handle_diagnostic_capture_result(self, payload: dict[str, Any]) -> None:
        """保存虚拟机返回的脱敏摘要，原始协议数据始终留在虚拟机。"""

        current = self._status.get("diagnostic_capture")
        if not isinstance(current, dict):
            return
        request_id = str(payload.get("requestId") or "").strip()
        capture_id = str(payload.get("captureId") or "").strip()
        call_id = str(payload.get("callId") or "").strip()
        if (
            request_id != str(current.get("request_id") or "")
            or capture_id != str(current.get("capture_id") or "")
            or call_id != str(current.get("call_id") or "")
        ):
            logger.warning("[qq_voice_call] 忽略与当前电话不匹配的挂断协议诊断结果")
            return
        status = str(payload.get("status") or "failed").strip().lower()
        if status not in {"completed", "expired", "cancelled", "failed", "limit_reached"}:
            status = "failed"
        try:
            event_count = max(0, int(payload.get("eventCount") or 0))
            byte_count = max(0, int(payload.get("byteCount") or 0))
        except (TypeError, ValueError):
            event_count = 0
            byte_count = 0
            status = "failed"
        file_sha256 = str(payload.get("fileSha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", file_sha256):
            file_sha256 = ""
        current.update(
            {
                "status": status,
                "event_count": event_count,
                "byte_count": byte_count,
                "file_path": str(payload.get("filePath") or "").strip()[:1024] or None,
                "file_sha256": file_sha256 or None,
                "reason": str(payload.get("reason") or "").strip()[:160] or None,
                "started_at": str(payload.get("startedAt") or "").strip()
                or current.get("started_at"),
                "finished_at": str(payload.get("finishedAt") or "").strip() or None,
            }
        )
        self._status["updated_at"] = time.time()
        logger.info(
            "[qq_voice_call] 手动挂断协议诊断完成: "
            f"status={status} events={event_count} reason={current.get('reason') or '-'}"
        )

    def _handle_visible_surface_diagnostic_result(self, payload: dict[str, Any]) -> None:
        """接收一次性只读 AVSDK 反射结果，并再次过滤全部 Renderer 字段。"""

        request_id = str(payload.get("requestId") or "").strip()
        current = self._status.get("diagnostic_visible_surface")
        if not isinstance(current, dict) or not request_id or request_id != current.get("request_id"):
            return
        status = str(payload.get("status") or "failed").strip().lower()
        if status == "already_completed":
            status = "completed"
        elif status == "already_failed":
            status = "failed"
        if status not in {"running", "completed", "failed", "rejected"}:
            status = "failed"
        sanitized = _sanitize_visible_surface_diagnostic(
            {
                "requestId": request_id,
                "status": status,
                "reason": payload.get("reason"),
                "startedAt": payload.get("startedAt"),
                "finishedAt": payload.get("finishedAt"),
                "report": payload.get("report"),
            }
        )
        if sanitized is None:
            sanitized = {
                "kind": "avsdk_visible_surface",
                "request_id": request_id,
                "status": "failed",
                "reason": "host_request_failed",
                "started_at": None,
                "finished_at": None,
                "report": None,
            }
        self._status["diagnostic_visible_surface"] = sanitized
        self._status["updated_at"] = time.time()
        future = self._pending_visible_surface_diagnostics.get(request_id)
        if future is not None and not future.done() and status != "running":
            future.set_result(copy.deepcopy(sanitized))
        report = sanitized.get("report") if isinstance(sanitized.get("report"), dict) else {}
        logger.info(
            "[qq_voice_call] AVSDK 可见面诊断结果: "
            f"status={sanitized['status']} methods={len(report.get('callable_methods') or [])} "
            f"control_candidates={len(report.get('control_candidates') or [])}"
        )

    def _handle_service_surface_diagnostic_result(self, payload: dict[str, Any]) -> None:
        """接收 Service 受限反射结果，并再次拒绝对象、值、参数与异常原文。"""

        request_id = str(payload.get("requestId") or "").strip()
        current = self._status.get("diagnostic_service_surface")
        if not isinstance(current, dict) or not request_id or request_id != current.get("request_id"):
            return
        status = str(payload.get("status") or "failed").strip().lower()
        if status == "already_completed":
            status = "completed"
        elif status == "already_failed":
            status = "failed"
        if status not in {"running", "completed", "failed", "rejected"}:
            status = "failed"
        sanitized = _sanitize_service_surface_diagnostic(
            {
                "requestId": request_id,
                "status": status,
                "reason": payload.get("reason"),
                "startedAt": payload.get("startedAt"),
                "finishedAt": payload.get("finishedAt"),
                "report": payload.get("report"),
            }
        )
        if sanitized is None:
            sanitized = {
                **_empty_service_surface_diagnostic(),
                "request_id": request_id,
                "status": "failed",
                "reason": "reflection_failed",
            }
        self._status["diagnostic_service_surface"] = sanitized
        self._status["updated_at"] = time.time()
        future = self._pending_service_surface_diagnostics.get(request_id)
        if future is not None and not future.done() and status != "running":
            future.set_result(copy.deepcopy(sanitized))
        report = sanitized.get("report") if isinstance(sanitized.get("report"), dict) else {}
        logger.info(
            "[qq_voice_call] AVSDK Service 诊断结果: "
            f"status={sanitized['status']} report={report.get('status') or 'none'} "
            f"control_candidates={len(report.get('control_candidates') or [])}"
        )

    def _handle_static_artifacts_diagnostic_result(self, payload: dict[str, Any]) -> None:
        """接收 VM 固定目标扫描结果，并再次去除路径、正文和未知字段。"""

        request_id = str(payload.get("requestId") or "").strip()
        current = self._status.get("diagnostic_static_artifacts")
        if not isinstance(current, dict) or not request_id or request_id != current.get("request_id"):
            return
        status = str(payload.get("status") or "failed").strip().lower()
        if status == "already_completed":
            status = "completed"
        elif status == "already_failed":
            status = "failed"
        if status not in {"running", "completed", "failed", "rejected"}:
            status = "failed"
        sanitized = _sanitize_static_artifacts_diagnostic(
            {
                "requestId": request_id,
                "status": status,
                "reason": payload.get("reason"),
                "startedAt": payload.get("startedAt"),
                "finishedAt": payload.get("finishedAt"),
                "report": payload.get("report"),
            }
        )
        if sanitized is None:
            sanitized = {
                "kind": "avsdk_static_artifacts",
                "request_id": request_id,
                "status": "failed",
                "reason": "scan_failed",
                "started_at": None,
                "finished_at": None,
                "report": None,
            }
        self._status["diagnostic_static_artifacts"] = sanitized
        self._status["updated_at"] = time.time()
        future = self._pending_static_artifacts_diagnostics.get(request_id)
        if future is not None and not future.done() and status != "running":
            future.set_result(copy.deepcopy(sanitized))
        report = sanitized.get("report") if isinstance(sanitized.get("report"), dict) else {}
        artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), list) else []
        logger.info(
            "[qq_voice_call] AVSDK 静态资源诊断结果: "
            f"status={sanitized['status']} report={report.get('status') or 'none'} "
            f"artifacts={len(artifacts)}"
        )

    def _apply_remote_status(self, incoming: Any) -> None:
        """只接受明确允许展示的远程状态字段，避免透传 AVSDK 原始数据。"""
        if not isinstance(incoming, dict):
            self._set_error("远程通话桥状态格式不正确")
            return
        avsdk_source = incoming.get("avsdk") if isinstance(incoming.get("avsdk"), dict) else {}
        call_source = incoming.get("call") if isinstance(incoming.get("call"), dict) else {}
        phase = str(call_source.get("phase") or "idle")
        if phase not in CALL_PHASES:
            phase = "error"
        previous_phase = self._last_phase
        previous_error = self._status.get("last_error")
        previous_call = self._status.get("call") if isinstance(self._status.get("call"), dict) else {}
        remote_status = self._status.get("remote") if isinstance(self._status.get("remote"), dict) else {}
        # 电话桥和普通 OneBot 反向 WS 都由同一机器人账号建立。把经鉴权的
        # self_id 固化进 call 快照，后续工具和流程文本才能锁定正确发送连接。
        bot_self_id = str(remote_status.get("self_id") or "").strip()
        incoming_remote_call_id = str(call_source.get("callId") or call_source.get("call_id") or "").strip()
        incoming_caller_uin = str(call_source.get("callerUin") or "").strip()
        incoming_caller_uid = str(call_source.get("callerUid") or "").strip()
        incoming_caller_name = str(call_source.get("callerName") or "").strip()
        active_remote_call_id = str(previous_call.get("remote_call_id") or "").strip()
        active_caller_uin = str(previous_call.get("caller_uin") or "").strip()
        active_caller_uid = str(previous_call.get("caller_uid") or "").strip()
        active_caller_name = str(previous_call.get("caller_name") or "").strip()
        is_busy_second_call = (
            phase == "ringing"
            and previous_phase in {"ringing", "accepting", "accepted", "connected"}
            and self._remote_identity_is_distinct(
                active_remote_call_id,
                active_caller_uin,
                incoming_remote_call_id,
                incoming_caller_uin,
            )
        )
        busy_call_matches = bool(
            self._busy_ringing_call
            and self._remote_identity_matches(
                self._busy_ringing_call,
                incoming_remote_call_id,
                incoming_caller_uin,
            )
        )
        # 远程端可能没有稳定的 callId，因此由本机在每次新的 ringing
        # 状态开始时生成；同一通话的后续状态始终复用它。
        is_new_primary_call = not is_busy_second_call and (
            (phase == "ringing" and previous_phase != "ringing")
            or (phase != "idle" and self._call_id is None)
        )
        if is_new_primary_call:
            self._call_id = uuid.uuid4().hex
            self._ringing_event_call_id = None
            self._last_error_event_key = None
            self._busy_ringing_call = None
            self._clear_local_termination()
        if is_busy_second_call:
            call_id = (
                str((self._busy_ringing_call or {}).get("call_id") or "").strip()
                if busy_call_matches
                else uuid.uuid4().hex
            )
            remote_call_id = incoming_remote_call_id
            caller_uin = incoming_caller_uin
            caller_uid = incoming_caller_uid
            caller_name = incoming_caller_name
        else:
            call_id = self._call_id
            # NapCat 正常状态会保留来电者字段；测试或旧插件若只上报阶段，
            # 继续保留当前主通话已确认的远端身份，以便识别后续第二通来电。
            # 新来电不能继承上一通的字段，避免把旧联系人的资料带入新房间。
            remote_call_id = incoming_remote_call_id or (
                "" if is_new_primary_call else active_remote_call_id
            )
            caller_uin = incoming_caller_uin or ("" if is_new_primary_call else active_caller_uin)
            caller_uid = incoming_caller_uid or ("" if is_new_primary_call else active_caller_uid)
            caller_name = incoming_caller_name or ("" if is_new_primary_call else active_caller_name)
        call = {
            "phase": phase,
            "call_id": call_id,
            "invite_at": call_source.get("inviteAt"),
            "invite_received_at": call_source.get("inviteReceivedAt"),
            "connected_at": call_source.get("connectedAt"),
            "ended_at": call_source.get("endedAt"),
            "end_reason": call_source.get("endReason"),
            "caller_uin": caller_uin,
            "caller_uid": caller_uid,
            "caller_name": caller_name,
            "bot_self_id": bot_self_id,
            "blocked_reason": str(call_source.get("blockedReason") or ""),
            "accept_decision": str(call_source.get("acceptDecision") or ""),
            "accept_decision_reason": str(call_source.get("acceptDecisionReason") or ""),
            "accept_command_posted_at": call_source.get("acceptCommandPostedAt"),
            "accept_post_state": str(call_source.get("acceptPostState") or ""),
            "accept_result_code": call_source.get("acceptResultCode"),
            "enter_room_result_code": call_source.get("enterRoomResultCode"),
            "last_control_action": str(call_source.get("lastControlAction") or "") or None,
            "last_control_request_id": str(call_source.get("lastControlRequestId") or "") or None,
            "last_control_status": str(call_source.get("lastControlStatus") or "") or None,
        }
        if remote_call_id:
            call["remote_call_id"] = remote_call_id
        locally_ended_call = bool(call_id and self._locally_ended_call_id == call_id)
        if locally_ended_call and phase in {"ended", "idle", "error"}:
            self._mark_remote_finished_after_local_termination(call_id, phase)
        avsdk = {
            "service_available": bool(avsdk_source.get("serviceAvailable")),
            "listener_registered": bool(avsdk_source.get("listenerRegistered")),
            "host_ready": bool(avsdk_source.get("hostReady")),
            "plugin_found": bool(avsdk_source.get("pluginFound")),
            "login_posted": bool(avsdk_source.get("loginPosted")),
            "last_output_command": avsdk_source.get("lastOutputCommand"),
            "output_count": int(avsdk_source.get("outputCount") or 0),
            "command_counts": copy.deepcopy(avsdk_source.get("commandCounts"))
            if isinstance(avsdk_source.get("commandCounts"), dict)
            else {},
            "kernel_event_count": int(avsdk_source.get("kernelEventCount") or 0),
            "last_kernel_event": copy.deepcopy(avsdk_source.get("lastKernelEvent"))
            if isinstance(avsdk_source.get("lastKernelEvent"), dict)
            else None,
            "kernel_action_forward_count": int(avsdk_source.get("kernelActionForwardCount") or 0),
            "kernel_action_forward_error": str(avsdk_source.get("kernelActionForwardError") or "") or None,
            "host_message_count": int(avsdk_source.get("hostMessageCount") or 0),
            "host_forwarded_count": int(avsdk_source.get("hostForwardedCount") or 0),
            "host_missing_payload_count": int(avsdk_source.get("hostMissingPayloadCount") or 0),
            "host_last_message_shape": copy.deepcopy(avsdk_source.get("hostLastMessageShape"))
            if isinstance(avsdk_source.get("hostLastMessageShape"), dict)
            else None,
            "host_forward_error": str(avsdk_source.get("hostForwardError") or "") or None,
        }
        visible_surface = _sanitize_visible_surface_diagnostic(
            avsdk_source.get("visibleSurfaceDiagnostic")
        )
        current_visible_surface = self._status.get("diagnostic_visible_surface")
        current_visible_surface = (
            current_visible_surface if isinstance(current_visible_surface, dict) else {}
        )
        current_visible_surface_status = str(current_visible_surface.get("status") or "idle")
        current_visible_surface_request_id = str(
            current_visible_surface.get("request_id") or ""
        ).strip()
        waiting_visible_surface_result = current_visible_surface_status in {"requesting", "running"}

        if waiting_visible_surface_result:
            # WebSocket 状态与专用结果消息可乱序抵达。等待中的本地请求只能由
            # 同一 request_id 的远端状态推进，不能被旧 Host 的空闲快照覆盖。
            if (
                visible_surface is not None
                and visible_surface.get("request_id") == current_visible_surface_request_id
            ):
                self._status["diagnostic_visible_surface"] = visible_surface
                if visible_surface.get("status") in {"completed", "failed", "rejected"}:
                    pending = self._pending_visible_surface_diagnostics.get(
                        current_visible_surface_request_id
                    )
                    if pending is not None and not pending.done():
                        pending.set_result(copy.deepcopy(visible_surface))
        elif visible_surface is not None:
            self._status["diagnostic_visible_surface"] = visible_surface
        elif avsdk_source.get("visibleSurfaceDiagnostic") is None:
            self._status["diagnostic_visible_surface"] = {
                "kind": "avsdk_visible_surface",
                "request_id": None,
                "status": "idle",
                "reason": None,
                "started_at": None,
                "finished_at": None,
                "report": None,
            }
        service_surface = _sanitize_service_surface_diagnostic(
            avsdk_source.get("serviceSurfaceDiagnostic")
        )
        current_service_surface = self._status.get("diagnostic_service_surface")
        current_service_surface = (
            current_service_surface if isinstance(current_service_surface, dict) else {}
        )
        current_service_surface_status = str(current_service_surface.get("status") or "idle")
        current_service_surface_request_id = str(
            current_service_surface.get("request_id") or ""
        ).strip()
        waiting_service_surface_result = current_service_surface_status in {"requesting", "running"}

        if waiting_service_surface_result:
            # 常规状态与专用结果可乱序；只能由同 request_id 的状态推进等待态。
            if (
                service_surface is not None
                and service_surface.get("request_id") == current_service_surface_request_id
            ):
                self._status["diagnostic_service_surface"] = service_surface
                if service_surface.get("status") in {"completed", "failed", "rejected"}:
                    pending = self._pending_service_surface_diagnostics.get(
                        current_service_surface_request_id
                    )
                    if pending is not None and not pending.done():
                        pending.set_result(copy.deepcopy(service_surface))
        elif service_surface is not None:
            self._status["diagnostic_service_surface"] = service_surface
        elif avsdk_source.get("serviceSurfaceDiagnostic") is None:
            self._status["diagnostic_service_surface"] = _empty_service_surface_diagnostic()

        static_artifacts = _sanitize_static_artifacts_diagnostic(
            avsdk_source.get("staticArtifactsDiagnostic")
        )
        current_static_artifacts = self._status.get("diagnostic_static_artifacts")
        current_static_artifacts = (
            current_static_artifacts if isinstance(current_static_artifacts, dict) else {}
        )
        current_static_artifacts_status = str(current_static_artifacts.get("status") or "idle")
        current_static_artifacts_request_id = str(
            current_static_artifacts.get("request_id") or ""
        ).strip()
        waiting_static_artifacts_result = current_static_artifacts_status in {"requesting", "running"}

        if waiting_static_artifacts_result:
            # 静态扫描同样可能遇到普通 status 与专用结果乱序；仅接受同一
            # request_id 的远端状态，不能让旧空快照清空本地等待态。
            if (
                static_artifacts is not None
                and static_artifacts.get("request_id") == current_static_artifacts_request_id
            ):
                self._status["diagnostic_static_artifacts"] = static_artifacts
                if static_artifacts.get("status") in {"completed", "failed", "rejected"}:
                    pending = self._pending_static_artifacts_diagnostics.get(
                        current_static_artifacts_request_id
                    )
                    if pending is not None and not pending.done():
                        pending.set_result(copy.deepcopy(static_artifacts))
        elif static_artifacts is not None:
            self._status["diagnostic_static_artifacts"] = static_artifacts
        elif avsdk_source.get("staticArtifactsDiagnostic") is None:
            self._status["diagnostic_static_artifacts"] = {
                "kind": "avsdk_static_artifacts",
                "request_id": None,
                "status": "idle",
                "reason": None,
                "started_at": None,
                "finished_at": None,
                "report": None,
            }
        if call["invite_received_at"] and call["invite_received_at"] != previous_call.get("invite_received_at"):
            logger.info("[qq_voice_call] 信令: invite_received")
        if call["accept_decision"] and call["accept_decision"] != previous_call.get("accept_decision"):
            logger.info(
                "[qq_voice_call] 接听决策: "
                f"{call['accept_decision']} reason={call['accept_decision_reason'] or 'unknown'}"
            )
        if call["accept_post_state"] and call["accept_post_state"] != previous_call.get("accept_post_state"):
            logger.info(f"[qq_voice_call] 接听命令发送状态: {call['accept_post_state']}")
        if call["accept_result_code"] != previous_call.get("accept_result_code"):
            if call["accept_result_code"] is not None:
                logger.info(f"[qq_voice_call] 接听结果: code={call['accept_result_code']}")
        if call["enter_room_result_code"] != previous_call.get("enter_room_result_code"):
            if call["enter_room_result_code"] is not None:
                logger.info(f"[qq_voice_call] 入房结果: code={call['enter_room_result_code']}")
        if call["end_reason"] is not None and call["end_reason"] != previous_call.get("end_reason"):
            logger.info(f"[qq_voice_call] 通话结束原因: {call['end_reason']}")
        if avsdk["last_output_command"] in {5, 20004, 20006}:
            previous_count = int((self._status.get("avsdk") or {}).get("output_count") or 0)
            if avsdk["output_count"] != previous_count:
                logger.info(
                    f"[qq_voice_call] AVSDK 输出: cmd={avsdk['last_output_command']} "
                    f"count={avsdk['output_count']}"
                )
        incoming_error = str(incoming.get("lastError") or "").strip() or None
        if is_busy_second_call or busy_call_matches:
            # 第二通电话的状态不得改写第一通的阶段、来电者或本地 call_id。
            # 只有通用桥接诊断仍可更新；上层会收到独立的 ringing 事件并走忙线流程。
            self._status.update(
                {
                    "runtime_active": bool(incoming.get("active")),
                    "avsdk": avsdk,
                    "last_error": incoming_error,
                    "updated_at": time.time(),
                }
            )
            if is_busy_second_call:
                if not busy_call_matches:
                    self._busy_ringing_call = copy.deepcopy(call)
                    logger.info(
                        "[qq_voice_call] 检测到第二通来电，保持当前通话不变: "
                        f"active_call_id={self._call_id} incoming_call_id={call_id}"
                    )
                    self._notify_status_event("ringing", call=call)
                elif self._busy_ringing_call is not None:
                    self._busy_ringing_call.update(copy.deepcopy(call))
            elif phase in {"ended", "idle", "error"}:
                self._busy_ringing_call = None
            return
        self._status.update(
            {
                "runtime_active": bool(incoming.get("active")),
                "avsdk": avsdk,
                "call": call,
                "last_error": incoming_error,
                "updated_at": time.time(),
            }
        )
        should_prepare = (
            not locally_ended_call
            and phase == "ringing"
            and call["accept_decision"] in {"pending", "accept"}
            and self._call_id is not None
            and self._ringing_event_call_id != self._call_id
        )
        if should_prepare:
            # 新版 NapCat 先完成名单策略后上报 pending，等待本机模型决定。
            # 旧版仍可能直接上报 accept，两种情况下同一来电只广播一次。
            self._ringing_event_call_id = self._call_id
            self._notify_status_event("ringing")
        phase_changed = phase != self._last_phase
        if phase_changed:
            logger.info(f"[qq_voice_call] 通话状态: {self._last_phase} -> {phase}")
            self._last_phase = phase
            if locally_ended_call:
                logger.info(
                    "[qq_voice_call] 已忽略本地结束电话的远端状态："
                    f"call_id={call_id} phase={phase}"
                )
            elif phase == "connected":
                self._notify_status_event("connected")
            elif phase == "ended":
                self._notify_status_event("ended")
            elif phase == "error":
                self._emit_error_event(incoming_error or "远程通话桥报告通话错误")
        if (
            not locally_ended_call
            and incoming_error
            and (incoming_error != previous_error or phase == "error" and phase_changed)
        ):
            self._emit_error_event(incoming_error)

    async def _send(self, payload: dict[str, Any], websocket: Any = None) -> bool:
        """向当前远程桥发送一条 JSON 消息；连接已断开时返回 False。"""
        target = websocket or self._websocket
        if target is None or target.closed:
            return False
        async with self._send_lock:
            if target.closed:
                return False
            await target.send_json(payload)
        return True

    async def control(self, action: str) -> dict[str, Any]:
        """控制当前活动通话；挂断仅结束 OneBot 本地电话房间。

        Args:
            action: ``accept``、``reject``、``hangup``、``mute`` 或 ``unmute``。

        Returns:
            结构化结果。``accept`` 等远程动作的 ``submitted`` 只代表请求已交给
            虚拟机插件；``hangup`` 返回 ``locally_ended`` 时只代表 OneBot 已停止
            本地电话媒体、模型和后续响应，不表示 QQ 对端已经结束。
        """
        safe_action = str(action or "").strip().lower()
        if safe_action not in CALL_CONTROL_ACTIONS:
            return {
                "success": False,
                "status": "invalid_action",
                "action": safe_action,
                "message": "不支持的通话控制动作",
            }

        current = self.get_status()
        call = current.get("call") if isinstance(current.get("call"), dict) else {}
        busy_call = self._busy_ringing_call if safe_action == "reject" else None
        if isinstance(busy_call, dict):
            # 上层收到第二通 ringing 后会调用 reject；此时主通话仍保持 connected，
            # 因而必须按这份忙线来电校验动作，而不是误判为当前主通话不能拒接。
            call = copy.deepcopy(busy_call)
        phase = str(call.get("phase") or "idle")
        if phase not in CALL_CONTROL_PHASES[safe_action]:
            if phase in {"idle", "ended", "error"}:
                return {
                    "success": False,
                    "status": "no_active_call",
                    "action": safe_action,
                    "phase": phase,
                    "message": "当前没有可控制的活动通话",
                }
            return {
                "success": False,
                "status": "invalid_state",
                "action": safe_action,
                "phase": phase,
                "message": f"当前阶段 {phase} 不允许执行 {safe_action}",
            }

        # 挂断没有可验证的 QQ 原生协议时，收口范围明确限定为 OneBot 电话房间。
        # 这条路径不依赖桥是否仍连接，也不会向 NapCat 发送未知控制消息。
        if safe_action == "hangup":
            return await self._end_call_locally(call, phase)

        websocket = self._websocket
        if websocket is None or websocket.closed or not self._status.get("runtime_active"):
            return {
                "success": False,
                "status": "bridge_disconnected",
                "action": safe_action,
                "phase": phase,
                "message": "虚拟机 NapCat 通话桥未处于可控制状态",
            }

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_controls[request_id] = future
        sent = False
        try:
            sent = await self._send(
                {
                    "type": "control",
                    "protocolVersion": self.protocol_version,
                    "requestId": request_id,
                    "action": safe_action,
                },
                websocket,
            )
            if not sent:
                return {
                    "success": False,
                    "status": "bridge_disconnected",
                    "action": safe_action,
                    "phase": phase,
                    "request_id": request_id,
                    "message": "控制请求未能发送到虚拟机 NapCat",
                }
            try:
                result = await asyncio.wait_for(future, timeout=5.0)
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "status": "timeout",
                    "action": safe_action,
                    "phase": phase,
                    "request_id": request_id,
                    "message": "虚拟机 NapCat 未在时限内返回控制结果",
                }
            remote = result if isinstance(result, dict) else {}
            remote_status = str(remote.get("status") or "failed")
            return {
                "success": remote_status in {"submitted", "succeeded"},
                "status": remote_status,
                "action": safe_action,
                "phase": str(remote.get("phase") or phase),
                "request_id": request_id,
                "supported": remote.get("supported"),
                "message": str(remote.get("message") or "").strip(),
            }
        finally:
            pending = self._pending_controls.pop(request_id, None)
            if pending is not None and not pending.done():
                pending.cancel()

    async def start_manual_hangup_capture(self, timeout_ms: int = 45000) -> dict[str, Any]:
        """在当前已接通电话中武装一次虚拟机本地原始协议捕获。

        原始 payload 不会通过桥回传；本方法只等待 NapCat 确认已经武装，
        最终文件摘要由后续 ``diagnostic_capture_result`` 异步更新。
        """

        if isinstance(timeout_ms, bool):
            timeout_ms = 0
        try:
            timeout_ms = int(timeout_ms)
        except (TypeError, ValueError):
            timeout_ms = 0
        if not 30000 <= timeout_ms <= 45000:
            return {
                "success": False,
                "status": "invalid_timeout",
                "message": "诊断超时必须在 30 到 45 秒之间",
            }

        current = self.get_status()
        call = current.get("call") if isinstance(current.get("call"), dict) else {}
        phase = str(call.get("phase") or "idle")
        call_id = str(call.get("call_id") or "").strip()
        if phase != "connected" or not call_id:
            return {
                "success": False,
                "status": "no_connected_call",
                "phase": phase,
                "message": "请先接通一通 QQ 语音电话，再武装挂断诊断",
            }

        capture = current.get("diagnostic_capture")
        if isinstance(capture, dict) and capture.get("status") in {"arming", "armed"}:
            return {
                "success": False,
                "status": "capture_busy",
                "capture_id": capture.get("capture_id"),
                "message": "当前电话已经有一项挂断协议诊断正在进行",
            }

        remote = current.get("remote") if isinstance(current.get("remote"), dict) else {}
        capabilities = remote.get("capabilities") if isinstance(remote.get("capabilities"), list) else []
        if MANUAL_HANGUP_CAPTURE_CAPABILITY not in capabilities:
            return {
                "success": False,
                "status": "unsupported",
                "message": "虚拟机 NapCat 插件版本不支持一次性挂断协议捕获，请先部署最新插件包",
            }

        websocket = self._websocket
        if websocket is None or websocket.closed or not current.get("runtime_active"):
            return {
                "success": False,
                "status": "bridge_disconnected",
                "message": "虚拟机 NapCat 通话桥未处于可诊断状态",
            }

        request_id = uuid.uuid4().hex
        capture_id = uuid.uuid4().hex
        capture_status = {
            "kind": "manual_hangup",
            "mode": "raw",
            "status": "arming",
            "request_id": request_id,
            "capture_id": capture_id,
            "call_id": call_id,
            "event_count": 0,
            "byte_count": 0,
            "file_path": None,
            "file_sha256": None,
            "reason": None,
            "started_at": None,
            "finished_at": None,
        }
        self._status["diagnostic_capture"] = capture_status
        self._status["updated_at"] = time.time()
        future = asyncio.get_running_loop().create_future()
        self._pending_diagnostic_arms[request_id] = future
        try:
            sent = await self._send(
                {
                    "type": "diagnostic_capture_start",
                    "protocolVersion": self.protocol_version,
                    "requestId": request_id,
                    "captureId": capture_id,
                    "callId": call_id,
                    "kind": "manual_hangup",
                    "mode": "raw",
                    "timeoutMs": timeout_ms,
                },
                websocket,
            )
            if not sent:
                capture_status.update({"status": "failed", "reason": "bridge_disconnected"})
                return {
                    "success": False,
                    "status": "bridge_disconnected",
                    "message": "诊断请求未能发送到虚拟机 NapCat",
                }
            try:
                result = await asyncio.wait_for(future, timeout=5.0)
            except asyncio.TimeoutError:
                capture_status.update({"status": "failed", "reason": "arm_timeout"})
                return {
                    "success": False,
                    "status": "timeout",
                    "capture_id": capture_id,
                    "message": "虚拟机 NapCat 未在时限内确认诊断已武装",
                }
            remote_status = str((result or {}).get("status") or "rejected").strip().lower()
            if remote_status != "armed":
                return {
                    "success": False,
                    "status": "rejected",
                    "capture_id": capture_id,
                    "message": str((result or {}).get("message") or "虚拟机拒绝了诊断武装请求"),
                }
            return {
                "success": True,
                "status": "armed",
                "capture_id": capture_id,
                "call_id": call_id,
                "timeout_ms": timeout_ms,
                "message": "诊断已武装，请从可实际操作通话的对端 QQ 客户端手动挂断本次电话",
            }
        finally:
            pending = self._pending_diagnostic_arms.pop(request_id, None)
            if pending is not None and not pending.done():
                pending.cancel()

    async def start_visible_surface_diagnostic(self) -> dict[str, Any]:
        """请求虚拟机 Host 执行一次不调用 AVSDK 的可见方法表面反射。

        该诊断不要求正在通话，也不发送 AVSDK 命令。每个 Host 生命周期只允许
        NapCat 执行一次；若要重新检测，必须由 Host 正常重启后重新建立桥连接。
        """

        current = self.get_status()
        remote = current.get("remote") if isinstance(current.get("remote"), dict) else {}
        capabilities = remote.get("capabilities") if isinstance(remote.get("capabilities"), list) else []
        if AVSDK_VISIBLE_SURFACE_DIAGNOSTIC_CAPABILITY not in capabilities:
            return {
                "success": False,
                "status": "unsupported",
                "message": "虚拟机 NapCat 插件不支持 AVSDK 可见面诊断，请先部署最新插件包",
            }

        existing = current.get("diagnostic_visible_surface")
        existing = existing if isinstance(existing, dict) else {}
        existing_status = str(existing.get("status") or "idle")
        if existing_status in {"requesting", "running"}:
            return {
                "success": False,
                "status": "diagnostic_busy",
                "message": "AVSDK 可见面诊断正在执行，请等待当前结果",
            }
        if existing_status in {"completed", "failed"} and existing.get("request_id"):
            return {
                "success": existing_status == "completed",
                "status": f"already_{existing_status}",
                "message": "当前 AVSDK Host 生命周期已完成一次诊断；重启虚拟机 NapCat/Host 后才能重新检查",
                "diagnostic": copy.deepcopy(existing),
            }

        websocket = self._websocket
        if websocket is None or websocket.closed or not current.get("runtime_active"):
            return {
                "success": False,
                "status": "bridge_disconnected",
                "message": "虚拟机 NapCat 通话桥未处于可诊断状态",
            }
        avsdk = current.get("avsdk") if isinstance(current.get("avsdk"), dict) else {}
        if not avsdk.get("host_ready") or not avsdk.get("plugin_found"):
            return {
                "success": False,
                "status": "host_not_ready",
                "message": "AVSDK Host 尚未就绪，稍后刷新通话状态再检查",
            }

        request_id = uuid.uuid4().hex
        diagnostic = {
            "kind": "avsdk_visible_surface",
            "request_id": request_id,
            "status": "requesting",
            "reason": None,
            "started_at": time.time(),
            "finished_at": None,
            "report": None,
        }
        self._status["diagnostic_visible_surface"] = diagnostic
        self._status["updated_at"] = time.time()
        future = asyncio.get_running_loop().create_future()
        self._pending_visible_surface_diagnostics[request_id] = future
        try:
            sent = await self._send(
                {
                    "type": "diagnostic_visible_surface_start",
                    "protocolVersion": self.protocol_version,
                    "requestId": request_id,
                    "kind": "avsdk_visible_surface",
                },
                websocket,
            )
            if not sent:
                diagnostic.update(
                    {
                        "status": "failed",
                        "reason": "bridge_disconnected",
                        "finished_at": time.time(),
                    }
                )
                return {
                    "success": False,
                    "status": "bridge_disconnected",
                    "message": "诊断请求未能发送到虚拟机 NapCat",
                }
            try:
                result = await asyncio.wait_for(future, timeout=6.0)
            except asyncio.TimeoutError:
                diagnostic.update(
                    {
                        "status": "failed",
                        "reason": "host_request_failed",
                        "finished_at": time.time(),
                    }
                )
                return {
                    "success": False,
                    "status": "timeout",
                    "message": "虚拟机 NapCat 未在时限内返回 AVSDK 可见面诊断结果",
                }
            result = result if isinstance(result, dict) else {}
            status = str(result.get("status") or "failed")
            return {
                "success": status == "completed",
                "status": status,
                "message": "AVSDK 可见面诊断完成" if status == "completed" else "AVSDK 可见面诊断未完成",
                "diagnostic": copy.deepcopy(result),
            }
        finally:
            pending = self._pending_visible_surface_diagnostics.pop(request_id, None)
            if pending is not None and not pending.done():
                pending.cancel()

    async def start_service_surface_diagnostic(self) -> dict[str, Any]:
        """请求当前 NapCat AVSDK Service 执行一次无调用的受限反射。"""

        current = self.get_status()
        remote = current.get("remote") if isinstance(current.get("remote"), dict) else {}
        capabilities = remote.get("capabilities") if isinstance(remote.get("capabilities"), list) else []
        if AVSDK_SERVICE_SURFACE_DIAGNOSTIC_CAPABILITY not in capabilities:
            return {
                "success": False,
                "status": "unsupported",
                "message": "虚拟机 NapCat 插件不支持 AVSDK Service 诊断，请先部署最新插件包",
            }

        existing = current.get("diagnostic_service_surface")
        existing = existing if isinstance(existing, dict) else {}
        existing_status = str(existing.get("status") or "idle")
        if existing_status in {"requesting", "running"}:
            return {
                "success": False,
                "status": "diagnostic_busy",
                "message": "AVSDK Service 诊断正在执行，请等待当前结果",
            }
        if existing_status in {"completed", "failed"} and existing.get("request_id"):
            return {
                "success": existing_status == "completed",
                "status": f"already_{existing_status}",
                "message": "当前 AVSDK Service 运行周期已完成一次诊断；重启虚拟机 NapCat 后才能重新检查",
                "diagnostic": copy.deepcopy(existing),
            }

        websocket = self._websocket
        if websocket is None or websocket.closed or not current.get("runtime_active"):
            return {
                "success": False,
                "status": "bridge_disconnected",
                "message": "虚拟机 NapCat 通话桥未处于可诊断状态",
            }
        avsdk = current.get("avsdk") if isinstance(current.get("avsdk"), dict) else {}
        if not avsdk.get("service_available"):
            return {
                "success": False,
                "status": "service_unavailable",
                "message": "NapCat 当前没有可用的 AVSDK Service，稍后刷新通话状态再检查",
            }

        request_id = uuid.uuid4().hex
        diagnostic = {
            "kind": "avsdk_service_surface",
            "request_id": request_id,
            "status": "requesting",
            "reason": None,
            "started_at": time.time(),
            "finished_at": None,
            "report": None,
        }
        self._status["diagnostic_service_surface"] = diagnostic
        self._status["updated_at"] = time.time()
        future = asyncio.get_running_loop().create_future()
        self._pending_service_surface_diagnostics[request_id] = future
        try:
            sent = await self._send(
                {
                    "type": "diagnostic_avsdk_service_surface_start",
                    "protocolVersion": self.protocol_version,
                    "requestId": request_id,
                    "kind": "avsdk_service_surface",
                },
                websocket,
            )
            if not sent:
                diagnostic.update(
                    {
                        "status": "failed",
                        "reason": "bridge_disconnected",
                        "finished_at": time.time(),
                    }
                )
                return {
                    "success": False,
                    "status": "bridge_disconnected",
                    "message": "诊断请求未能发送到虚拟机 NapCat",
                }
            try:
                result = await asyncio.wait_for(future, timeout=6.0)
            except asyncio.TimeoutError:
                diagnostic.update(
                    {
                        "status": "failed",
                        "reason": "reflection_failed",
                        "finished_at": time.time(),
                    }
                )
                return {
                    "success": False,
                    "status": "timeout",
                    "message": "虚拟机 NapCat 未在时限内返回 AVSDK Service 诊断结果",
                }
            result = result if isinstance(result, dict) else {}
            status = str(result.get("status") or "failed")
            return {
                "success": status == "completed",
                "status": status,
                "message": "AVSDK Service 诊断完成" if status == "completed" else "AVSDK Service 诊断未完成",
                "diagnostic": copy.deepcopy(result),
            }
        finally:
            pending = self._pending_service_surface_diagnostics.pop(request_id, None)
            if pending is not None and not pending.done():
                pending.cancel()

    async def start_static_artifacts_diagnostic(self) -> dict[str, Any]:
        """请求 VM 插件扫描两个固定安装目标，不调用 AVSDK 或读取用户数据。"""

        current = self.get_status()
        remote = current.get("remote") if isinstance(current.get("remote"), dict) else {}
        capabilities = remote.get("capabilities") if isinstance(remote.get("capabilities"), list) else []
        if AVSDK_STATIC_ARTIFACTS_DIAGNOSTIC_CAPABILITY not in capabilities:
            return {
                "success": False,
                "status": "unsupported",
                "message": "虚拟机 NapCat 插件不支持 AVSDK 静态资源诊断，请先部署最新插件包",
            }

        existing = current.get("diagnostic_static_artifacts")
        existing = existing if isinstance(existing, dict) else {}
        existing_status = str(existing.get("status") or "idle")
        if existing_status in {"requesting", "running"}:
            return {
                "success": False,
                "status": "diagnostic_busy",
                "message": "AVSDK 静态资源诊断正在执行，请等待当前结果",
            }
        if existing_status in {"completed", "failed"} and existing.get("request_id"):
            return {
                "success": existing_status == "completed",
                "status": f"already_{existing_status}",
                "message": "当前 NapCat 插件生命周期已完成一次静态资源诊断；重启虚拟机 NapCat 后才能重新检查",
                "diagnostic": copy.deepcopy(existing),
            }

        websocket = self._websocket
        if websocket is None or websocket.closed or not current.get("runtime_active"):
            return {
                "success": False,
                "status": "bridge_disconnected",
                "message": "虚拟机 NapCat 通话桥未处于可诊断状态",
            }

        request_id = uuid.uuid4().hex
        diagnostic = {
            "kind": "avsdk_static_artifacts",
            "request_id": request_id,
            "status": "requesting",
            "reason": None,
            "started_at": time.time(),
            "finished_at": None,
            "report": None,
        }
        self._status["diagnostic_static_artifacts"] = diagnostic
        self._status["updated_at"] = time.time()
        future = asyncio.get_running_loop().create_future()
        self._pending_static_artifacts_diagnostics[request_id] = future
        try:
            sent = await self._send(
                {
                    "type": "diagnostic_static_artifacts_start",
                    "protocolVersion": self.protocol_version,
                    "requestId": request_id,
                    "kind": "avsdk_static_artifacts",
                },
                websocket,
            )
            if not sent:
                diagnostic.update(
                    {
                        "status": "failed",
                        "reason": "bridge_disconnected",
                        "finished_at": time.time(),
                    }
                )
                return {
                    "success": False,
                    "status": "bridge_disconnected",
                    "message": "诊断请求未能发送到虚拟机 NapCat",
                }
            try:
                result = await asyncio.wait_for(future, timeout=20.0)
            except asyncio.TimeoutError:
                diagnostic.update(
                    {
                        "status": "failed",
                        "reason": "scan_failed",
                        "finished_at": time.time(),
                    }
                )
                return {
                    "success": False,
                    "status": "timeout",
                    "message": "虚拟机 NapCat 未在时限内返回 AVSDK 静态资源诊断结果",
                }
            result = result if isinstance(result, dict) else {}
            status = str(result.get("status") or "failed")
            return {
                "success": status == "completed",
                "status": status,
                "message": "AVSDK 静态资源诊断完成" if status == "completed" else "AVSDK 静态资源诊断未完成",
                "diagnostic": copy.deepcopy(result),
            }
        finally:
            pending = self._pending_static_artifacts_diagnostics.pop(request_id, None)
            if pending is not None and not pending.done():
                pending.cancel()

    def _set_error(self, message: str) -> None:
        normalized = str(message)
        previous = self._status.get("last_error")
        self._status["last_error"] = normalized
        self._status["updated_at"] = time.time()
        logger.warning(f"[qq_voice_call] {normalized}")
        if normalized != previous:
            self._emit_error_event(normalized)

    async def stop_accepting(self) -> None:
        """阻止新连接，并先通知远程插件销毁 AVSDK 资源。"""
        self._accepting = False
        try:
            await self._send({"type": "deactivate", "reason": "onebot_stopping"})
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[qq_voice_call] 发送远程停用命令失败: {error}")

    async def terminate(self) -> None:
        """关闭桥接连接；NapCat 端也会因断线再次执行幂等清理。"""
        await self.stop_accepting()
        websocket = self._websocket
        if websocket is not None and not websocket.closed:
            await websocket.close(code=1001, message=b"OneBot plugin unloaded")
        self._websocket = None
        self._status.update(
            {
                "bridge_connected": False,
                "runtime_active": False,
                "updated_at": time.time(),
            }
        )


__all__ = [
    "CALL_CONTROL_ACTIONS",
    "CALL_CONTROL_PHASES",
    "DIALOGUE_DEFAULT_CONFIG",
    "QQVoiceCallRuntime",
    "normalize_config",
    "normalize_dialogue_config",
]
