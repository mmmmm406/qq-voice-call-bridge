import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { normalizeRuntimeConfig } from "../protocol.mjs";

const source = readFileSync(new URL("../index.mjs", import.meta.url), "utf8");
const stopSource = source.slice(source.indexOf("function stopHostProcess()"), source.indexOf("function loadPluginConfig("));

function fixture({ killed = false, exitCode = null } = {}) {
  const child = new EventEmitter();
  const signals = [];
  const timers = new Set();
  Object.assign(child, { killed, exitCode, signalCode: null, kill: (signal) => { signals.push(signal); return true; } });
  const stop = runInNewContext(`let hostProcess = child; ${stopSource}\nstopHostProcess`, {
    child,
    setTimeout: (run) => {
      const timer = { run, unref() {} };
      timers.add(timer);
      return timer;
    },
    clearTimeout: (timer) => timers.delete(timer),
  });
  return { child, signals, timers, stop };
}

test("已发过终止信号的 Host 仍需等待退出，并在退出后取消强杀任务", async () => {
  const runtime = fixture({ killed: true });
  let completed = false;
  const stopping = runtime.stop().then(() => { completed = true; });
  await Promise.resolve();
  assert.equal(completed, false);
  assert.deepEqual(runtime.signals, ["SIGTERM"]);
  assert.equal(runtime.timers.size, 1);
  runtime.child.emit("exit", 0, null);
  await stopping;
  assert.equal(runtime.timers.size, 0);
});

test("Host 未响应终止时强制结束，已退出的进程直接完成清理", async () => {
  const runtime = fixture();
  let completed = false;
  const stopping = runtime.stop().then(() => { completed = true; });
  [...runtime.timers][0].run();
  assert.deepEqual(runtime.signals, ["SIGTERM", "SIGKILL"]);
  await Promise.resolve();
  assert.equal(completed, false);
  runtime.child.emit("exit", null, "SIGKILL");
  await stopping;
  assert.equal(runtime.timers.size, 0);
  const exited = fixture({ exitCode: 0 });
  await exited.stop();
  assert.deepEqual(exited.signals, []);
  assert.equal(exited.timers.size, 0);
});

test("终止信号失败会保留 Host 句柄，可再次清理", async () => {
  for (const failedSignal of ["SIGTERM", "SIGKILL"]) {
    const runtime = fixture();
    runtime.child.kill = (signal) => {
      if (signal === failedSignal) throw new Error("synthetic termination failure");
      return true;
    };
    const stopping = runtime.stop();
    const rejected = assert.rejects(stopping, /synthetic termination failure/);
    if (failedSignal === "SIGKILL") [...runtime.timers][0].run();
    await rejected;
    assert.equal(runtime.timers.size, 0);
    runtime.child.kill = () => true;
    const retry = runtime.stop();
    assert.equal(runtime.timers.size, 1);
    runtime.child.emit("exit", 0, null);
    await retry;
    assert.equal(runtime.timers.size, 0);
  }
});

/** 执行真实 Host 创建入口，文件、Electron 子进程和回环 HTTP 均使用合成替身。 */
function hostFixture({ loaderText = "// QQ_VOICE_CALL_LOADER_HOOK_V1" } = {}) {
  const calls = [];
  const pluginPath = path.resolve("/synthetic/napcat/plugins/qq-voice-call");
  const files = new Set([
    "/opt/QQ/qq", "/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so",
    "/opt/QQ/resources/app/loadNapCat.js", path.join(pluginPath, "host.cjs"),
    path.join(pluginPath, "host-bootstrap/napcat/napcat.mjs"),
  ].map((filename) => path.resolve(filename)));
  const runtimeSource = source.slice(source.indexOf("const PLUGIN_VERSION")).replace(/^export /gm, "");
  const runtime = runInNewContext(`${runtimeSource}
    context = { pluginPath, dataPath: pluginPath };
    logger = { info() {}, warn() {} };
    startHostControlServer = async () => { hostToken = 'synthetic-host-token'; };
    requestHost = async () => ({ ready: true, pluginFound: true });
    ({ start: async () => {
         state.active = true;
         state.avsdk = idleAVSDK();
         await createAVSDKHost();
       }, stop: stopHostProcess, snapshot: publicStatus });
  `, {
    normalizeRuntimeConfig, pluginPath, path,
    fs: { existsSync: (filename) => files.has(path.resolve(filename)), readFileSync: () => loaderText },
    process: { platform: "linux", env: { ELECTRON_RUN_AS_NODE: "1" } },
    setTimeout, clearTimeout,
    spawn: (executable, args, options) => {
      const child = new EventEmitter();
      Object.assign(child, {
        stdout: new EventEmitter(), stderr: new EventEmitter(), exitCode: null, signalCode: null,
        kill: () => true,
      });
      calls.push({ executable, args, options, child });
      return child;
    },
  });
  return { ...runtime, calls };
}

test("Docker Host 使用专用入口，上一代的迟到退出和错误不会清理新 Host", async () => {
  const runtime = hostFixture();
  await runtime.start();
  const first = runtime.calls[0];
  assert.equal(first.options.env.QQ_VOICE_CALL_AV_HOST, "1");
  assert.match(first.options.env.QQ_VOICE_CALL_AV_HOST_ENTRY, /host\.cjs$/);
  assert.equal(first.options.env.ELECTRON_RUN_AS_NODE, undefined);
  assert.equal(first.options.cwd, path.resolve("/opt/QQ/resources/app"));
  const stopFirst = runtime.stop();
  first.child.exitCode = 0;
  first.child.emit("exit", 0, null);
  await stopFirst;
  await runtime.start();
  first.child.emit("error", new Error("synthetic stale error"));
  first.child.emit("exit", 1, null);
  assert.equal(runtime.snapshot().avsdk.hostReady, true);
  assert.equal(runtime.snapshot().lastError, null);
  const stopSecond = runtime.stop();
  runtime.calls[1].child.emit("exit", 0, null);
  await stopSecond;
});

test("QQ Loader 尚未安装 Host 分流时，激活在创建 QQ 子进程前报错", async () => {
  const runtime = hostFixture({ loaderText: "import('file:///app/napcat/napcat.mjs');" });
  await assert.rejects(runtime.start(), /Loader 不支持/);
  assert.equal(runtime.calls.length, 0);
});
