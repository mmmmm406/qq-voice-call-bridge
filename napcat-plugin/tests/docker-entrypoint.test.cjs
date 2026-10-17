"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const repo = path.resolve(__dirname, "../..");
const temporaryRoot = path.join(repo, "temp");
const source = fs.readFileSync(path.join(repo, "docker/entrypoint.sh"), "utf8");
const bash = process.env.QQ_VOICE_CALL_TEST_BASH || "bash";
const shellPath = (value) => value.replaceAll("\\", "/").replace(/^([A-Za-z]):/, (_, drive) => `/${drive.toLowerCase()}`);
const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`;

/** 运行真实包装脚本的路径映射副本，Python、解压器和上游入口均为隔离替身。 */
function fixture(t, { installed = false, fail = false } = {}) {
  fs.mkdirSync(temporaryRoot, { recursive: true });
  const root = fs.mkdtempSync(path.join(temporaryRoot, "docker-entrypoint-"));
  t.after(() => {
    assert.equal(path.dirname(root), temporaryRoot);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const app = path.join(root, "app");
  const qq = path.join(root, "QQ");
  const plugin = path.join(root, "plugin");
  const bin = path.join(root, "bin");
  const trace = path.join(root, "trace");
  for (const directory of [path.join(app, "napcat/config"), qq, plugin, bin]) {
    fs.mkdirSync(directory, { recursive: true });
  }
  const config = path.join(app, "napcat/config/napcat.json");
  fs.writeFileSync(config, '{"synthetic":"retained"}');
  fs.writeFileSync(path.join(app, "NapCat.Shell.zip"), "synthetic archive");
  fs.writeFileSync(path.join(plugin, "install-docker.py"), "synthetic installer");
  if (installed) fs.writeFileSync(path.join(app, "napcat/napcat.mjs"), "existing bundle");
  const log = (value) => `printf '%s\\n' '${value}' >> ${quote(shellPath(trace))}\n`;
  const executable = (filename, body) => fs.writeFileSync(filename, `#!/bin/bash\nset -euo pipefail\n${body}`, { mode: 0o755 });
  executable(path.join(bin, "unzip"), '[ "$6" = "config/*" ]\n' +
    'printf "%s" "unpacked bundle" > "$4/napcat.mjs"\n' + log("unpack"));
  executable(path.join(bin, "python3"),
    `[ "$1" = ${quote(shellPath(path.join(plugin, "install-docker.py")))} ]\n` +
    `test -f ${quote(shellPath(path.join(app, "napcat/napcat.mjs")))}\n` +
    log("install") + (fail ? "exit 23\n" : ""));
  // 安装器必须由 Python 执行；意外提前启动 QQ 会让本夹具失败。
  executable(path.join(qq, "qq"), "exit 97\n");
  executable(path.join(app, "entrypoint.sh"), '[ -z "${ELECTRON_RUN_AS_NODE:-}" ]\n' + log("normal"));

  let mapped = source.replaceAll("/app", quote(shellPath(app)))
    .replaceAll("/opt/QQ", quote(shellPath(qq)))
    .replaceAll("/opt/qq-voice-call", quote(shellPath(plugin)));
  const searchPaths = [bin];
  if (path.isAbsolute(bash)) searchPaths.push(path.dirname(bash), path.resolve(path.dirname(bash), "../usr/bin"));
  mapped = `export PATH=${searchPaths.map((entry) => quote(shellPath(entry))).join(":")}:$PATH\n` + mapped;
  const wrapper = path.join(root, "wrapper.sh");
  fs.writeFileSync(wrapper, mapped);
  const result = spawnSync(bash, ["--noprofile", "--norc", shellPath(wrapper)], { encoding: "utf8", timeout: 15000 });
  assert.ifError(result.error);
  return {
    result,
    trace: fs.readFileSync(trace, "utf8").trim().split("\n"),
    config: fs.readFileSync(config, "utf8"),
    bundle: fs.readFileSync(path.join(app, "napcat/napcat.mjs"), "utf8"),
  };
}

test("首次启动先解压代码、安装插件，再交给上游入口，保留配置", (t) => {
  const actual = fixture(t);
  assert.equal(actual.result.status, 0, actual.result.stderr);
  assert.deepEqual(actual.trace, ["unpack", "install", "normal"]);
  assert.equal(actual.bundle, "unpacked bundle");
  assert.equal(actual.config, '{"synthetic":"retained"}');
});

test("已有 NapCat 代码时直接安装，沿用现有版本与配置", (t) => {
  const actual = fixture(t, { installed: true });
  assert.equal(actual.result.status, 0, actual.result.stderr);
  assert.deepEqual(actual.trace, ["install", "normal"]);
  assert.equal(actual.bundle, "existing bundle");
  assert.equal(actual.config, '{"synthetic":"retained"}');
});

test("安装失败时结束本次启动", (t) => {
  const actual = fixture(t, { installed: true, fail: true });
  assert.equal(actual.result.status, 23, actual.result.stderr);
  assert.deepEqual(actual.trace, ["install"]);
});
