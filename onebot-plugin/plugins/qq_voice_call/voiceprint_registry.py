"""QQ 语音通话的全局声纹登记与联系人语境适配。

普通本机语音聊天继续使用 ``utils.sv.SV_Manager`` 及其原有目录。
本模块只为 QQ 语音通话创建一个独立的管理边界：所有 QQ 联系人共享
同一份声纹登记文件，但每条登记额外记录首次来源、角色和联系人绑定，
从而让小明的声音在小李的电话中仍然能够被识别成小明。
"""

from __future__ import annotations

import copy
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

from utils.gpt_model.file_lock import cross_process_lock
from utils.sv import SV_Manager


DEFAULT_UNKNOWN_NAME = "未知说话人"
DEFAULT_OTHER_NAME_TEMPLATE = "{caller_name}旁边的人{ordinal_suffix}"
DEFAULT_CONTACT_ROLE = "contact"
DEFAULT_PARTICIPANT_ROLE = "participant"


def _text(value: Any) -> str:
    """把用户输入转换为去除首尾空白的文本。"""

    return str(value or "").strip()


def _aliases(value: Any) -> list[str]:
    """规范化别名列表并保持原有顺序。"""

    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        alias = _text(item)
        if alias and alias not in result:
            result.append(alias)
    return result


class QQVoiceprintRegistry:
    """管理全局 QQ 电话声纹，并为未知声音生成联系人语境名称。

    Args:
        enroll_path: QQ 专用声纹目录。该目录不应指向普通本机语音聊天的
            ``talk.sv.enroll_path``。
        other_name_template: 当前联系人周围的未知参与者命名模板。模板可用
            ``caller_name``、``ordinal`` 和 ``ordinal_suffix`` 三个字段。
        unknown_name: 没有联系人信息且无法登记时使用的展示名称。
        default_role: 没有明确联系人归属的登记角色。

    ``speaker_info.json`` 的顶层仍保持 ``SV_Manager`` 需要的
    ``speakers``/``next_id`` 结构，并新增 ``contacts`` 字典。旧登记记录
    没有这些字段时会按兼容方式补齐，不会影响本机聊天声纹库。
    """

    schema_version = 1

    def __init__(
        self,
        enroll_path: str | os.PathLike[str],
        *,
        other_name_template: str = DEFAULT_OTHER_NAME_TEMPLATE,
        unknown_name: str = DEFAULT_UNKNOWN_NAME,
        default_role: str = DEFAULT_PARTICIPANT_ROLE,
        manager: SV_Manager | None = None,
    ) -> None:
        self.enroll_path = os.path.abspath(os.fspath(enroll_path))
        # 允许测试和上层运行时注入已初始化的管理器，但默认仍使用项目现有实现。
        self.manager = manager or SV_Manager(self.enroll_path)
        template = _text(other_name_template) or DEFAULT_OTHER_NAME_TEMPLATE
        # 配置界面通常只填写“旁边的人”这样的后缀；没有模板字段时自动补上联系人名。
        if "{caller_name" not in template and "{ordinal" not in template:
            template = "{caller_name}" + template + "{ordinal_suffix}"
        self.other_name_template = template
        self.unknown_name = _text(unknown_name) or DEFAULT_UNKNOWN_NAME
        self.default_role = _text(default_role) or DEFAULT_PARTICIPANT_ROLE
        # ponytail: 单进程声纹登记通常只有 OneBot 与 WebUI 两个调用者，
        # 先用实例锁配合 SV_Manager 的跨进程原子保存；并发量上来后再拆分账户锁。
        self._lock = threading.RLock()
        self._ensure_schema()

    @contextmanager
    def _transaction(self):
        """在同一命名互斥量内刷新并操作 QQ 全局声纹快照。

        该锁不会创建磁盘文件。OneBot 和 WebUI 都必须经过这里，才能保证
        ``next_id`` 分配、登记音频复制和 JSON 替换属于同一个事务。
        """

        with self._lock:
            with cross_process_lock(self.enroll_path, ".speaker_info.lock"):
                self.manager.reload_speaker_info()
                yield

    @property
    def speaker_info_file(self) -> str:
        """返回全局 QQ 声纹登记文件路径。"""

        return self.manager.speaker_info_file

    def _ensure_schema(self) -> None:
        """为旧登记文件补充 QQ 联系人映射所需的顶层字段。"""

        with self._transaction():
            info = self.manager.speaker_info
            changed = False
            if not isinstance(info.get("speakers"), dict):
                info["speakers"] = {}
                changed = True
            if not isinstance(info.get("contacts"), dict):
                info["contacts"] = {}
                changed = True
            valid_ids = set(info["speakers"])
            for contact in info["contacts"].values():
                if not isinstance(contact, dict):
                    continue
                primary_id = _text(contact.get("primary_speaker_id"))
                if primary_id and primary_id not in valid_ids:
                    contact.pop("primary_speaker_id", None)
                    changed = True
            if info.get("qq_voiceprint_schema_version") != self.schema_version:
                info["qq_voiceprint_schema_version"] = self.schema_version
                changed = True
            if changed:
                # 只有 QQ 专用目录会写入这些字段，普通本机声纹管理器不会被触碰。
                self.manager.save_speaker_info()

    def _contacts(self) -> dict[str, dict[str, Any]]:
        """取得联系人映射；调用方必须已持有 ``self._lock``。"""

        contacts = self.manager.speaker_info.get("contacts")
        if not isinstance(contacts, dict):
            contacts = {}
            self.manager.speaker_info["contacts"] = contacts
        return contacts

    @staticmethod
    def _contact_key(caller_uin: Any) -> str:
        """将 QQ 号规范化为持久化键；没有 QQ 号时返回空字符串。"""

        return _text(caller_uin)

    def _contact_record(
        self,
        caller_uin: Any,
        *,
        create: bool = False,
        caller_name: Any = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """读取或创建一个联系人映射记录。"""

        key = self._contact_key(caller_uin)
        if not key:
            return "", None
        contacts = self._contacts()
        record = contacts.get(key)
        if not isinstance(record, dict):
            if not create:
                return key, None
            record = {}
            contacts[key] = record
        if create:
            nickname = _text(caller_name)
            if nickname:
                record["nickname"] = nickname
            record.setdefault("unknown_count", 0)
        return key, record

    def _persist(self) -> bool:
        """通过现有原子写入和命名互斥锁保存登记信息。"""

        return bool(self.manager.save_speaker_info())

    def get_all_speakers(self) -> dict[str, dict[str, Any]]:
        """返回全局声纹快照，避免调用方直接改写管理器内存。"""

        with self._transaction():
            return copy.deepcopy(self.manager.get_all_speakers())

    def get_speaker_info(self, speaker_id: Any) -> dict[str, Any] | None:
        """返回一个说话人的元数据快照。"""

        with self._transaction():
            info = self.manager.get_speaker_info(str(speaker_id))
            return copy.deepcopy(info) if isinstance(info, dict) else None

    def get_contact_binding(self, caller_uin: Any) -> dict[str, Any] | None:
        """返回联系人当前绑定的主声纹和未知参与者计数。"""

        with self._transaction():
            _key, record = self._contact_record(caller_uin)
            return copy.deepcopy(record) if isinstance(record, dict) else None

    def bind_contact_primary(
        self,
        caller_uin: Any,
        speaker_id: Any,
        *,
        caller_name: Any = None,
        replace: bool = False,
    ) -> bool:
        """把联系人绑定到全局声纹；绑定只用于语境，不限制后续匹配。

        Args:
            caller_uin: 联系人的 QQ 号。
            speaker_id: 已存在的全局声纹 ID。
            caller_name: 可选的最新联系人昵称。
            replace: 是否允许覆盖已有主声纹绑定。

        Returns:
            绑定并成功持久化时返回 ``True``。
        """

        key = self._contact_key(caller_uin)
        speaker_key = _text(speaker_id)
        if not key or not speaker_key:
            return False
        with self._transaction():
            if speaker_key not in self.manager.speaker_info["speakers"]:
                return False
            _key, record = self._contact_record(
                key, create=True, caller_name=caller_name
            )
            assert record is not None
            old_record = copy.deepcopy(record)
            current = _text(record.get("primary_speaker_id"))
            if current and current != speaker_key and not replace:
                return False
            record["primary_speaker_id"] = speaker_key
            if _text(caller_name):
                record["nickname"] = _text(caller_name)
            record["updated_at"] = time.time()
            if self._persist():
                return True
            record.clear()
            record.update(old_record)
            return False

    def _unknown_name(
        self,
        caller_name: str,
        ordinal: int,
    ) -> str:
        """根据联系人昵称和序号生成未知参与者名称。"""

        caller = caller_name or "电话参与者"
        suffix = "" if ordinal <= 1 else str(ordinal)
        try:
            name = self.other_name_template.format(
                caller_name=caller,
                ordinal=max(1, ordinal),
                ordinal_suffix=suffix,
            )
        except (KeyError, IndexError, ValueError):
            # 配置模板写错时仍给出可读名称，不能阻断通话。
            name = f"{caller}旁边的人{suffix}"
        return _text(name) or f"{caller}旁边的人{suffix}"

    def _enroll_with_metadata(
        self,
        audio_file_path: str | os.PathLike[str],
        *,
        name: str,
        role: str,
        aliases: list[str],
        caller_uin: str,
        caller_name: str,
        auto_registered: bool,
    ) -> tuple[str | None, str | None]:
        """调用通用管理器登记一条声纹并写入 QQ 来源元数据。"""

        metadata: dict[str, Any] = {
            "name": name,
            "role": role,
            "aliases": aliases,
            "registered_from_uin": caller_uin or None,
            "registered_from_nickname": caller_name or None,
            "auto_registered": bool(auto_registered),
            "registered_at": time.time(),
        }
        speaker_id, _generated_name = self.manager.enroll_speaker(
            os.fspath(audio_file_path),
            default_prefix=name or "说话人",
            metadata=metadata,
        )
        if speaker_id is None:
            return None, None
        # 管理器返回的是兼容旧接口生成的名称；QQ 侧以元数据中的精确名称为准。
        return str(speaker_id), name

    def enroll_unknown(
        self,
        audio_file_path: str | os.PathLike[str],
        caller_uin: Any = None,
        caller_name: Any = None,
        *,
        role: str | None = None,
        aliases: Any = None,
        auto_registered: bool = True,
        name: str | None = None,
        is_active: Callable[[], bool] | None = None,
    ) -> tuple[str | None, str | None]:
        """登记一个当前电话中尚未匹配的声音。

        联系人尚未绑定主声纹时，第一条未知声音使用联系人昵称作为名称；
        之后的未知声音使用 ``other_name_template``。登记库本身始终是全局的，
        ``registered_from_uin`` 只记录审计来源，不参与声纹匹配限制。
        """

        key = self._contact_key(caller_uin)
        nickname = _text(caller_name)
        with self._transaction():
            if callable(is_active) and not is_active():
                return None, None
            info = self.manager.speaker_info
            contacts_before = copy.deepcopy(info.get("contacts", {}))
            next_id = str(info.get("next_id", 1))
            _key, contact = self._contact_record(
                key, create=bool(key), caller_name=nickname
            )
            primary_id = _text(contact.get("primary_speaker_id")) if contact else ""
            if primary_id and primary_id not in info["speakers"]:
                primary_id = ""
                if contact is not None:
                    contact.pop("primary_speaker_id", None)

            if name is not None and _text(name):
                display_name = _text(name)
            elif not primary_id and nickname:
                display_name = nickname
            elif contact is not None:
                ordinal = int(contact.get("unknown_count", 0) or 0) + 1
                display_name = self._unknown_name(nickname, ordinal)
            else:
                display_name = self.unknown_name

            is_primary = bool(key and not primary_id)
            selected_role = _text(role) or (
                DEFAULT_CONTACT_ROLE if is_primary else self.default_role
            )
            aliases_list = _aliases(aliases)

            if callable(is_active) and not is_active():
                return None, None

            # 在登记前写入联系人计数/主绑定，使管理器的一次原子保存包含两类数据。
            if contact is not None:
                if is_primary:
                    contact["primary_speaker_id"] = next_id
                else:
                    contact["unknown_count"] = int(
                        contact.get("unknown_count", 0) or 0
                    ) + 1
                contact["updated_at"] = time.time()

            speaker_id, speaker_name = self._enroll_with_metadata(
                audio_file_path,
                name=display_name,
                role=selected_role,
                aliases=aliases_list,
                caller_uin=key,
                caller_name=nickname,
                auto_registered=auto_registered,
            )
            if speaker_id is None:
                # 登记失败时撤销本次联系人映射和计数，避免下次跳过主声纹。
                info["contacts"] = contacts_before
                return None, None

            # 理论上 ID 等于登记前的 next_id；若管理器未来改变分配策略，修正绑定。
            if contact is not None and is_primary and contact.get("primary_speaker_id") != speaker_id:
                contact["primary_speaker_id"] = speaker_id
                if not self._persist():
                    info["contacts"] = contacts_before
            return speaker_id, speaker_name

    # 语义别名：上层运行时可用更明确的命名调用同一规则。
    enroll_unknown_speaker = enroll_unknown

    def identify_speaker(
        self,
        input_audio_path: str | os.PathLike[str],
        threshold: float,
        sv_pipeline: Any,
    ) -> tuple[str | None, str | None]:
        """在全局登记库中匹配一段音频，具体算法复用 ``SV_Manager``。"""

        with self._transaction():
            return self.manager.identify_speaker(
                os.fspath(input_audio_path), threshold, sv_pipeline
            )

    def resolve_utterance(
        self,
        input_audio_path: str | os.PathLike[str],
        *,
        caller_uin: Any = None,
        caller_name: Any = None,
        threshold: float = 0.35,
        sv_pipeline: Any = None,
        duration_seconds: float | None = None,
        auto_enroll_unknown: bool = True,
        min_enroll_duration: float = 3.0,
        first_utterance: bool = False,
        is_active: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """完成“匹配，否则按联系人语境登记”的一次电话语音解析。

        返回值包含 ``speaker_id``、``name``、``role``、``matched``、
        ``enrolled`` 和 ``reason``，便于对话运行时记录审计状态。
        ``first_utterance`` 为旧调用方兼容参数；匹配已有全局人物时不自动
        绑定联系人，避免把来电人旁边先说话的人误认成账号主人。
        """

        caller_key = self._contact_key(caller_uin)
        nickname = _text(caller_name)
        speaker_id: str | None = None
        speaker_name: str | None = None
        if sv_pipeline is not None:
            speaker_id, speaker_name = self.identify_speaker(
                input_audio_path, threshold, sv_pipeline
            )

        if speaker_id is not None:
            info = self.get_speaker_info(speaker_id) or {}
            return {
                "speaker_id": str(speaker_id),
                "name": _text(info.get("name")) or _text(speaker_name) or self.unknown_name,
                "role": _text(info.get("role")) or self.default_role,
                "aliases": _aliases(info.get("aliases")),
                "matched": True,
                "enrolled": False,
                "reason": "matched",
            }

        duration = None if duration_seconds is None else float(duration_seconds)
        if callable(is_active) and not is_active():
            return self._unknown_result("call_ended")
        if not auto_enroll_unknown:
            return self._unknown_result("auto_enroll_disabled")
        if duration is not None and duration < float(min_enroll_duration):
            return self._unknown_result("duration_too_short")

        enrolled_id, enrolled_name = self.enroll_unknown(
            input_audio_path,
            caller_key,
            nickname,
            is_active=is_active,
        )
        if enrolled_id is None:
            return self._unknown_result("enroll_failed")
        info = self.get_speaker_info(enrolled_id) or {}
        return {
            "speaker_id": enrolled_id,
            "name": enrolled_name or _text(info.get("name")) or self.unknown_name,
            "role": _text(info.get("role")) or self.default_role,
            "aliases": _aliases(info.get("aliases")),
            "matched": False,
            "enrolled": True,
            "reason": "auto_enrolled",
        }

    # 另一个常用名称，方便未来对话运行时迁移而不改行为。
    resolve = resolve_utterance

    def _unknown_result(self, reason: str) -> dict[str, Any]:
        """构造不登记的未知说话人结果。"""

        return {
            "speaker_id": None,
            "name": self.unknown_name,
            "role": self.default_role,
            "aliases": [],
            "matched": False,
            "enrolled": False,
            "reason": reason,
        }

    def update_speaker_metadata(
        self,
        speaker_id: Any,
        *,
        name: str | None = None,
        role: str | None = None,
        aliases: Any = None,
    ) -> bool:
        """更新 QQ 声纹的展示名称、角色或别名，并保留旧数据可回滚。"""

        key = _text(speaker_id)
        with self._transaction():
            record = self.manager.speaker_info["speakers"].get(key)
            if not isinstance(record, dict):
                return False
            old_record = copy.deepcopy(record)
            if name is not None:
                new_name = _text(name)
                if not new_name:
                    return False
                record["name"] = new_name
            if role is not None:
                new_role = _text(role)
                if not new_role:
                    return False
                record["role"] = new_role
            if aliases is not None:
                record["aliases"] = _aliases(aliases)
            if self._persist():
                return True
            record.clear()
            record.update(old_record)
            return False

    def update_speaker_name(self, speaker_id: Any, new_name: str) -> bool:
        """兼容 WebUI 现有的改名入口。"""

        return self.update_speaker_metadata(speaker_id, name=new_name)

    def delete_speaker(self, speaker_id: Any) -> bool:
        """删除全局声纹，并清理联系人对它的主声纹绑定。"""

        key = _text(speaker_id)
        with self._transaction():
            if key not in self.manager.speaker_info["speakers"]:
                return False
            old_contacts = copy.deepcopy(self._contacts())
            for record in self._contacts().values():
                if isinstance(record, dict) and _text(record.get("primary_speaker_id")) == key:
                    record.pop("primary_speaker_id", None)
            if not self.manager.delete_speaker(key):
                self.manager.speaker_info["contacts"] = old_contacts
                return False
            # 管理器的一次原子保存已经同时包含说话人删除和联系人清理。
            return True

    def resolve_enroll_file(self, enroll_file: Any) -> str:
        """暴露管理器的登记音频路径解析，供 QQ Tab 的管理界面复用。"""

        return self.manager.resolve_enroll_file(enroll_file)

    def update_speaker_enroll_file(
        self,
        speaker_id: Any,
        new_path: str | os.PathLike[str],
    ) -> bool:
        """更新登记音频路径，并让通用管理器清理对应嵌入缓存。"""

        with self._transaction():
            return bool(
                self.manager.update_speaker_enroll_file(
                    str(speaker_id), os.fspath(new_path)
                )
            )


__all__ = [
    "DEFAULT_CONTACT_ROLE",
    "DEFAULT_OTHER_NAME_TEMPLATE",
    "DEFAULT_PARTICIPANT_ROLE",
    "DEFAULT_UNKNOWN_NAME",
    "QQVoiceprintRegistry",
]
