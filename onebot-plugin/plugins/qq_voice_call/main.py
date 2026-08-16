"""QQ 原生语音通话 OneBot 插件入口。"""

from __future__ import annotations

from onebot.logger import logger
from onebot.runtime import (
    register_qq_voice_call_runtime,
    register_qq_voice_dialogue_service,
    unregister_qq_voice_call_runtime,
    unregister_qq_voice_dialogue_service,
)

from .dialogue import QQVoiceDialogueRuntime
from .runtime import QQVoiceCallRuntime


_runtime = None
_dialogue_runtime = None


def _schedule_dialogue_stop(dialogue, reason="replaced"):
    """在同步插件注册入口中安排旧电话对话运行时的异步清理。"""

    if dialogue is None:
        return
    try:
        import asyncio

        loop = asyncio.get_running_loop()
        loop.create_task(dialogue.stop(reason))
    except RuntimeError:
        # OneBot 退出阶段通常已经有事件循环；没有事件循环时由对象的
        # 媒体 close 和路由快照同步兜底，不能为了清理阻断新插件加载。
        try:
            dialogue.media.close()
        except Exception:
            pass
        try:
            call_id = str(dialogue.get_status().get("call_id") or "").strip() or None
            dialogue.route_guard.restore(call_id)
        except Exception:
            pass


def register(_hook_manager, config):
    """创建通话运行时；没有注册消息钩子，普通 QQ 消息链不受影响。"""
    global _runtime, _dialogue_runtime
    if isinstance(config, dict) and config.get("enabled") is False:
        previous = _runtime
        previous_dialogue = _dialogue_runtime
        _runtime = None
        _dialogue_runtime = None
        unregister_qq_voice_call_runtime(previous)
        unregister_qq_voice_dialogue_service(previous_dialogue)
        _schedule_dialogue_stop(previous_dialogue, "disabled")
        logger.info("[qq_voice_call] 插件配置已禁用，不注册 QQ 通话运行时")
        return
    previous = _runtime
    previous_dialogue = _dialogue_runtime
    try:
        replacement = QQVoiceCallRuntime(config)
        dialogue = QQVoiceDialogueRuntime(config, call_control=replacement.control)
        replacement.set_dialogue_service(dialogue)
        replacement.set_admission_service(dialogue)
        replacement.add_status_listener(dialogue.handle_call_event)
    except ValueError as error:
        _runtime = previous
        logger.error(f"[qq_voice_call] 配置无效，插件未启动: {error}")
        return
    _runtime = replacement
    _dialogue_runtime = dialogue
    unregister_qq_voice_call_runtime(previous)
    unregister_qq_voice_dialogue_service(previous_dialogue)
    _schedule_dialogue_stop(previous_dialogue, "replaced")
    register_qq_voice_call_runtime(_runtime)
    register_qq_voice_dialogue_service(_dialogue_runtime)
    logger.info("[qq_voice_call] OneBot 通话桥已就绪，等待虚拟机 NapCat 主动连接")
    dialogue_config = dialogue.config
    if dialogue_config.get("enabled"):
        audio_config = dialogue_config.get("audio") or {}
        logger.info(
            "[qq_voice_call] 电话模型对话已启用: "
            f"input_device={audio_config.get('input_device_index')} "
            f"output_device={audio_config.get('output_device_index')} "
            f"mode={dialogue_config.get('mode') or 'full_duplex'}"
        )
    else:
        logger.info("[qq_voice_call] 电话模型对话未启用，仅保留原生通话桥")


async def terminate():
    """OneBot 退出时先停用远程 AVSDK，再注销运行时。"""
    global _runtime, _dialogue_runtime
    if _runtime is None:
        return
    runtime = _runtime
    dialogue = _dialogue_runtime
    _runtime = None
    _dialogue_runtime = None
    unregister_qq_voice_call_runtime(runtime)
    unregister_qq_voice_dialogue_service(dialogue)
    if dialogue is not None:
        await dialogue.stop("plugin_terminated")
    await runtime.terminate()
