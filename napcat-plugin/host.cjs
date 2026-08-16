"use strict";

/**
 * QQ 原生语音通话的独立 Electron Host。
 *
 * 这个文件只由 QQ/NapCat launcher 在 QQ_VOICE_CALL_AV_HOST=1 时加载。它不参与
 * NapCat 插件生命周期，而是创建隐藏 BrowserWindow、加载 QQ 自带的 PPAPI
 * AVSDK，再通过回环 HTTP 将 AVSDK 命令和输出转发给 NapCat 插件。
 */

const { app, BrowserWindow, ipcMain } = require("electron");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const crypto = require("node:crypto");
const { sanitizeVisibleSurfaceReport } = require("./host-capabilities.cjs");

const STATE_CHANNEL = "qq-voice-call:host-state";
const OUTPUT_CHANNEL = "qq-voice-call:avsdk-output";
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);
const ALLOWED_COMMANDS = new Set([1, 5, 55]);
const HOST_BUILD_MARKER = "2026-08-15-avsdk-main-world-3";

function readCommandLineSwitch(name) {
  let enabled = null;
  let value = null;
  try {
    if (typeof app.commandLine.hasSwitch === "function") {
      enabled = app.commandLine.hasSwitch(name);
    }
  } catch {
    enabled = null;
  }
  try {
    if (typeof app.commandLine.getSwitchValue === "function") {
      value = app.commandLine.getSwitchValue(name) || null;
    }
  } catch {
    value = null;
  }
  return { enabled, value };
}

function collectHostDiagnostics() {
  return {
    buildMarker: HOST_BUILD_MARKER,
    execPath: process.execPath,
    argv: process.argv.slice(0, 64),
    versions: {
      electron: process.versions.electron || null,
      chrome: process.versions.chrome || null,
      node: process.versions.node || null,
    },
    commandLine: {
      allowCommandLinePlugins: readCommandLineSwitch("allow-command-line-plugins"),
      registerPepperPlugins: readCommandLineSwitch("register-pepper-plugins"),
      plugins: readCommandLineSwitch("enable-plugins"),
    },
    expectedWebPreferences: {
      contextIsolation: false,
      nodeIntegration: true,
      plugins: true,
    },
  };
}

function integerSetting(value, fallback, name) {
  const parsed = Number(value ?? fallback);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${name} must be an integer TCP port`);
  }
  return parsed;
}

function loadSettings(env = process.env) {
  const host = String(env.QQ_VOICE_CALL_AV_HOST_HOST || "127.0.0.1");
  const bridgeHost = String(env.QQ_VOICE_CALL_BRIDGE_HOST || "127.0.0.1");
  if (!LOOPBACK_HOSTS.has(host) || !LOOPBACK_HOSTS.has(bridgeHost)) {
    throw new Error("AVSDK Host endpoints must use loopback addresses");
  }
  const avsdkPath = path.resolve(
    env.QQ_VOICE_CALL_AVSDK_PATH || "/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so",
  );
  const userDataDir = path.resolve(
    env.QQ_VOICE_CALL_USER_DATA_DIR || path.join(__dirname, "av-host-profile"),
  );
  const token = String(env.QQ_VOICE_CALL_BRIDGE_TOKEN || "").trim();
  if (Buffer.byteLength(token, "utf8") < 32) {
    throw new Error("AVSDK Host control token is missing or shorter than 32 bytes");
  }
  return {
    host,
    hostPort: integerSetting(env.QQ_VOICE_CALL_AV_HOST_PORT, 6111, "AV host port"),
    bridgeHost,
    bridgePort: integerSetting(env.QQ_VOICE_CALL_BRIDGE_PORT, 6110, "bridge port"),
    token,
    avsdkPath,
    userDataDir,
  };
}

const settings = loadSettings();
let avWindow = null;
let controlServer = null;
let nextInvocationId = 1;
let rendererState = {
  ready: false,
  pluginFound: false,
  introspection: null,
  messageCount: 0,
  forwardedCount: 0,
  lastForwardedCommand: null,
  forwardError: null,
  missingPayloadCount: 0,
  lastMessageShape: null,
  invocationCount: 0,
  lastInvocationCommand: null,
  lastInvocationAt: null,
  diagnostics: null,
  error: null,
};

function tokenMatches(request) {
  const header = String(request.headers.authorization || "");
  if (!header.startsWith("Bearer ")) return false;
  const supplied = Buffer.from(header.slice(7), "utf8");
  const expected = Buffer.from(settings.token, "utf8");
  return supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
}

function sendJson(response, statusCode, body) {
  const encoded = Buffer.from(JSON.stringify(body));
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": encoded.byteLength,
    "Cache-Control": "no-store",
  });
  response.end(encoded);
}

async function readJsonBody(request, limit = 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new Error("request body is too large");
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8");
  return text ? JSON.parse(text) : {};
}

function postToBridge(body) {
  const encoded = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        host: settings.bridgeHost,
        port: settings.bridgePort,
        path: "/v1/avsdk/output",
        method: "POST",
        headers: {
          Authorization: `Bearer ${settings.token}`,
          "Content-Type": "application/json",
          "Content-Length": encoded.byteLength,
        },
        timeout: 3000,
      },
      (response) => {
        response.resume();
        response.on("end", () => {
          if (response.statusCode >= 200 && response.statusCode < 300) resolve();
          else reject(new Error(`NapCat bridge returned HTTP ${response.statusCode}`));
        });
      },
    );
    request.on("timeout", () => request.destroy(new Error("NapCat bridge timeout")));
    request.on("error", reject);
    request.end(encoded);
  });
}

async function startControlServer() {
  controlServer = http.createServer(async (request, response) => {
    const urlHost = settings.host.includes(":") ? `[${settings.host}]` : settings.host;
    const requestUrl = new URL(request.url || "/", `http://${urlHost}:${settings.hostPort}`);
    if (request.method === "GET" && requestUrl.pathname === "/healthz") {
      sendJson(response, 200, { ok: true });
      return;
    }
    if (!tokenMatches(request)) {
      sendJson(response, 401, { code: -1, message: "Unauthorized" });
      return;
    }
    if (request.method === "GET" && requestUrl.pathname === "/v1/status") {
      sendJson(response, 200, {
        code: 0,
        data: {
          electron: process.versions.electron || null,
          chrome: process.versions.chrome || null,
          pid: process.pid,
          hostDiagnostics: collectHostDiagnostics(),
          ...rendererState,
        },
      });
      return;
    }
    if (request.method === "POST" && requestUrl.pathname === "/v1/invoke") {
      try {
        const body = await readJsonBody(request);
        const command = Number(body?.command);
        const params = body?.params;
        if (!ALLOWED_COMMANDS.has(command)) {
          sendJson(response, 400, { code: -1, message: "command is not allowed" });
          return;
        }
        if (!Array.isArray(params) || params.length > 16) {
          sendJson(response, 400, { code: -1, message: "invalid params" });
          return;
        }
        if (!avWindow || avWindow.isDestroyed() || !rendererState.ready) {
          sendJson(response, 503, { code: -1, message: "AVSDK renderer is not ready" });
          return;
        }
        const invocationId = Number.isInteger(body?.id) ? body.id : nextInvocationId++;
        const result = await avWindow.webContents.executeJavaScript(
          `window.qqVoiceCall.invoke(${JSON.stringify(command)},` +
            `${JSON.stringify(invocationId)},${JSON.stringify(params)})`,
          true,
        );
        rendererState = {
          ...rendererState,
          invocationCount: rendererState.invocationCount + 1,
          lastInvocationCommand: command,
          lastInvocationAt: new Date().toISOString(),
        };
        sendJson(response, 200, { code: 0, data: result || null });
      } catch (error) {
        rendererState = { ...rendererState, error: error?.message || String(error) };
        sendJson(response, 500, { code: -1, message: "AVSDK invocation failed" });
      }
      return;
    }
    if (request.method === "POST" && requestUrl.pathname === "/v1/introspection") {
      if (!avWindow || avWindow.isDestroyed() || !rendererState.ready || !rendererState.pluginFound) {
        sendJson(response, 503, { code: -1, message: "AVSDK renderer is not ready" });
        return;
      }
      try {
        // 只执行固定的本地反射函数；HTTP 请求体绝不参与 JavaScript 组装。
        const rawReport = await avWindow.webContents.executeJavaScript(
          "window.qqVoiceCall.inspectVisibleSurface()",
          true,
        );
        const report = sanitizeVisibleSurfaceReport(rawReport);
        rendererState = { ...rendererState, introspection: report };
        sendJson(response, 200, { code: 0, data: report });
      } catch {
        rendererState = {
          ...rendererState,
          // 不把 Renderer 异常文本回传到状态接口，避免泄露虚拟机环境信息。
          error: "AVSDK visible-surface inspection failed",
        };
        sendJson(response, 500, { code: -1, message: "AVSDK visible-surface inspection failed" });
      }
      return;
    }
    sendJson(response, 404, { code: -1, message: "Not Found" });
  });
  controlServer.on("clientError", (_error, socket) => socket.destroy());
  await new Promise((resolve, reject) => {
    controlServer.once("error", reject);
    controlServer.listen(settings.hostPort, settings.host, () => {
      console.log(`[QQVoiceCallAVHost] listening on ${settings.host}:${settings.hostPort}`);
      resolve();
    });
  });
}

ipcMain.on(STATE_CHANNEL, (_event, incoming) => {
  rendererState = {
    ...rendererState,
    ready: Boolean(incoming?.ready),
    pluginFound: Boolean(incoming?.pluginFound),
    diagnostics:
      incoming?.diagnostics && typeof incoming.diagnostics === "object"
        ? incoming.diagnostics
        : null,
    error: typeof incoming?.error === "string" ? incoming.error.slice(0, 500) : null,
  };
});

ipcMain.on(OUTPUT_CHANNEL, (_event, incoming) => {
  rendererState = { ...rendererState, messageCount: rendererState.messageCount + 1 };
  if (!incoming || typeof incoming !== "object") {
    rendererState = {
      ...rendererState,
      missingPayloadCount: rendererState.missingPayloadCount + 1,
      lastMessageShape: { type: typeof incoming },
    };
    console.warn(
      `[QQVoiceCallAVHost] AVSDK output ignored: ` +
      `messageType=${typeof incoming}`,
    );
    return;
  }
  const command = Number(incoming.cmd ?? incoming.command);
  const value = incoming.value ?? incoming.param ?? incoming.data ?? incoming.payload;
  rendererState = {
    ...rendererState,
    lastMessageShape: {
      keys: Object.keys(incoming).slice(0, 16),
      command: Number.isInteger(command) ? command : null,
      valueType: Array.isArray(value) ? "array" : typeof value,
      valueLength: typeof value === "string" || Array.isArray(value) ? value.length : null,
    },
  };
  if (value === undefined) {
    rendererState = {
      ...rendererState,
      missingPayloadCount: rendererState.missingPayloadCount + 1,
    };
    console.warn(
      `[QQVoiceCallAVHost] AVSDK output has no value/param: cmd=${command}`,
    );
  }
  if (!Number.isInteger(command)) {
    rendererState = {
      ...rendererState,
      missingPayloadCount: rendererState.missingPayloadCount + 1,
    };
    console.warn(
      `[QQVoiceCallAVHost] AVSDK output ignored: ` +
      `invalidCommand keys=${Object.keys(incoming).slice(0, 16).join(",")}`,
    );
    return;
  }
  void postToBridge({
    command,
    id: Number.isInteger(incoming.id) ? incoming.id : 0,
    value,
  }).then(
    () => {
      rendererState = {
        ...rendererState,
        forwardedCount: rendererState.forwardedCount + 1,
        lastForwardedCommand: command,
        forwardError: null,
      };
    },
    (error) => {
      rendererState = { ...rendererState, forwardError: error?.message || String(error) };
      console.warn(
        `[QQVoiceCallAVHost] AVSDK output forward failed: ` +
        `cmd=${command} error=${error?.message || String(error)}`,
      );
    },
  );
});

if (!fs.existsSync(settings.avsdkPath)) {
  throw new Error(`QQ AVSDK library was not found: ${settings.avsdkPath}`);
}
fs.mkdirSync(settings.userDataDir, { recursive: true });
// Chromium 默认会拒绝只由命令行注册的 PPAPI；QQ 自己的 Renderer 参数也带有此开关。
app.commandLine.appendSwitch("allow-command-line-plugins");
app.commandLine.appendSwitch(
  "register-pepper-plugins",
  `${settings.avsdkPath};application/x-ppapi-avSDK`,
);
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("no-sandbox");
app.setPath("userData", settings.userDataDir);
console.log(
  `[QQVoiceCallAVHost][diagnostics] ${JSON.stringify(collectHostDiagnostics())}`,
);

app.whenReady()
  .then(async () => {
    avWindow = new BrowserWindow({
      width: 320,
      height: 240,
      show: false,
      webPreferences: {
        // QQ AVSDK 的 Pepper 对象只在页面主世界暴露原生方法；Host 只加载
        // 插件包内的可信 host.html，因此这里按 QQ 原生 Host 的兼容边界运行。
        contextIsolation: false,
        nodeIntegration: true,
        plugins: true,
        sandbox: false,
        preload: path.join(__dirname, "host-preload.cjs"),
      },
    });
    // nodeIntegration 只为本地 PPAPI 页面服务；阻止任何导航或新窗口把它带到外部内容。
    avWindow.webContents.on("will-navigate", (event) => event.preventDefault());
    avWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    avWindow.webContents.on("render-process-gone", (_event, details) => {
      rendererState = {
        ...rendererState,
        ready: false,
        pluginFound: false,
        error: `renderer process gone: ${details?.reason || "unknown"}`,
        diagnostics: {
          ...(rendererState.diagnostics || {}),
          renderProcessGone: {
            reason: details?.reason || null,
            exitCode: Number.isInteger(details?.exitCode) ? details.exitCode : null,
          },
        },
      };
    });
    avWindow.webContents.on("console-message", (_event, level, message, line, sourceId) => {
      console.log(
        `[QQVoiceCallAVHost][renderer:${level}] ${String(message).slice(0, 500)} ` +
          `(${String(sourceId || "unknown")}:${Number(line) || 0})`,
      );
    });
    await avWindow.loadFile(path.join(__dirname, "host.html"));
    await startControlServer();
  })
  .catch((error) => {
    rendererState = { ...rendererState, error: error?.message || String(error) };
    console.error(`[QQVoiceCallAVHost] startup failed: ${rendererState.error}`);
    app.quit();
  });

app.on("window-all-closed", () => app.quit());
