"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { pathToFileURL } = require("node:url");
const { installDockerPlugin, patchHostLoader, patchPluginWhitelist } = require("../install-docker.cjs");

const sourceRoot = path.resolve(__dirname, "..");
const temporaryRoot = path.resolve(__dirname, "../../temp");
const whitelist = 'const allowed = new Set(["napcat-plugin-builtin","napcat-plugin-cleaner",' +
  '"napcat-plugin-ssqq","napcat-plugin-qce"]);\n' +
  'function reject() { return "not in official plugin whitelist"; }';

function fixture(t) {
  fs.mkdirSync(temporaryRoot, { recursive: true });
  const root = fs.mkdtempSync(path.join(temporaryRoot, "qq-call-docker-"));
  t.after(() => {
    assert.equal(path.dirname(root), temporaryRoot);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const napcatRoot = path.join(root, "napcat");
  const qqRoot = path.join(root, "QQ");
  const appRoot = path.join(qqRoot, "resources", "app");
  fs.mkdirSync(napcatRoot, { recursive: true });
  fs.mkdirSync(path.join(appRoot, "avsdk"), { recursive: true });
  fs.writeFileSync(path.join(napcatRoot, "napcat.mjs"), whitelist);
  fs.writeFileSync(path.join(appRoot, "package.json"), '{"main":"./loadNapCat.js"}');
  fs.writeFileSync(path.join(appRoot, "avsdk/libAVSDKPlugin.so"), "synthetic library");
  const loader = path.join(appRoot, "loadNapCat.js");
  const original = `(async () => {await import('${pathToFileURL(path.join(napcatRoot, "napcat.mjs")).href}');})();\n`;
  fs.writeFileSync(loader, original);
  return { napcatRoot, qqRoot, appRoot, loader, original, sourceRoot };
}

test("安装、重复安装和发布包覆盖后保留配置与数据", (t) => {
  const paths = fixture(t);
  fs.mkdirSync(path.join(paths.napcatRoot, "config"));
  const status = path.join(paths.napcatRoot, "config/plugins.json");
  fs.writeFileSync(status, '{"qq-voice-call":false,"another-plugin":true}');
  const first = installDockerPlugin(paths);
  assert.equal(first.version, JSON.parse(fs.readFileSync(path.join(sourceRoot, "package.json"))).version);
  assert.ok(fs.existsSync(path.join(first.target, "host-bootstrap/napcat/napcat.mjs")));
  assert.equal(fs.existsSync(path.join(first.target, "tests")), false);
  // NapCat 4.18.x 的 context.dataPath 位于 config/plugins/<id>，由已有配置卷持久化。
  const dataRoot = path.join(paths.napcatRoot, "config/plugins/qq-voice-call");
  fs.mkdirSync(dataRoot, { recursive: true });
  fs.writeFileSync(path.join(dataRoot, "config.json"), '{"avHost":{"loaderPath":"saved-loader"}}');
  fs.writeFileSync(path.join(dataRoot, "synthetic.json"), '{"retained":true}');
  const configBefore = fs.readFileSync(status, "utf8");
  const loaderBefore = fs.readFileSync(paths.loader, "utf8");
  const bundleBefore = fs.readFileSync(path.join(paths.napcatRoot, "napcat.mjs"), "utf8");
  installDockerPlugin(paths);
  assert.equal(fs.readFileSync(paths.loader, "utf8"), loaderBefore);
  assert.equal(fs.readFileSync(path.join(paths.napcatRoot, "napcat.mjs"), "utf8"), bundleBefore);
  fs.writeFileSync(path.join(paths.napcatRoot, "napcat.mjs"), whitelist);
  installDockerPlugin(paths);
  assert.equal(fs.readFileSync(path.join(paths.napcatRoot, "napcat.mjs"), "utf8"), bundleBefore);
  assert.equal(fs.readFileSync(status, "utf8"), configBefore);
  assert.equal(fs.readFileSync(path.join(dataRoot, "synthetic.json"), "utf8"), '{"retained":true}');
  assert.equal(fs.readFileSync(path.join(dataRoot, "config.json"), "utf8"), '{"avHost":{"loaderPath":"saved-loader"}}');
  assert.equal(fs.readFileSync(path.join(paths.appRoot, "loadNapCat.qq-voice-call.original.cjs"), "utf8"), paths.original);
});

test("普通 QQ 与 AVSDK Host 各执行自己的入口", () => {
  const root = path.resolve("/app/napcat");
  const url = pathToFileURL(path.join(root, "napcat.mjs")).href;
  const code = patchHostLoader(`(async () => {await import('${url}');})();`, root);
  for (const isHost of [false, true]) {
    const required = [];
    vm.runInNewContext(code, {
      process: { env: isHost ? { QQ_VOICE_CALL_AV_HOST: "1", QQ_VOICE_CALL_AV_HOST_ENTRY: "/plugin/host.cjs" } : {} },
      require: (entry) => required.push(entry),
    });
    assert.deepEqual(required, [isHost ? "/plugin/host.cjs" : "./loadNapCat.qq-voice-call.original.cjs"]);
  }
});

test("未知 Loader、QQ main 与入口不符、原入口备份残缺时提前拒绝安装", (t) => {
  for (const change of ["unknown-loader", "wrong-main", "missing-original"]) {
    const paths = fixture(t);
    if (change === "unknown-loader") fs.writeFileSync(paths.loader, "launchUnknownQQ();");
    if (change === "wrong-main") fs.writeFileSync(path.join(paths.appRoot, "package.json"), '{"main":"./other.js"}');
    if (change === "missing-original") {
      fs.writeFileSync(paths.loader, patchHostLoader(paths.original, paths.napcatRoot));
    }
    assert.throws(() => installDockerPlugin(paths));
    assert.equal(fs.readFileSync(path.join(paths.napcatRoot, "napcat.mjs"), "utf8"), whitelist);
    assert.equal(fs.existsSync(path.join(paths.napcatRoot, "plugins/qq-voice-call")), false);
  }
});

test("白名单只增加本插件，并拒绝未知或重复的白名单", () => {
  const patched = patchPluginWhitelist(whitelist);
  const allowed = vm.runInNewContext(`${patched}\nallowed`);
  assert.equal(allowed.size, 5);
  assert.equal(allowed.has("qq-voice-call"), true);
  assert.equal(patchPluginWhitelist(patched), patched);
  assert.throws(() => patchPluginWhitelist("unknown bundle"));
  assert.throws(() => patchPluginWhitelist(whitelist.slice(0, whitelist.indexOf("function reject")) + whitelist));
});
