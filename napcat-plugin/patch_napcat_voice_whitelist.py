#!/usr/bin/env python3
"""为 NapCat 4.18.13 的本地插件白名单做可回退的最小补丁。

脚本默认只扫描并报告候选文件。只有传入 ``--apply`` 时才会修改文件，
并且修改前会在同一目录创建带时间戳的备份。脚本只处理包含 NapCat
官方白名单拒绝提示的 JavaScript 文件，不会修改 QQ 配置、插件配置或
其他没有明确匹配的文件。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


PLUGIN_ID = "qq-voice-call"
REJECT_MARKER = "not in official plugin whitelist"
OFFICIAL_IDS = (
    "napcat-plugin-builtin",
    "napcat-plugin-cleaner",
    "napcat-plugin-ssqq",
    "napcat-plugin-qce",
)
TEXT_SUFFIXES = {".js", ".mjs", ".cjs"}
MAX_FILE_BYTES = 64 * 1024 * 1024
SKIP_DIR_NAMES = {".git", "cache", "caches", "data", "log", "logs", "tmp", "temp"}
QCE_TOKEN = re.compile(r'''(['"`])napcat-plugin-qce\1''')
PLUGIN_TOKEN = re.compile(r'''(['"`])qq-voice-call\1''')
BACKUP_NAME = re.compile(
    r"^(?P<target>.+)\.qq-voice-call\.bak-\d{8}T\d{6}Z-\d+$"
)


class PatchError(RuntimeError):
    """补丁目标不满足安全校验时抛出的异常。"""


@dataclass(frozen=True)
class Candidate:
    """扫描结果中的一个 NapCat 加载器候选文件。"""

    path: Path
    status: str
    detail: str


def _timestamp() -> str:
    """返回用于备份文件名的 UTC 时间戳和进程号。"""

    return f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{os.getpid()}"


def _read_utf8(path: Path) -> str | None:
    """读取 UTF-8 文本；二进制文件或无法解码的文件返回 ``None``。"""

    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _iter_script_files(root: Path):
    """递归枚举 NapCat 根目录下的 JavaScript 文件，跳过运行数据目录。"""

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIR_NAMES)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def _candidate_for_text(path: Path, text: str) -> Candidate | None:
    """根据 NapCat 加载器的拒绝标记判断文件是否可补丁。"""

    marker_index = text.find(REJECT_MARKER)
    if marker_index < 0:
        return None

    plugin_matches = [match for match in PLUGIN_TOKEN.finditer(text) if match.start() < marker_index]
    if plugin_matches:
        return Candidate(path, "already_patched", "已包含 通话插件 ID")

    missing_ids = [plugin_id for plugin_id in OFFICIAL_IDS if plugin_id not in text]
    if missing_ids:
        return Candidate(path, "unsupported", f"缺少官方 ID：{', '.join(missing_ids)}")

    # 白名单声明应位于拒绝提示之前。只接受唯一的 qce 字符串，
    # 这样不会因为其他代码中的同名文字而误改文件。
    qce_matches = [match for match in QCE_TOKEN.finditer(text) if match.start() < marker_index]
    if len(qce_matches) != 1:
        return Candidate(
            path,
            "ambiguous",
            f"拒绝提示前找到 {len(qce_matches)} 个 qce 白名单项",
        )
    return Candidate(path, "patchable", "匹配 NapCat 官方白名单加载器")


def scan_root(root: Path) -> list[Candidate]:
    """扫描 NapCat 根目录，返回所有包含拒绝逻辑的候选文件。"""

    if not root.is_dir():
        raise PatchError(f"NapCat 根目录不存在或不是目录：{root}")
    candidates: list[Candidate] = []
    for path in _iter_script_files(root):
        text = _read_utf8(path)
        if text is None:
            continue
        candidate = _candidate_for_text(path, text)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def patch_text(text: str) -> str:
    """只在唯一白名单项后加入 通话插件 ID，并返回修改后的文本。"""

    candidate = _candidate_for_text(Path("<memory>"), text)
    if candidate is None:
        raise PatchError("文件不包含 NapCat 4.18.13 的白名单拒绝逻辑")
    if candidate.status == "already_patched":
        return text
    if candidate.status != "patchable":
        raise PatchError(candidate.detail)

    marker_index = text.find(REJECT_MARKER)
    match = next(match for match in QCE_TOKEN.finditer(text) if match.start() < marker_index)
    quote = match.group(1)
    replacement = f"{quote}napcat-plugin-qce{quote},{quote}{PLUGIN_ID}{quote}"
    return text[: match.start()] + replacement + text[match.end() :]


def _write_atomic(path: Path, content: bytes) -> None:
    """在目标目录内原子替换文件，并保留原文件权限。"""

    temporary = path.with_name(f".{path.name}.qq-voice-call-tmp-{os.getpid()}")
    try:
        temporary.write_bytes(content)
        temporary.chmod(path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_file(path: Path) -> Path:
    """备份并补丁一个已通过安全校验的加载器文件，返回备份路径。"""

    original = path.read_bytes()
    text = original.decode("utf-8")
    patched = patch_text(text)
    if patched == text:
        raise PatchError(f"文件已经包含 {PLUGIN_ID}，无需重复补丁：{path}")

    backup = path.with_name(f"{path.name}.qq-voice-call.bak-{_timestamp()}")
    shutil.copy2(path, backup)
    try:
        _write_atomic(path, patched.encode("utf-8"))
    except Exception:
        if backup.exists():
            shutil.copy2(backup, path)
        raise

    if PLUGIN_ID not in path.read_bytes().decode("utf-8"):
        shutil.copy2(backup, path)
        raise PatchError(f"补丁后校验失败，已恢复原文件：{path}")
    return backup


def rollback_file(backup: Path) -> Path:
    """从脚本生成的备份恢复原文件，并返回恢复前的当前文件备份。"""

    match = BACKUP_NAME.match(backup.name)
    if not match:
        raise PatchError("备份文件名不是本脚本生成的格式，拒绝回滚")
    target = backup.with_name(match.group("target"))
    original = backup.read_bytes()
    if PLUGIN_ID in original.decode("utf-8"):
        raise PatchError("备份文件仍包含 通话插件 ID，拒绝回滚")
    if target.exists() and PLUGIN_ID not in target.read_bytes().decode("utf-8"):
        raise PatchError("当前目标文件未发现补丁 ID，拒绝覆盖")

    current_backup: Path | None = None
    if target.exists():
        current_backup = target.with_name(
            f"{target.name}.qq-voice-call.before-rollback-{_timestamp()}"
        )
        shutil.copy2(target, current_backup)
    _write_atomic(target, original)
    return current_backup or target


def _resolve_file(root: Path, value: str) -> Path:
    """解析用户指定的文件，并确保它位于 NapCat 根目录内。"""

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    root = root.resolve()
    if path != root and root not in path.parents:
        raise PatchError(f"目标文件不在 NapCat 根目录内：{path}")
    if not path.is_file():
        raise PatchError(f"目标文件不存在：{path}")
    return path


def _print_candidates(candidates: list[Candidate]) -> None:
    """以适合复制给排障人员的格式打印扫描结果。"""

    if not candidates:
        print("未找到包含 NapCat 白名单拒绝逻辑的 JavaScript 文件。")
        print("请确认 --root 指向 <NapCat.Shell root>，并把本输出发回排查；不要手工改文件。")
        return
    for candidate in candidates:
        print(f"[{candidate.status}] {candidate.path}")
        print(f"  {candidate.detail}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=".",
        help="NapCat Shell 根目录；建议显式填写绝对路径，默认使用当前目录",
    )
    parser.add_argument(
        "--file",
        help="需要补丁的具体文件；不填写时要求扫描结果中只有一个可补丁文件",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="确认后备份并写入补丁")
    action.add_argument("--rollback", metavar="BACKUP", help="从指定备份文件回滚")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行扫描、补丁或回滚动作。"""

    args = _build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    try:
        if args.rollback:
            backup = _resolve_file(root, args.rollback)
            restored = rollback_file(backup)
            print(f"已从备份恢复：{backup}")
            if restored != backup:
                print(f"回滚前的当前文件备份：{restored}")
            return 0

        candidates = scan_root(root)
        _print_candidates(candidates)
        if not args.apply:
            return 0 if candidates else 1

        if args.file:
            target = _resolve_file(root, args.file)
        else:
            patchable = [candidate.path for candidate in candidates if candidate.status == "patchable"]
            if len(patchable) != 1:
                raise PatchError("可补丁文件不是唯一一个，请使用 --file 指定并先核对扫描输出")
            target = patchable[0]
        backup = apply_file(target)
        print(f"补丁已写入：{target}")
        print(f"原文件备份：{backup}")
        print("请重启虚拟机 NapCat，然后在插件管理中启用 qq-voice-call。")
        return 0
    except (OSError, UnicodeDecodeError, PatchError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
