#!/usr/bin/env python3
"""为物理 QQ Electron Loader 安装可回滚的 AVSDK Host 分流补丁。

NapCat Linux launcher 通常通过 ``LD_PRELOAD`` 在 NapCat Shell 当前工作目录
生成 Loader，并在进程内虚拟替换 QQ 的 ``package.json``；这种版本不需要本脚本，
插件会使用 launcher 提供的 ``NAPCAT_BOOTMAIN`` 启动 Host。本脚本只兼容确实存在
物理 ``loadNapCat.js`` 的旧版/非 launcher 安装。只有传入 ``--apply`` 才会创建
备份并写入补丁；补丁让普通 NapCat 进程继续执行原 Loader，只有带
``QQ_VOICE_CALL_AV_HOST=1`` 的独立 Host 进程才加载插件目录中的 ``host.cjs``。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import sys
from pathlib import Path


MARKER = "QQ_VOICE_CALL_LOADER_HOOK_V1"
STABLE_BACKUP_NAME = "loadNapCat.qq-voice-call.original.cjs"
BACKUP_NAME = re.compile(
    r"^loadNapCat\.js\.qq-voice-call\.bak-\d{8}T\d{6}Z-\d+$"
)


class PatchError(RuntimeError):
    """Loader 不满足安全校验时抛出的异常。"""


def _timestamp() -> str:
    """返回用于备份文件名的 UTC 时间戳和进程号。"""

    return f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{os.getpid()}"


def _loader_path(root: Path) -> Path:
    """解析 NapCat Shell 或 QQ 根目录下唯一可识别的物理 Loader。"""

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise PatchError(f"QQ 根目录不存在或不是目录：{root}")
    candidates = (
        root / "loadNapCat.js",
        root / "resources" / "app" / "loadNapCat.js",
    )
    existing = [path for path in candidates if path.is_file()]
    recognized: list[Path] = []
    for path in existing:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if MARKER in text or "napcat/napcat.mjs" in text or "./napcat/napcat.mjs" in text:
            recognized.append(path)
    if len(recognized) == 1:
        return recognized[0]
    if len(recognized) > 1:
        paths = ", ".join(str(path) for path in recognized)
        raise PatchError(f"发现多个可识别 QQ Loader：{paths}；请使用 --file 指定实际入口")
    if existing:
        paths = ", ".join(str(path) for path in existing)
        raise PatchError(f"找到 Loader 文件但无法识别 NapCat 入口：{paths}")
    raise PatchError(
        "未找到物理 QQ Loader。NapCat Linux launcher 通常把 Loader 生成在 Shell 当前工作目录；"
        "请将 --root 改为 NapCat Shell 根目录（例如 <NapCat.Shell root>），"
        "或使用 --file 指定实际的 loadNapCat.js。当前 launcher 版本无需本脚本。"
    )


def _validate_original(text: str) -> None:
    """拒绝把未知的自定义 Loader 当作干净原文件覆盖。"""

    if MARKER in text:
        return
    if "napcat/napcat.mjs" not in text and "./napcat/napcat.mjs" not in text:
        raise PatchError("目标不是可识别的 NapCat QQ Loader，拒绝覆盖")
    if "AV_HOST" in text or "MAIBOT_QQ_CALL" in text or "QQ_VOICE_CALL" in text:
        raise PatchError("目标已包含其他 Host 分流标记，请先提供干净 Loader 备份")


def patch_text(text: str) -> str:
    """生成只包含 Host 分流逻辑的 CommonJS Loader。"""

    _validate_original(text)
    if MARKER in text:
        return text
    return (
        '"use strict";\n'
        f"// {MARKER}\n"
        'if (process.env.QQ_VOICE_CALL_AV_HOST === "1") {\n'
        '  require(process.env.QQ_VOICE_CALL_AV_HOST_ENTRY);\n'
        "} else {\n"
        f"  require('./{STABLE_BACKUP_NAME}');\n"
        "}\n"
    )


def _write_atomic(path: Path, content: bytes) -> None:
    """在目标目录中原子替换文件并保留原权限。"""

    temporary = path.with_name(f".{path.name}.qq-voice-call-tmp-{os.getpid()}")
    try:
        temporary.write_bytes(content)
        temporary.chmod(path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_file(path: Path) -> Path:
    """备份并写入 Loader 补丁，返回时间戳备份路径。"""

    original = path.read_bytes()
    text = original.decode("utf-8")
    patched = patch_text(text)
    if patched == text:
        raise PatchError(f"文件已经安装 {MARKER}，无需重复补丁：{path}")
    stable_backup = path.with_name(STABLE_BACKUP_NAME)
    if stable_backup.exists():
        if stable_backup.read_bytes() != original:
            raise PatchError(f"稳定原始 Loader 备份已存在但内容不同：{stable_backup}")
    else:
        shutil.copy2(path, stable_backup)
    backup = path.with_name(f"{path.name}.qq-voice-call.bak-{_timestamp()}")
    shutil.copy2(path, backup)
    try:
        _write_atomic(path, patched.encode("utf-8"))
    except Exception:
        shutil.copy2(backup, path)
        raise
    if MARKER not in path.read_bytes().decode("utf-8"):
        shutil.copy2(backup, path)
        raise PatchError(f"补丁后校验失败，已恢复原文件：{path}")
    return backup


def rollback_file(backup: Path) -> Path:
    """从时间戳备份恢复 Loader，并备份当前补丁文件。"""

    if not BACKUP_NAME.fullmatch(backup.name):
        raise PatchError("备份文件名不是本脚本生成的格式，拒绝回滚")
    target = backup.with_name("loadNapCat.js")
    original = backup.read_bytes()
    _validate_original(original.decode("utf-8"))
    if not target.is_file() or MARKER not in target.read_text(encoding="utf-8"):
        raise PatchError("当前 Loader 未发现 QQ Voice Call Host 补丁，拒绝覆盖")
    current_backup = target.with_name(
        f"{target.name}.qq-voice-call.before-rollback-{_timestamp()}"
    )
    shutil.copy2(target, current_backup)
    _write_atomic(target, original)
    return current_backup


def _resolve_file(root: Path, value: str) -> Path:
    """解析用户指定文件并限制在 QQ 根目录内。"""

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    root = root.resolve()
    if path != root and root not in path.parents:
        raise PatchError(f"目标文件不在 QQ 根目录内：{path}")
    if not path.is_file():
        raise PatchError(f"目标文件不存在：{path}")
    if path.name != "loadNapCat.js":
        raise PatchError(f"目标文件必须是 loadNapCat.js：{path}")
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default="/opt/QQ",
        help="NapCat Shell 根目录或 QQ 安装根目录，默认 /opt/QQ",
    )
    parser.add_argument("--file", help="已核对的 Loader 绝对或相对路径")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="确认后备份并写入补丁")
    action.add_argument("--rollback", metavar="BACKUP", help="从时间戳备份回滚")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行扫描、补丁或回滚。"""

    args = _build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    try:
        if args.rollback:
            backup = _resolve_file(root, args.rollback)
            restored = rollback_file(backup)
            print(f"已从备份恢复：{backup}")
            print(f"回滚前的当前文件备份：{restored}")
            return 0
        target = _resolve_file(root, args.file) if args.file else _loader_path(root)
        text = target.read_text(encoding="utf-8")
        if MARKER in text:
            print(f"[already_patched] {target}")
            print("  已发现 QQ AVSDK Host 分流补丁")
            return 0
        _validate_original(text)
        print(f"[patchable] {target}")
        print("  匹配 QQ/NapCat Electron Loader")
        if not args.apply:
            return 0
        backup = apply_file(target)
        print(f"补丁已写入：{target}")
        print(f"原文件备份：{backup}")
        print(f"稳定原始 Loader：{target.with_name(STABLE_BACKUP_NAME)}")
        print("部署新插件并重启虚拟机 NapCat 后，OneBot 激活时会自动启动隐藏 AVSDK Host。")
        return 0
    except (OSError, UnicodeDecodeError, PatchError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
