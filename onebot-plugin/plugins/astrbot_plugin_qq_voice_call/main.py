"""为 QQ 私聊上下文提供当前原生通话控制工具。"""

from __future__ import annotations

from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from onebot.runtime import get_qq_voice_call_runtime


@register("qq_voice_call", "local", "QQ 原生语音通话状态与控制工具", "1.0")
class QQVoiceCallTools(Star):
    """只控制当前活动通话，不允许模型指定任意 QQ 目标。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context, config or {})

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """说明通话控制结果的真实含义，避免把提交状态误报成 QQ 已执行。"""
        instruction = (
            "QQ 原生语音通话工具只控制当前活动通话，不得传入或猜测其他 QQ 号。"
            "挂断工具返回 queued_after_reply 只代表已登记本轮回复播放完成后的挂断，"
            "电话结束语播放完成后 OneBot 会结束自身电话房间；这不代表 QQ 对端已经断线。"
            "工具返回 locally_ended 代表 OneBot 本地已停止电话媒体、模型和后续响应，"
            "远端状态只在 QQ语音通话页面作为提示。"
            "工具返回 submitted 只代表请求已交给虚拟机 NapCat，必须等待后续状态确认后"
            "才能说已经接听；返回 unsupported、failed、timeout 或 no_active_call"
            "时不得声称动作完成。"
        )
        existing = str(getattr(req, "stable_system_prompt", "") or "").strip()
        req.stable_system_prompt = "\n\n".join(part for part in (existing, instruction) if part)

    @staticmethod
    def _private_scope_error(
        event: AstrMessageEvent,
        runtime=None,
    ) -> dict[str, Any] | None:
        """限制为当前来电者的私聊上下文，避免跨用户控制全局电话。"""
        if event.get_group_id():
            return {"success": False, "status": "private_chat_required", "message": "通话控制只允许在私聊上下文使用"}
        if runtime is not None:
            status = runtime.get_status()
            call = status.get("call") if isinstance(status, dict) else {}
            caller_uin = str(call.get("caller_uin") or "").strip() if isinstance(call, dict) else ""
            sender_id = str(event.get_sender_id() or "").strip()
            if caller_uin and sender_id and caller_uin != sender_id:
                return {
                    "success": False,
                    "status": "caller_scope_mismatch",
                    "message": "当前私聊用户不是活动通话的来电者",
                }
        return None

    @staticmethod
    def _runtime_error() -> dict[str, Any]:
        return {
            "success": False,
            "status": "plugin_unavailable",
            "message": "OneBot QQ 语音通话插件未启动",
        }

    async def _control(self, event: AstrMessageEvent, action: str) -> dict[str, Any]:
        runtime = get_qq_voice_call_runtime()
        if runtime is None:
            return self._runtime_error()
        scope_error = self._private_scope_error(event, runtime)
        if scope_error:
            return scope_error
        # 振铃决策期的接听/拒接只是模型意图。电话分支、开场白、TTS 和音频
        # 路由尚未准备好时绝不能提前向 AVSDK 提交 accept。
        if action in {"accept", "reject"}:
            record_decision = getattr(runtime, "record_admission_decision", None)
            if callable(record_decision):
                try:
                    caller_uin = str(event.get_sender_id() or "")
                except Exception:
                    caller_uin = ""
                decision = await record_decision(
                    action,
                    caller_uin=caller_uin,
                )
                if decision is not None:
                    return decision
        if action == "hangup":
            # 模型挂断必须绑定本轮电话回复；禁止回退到即时 control，避免
            # 结束语尚未写入电话输出就被桥端截断。
            record_after_reply = getattr(runtime, "record_hangup_after_reply", None)
            if callable(record_after_reply):
                try:
                    caller_uin = str(event.get_sender_id() or "")
                except Exception:
                    caller_uin = ""
                return await record_after_reply(caller_uin=caller_uin)
            return {
                "success": False,
                "status": "dialogue_unavailable",
                "action": action,
                "message": "当前 OneBot 运行时没有电话回复完成后的挂断入口",
            }
        control = getattr(runtime, "control", None)
        if not callable(control):
            return {
                "success": False,
                "status": "unsupported",
                "action": action,
                "message": "当前 OneBot 运行时没有通话控制接口",
            }
        return await control(action)

    @filter.llm_tool(name="qq_voice_call_status")
    async def status(self, event: AstrMessageEvent):
        """查询当前 QQ 原生通话状态和桥接诊断信息。"""
        runtime = get_qq_voice_call_runtime()
        if runtime is None:
            return self._runtime_error()
        scope_error = self._private_scope_error(event, runtime)
        if scope_error:
            return scope_error
        return {"success": True, "status": "ok", "data": runtime.get_status()}

    @filter.llm_tool(name="qq_voice_call_accept")
    async def accept(self, event: AstrMessageEvent):
        """接听当前正在振铃的私聊原生来电。"""
        return await self._control(event, "accept")

    @filter.llm_tool(name="qq_voice_call_reject")
    async def reject(self, event: AstrMessageEvent):
        """拒接当前正在振铃的私聊原生来电。"""
        return await self._control(event, "reject")

    @filter.llm_tool(name="qq_voice_call_hangup")
    async def hangup(self, event: AstrMessageEvent):
        """挂断当前活动的原生语音通话。"""
        return await self._control(event, "hangup")


__all__ = ["QQVoiceCallTools"]
