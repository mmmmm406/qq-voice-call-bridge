"""用合成目录验证安装、持久数据、入口执行和提前拒绝未知结构。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_docker", SOURCE_ROOT / "install-docker.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
WHITELIST = ('const allowed = new Set(["napcat-plugin-builtin","napcat-plugin-cleaner",'
             '"napcat-plugin-ssqq","napcat-plugin-qce"]);\n'
             'function reject() { return "not in official plugin whitelist"; }')


class DockerInstallTests(unittest.TestCase):
    def fixture(self):
        temporary_root = SOURCE_ROOT.parent / "temp"
        temporary_root.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="qq-call-docker-", dir=temporary_root)
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        napcat_root, qq_root, source_root = (root / name for name in ("napcat", "QQ", "source"))
        app_root = qq_root / "resources/app"
        napcat_root.mkdir()
        (app_root / "avsdk").mkdir(parents=True)
        (napcat_root / "napcat.mjs").write_text(WHITELIST, encoding="utf-8")
        (app_root / "package.json").write_text('{"main":"./loadNapCat.js"}', encoding="utf-8")
        (app_root / "avsdk/libAVSDKPlugin.so").write_bytes(b"synthetic library")
        original = f"(async () => {{await import('{(napcat_root / 'napcat.mjs').as_uri()}');}})();\r\n"
        (app_root / "loadNapCat.js").write_bytes(original.encode("utf-8"))
        source_root.mkdir()
        (source_root / "package.json").write_text(json.dumps({"name": installer.PLUGIN_ID, "version": "9.8.7"}))
        for name in ("index.mjs", "host.cjs", "host-preload.cjs", "host.html", "host-bootstrap/napcat/napcat.mjs",
                     "tests/synthetic.txt", "data/synthetic.txt", "__pycache__/synthetic.txt"):
            filename = source_root / name
            filename.parent.mkdir(parents=True, exist_ok=True)
            filename.write_bytes(b"synthetic plugin file")
        return {"napcat_root": napcat_root, "qq_root": qq_root, "source_root": source_root}, app_root, original

    def test_install_reinstall_and_release_replacement_preserve_configuration(self):
        paths, app_root, original = self.fixture()
        config = paths["napcat_root"] / "config"
        data_root = config / "plugins" / installer.PLUGIN_ID
        data_root.mkdir(parents=True)
        status = config / "plugins.json"
        saved = json.dumps({installer.PLUGIN_ID: False, "another-plugin": True}).encode()
        status.write_bytes(saved)
        data = data_root / "config.json"
        data.write_bytes(b'{"avHost":{"loaderPath":"saved-loader"}}')
        bundle = paths["napcat_root"] / "napcat.mjs"
        bundle.chmod(0o640)
        first = installer.install_docker_plugin(**paths)
        self.assertEqual(first["version"], "9.8.7")
        target = Path(first["target"])
        self.assertTrue((target / "host-bootstrap/napcat/napcat.mjs").is_file())
        for excluded in ("data", "tests", "__pycache__"):
            self.assertFalse((target / excluded).exists())
        loader_before = (app_root / "loadNapCat.js").read_bytes()
        bundle_before = bundle.read_bytes()
        installer.install_docker_plugin(**paths)
        self.assertEqual(bundle.read_bytes(), bundle_before)
        bundle.write_text(WHITELIST, encoding="utf-8")
        installer.install_docker_plugin(**paths)
        self.assertEqual(bundle.read_bytes(), bundle_before)
        self.assertEqual((app_root / "loadNapCat.js").read_bytes(), loader_before)
        self.assertEqual((app_root / installer.ORIGINAL_LOADER).read_bytes(), original.encode())
        self.assertEqual(status.read_bytes(), saved)
        self.assertEqual(data.read_bytes(), b'{"avHost":{"loaderPath":"saved-loader"}}')
        if os.name != "nt":
            self.assertEqual(bundle.stat().st_mode & 0o777, 0o640)

    @unittest.skipUnless(shutil.which("node"), "生成的 QQ Loader 执行检查需要 Node.js")
    def test_ordinary_qq_and_host_execute_their_own_entry(self):
        root = Path("/app/napcat").resolve()
        source = f"(async () => {{await import('{(root / 'napcat.mjs').as_uri()}');}})();"
        code = installer.patch_host_loader(source, root)
        # 用 VM 执行生成的 JS，require 替身只记录入口，避免启动任何 QQ 程序。
        script = """
const assert = require('node:assert/strict');
const vm = require('node:vm');
const code = require('node:fs').readFileSync(0, 'utf8');
for (const isHost of [false, true]) {
  const required = [];
  vm.runInNewContext(code, {
    process: {env: isHost ? {QQ_VOICE_CALL_AV_HOST: '1', QQ_VOICE_CALL_AV_HOST_ENTRY: '/plugin/host.cjs'} : {}},
    require: (entry) => required.push(entry),
  });
  assert.deepEqual(required, [isHost ? '/plugin/host.cjs' : './loadNapCat.qq-voice-call.original.cjs']);
}
"""
        result = subprocess.run([shutil.which("node"), "-e", script], input=code, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_loader_main_library_and_backup_reject_before_deployment(self):
        for change in ("unknown-loader", "wrong-main", "missing-library", "missing-original", "damaged-original"):
            with self.subTest(change=change):
                paths, app_root, original = self.fixture()
                loader = app_root / "loadNapCat.js"
                if change == "unknown-loader":
                    loader.write_text("launchUnknownQQ();")
                elif change == "wrong-main":
                    (app_root / "package.json").write_text('{"main":"./other.js"}')
                elif change == "missing-library":
                    (app_root / "avsdk/libAVSDKPlugin.so").unlink()
                else:
                    loader.write_text(installer.patch_host_loader(original, paths["napcat_root"]))
                    if change == "damaged-original":
                        (app_root / installer.ORIGINAL_LOADER).write_bytes(loader.read_bytes())
                with self.assertRaises((ValueError, OSError)):
                    installer.install_docker_plugin(**paths)
                self.assertEqual((paths["napcat_root"] / "napcat.mjs").read_text(), WHITELIST)
                self.assertFalse((paths["napcat_root"] / "plugins" / installer.PLUGIN_ID).exists())

    def test_whitelist_changes_only_this_plugin_and_rejects_ambiguous_sources(self):
        patched = installer.patch_plugin_whitelist(WHITELIST)
        self.assertEqual(patched.replace(',' + json.dumps(installer.PLUGIN_ID), ""), WHITELIST)
        self.assertEqual(installer.patch_plugin_whitelist(patched), patched)
        with self.assertRaises(ValueError):
            installer.patch_plugin_whitelist("unknown bundle")
        with self.assertRaises(ValueError):
            installer.patch_plugin_whitelist(WHITELIST[:WHITELIST.index("function reject")] + WHITELIST)


if __name__ == "__main__":
    unittest.main()
