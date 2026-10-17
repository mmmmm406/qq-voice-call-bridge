import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import path from "node:path";
import { normalizeRuntimeConfig } from "../protocol.mjs";

const source = readFileSync(new URL("../index.mjs", import.meta.url), "utf8");
// 执行真实入口的状态机；跳过外部模块导入，VM 中只提供合成身份、时钟和登录替身。
const runtimeSource = source.slice(source.indexOf("const PLUGIN_VERSION")).replace(/^export /gm, "");

function createRuntime({ costMs = 0, fail = false, stopCostMs = 0, loaderPaths = [] } = {}) {
  let now = 0;
  let inFlight = 0;
  let maxInFlight = 0;
  let hostStarts = 0;
  const timers = new Set();
  const calls = [];
  const fakeSetTimeout = (run, delay) => {
    const timer = { at: now + delay, run, unref() {} };
    timers.add(timer);
    return timer;
  };
  const sandbox = {
    normalizeRuntimeConfig,
    path,
    fs: { existsSync: (filename) => loaderPaths.includes(filename) },
    Date: class extends Date {
      constructor(...args) { super(...(args.length ? args : [now])); }
      static now() { return now; }
    },
    setTimeout: fakeSetTimeout,
    clearTimeout: (timer) => timers.delete(timer),
    testContext: {
      pluginPath: "/synthetic/napcat/plugins/qq-voice-call",
      core: {
        selfInfo: { uid: "synthetic_uid", uin: "10000" },
        context: { session: {
          getAccountPath: () => "/synthetic/account",
          getAVSDKService: () => ({
            addKernelAVSDKListener: () => 1,
            removeKernelAVSDKListener() {},
          }),
        } },
      },
    },
    invokeStub: async (command) => {
      assert.equal(command, 1);
      calls.push(now);
      maxInFlight = Math.max(maxInFlight, ++inFlight);
      if (costMs) await new Promise((resolve) => fakeSetTimeout(resolve, costMs));
      inFlight -= 1;
      if (fail) throw new Error("synthetic login failure");
      return { posted: true };
    },
    startHostStub: async () => { hostStarts += 1; },
    stopHostStub: async () => {
      if (stopCostMs) await new Promise((resolve) => fakeSetTimeout(resolve, stopCostMs));
    },
  };
  const runtime = runInNewContext(`${runtimeSource}
    context = testContext;
    invokeAVSDK = invokeStub;
    createAVSDKHost = startHostStub;
    stopHostProcess = stopHostStub;
    ({ start: activateRuntime, stop: cleanupAVSDKResources,
       emit: (cmd) => handleAVSDKOutput(null, { cmd }), snapshot: publicStatus,
       hostSettings: normalizeHostSettings });
  `, sandbox);
  return {
    ...runtime,
    calls,
    get maxInFlight() { return maxInFlight; },
    get hostStarts() { return hostStarts; },
    // 推进虚拟时间，并让每次定时回调产生的 Promise 完成后再执行后续任务。
    async advance(ms) {
      const end = now + ms;
      while (true) {
        const next = [...timers].filter((timer) => timer.at <= end).sort((a, b) => a.at - b.at)[0];
        if (!next) break;
        now = next.at;
        timers.delete(next);
        next.run();
        await new Promise(setImmediate);
      }
      now = end;
    },
  };
}

test("每秒 10 次重登回调在不同登录耗时下均至少间隔 60 秒", async () => {
  for (const costMs of [0, 50, 150, 400]) {
    const runtime = createRuntime({ costMs });
    await runtime.start();
    await runtime.advance(500);
    for (let tick = 0; tick < 1200; tick += 1) {
      await runtime.advance(100);
      await runtime.emit(tick % 2 ? 120043 : 20050);
    }
    await runtime.advance(costMs);
    assert.deepEqual(runtime.calls, [500, 60500, 120500]);
    assert.equal(runtime.maxInFlight, 1);
    const { avsdk } = runtime.snapshot();
    assert.ok(avsdk.loginSuppressed > 1000);
    assert.equal(avsdk.lastLoginAt, new Date(120500).toISOString());
    assert.equal(avsdk.lastLoginResult, "object{posted}");
    await runtime.stop("test_complete");
  }
});

test("单次回调保留到期任务，回调停止后仍能重登", async () => {
  const runtime = createRuntime();
  await runtime.start();
  await runtime.advance(500);
  await runtime.emit(20050);
  assert.equal(runtime.snapshot().avsdk.loginPosted, false);
  await runtime.advance(59999);
  assert.equal(runtime.calls.length, 1);
  await runtime.advance(1);
  assert.deepEqual(runtime.calls, [500, 60500]);
  assert.equal(runtime.snapshot().avsdk.loginPosted, true);
  await runtime.stop("test_complete");
});

test("首次登录失败后的自动重试同样遵守 60 秒间隔", async () => {
  const runtime = createRuntime({ fail: true });
  await runtime.start();
  await runtime.advance(180500);
  assert.deepEqual(runtime.calls, [500, 60500, 120500, 180500]);
  assert.match(runtime.snapshot().lastError, /synthetic login failure/);
  assert.equal(runtime.snapshot().avsdk.lastLoginResult, null);
  await runtime.stop("test_complete");
});

test("登录耗时超过冷却窗口时也只允许一个命令在途", async () => {
  const runtime = createRuntime({ costMs: 65000 });
  await runtime.start();
  for (let tick = 0; tick < 655; tick += 1) {
    await runtime.advance(100);
    await runtime.emit(120043);
  }
  assert.deepEqual(runtime.calls, [500]);
  await runtime.advance(100);
  assert.deepEqual(runtime.calls, [500, 65600]);
  assert.equal(runtime.maxInFlight, 1);
  await runtime.stop("test_complete");
  await runtime.advance(65000);
  assert.equal(runtime.snapshot().avsdk.loginPosted, false);
});

test("清理取消待执行任务，重新激活仍保留最近登录的冷却", async () => {
  const runtime = createRuntime();
  await runtime.start();
  await runtime.advance(500);
  await runtime.emit(20050);
  await runtime.stop("bridge_disconnected");
  await runtime.advance(1000);
  await runtime.start();
  await runtime.advance(58999);
  assert.deepEqual(runtime.calls, [500]);
  await runtime.advance(1);
  assert.deepEqual(runtime.calls, [500, 60500]);
  await runtime.emit(20050);
  await runtime.stop("test_complete");
  await runtime.advance(120000);
  assert.equal(runtime.calls.length, 2);
});

test("桥重连期间上一代登录仍在途时，新一代等待其结束", async () => {
  const runtime = createRuntime({ costMs: 65000 });
  await runtime.start();
  await runtime.advance(500);
  await runtime.stop("bridge_disconnected");
  await runtime.start();
  await runtime.advance(60000);
  assert.deepEqual(runtime.calls, [500]);
  await runtime.advance(5500);
  assert.equal(runtime.calls.length, 2);
  assert.equal(runtime.maxInFlight, 1);
  await runtime.stop("test_complete");
  await runtime.advance(65000);
  assert.equal(runtime.snapshot().avsdk.loginPosted, false);
});

test("上一代 Host 清理完成后才允许重新激活", async () => {
  const runtime = createRuntime({ stopCostMs: 2500 });
  await runtime.start();
  const stop = runtime.stop("host_exit");
  const start = runtime.start();
  await new Promise(setImmediate);
  assert.equal(runtime.hostStarts, 1);
  await runtime.advance(2500);
  await Promise.all([stop, start]);
  assert.equal(runtime.hostStarts, 2);
  assert.equal(runtime.snapshot().active, true);
  const finalStop = runtime.stop("test_complete");
  await runtime.advance(2500);
  await finalStop;
});

test("Loader 自动定位兼容 Shell 与 Docker 的物理入口", () => {
  const shell = path.resolve("/synthetic/napcat/plugins/qq-voice-call", "../../..", "loadNapCat.js");
  const docker = path.resolve("/opt/QQ/resources/app/loadNapCat.js");
  assert.equal(createRuntime({ loaderPaths: [shell, docker] }).hostSettings().loaderPath, shell);
  assert.equal(createRuntime({ loaderPaths: [docker] }).hostSettings().loaderPath, docker);
});
