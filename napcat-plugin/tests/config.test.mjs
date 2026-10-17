import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { deriveBridgeSettings, normalizeRuntimeConfig } from "../protocol.mjs";

const source = fs.readFileSync(new URL("../index.mjs", import.meta.url), "utf8");
const runtimeSource = source.slice(source.indexOf("const PLUGIN_VERSION")).replace(/^export /gm, "");
const temporaryRoot = fileURLToPath(new URL("../../temp/", import.meta.url));
const plain = (value) => JSON.parse(JSON.stringify(value));

/** 执行实际配置入口；只向新建目录写合成设置，WebSocket、Host 和定时器均使用替身。 */
function fixture(t, saved) {
  fs.mkdirSync(temporaryRoot, { recursive: true });
  const root = fs.mkdtempSync(path.join(temporaryRoot, "qq-call-config-"));
  const configPath = path.join(root, "config.json");
  if (saved) fs.writeFileSync(configPath, JSON.stringify(saved));
  const pluginPath = path.join(root, "plugin");
  const appRoot = path.join(root, "QQ/resources/app");
  const hostConfig = {
    qqExecutable: path.join(root, "QQ/qq"),
    avsdkPath: path.join(appRoot, "avsdk/libAVSDKPlugin.so"),
    loaderPath: path.join(appRoot, "loadNapCat.js"),
    controlHost: "::1", controlPort: 16110, hostPort: 16111,
    userDataDir: path.join(root, "profile"),
  };
  for (const filename of [hostConfig.qqExecutable, hostConfig.avsdkPath, hostConfig.loaderPath,
    path.join(pluginPath, "host.cjs"), path.join(pluginPath, "host-bootstrap/napcat/napcat.mjs")]) {
    fs.mkdirSync(path.dirname(filename), { recursive: true });
    fs.writeFileSync(filename, "// QQ_VOICE_CALL_LOADER_HOOK_V1");
  }
  const connections = [], hosts = [], timers = new Set();
  const io = { failRename: false };
  class FakeWebSocket extends EventEmitter {
    static OPEN = 1;
    readyState = 1;
    constructor(url, options) { super(); connections.push({ url, options }); }
    send() {}
    close() { this.readyState = 3; }
  }
  const runtime = runInNewContext(`${runtimeSource}
    startHostControlServer = async () => { hostToken = 'synthetic-host-token'; };
    requestHost = async () => ({ ready: true, pluginFound: true });
    ({ init: plugin_init, cleanup: plugin_cleanup, get: plugin_get_config, set: plugin_set_config,
       schema: plugin_config_ui, activeConfig: () => pluginConfig, startHost: createAVSDKHost,
       scheduleReconnect });
  `, {
    deriveBridgeSettings, normalizeRuntimeConfig, path, URL,
    fs: { ...fs, renameSync: (...args) => {
      if (io.failRename) throw new Error("synthetic rename failure");
      return fs.renameSync(...args);
    } },
    process: { platform: "linux", env: {} },
    WebSocket: FakeWebSocket,
    setTimeout: (run, delay) => {
      const timer = { run, delay, unref() {} };
      timers.add(timer);
      return timer;
    },
    clearTimeout: (timer) => timers.delete(timer),
    spawn: (executable, args, options) => {
      const child = new EventEmitter();
      Object.assign(child, {
        stdout: new EventEmitter(), stderr: new EventEmitter(), exitCode: null, signalCode: null,
        kill: () => {
          queueMicrotask(() => { child.exitCode = 0; child.emit("exit", 0, null); });
          return true;
        },
      });
      hosts.push({ executable, args, options });
      return child;
    },
  });
  const context = {
    configPath, pluginPath, dataPath: root,
    logger: { info() {}, warn() {} },
    oneBot: { configLoader: { configData: { network: { websocketClients: [
      { enable: true, url: "ws://127.0.0.1:6199/ws", token: "inherited-fixture" },
    ] } } } },
  };
  t.after(async () => {
    try {
      await runtime.cleanup();
      assert.equal(timers.size, 0);
    } finally {
      assert.equal(path.dirname(root), path.resolve(temporaryRoot));
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
  return { ...runtime, context, configPath, hostConfig, connections, hosts, timers, io };
}

test("NapCat 配置表单显示全部设置、默认值和生效说明，运行版本与包版本一致", async (t) => {
  const runtime = fixture(t);
  const form = await runtime.get();
  const fields = runtime.schema.filter((field) => field.type !== "text");
  assert.equal(fields.length, 12);
  assert.equal(new Set(fields.map((field) => field.key)).size, fields.length);
  for (const field of fields) {
    assert.ok(Object.hasOwn(form, field.key), field.key);
    assert.deepEqual(plain(form[field.key]), plain(field.default), field.key);
  }
  assert.match(runtime.schema[0].default, /重新启用插件或重启 NapCat/);
  const metadata = JSON.parse(fs.readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(source.match(/const PLUGIN_VERSION = "([^"]+)"/)[1], metadata.version);
});

test("保存全部表单字段后重新加载，实际桥接 URL、鉴权、重连间隔与 Host 启动参数使用新值", async (t) => {
  const runtime = fixture(t);
  await runtime.init(runtime.context);
  const form = plain(await runtime.get());
  Object.assign(form, { serverUrl: "wss://control.invalid:8443/ws", token: "new-fixture-token",
    voicePath: "/calls/custom", reconnectIntervalMs: 12345 });
  for (const [key, value] of Object.entries(runtime.hostConfig)) form[`avHost.${key}`] = value;
  await runtime.set(runtime.context, form);
  assert.equal(runtime.connections.length, 1);
  assert.equal(runtime.hosts.length, 0);
  assert.equal(runtime.activeConfig().serverUrl, "");
  const persisted = JSON.parse(fs.readFileSync(runtime.configPath, "utf8"));
  assert.deepEqual(persisted.avHost, runtime.hostConfig);
  assert.equal(persisted.token, "new-fixture-token");
  assert.equal(Object.keys(persisted).some((key) => key.includes(".")), false);
  assert.equal(Object.hasOwn(persisted, "clearToken"), false);
  const reentered = await runtime.get();
  assert.equal(reentered.token, "");
  assert.equal(reentered["avHost.hostPort"], 16111);

  await runtime.cleanup();
  await runtime.init(runtime.context);
  const connection = runtime.connections.at(-1);
  assert.equal(connection.url, "wss://control.invalid:8443/calls/custom");
  assert.equal(connection.options.headers.Authorization, "Bearer new-fixture-token");
  runtime.scheduleReconnect();
  assert.equal([...runtime.timers][0].delay, 12345);
  await runtime.startHost();
  const host = runtime.hosts[0];
  assert.equal(host.executable, runtime.hostConfig.qqExecutable);
  assert.equal(host.options.cwd, path.dirname(runtime.hostConfig.loaderPath));
  assert.equal(host.options.env.QQ_VOICE_CALL_AVSDK_PATH, runtime.hostConfig.avsdkPath);
  assert.equal(host.options.env.QQ_VOICE_CALL_AV_HOST_HOST, "::1");
  assert.equal(host.options.env.QQ_VOICE_CALL_AV_HOST_PORT, "16111");
  assert.equal(host.options.env.QQ_VOICE_CALL_BRIDGE_HOST, "::1");
  assert.equal(host.options.env.QQ_VOICE_CALL_BRIDGE_PORT, "16110");
  assert.equal(host.options.env.QQ_VOICE_CALL_USER_DATA_DIR, runtime.hostConfig.userDataDir);
  assert.ok(host.args.includes(`--user-data-dir=${runtime.hostConfig.userDataDir}`));
  await runtime.set(runtime.context, { "avHost.controlPort": 16112, serverUrl: "ws://next.invalid:6199/ws" });
  assert.equal(runtime.activeConfig().avHost.controlPort, 16110);
  assert.equal((await runtime.get())["avHost.controlPort"], 16112);
  assert.equal(runtime.hosts.length, 1);
  assert.equal(runtime.connections.length, 2);
});

test("令牌留空保留旧值，明确清除后复用 NapCat Token，读取始终隐藏令牌", async (t) => {
  const runtime = fixture(t, { token: "saved-fixture-token" });
  await runtime.init(runtime.context);
  await runtime.set(runtime.context, { token: "", reconnectIntervalMs: 6000 });
  assert.equal(JSON.parse(fs.readFileSync(runtime.configPath, "utf8")).token, "saved-fixture-token");
  assert.equal((await runtime.get()).token, "");
  await runtime.set(runtime.context, { clearToken: true });
  assert.equal(JSON.parse(fs.readFileSync(runtime.configPath, "utf8")).token, "");
  assert.equal((await runtime.get()).clearToken, false);
  await runtime.cleanup();
  await runtime.init(runtime.context);
  assert.equal(runtime.connections.at(-1).options.headers.Authorization, "Bearer inherited-fixture");
});

test("旧嵌套 Host 配置补齐缺省值，部分保存保留已有路径和令牌", async (t) => {
  const runtime = fixture(t, { token: "legacy-fixture", avHost: { loaderPath: "/legacy/loadNapCat.js" } });
  const before = fs.readFileSync(runtime.configPath, "utf8");
  await runtime.init(runtime.context);
  assert.equal(fs.readFileSync(runtime.configPath, "utf8"), before);
  const form = await runtime.get();
  assert.equal(form["avHost.loaderPath"], "/legacy/loadNapCat.js");
  assert.equal(form["avHost.hostPort"], 6111);
  await runtime.set(runtime.context, { avHost: { controlPort: 7110 } });
  const saved = JSON.parse(fs.readFileSync(runtime.configPath, "utf8"));
  assert.equal(saved.avHost.loaderPath, "/legacy/loadNapCat.js");
  assert.equal(saved.avHost.controlPort, 7110);
  assert.equal(saved.avHost.hostPort, 6111);
  assert.equal(saved.token, "legacy-fixture");
});

test("非法地址、范围、端口冲突和字段类型在落盘前拒绝，保存不会触发重连或 Host 启动", async (t) => {
  const runtime = fixture(t, { serverUrl: "ws://control.invalid:6199/ws" });
  await runtime.init(runtime.context);
  const before = fs.readFileSync(runtime.configPath, "utf8");
  const formBefore = plain(await runtime.get());
  for (const incoming of [
    { serverUrl: "https://control.invalid" }, { serverUrl: "ws://user:secret@control.invalid" },
    { reconnectIntervalMs: 999 }, { reconnectIntervalMs: 60001 }, { reconnectIntervalMs: true },
    { "avHost.controlPort": 0 }, { "avHost.hostPort": 65536 }, { "avHost.controlPort": 6111 },
    { "avHost.controlHost": "0.0.0.0" }, { "avHost.qqExecutable": "" }, { "avHost.avsdkPath": "" },
    { avHost: [] }, { token: {} }, { voicePath: "/calls?query" }, { clearToken: "true" },
  ]) {
    await assert.rejects(runtime.set(runtime.context, incoming));
    assert.equal(fs.readFileSync(runtime.configPath, "utf8"), before);
    assert.deepEqual(plain(await runtime.get()), formBefore);
  }
  assert.equal(runtime.connections.length, 1);
  assert.equal(runtime.hosts.length, 0);
  assert.equal(runtime.timers.size, 0);
});

test("写入失败保留已保存配置和内存快照，并清理临时文件", async (t) => {
  const runtime = fixture(t, { reconnectIntervalMs: 7000 });
  await runtime.init(runtime.context);
  const before = fs.readFileSync(runtime.configPath, "utf8");
  runtime.io.failRename = true;
  await assert.rejects(runtime.set(runtime.context, { reconnectIntervalMs: 8000 }), /synthetic rename failure/);
  assert.equal(fs.readFileSync(runtime.configPath, "utf8"), before);
  assert.equal((await runtime.get()).reconnectIntervalMs, 7000);
  assert.equal(runtime.activeConfig().reconnectIntervalMs, 7000);
  assert.equal(fs.existsSync(`${runtime.configPath}.tmp`), false);
});
