#!/usr/bin/env python3
"""在 QQ 启动前部署 Docker 电话桥，使用基础镜像自带的 Python 标准库。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile


PLUGIN_ID = "qq-voice-call"
HOST_MARKER = "QQ_VOICE_CALL_LOADER_HOOK_V1"
ORIGINAL_LOADER = "loadNapCat.qq-voice-call.original.cjs"
OFFICIAL_IDS = {
    "napcat-plugin-builtin", "napcat-plugin-cleaner", "napcat-plugin-ssqq", "napcat-plugin-qce",
}


def patch_plugin_whitelist(source: str) -> str:
    """只修改拒绝提示之前、包含完整官方 ID 的唯一 Set。"""
    marker = source.find("not in official plugin whitelist")
    if marker < 0:
        raise ValueError("无法识别 NapCat 插件白名单")
    candidates = []
    for match in re.finditer(r"new Set\(\[([^\]]*)\]\)", source):
        ids = {item[1] for item in re.findall(r"([\"'`])([^\"'`]+)\1", match[1])}
        if match.start() < marker and OFFICIAL_IDS.issubset(ids):
            candidates.append((match, ids))
    if len(candidates) != 1:
        raise ValueError("NapCat 官方插件白名单不唯一")
    match, ids = candidates[0]
    if PLUGIN_ID in ids:
        return source
    end = match.end(1)
    separator = "" if match[1].rstrip().endswith(",") else ","
    return source[:end] + separator + json.dumps(PLUGIN_ID) + source[end:]


def patch_host_loader(source: str, napcat_root: Path) -> str:
    """物理 Loader 为带 Host 标志的子进程分流，普通 QQ 执行原入口。"""
    if HOST_MARKER in source:
        return source
    expected_url = (Path(napcat_root).resolve() / "napcat.mjs").as_uri()
    match = re.fullmatch(
        r"\(async\s*\(\)\s*=>\s*\{\s*await\s+import\(([\"'])([^\"']+)\1\);?\s*\}\)\(\);?",
        source.strip(),
    )
    if not match or match[2] != expected_url:
        raise ValueError("无法识别 Docker QQ 的物理 Loader")
    return (
        f'"use strict";\n// {HOST_MARKER}\n'
        'if (process.env.QQ_VOICE_CALL_AV_HOST === "1") {\n'
        '  require(process.env.QQ_VOICE_CALL_AV_HOST_ENTRY);\n'
        '} else {\n'
        f"  require('./{ORIGINAL_LOADER}');\n}}\n"
    )


def read_source(filename: Path) -> str:
    """保留原文件换行，以便入口备份与重复安装的校验按原文进行。"""
    return filename.read_bytes().decode("utf-8")


def write_atomic(filename: Path, content: str) -> None:
    """在原目录中替换文件，保留已有权限；失败时清理临时文件。"""
    mode = stat.S_IMODE(filename.stat().st_mode) if filename.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=filename.name + ".qq-voice-call-tmp-", dir=filename.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content.encode("utf-8"))
        temporary.chmod(mode)
        os.replace(temporary, filename)
    finally:
        temporary.unlink(missing_ok=True)


def install_docker_plugin(
    *, napcat_root: Path = Path("/app/napcat"), qq_root: Path = Path("/opt/QQ"),
    source_root: Path = Path(__file__).resolve().parent,
) -> dict:
    """先校验入口与资源，再部署；参数供隔离夹具指定自己的目录。"""
    napcat_root, qq_root, source_root = map(Path, (napcat_root, qq_root, source_root))
    app_root = qq_root / "resources/app"
    loader = app_root / "loadNapCat.js"
    app_package = json.loads(read_source(app_root / "package.json"))
    if (app_root / (app_package.get("main") or "")).resolve() != loader.resolve():
        raise ValueError("QQ main 与待适配的物理 Loader 不一致")
    metadata = json.loads(read_source(source_root / "package.json"))
    if metadata["name"] != PLUGIN_ID:
        raise ValueError("电话插件 ID 不匹配")
    for entry in ("index.mjs", "host.cjs", "host-preload.cjs", "host.html", "host-bootstrap/napcat/napcat.mjs"):
        if not (source_root / entry).is_file():
            raise ValueError(f"电话插件文件缺失：{entry}")
    if not (app_root / "avsdk/libAVSDKPlugin.so").is_file():
        raise ValueError("QQ AVSDK 原生库缺失")

    bundle = napcat_root / "napcat.mjs"
    bundle_source = read_source(bundle)
    patched_bundle = patch_plugin_whitelist(bundle_source)
    loader_source = read_source(loader)
    patched_loader = patch_host_loader(loader_source, napcat_root)
    original = app_root / ORIGINAL_LOADER
    if HOST_MARKER in loader_source:
        original_source = read_source(original)
        if HOST_MARKER in original_source:
            raise ValueError("QQ 原入口备份包含 Host 分流逻辑")
        patch_host_loader(original_source, napcat_root)
    elif original.exists() and read_source(original) != loader_source:
        raise ValueError("QQ Loader 备份与当前原入口不一致")

    target = napcat_root / "plugins" / PLUGIN_ID
    shutil.copytree(source_root, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("data", "tests", "__pycache__"))
    if patched_bundle != bundle_source:
        write_atomic(Path(str(bundle) + ".qq-voice-call.original"), bundle_source)
        write_atomic(bundle, patched_bundle)
    if patched_loader != loader_source:
        write_atomic(original, loader_source)
        write_atomic(loader, patched_loader)
    return {"pluginId": PLUGIN_ID, "version": metadata["version"], "target": str(target), "loader": str(loader)}


if __name__ == "__main__":
    try:
        installed = install_docker_plugin()
        print(f'[QQVoiceCall] Docker plugin v{installed["version"]} installed; enable it in NapCat plugin management')
    except Exception as error:
        print(f"[QQVoiceCall] Docker installation failed: {error}", file=sys.stderr)
        sys.exit(1)
