/**
 * QQ 原生语音通话 NapCat 插件。
 *
 * AVSDK 信令状态机适配自 maibot-qq-voice-call (GPL-3.0-only)：
 * https://github.com/ClaudiaGardner/maibot-qq-voice-call
 * 本版本改为 Windows/虚拟机 NapCat 主动连接 OneBot，并把 AVSDK Host
 * 通过独立 QQ/Electron AVSDK Host 加载 PPAPI 插件；OneBot 连接断开时会立即销毁 Listener 和 Host。
 */

import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { spawn } from "node:child_process";
import { WebSocket } from "ws";
import capabilityModule from "./host-capabilities.cjs";
import serviceCapabilityModule from "./service-capabilities.cjs";
import staticArtifactModule from "./static-artifact-report.cjs";

import {
  AVSDK_SERVICE_SURFACE_DIAGNOSTIC_CAPABILITY,
  AVSDK_STATIC_ARTIFACTS_DIAGNOSTIC_CAPABILITY,
  AVSDK_VISIBLE_SURFACE_DIAGNOSTIC_CAPABILITY,
  DIAGNOSTIC_CAPTURE_LIMITS,
  MANUAL_HANGUP_CAPTURE_CAPABILITY,
  PROTOCOL_VERSION,
  buildAcceptParams,
  deriveBridgeSettings,
  normalizeDiagnosticCaptureStart,
  normalizeAVSDKServiceSurfaceDiagnosticStart,
  normalizeStaticArtifactsDiagnosticStart,
  normalizeVisibleSurfaceDiagnosticStart,
  normalizeRuntimeConfig,
  serializeCaptureValue,
  shouldAcceptCaller,
} from "./protocol.mjs";

const { CAPABILITY_REPORT_SCHEMA, sanitizeVisibleSurfaceReport } = capabilityModule;
const {
  SERVICE_SURFACE_REPORT_SCHEMA,
  collectAVSDKServiceSurface,
  sanitizeAVSDKServiceSurfaceReport,
} = serviceCapabilityModule;
const {
  STATIC_ARTIFACT_REPORT_SCHEMA,
  collectStaticArtifactReport,
  sanitizeStaticArtifactReport,
} = staticArtifactModule;

const PLUGIN_VERSION = "0.1.7";
const STATIC_AVSDK_PLUGIN_PATH = "/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so";
let context = null;
let logger = null;
let pluginConfig = {
  serverUrl: "",
  token: "",
  voicePath: "/qq-voice-call",
  reconnectIntervalMs: 5000,
  avHost: {
    qqExecutable: "/opt/QQ/qq",
    avsdkPath: "/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so",
    loaderPath: "",
    controlHost: "127.0.0.1",
    controlPort: 6110,
    hostPort: 6111,
    userDataDir: "",
  },
};
let bridgeSettings = null;
let bridgeSocket = null;
let reconnectTimer = null;
let shuttingDown = false;
// 桥接控制消息、通话控制输出和 20001 网络包分队列，避免网络包处理耗时时错过来电。
let bridgeCommandQueue = Promise.resolve();
let outputControlQueue = Promise.resolve();
let outputNetworkQueue = Promise.resolve();

let hostControlServer = null;
let hostProcess = null;
let hostSettings = null;
let hostToken = null;
let avsdkService = null;
let listener = null;
let listenerId = null;
let activeInvite = null;
let acceptTimer = null;
let loginTimer = null;
let nextInvocationId = 1;
let runtimeConfig = normalizeRuntimeConfig();
let activationPromise = null;
let diagnosticCapture = null;
let diagnosticFinalizePromise = null;
let visibleSurfaceDiagnostic = null;
let serviceSurfaceDiagnostic = null;
let staticArtifactsDiagnostic = null;

const CALL_CONTROL_ACTIONS = new Set(["accept", "reject", "hangup", "mute", "unmute"]);
const DIAGNOSTIC_TERMINAL_GRACE_MS = 300;

const state = {
  active: false,
  lastError: null,
  bridge: {
    connected: false,
    url: null,
    lastConnectedAt: null,
    lastDisconnectedAt: null,
  },
  avsdk: idleAVSDK(),
  call: idleCall(),
};

function idleAVSDK() {
  return {
    serviceAvailable: false,
    listenerRegistered: false,
    hostReady: false,
    pluginFound: false,
    hostDiagnostics: null,
    rendererDiagnostics: null,
    loginPosted: false,
    lastOutputCommand: null,
    outputCount: 0,
    commandCounts: {},
    kernelEventCount: 0,
    lastKernelEvent: null,
    kernelActionForwardCount: 0,
    kernelActionForwardError: null,
    hostMessageCount: 0,
    hostForwardedCount: 0,
    hostMissingPayloadCount: 0,
    hostLastMessageShape: null,
    hostForwardError: null,
    visibleSurfaceDiagnostic: null,
    serviceSurfaceDiagnostic: null,
    staticArtifactsDiagnostic: null,
  };
}

function idleCall() {
  return {
    phase: "idle",
    inviteAt: null,
    inviteReceivedAt: null,
    connectedAt: null,
    endedAt: null,
    endReason: null,
    callerUid: null,
    callerUin: null,
    callerName: null,
    blockedReason: null,
    acceptDecision: null,
    acceptDecisionReason: null,
    acceptCommandPostedAt: null,
    acceptPostState: null,
    acceptResultCode: null,
    enterRoomResultCode: null,
    lastControlAction: null,
    lastControlRequestId: null,
    lastControlStatus: null,
  };
}

function publicStatus() {
  return {
    active: state.active,
    lastError: state.lastError,
    bridge: { ...state.bridge },
    avsdk: { ...state.avsdk },
    call: { ...state.call },
  };
}

function setError(error) {
  state.lastError = String(error?.message ?? error ?? "unknown error").slice(0, 500);
  logger?.warn(`[QQVoiceCall] ${state.lastError}`);
  sendStatus();
}

function normalizeHostSettings() {
  const source = pluginConfig.avHost && typeof pluginConfig.avHost === "object"
    ? pluginConfig.avHost
    : {};
  const integer = (value, fallback, name) => {
    const parsed = Number(value ?? fallback);
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
      throw new Error(`avHost.${name} 必须是 1 到 65535 的整数`);
    }
    return parsed;
  };
  const configuredHost = String(source.controlHost ?? "127.0.0.1").trim();
  const controlHost = configuredHost === "localhost" ? "127.0.0.1" : configuredHost;
  if (controlHost !== "127.0.0.1" && controlHost !== "::1") {
    throw new Error("avHost.controlHost 只允许回环地址");
  }
  const qqExecutable = String(source.qqExecutable ?? "/opt/QQ/qq").trim();
  const avsdkPath = String(
    source.avsdkPath ?? "/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so",
  ).trim();
  const defaultLoaderPath = path.resolve(
    context?.pluginPath ?? ".",
    "../../..",
    "loadNapCat.js",
  );
  const loaderPath = path.resolve(
    String(source.loaderPath ?? "").trim() || defaultLoaderPath,
  );
  if (!qqExecutable || !avsdkPath || !loaderPath) throw new Error("QQ AVSDK Host 路径不能为空");
  const userDataDir = String(
    source.userDataDir || path.join(context?.dataPath ?? context?.pluginPath ?? ".", "av-host-profile"),
  ).trim();
  return {
    qqExecutable,
    avsdkPath,
    loaderPath,
    controlHost,
    controlPort: integer(source.controlPort, 6110, "controlPort"),
    hostPort: integer(source.hostPort, 6111, "hostPort"),
    userDataDir,
  };
}

function timingSafeTokenMatches(request) {
  const header = String(request.headers.authorization ?? "");
  if (!header.startsWith("Bearer ") || !hostToken) return false;
  const supplied = Buffer.from(header.slice(7), "utf8");
  const expected = Buffer.from(hostToken, "utf8");
  return supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
}

function writeJson(response, statusCode, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": body.byteLength,
    "Cache-Control": "no-store",
  });
  response.end(body);
}

async function readJsonBody(request, limit = 2 * 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new Error("AVSDK Host 回调数据过大");
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8");
  return text ? JSON.parse(text) : {};
}

async function startHostControlServer() {
  if (hostControlServer) return;
  hostSettings ??= normalizeHostSettings();
  hostToken = crypto.randomBytes(32).toString("hex");
  const server = http.createServer(async (request, response) => {
    const urlHost = hostSettings.controlHost.includes(":")
      ? `[${hostSettings.controlHost}]`
      : hostSettings.controlHost;
    const requestUrl = new URL(
      request.url ?? "/",
      `http://${urlHost}:${hostSettings.controlPort}`,
    );
    if (request.method === "GET" && requestUrl.pathname === "/healthz") {
      writeJson(response, 200, { ok: true });
      return;
    }
    if (!timingSafeTokenMatches(request)) {
      writeJson(response, 401, { code: -1, message: "Unauthorized" });
      return;
    }
    if (request.method === "POST" && requestUrl.pathname === "/v1/avsdk/output") {
      try {
        const incoming = await readJsonBody(request);
        const task = enqueueAVSDKOutput(incoming);
        await task;
        writeJson(response, 200, { code: 0, message: "accepted" });
      } catch (error) {
        setError(`AVSDK Host 输出处理失败: ${error?.message ?? error}`);
        writeJson(response, 400, { code: -1, message: "invalid AVSDK output" });
      }
      return;
    }
    writeJson(response, 404, { code: -1, message: "Not Found" });
  });
  server.on("clientError", (_error, socket) => socket.destroy());
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(hostSettings.controlPort, hostSettings.controlHost, resolve);
  });
  hostControlServer = server;
}

/**
 * 让通话控制输出优先于可能长时间等待的 20001 网络信令。
 *
 * Args:
 *   incoming: AVSDK Host 转发的原始输出对象。
 * Returns:
 *   当前输出处理 Promise。
 */
function enqueueAVSDKOutput(incoming) {
  const command = Number(incoming?.cmd ?? incoming?.command);
  if (command === 20001) {
    const task = outputNetworkQueue.then(() => handleAVSDKOutput(null, incoming));
    outputNetworkQueue = task.catch(setError);
    return task;
  }
  const task = outputControlQueue.then(() => handleAVSDKOutput(null, incoming));
  outputControlQueue = task.catch(setError);
  return task;
}

function requestHost(pathname, method = "GET", body = undefined) {
  if (!hostSettings || !hostToken) throw new Error("AVSDK Host 控制端尚未初始化");
  const encoded = body === undefined ? null : Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        host: hostSettings.controlHost,
        port: hostSettings.hostPort,
        path: pathname,
        method,
        headers: {
          Authorization: `Bearer ${hostToken}`,
          ...(encoded
            ? {
                "Content-Type": "application/json",
                "Content-Length": encoded.byteLength,
              }
            : {}),
        },
        timeout: 3000,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          let parsed = {};
          try {
            parsed = text ? JSON.parse(text) : {};
          } catch {
            parsed = { message: text.slice(0, 500) };
          }
          if (response.statusCode >= 200 && response.statusCode < 300) {
            resolve(parsed);
          } else {
            reject(new Error(`AVSDK Host 返回 HTTP ${response.statusCode}: ${parsed.message ?? "unknown"}`));
          }
        });
      },
    );
    request.on("timeout", () => request.destroy(new Error("AVSDK Host 请求超时")));
    request.on("error", reject);
    if (encoded) request.write(encoded);
    request.end();
  });
}

function stopHostProcess() {
  const child = hostProcess;
  hostProcess = null;
  if (!child || child.killed || child.exitCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    child.once("exit", finish);
    try {
      child.kill("SIGTERM");
    } catch {
      finish();
      return;
    }
    setTimeout(() => {
      if (!settled) {
        try {
          child.kill("SIGKILL");
        } catch {
          // 进程已退出时忽略二次终止错误。
        }
        finish();
      }
    }, 2500).unref?.();
  });
}

function loadPluginConfig(ctx) {
  if (!fs.existsSync(ctx.configPath)) return;
  try {
    const saved = JSON.parse(fs.readFileSync(ctx.configPath, "utf8"));
    if (saved && typeof saved === "object" && !Array.isArray(saved)) {
      pluginConfig = { ...pluginConfig, ...saved };
    }
  } catch (error) {
    logger?.warn(`[QQVoiceCall] plugin config load failed: ${error?.message ?? error}`);
  }
}

function persistPluginConfig(ctx) {
  fs.mkdirSync(path.dirname(ctx.configPath), { recursive: true });
  const temporaryPath = `${ctx.configPath}.tmp`;
  fs.writeFileSync(temporaryPath, JSON.stringify(pluginConfig, null, 2), "utf8");
  fs.renameSync(temporaryPath, ctx.configPath);
}

function sendBridge(payload) {
  if (!bridgeSocket || bridgeSocket.readyState !== WebSocket.OPEN) return false;
  bridgeSocket.send(JSON.stringify(payload));
  return true;
}

function sendHello() {
  return sendBridge({
    type: "hello",
    protocolVersion: PROTOCOL_VERSION,
    pluginVersion: PLUGIN_VERSION,
    capabilities: [
      MANUAL_HANGUP_CAPTURE_CAPABILITY,
      AVSDK_VISIBLE_SURFACE_DIAGNOSTIC_CAPABILITY,
      AVSDK_SERVICE_SURFACE_DIAGNOSTIC_CAPABILITY,
      AVSDK_STATIC_ARTIFACTS_DIAGNOSTIC_CAPABILITY,
    ],
    selfId: String(context?.core?.selfInfo?.uin ?? ""),
    platform: process.platform,
    qqVersion: String(context?.core?.context?.basicInfoWrapper?.getFullQQVersion?.() ?? ""),
  });
}

function sendStatus() {
  sendBridge({ type: "status", data: publicStatus() });
}

function diagnosticCaptureDirectory() {
  const dataPath = String(context?.dataPath ?? "").trim();
  if (!dataPath) throw new Error("plugin_data_path_unavailable");
  return path.resolve(dataPath, "protocol_capture");
}

function diagnosticCaptureIdentity(capture) {
  return {
    requestId: capture.requestId,
    captureId: capture.captureId,
    callId: capture.callId,
    kind: capture.kind,
    mode: capture.mode,
  };
}

function sendDiagnosticCaptureStatus(capture, status, reason = null) {
  sendBridge({
    type: "diagnostic_capture_status",
    protocolVersion: PROTOCOL_VERSION,
    ...diagnosticCaptureIdentity(capture),
    status,
    reason,
    phase: state.call.phase,
    timeoutMs: capture.timeoutMs,
    startedAt: capture.startedAt ?? null,
  });
}

/** 记录一条原始观察事件；任何序列化失败都不得干扰真实电话状态机。 */
function recordDiagnosticCaptureEvent(source, data) {
  const capture = diagnosticCapture;
  if (!capture) return false;
  if (capture.events.length >= DIAGNOSTIC_CAPTURE_LIMITS.maxEvents) {
    void finalizeDiagnosticCapture("limit_reached", "event_limit");
    return false;
  }

  let serialized;
  try {
    serialized = serializeCaptureValue(data);
  } catch (error) {
    serialized = {
      $type: "SerializationError",
      message: String(error?.message ?? error).slice(0, 500),
    };
  }
  const event = {
    sequence: capture.events.length + 1,
    at: new Date().toISOString(),
    source,
    data: serialized,
  };
  const eventBytes = Buffer.byteLength(JSON.stringify(event), "utf8") + 1;
  if (capture.eventBytes + eventBytes > DIAGNOSTIC_CAPTURE_LIMITS.maxBytes) {
    void finalizeDiagnosticCapture("limit_reached", "byte_limit");
    return false;
  }
  capture.events.push(event);
  capture.eventBytes += eventBytes;
  if (capture.events.length >= DIAGNOSTIC_CAPTURE_LIMITS.maxEvents) {
    void finalizeDiagnosticCapture("limit_reached", "event_limit");
  }
  return true;
}

function scheduleDiagnosticTerminalFinalize(reason) {
  const capture = diagnosticCapture;
  if (!capture) return;
  capture.terminalDetected = true;
  capture.terminalReason = reason;
  if (capture.terminalTimer) return;
  capture.terminalTimer = setTimeout(() => {
    capture.terminalTimer = null;
    void finalizeDiagnosticCapture("completed", reason);
  }, DIAGNOSTIC_TERMINAL_GRACE_MS);
  capture.terminalTimer.unref?.();
}

/**
 * 只允许一个调用方取得捕获状态，并用临时文件原子落盘。
 * 返回值是发送给 OneBot 的摘要；原始 events 永不经过桥连接传回。
 */
function finalizeDiagnosticCapture(status, reason) {
  if (!diagnosticCapture) return diagnosticFinalizePromise ?? Promise.resolve(null);
  const capture = diagnosticCapture;
  diagnosticCapture = null;
  if (capture.timeoutTimer) clearTimeout(capture.timeoutTimer);
  if (capture.terminalTimer) clearTimeout(capture.terminalTimer);
  capture.timeoutTimer = null;
  capture.terminalTimer = null;

  const finishedAt = new Date().toISOString();
  let temporaryPath = null;
  const task = (async () => {
    try {
      const directory = diagnosticCaptureDirectory();
      const captureToken = crypto
        .createHash("sha256")
        .update(capture.captureId, "utf8")
        .digest("hex")
        .slice(0, 16);
      const timestamp = capture.startedAt.replace(/[^0-9]/g, "").slice(0, 17);
      const filePath = path.join(directory, `manual_hangup_${timestamp}_${captureToken}.json`);
      temporaryPath = `${filePath}.${process.pid}.${crypto.randomBytes(4).toString("hex")}.tmp`;
      const document = {
        schema: "qq_voice_call.protocol_capture.v1",
        protocolVersion: PROTOCOL_VERSION,
        pluginVersion: PLUGIN_VERSION,
        capture: {
          ...diagnosticCaptureIdentity(capture),
          status,
          reason,
          timeoutMs: capture.timeoutMs,
          startedAt: capture.startedAt,
          finishedAt,
        },
        environment: capture.environment,
        limits: DIAGNOSTIC_CAPTURE_LIMITS,
        eventCount: capture.events.length,
        eventBytes: capture.eventBytes,
        events: capture.events,
      };
      const content = Buffer.from(`${JSON.stringify(document, null, 2)}\n`, "utf8");
      const fileSha256 = crypto.createHash("sha256").update(content).digest("hex");
      await fs.promises.mkdir(directory, { recursive: true, mode: 0o700 });
      await fs.promises.writeFile(temporaryPath, content, { flag: "wx", mode: 0o600 });
      await fs.promises.rename(temporaryPath, filePath);
      const result = {
        type: "diagnostic_capture_result",
        protocolVersion: PROTOCOL_VERSION,
        ...diagnosticCaptureIdentity(capture),
        status,
        reason,
        eventCount: capture.events.length,
        byteCount: content.byteLength,
        filePath,
        fileSha256,
        startedAt: capture.startedAt,
        finishedAt,
      };
      sendBridge(result);
      logger?.info(
        `[QQVoiceCall] diagnostic capture finalized: ` +
        `status=${status} reason=${reason} events=${capture.events.length}`,
      );
      return result;
    } catch (error) {
      if (temporaryPath) {
        try {
          await fs.promises.unlink(temporaryPath);
        } catch {
          // 写入失败时只清理由本次捕获创建的临时文件。
        }
      }
      const result = {
        type: "diagnostic_capture_result",
        protocolVersion: PROTOCOL_VERSION,
        ...diagnosticCaptureIdentity(capture),
        status: "failed",
        reason: "write_failed",
        eventCount: capture.events.length,
        byteCount: 0,
        filePath: null,
        fileSha256: null,
        errorCode: String(error?.code ?? "unknown").slice(0, 80),
        startedAt: capture.startedAt,
        finishedAt,
      };
      sendBridge(result);
      logger?.warn(
        `[QQVoiceCall] diagnostic capture finalized: ` +
        `status=failed reason=write_failed events=${capture.events.length} ` +
        `errorCode=${result.errorCode}`,
      );
      return result;
    }
  })();
  diagnosticFinalizePromise = task;
  const clearFinalizer = () => {
    if (diagnosticFinalizePromise === task) diagnosticFinalizePromise = null;
  };
  void task.then(clearFinalizer, clearFinalizer);
  return task;
}

/** 校验并武装当前已接通电话的一次性 raw 捕获。 */
function handleDiagnosticCaptureStart(message) {
  let request;
  try {
    request = normalizeDiagnosticCaptureStart(message);
  } catch (error) {
    const boundedIdentity = {
      requestId: String(message?.requestId ?? "").trim().slice(0, 128),
      captureId: String(message?.captureId ?? "").trim().slice(0, 128),
      callId: String(message?.callId ?? "").trim().slice(0, 128),
      kind: "manual_hangup",
      mode: "raw",
      timeoutMs: null,
    };
    sendDiagnosticCaptureStatus(
      boundedIdentity,
      "rejected",
      String(error?.message ?? "invalid_request").slice(0, 100),
    );
    logger?.warn("[QQVoiceCall] diagnostic capture rejected: reason=invalid_request");
    return;
  }

  let rejectedReason = null;
  if (diagnosticCapture) rejectedReason = "capture_already_armed";
  else if (diagnosticFinalizePromise) rejectedReason = "capture_finalizing";
  else if (!state.active || state.call.phase !== "connected") rejectedReason = "call_not_connected";
  else {
    try {
      diagnosticCaptureDirectory();
    } catch {
      rejectedReason = "plugin_data_path_unavailable";
    }
  }
  if (rejectedReason) {
    sendDiagnosticCaptureStatus(request, "rejected", rejectedReason);
    logger?.warn(`[QQVoiceCall] diagnostic capture rejected: reason=${rejectedReason}`);
    return;
  }

  const startedAt = new Date().toISOString();
  diagnosticCapture = {
    ...request,
    startedAt,
    environment: {
      platform: process.platform,
      arch: process.arch,
      nodeVersion: process.version,
      qqVersion: String(context?.core?.context?.basicInfoWrapper?.getFullQQVersion?.() ?? ""),
    },
    events: [],
    eventBytes: 0,
    terminalDetected: false,
    terminalReason: null,
    terminalTimer: null,
    timeoutTimer: null,
  };
  diagnosticCapture.timeoutTimer = setTimeout(() => {
    void finalizeDiagnosticCapture("expired", "timeout");
  }, request.timeoutMs);
  diagnosticCapture.timeoutTimer.unref?.();
  recordDiagnosticCaptureEvent("capture_started", {
    phase: state.call.phase,
    callState: state.call,
  });
  sendDiagnosticCaptureStatus(diagnosticCapture, "armed");
  logger?.info(
    `[QQVoiceCall] diagnostic capture armed: ` +
    `kind=manual_hangup timeoutMs=${request.timeoutMs}`,
  );
}

function visibleSurfaceDiagnosticIdentity(diagnostic) {
  return {
    requestId: diagnostic.requestId,
    kind: "avsdk_visible_surface",
  };
}

function sendVisibleSurfaceDiagnosticResult(diagnostic, status, reason = null) {
  sendBridge({
    type: "diagnostic_visible_surface_result",
    protocolVersion: PROTOCOL_VERSION,
    ...visibleSurfaceDiagnosticIdentity(diagnostic),
    status,
    reason,
    startedAt: diagnostic.startedAt ?? null,
    finishedAt: diagnostic.finishedAt ?? null,
    report: diagnostic.report ?? null,
  });
}

/**
 * 显式执行一次受限的 AVSDK 可见面反射。
 *
 * 该路径只调用 Host 内写死的 `inspectVisibleSurface()`，不会转发调用方的
 * 方法名、参数或 JavaScript，也不会调用任何 AVSDK 控制命令。
 */
async function handleVisibleSurfaceDiagnosticStart(message) {
  let request;
  try {
    request = normalizeVisibleSurfaceDiagnosticStart(message);
  } catch {
    sendVisibleSurfaceDiagnosticResult(
      {
        requestId: String(message?.requestId ?? "").trim().slice(0, 128),
        startedAt: null,
        finishedAt: new Date().toISOString(),
        report: null,
      },
      "rejected",
      "invalid_request",
    );
    return;
  }

  if (visibleSurfaceDiagnostic) {
    const previousStatus = visibleSurfaceDiagnostic.status;
    sendVisibleSurfaceDiagnosticResult(
      visibleSurfaceDiagnostic,
      previousStatus === "completed"
        ? "already_completed"
        : previousStatus === "failed"
          ? "already_failed"
          : "running",
      "host_lifecycle_limit",
    );
    return;
  }
  if (!state.active || !state.avsdk.hostReady || !state.avsdk.pluginFound) {
    const rejected = {
      ...request,
      status: "rejected",
      reason: "host_not_ready",
      startedAt: null,
      finishedAt: new Date().toISOString(),
      report: null,
    };
    state.avsdk.visibleSurfaceDiagnostic = rejected;
    sendStatus();
    sendVisibleSurfaceDiagnosticResult(rejected, "rejected", "host_not_ready");
    return;
  }

  const diagnostic = {
    ...request,
    status: "running",
    reason: null,
    startedAt: new Date().toISOString(),
    finishedAt: null,
    report: null,
  };
  visibleSurfaceDiagnostic = diagnostic;
  state.avsdk.visibleSurfaceDiagnostic = diagnostic;
  sendStatus();
  try {
    const response = await requestHost("/v1/introspection", "POST");
    const rawReport = response?.data ?? response;
    if (
      !rawReport ||
      typeof rawReport !== "object" ||
      Array.isArray(rawReport) ||
      rawReport.schema !== CAPABILITY_REPORT_SCHEMA
    ) {
      throw new Error("invalid_host_report");
    }
    // Host 已做一次裁剪，这里在跨进程桥前再次白名单化，防止扩散原生对象或环境字段。
    const report = sanitizeVisibleSurfaceReport(rawReport);
    diagnostic.status = "completed";
    diagnostic.report = report;
    diagnostic.finishedAt = new Date().toISOString();
    state.avsdk.visibleSurfaceDiagnostic = diagnostic;
    const callableCount = Array.isArray(report.callableMethods) ? report.callableMethods.length : 0;
    const candidateCount = Array.isArray(report.controlCandidates) ? report.controlCandidates.length : 0;
    logger?.info(
      `[QQVoiceCall] visible-surface diagnostic completed: ` +
        `methods=${callableCount} controlCandidates=${candidateCount}`,
    );
    sendStatus();
    sendVisibleSurfaceDiagnosticResult(diagnostic, "completed");
  } catch (error) {
    diagnostic.status = "failed";
    diagnostic.reason = "host_request_failed";
    diagnostic.finishedAt = new Date().toISOString();
    diagnostic.report = null;
    state.avsdk.visibleSurfaceDiagnostic = diagnostic;
    logger?.warn("[QQVoiceCall] visible-surface diagnostic failed: reason=host_request_failed");
    sendStatus();
    sendVisibleSurfaceDiagnosticResult(diagnostic, "failed", "host_request_failed");
  }
}

function serviceSurfaceDiagnosticIdentity(diagnostic) {
  return {
    requestId: diagnostic.requestId,
    kind: "avsdk_service_surface",
  };
}

function sendServiceSurfaceDiagnosticResult(diagnostic, status, reason = null) {
  sendBridge({
    type: "diagnostic_avsdk_service_surface_result",
    protocolVersion: PROTOCOL_VERSION,
    ...serviceSurfaceDiagnosticIdentity(diagnostic),
    status,
    reason,
    startedAt: diagnostic.startedAt ?? null,
    finishedAt: diagnostic.finishedAt ?? null,
    report: diagnostic.report ?? null,
  });
}

/**
 * 对当前激活周期已缓存的 AVSDK Service 做一次受限反射。
 *
 * 不会再次取得 Service，不读取 getter/属性值，不调用候选方法，也不扩展
 * AVSDK 控制命令。候选名只用于后续协议研究，不代表可以挂断电话。
 */
async function handleServiceSurfaceDiagnosticStart(message) {
  let request;
  try {
    request = normalizeAVSDKServiceSurfaceDiagnosticStart(message);
  } catch {
    sendServiceSurfaceDiagnosticResult(
      {
        requestId: String(message?.requestId ?? "").trim().slice(0, 128),
        startedAt: null,
        finishedAt: new Date().toISOString(),
        report: null,
      },
      "rejected",
      "invalid_request",
    );
    return;
  }

  if (serviceSurfaceDiagnostic) {
    const previousStatus = serviceSurfaceDiagnostic.status;
    sendServiceSurfaceDiagnosticResult(
      serviceSurfaceDiagnostic,
      previousStatus === "completed"
        ? "already_completed"
        : previousStatus === "failed"
          ? "already_failed"
          : "running",
      "service_runtime_limit",
    );
    return;
  }
  if (!state.active || !avsdkService) {
    const reason = state.active ? "service_unavailable" : "runtime_not_active";
    const rejected = {
      ...request,
      status: "rejected",
      reason,
      startedAt: null,
      finishedAt: new Date().toISOString(),
      report: null,
    };
    state.avsdk.serviceSurfaceDiagnostic = rejected;
    sendStatus();
    sendServiceSurfaceDiagnosticResult(rejected, "rejected", reason);
    return;
  }

  const diagnostic = {
    ...request,
    status: "running",
    reason: null,
    startedAt: new Date().toISOString(),
    finishedAt: null,
    report: null,
  };
  serviceSurfaceDiagnostic = diagnostic;
  state.avsdk.serviceSurfaceDiagnostic = diagnostic;
  sendStatus();
  try {
    const rawReport = collectAVSDKServiceSurface(avsdkService);
    if (
      !rawReport ||
      typeof rawReport !== "object" ||
      Array.isArray(rawReport) ||
      rawReport.schema !== SERVICE_SURFACE_REPORT_SCHEMA
    ) {
      throw new Error("invalid_service_surface_report");
    }
    // 模块已限制字段；跨桥前再次净化，避免未来 Service 结构变更泄漏数据。
    const report = sanitizeAVSDKServiceSurfaceReport(rawReport);
    diagnostic.status = "completed";
    diagnostic.report = report;
    diagnostic.finishedAt = new Date().toISOString();
    state.avsdk.serviceSurfaceDiagnostic = diagnostic;
    const candidateCount = Array.isArray(report.controlCandidates) ? report.controlCandidates.length : 0;
    logger?.info(
      `[QQVoiceCall] service-surface diagnostic completed: ` +
        `status=${report.status} controlCandidates=${candidateCount}`,
    );
    sendStatus();
    sendServiceSurfaceDiagnosticResult(diagnostic, "completed");
  } catch {
    diagnostic.status = "failed";
    diagnostic.reason = "reflection_failed";
    diagnostic.finishedAt = new Date().toISOString();
    diagnostic.report = null;
    state.avsdk.serviceSurfaceDiagnostic = diagnostic;
    logger?.warn("[QQVoiceCall] service-surface diagnostic failed: reason=reflection_failed");
    sendStatus();
    sendServiceSurfaceDiagnosticResult(diagnostic, "failed", "reflection_failed");
  }
}

function staticArtifactsDiagnosticIdentity(diagnostic) {
  return {
    requestId: diagnostic.requestId,
    kind: "avsdk_static_artifacts",
  };
}

function sendStaticArtifactsDiagnosticResult(diagnostic, status, reason = null) {
  sendBridge({
    type: "diagnostic_static_artifacts_result",
    protocolVersion: PROTOCOL_VERSION,
    ...staticArtifactsDiagnosticIdentity(diagnostic),
    status,
    reason,
    startedAt: diagnostic.startedAt ?? null,
    finishedAt: diagnostic.finishedAt ?? null,
    report: diagnostic.report ?? null,
  });
}

function defaultNapCatLoaderPath() {
  const pluginPath = String(context?.pluginPath ?? "").trim();
  return pluginPath ? path.resolve(pluginPath, "../../..", "loadNapCat.js") : "";
}

/**
 * 显式执行一次固定目标的 AVSDK 静态资源摘要扫描。
 *
 * 该路径只在 NapCat 插件进程内对两个写死的安装文件做有界读取；不会接受
 * 来自桥的文件参数、不会列举用户目录、不会执行命令，也不会调用 Host/AVSDK。
 */
async function handleStaticArtifactsDiagnosticStart(message) {
  let request;
  try {
    request = normalizeStaticArtifactsDiagnosticStart(message);
  } catch {
    sendStaticArtifactsDiagnosticResult(
      {
        requestId: String(message?.requestId ?? "").trim().slice(0, 128),
        startedAt: null,
        finishedAt: new Date().toISOString(),
        report: null,
      },
      "rejected",
      "invalid_request",
    );
    return;
  }

  if (staticArtifactsDiagnostic) {
    const previousStatus = staticArtifactsDiagnostic.status;
    sendStaticArtifactsDiagnosticResult(
      staticArtifactsDiagnostic,
      previousStatus === "completed"
        ? "already_completed"
        : previousStatus === "failed"
          ? "already_failed"
          : "running",
      "plugin_lifecycle_limit",
    );
    return;
  }
  if (!state.active) {
    const rejected = {
      ...request,
      status: "rejected",
      reason: "runtime_not_active",
      startedAt: null,
      finishedAt: new Date().toISOString(),
      report: null,
    };
    state.avsdk.staticArtifactsDiagnostic = rejected;
    sendStatus();
    sendStaticArtifactsDiagnosticResult(rejected, "rejected", "runtime_not_active");
    return;
  }

  const diagnostic = {
    ...request,
    status: "running",
    reason: null,
    startedAt: new Date().toISOString(),
    finishedAt: null,
    report: null,
  };
  staticArtifactsDiagnostic = diagnostic;
  state.avsdk.staticArtifactsDiagnostic = diagnostic;
  sendStatus();
  try {
    const rawReport = await collectStaticArtifactReport({
      avsdkPluginPath: STATIC_AVSDK_PLUGIN_PATH,
      napcatLoaderPath: defaultNapCatLoaderPath(),
    });
    if (
      !rawReport ||
      typeof rawReport !== "object" ||
      Array.isArray(rawReport) ||
      rawReport.schema !== STATIC_ARTIFACT_REPORT_SCHEMA
    ) {
      throw new Error("invalid_static_artifact_report");
    }
    // 扫描器已不返回路径或正文，跨桥前仍再次固定字段，避免未来变更扩散。
    const report = sanitizeStaticArtifactReport(rawReport);
    diagnostic.status = "completed";
    diagnostic.report = report;
    diagnostic.finishedAt = new Date().toISOString();
    state.avsdk.staticArtifactsDiagnostic = diagnostic;
    const scannedCount = report.artifacts.filter((item) => item.status === "scanned").length;
    logger?.info(
      `[QQVoiceCall] static-artifacts diagnostic completed: ` +
        `status=${report.status} artifacts=${scannedCount}/${report.artifacts.length}`,
    );
    sendStatus();
    sendStaticArtifactsDiagnosticResult(diagnostic, "completed");
  } catch {
    diagnostic.status = "failed";
    diagnostic.reason = "scan_failed";
    diagnostic.finishedAt = new Date().toISOString();
    diagnostic.report = null;
    state.avsdk.staticArtifactsDiagnostic = diagnostic;
    logger?.warn("[QQVoiceCall] static-artifacts diagnostic failed: reason=scan_failed");
    sendStatus();
    sendStaticArtifactsDiagnosticResult(diagnostic, "failed", "scan_failed");
  }
}

function scheduleReconnect() {
  if (shuttingDown || reconnectTimer) return;
  const delay = bridgeSettings?.reconnectIntervalMs ?? 5000;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectBridge();
  }, delay);
  reconnectTimer.unref?.();
}

function connectBridge() {
  if (shuttingDown || !context || bridgeSocket) return;
  try {
    bridgeSettings = deriveBridgeSettings(
      pluginConfig,
      context.oneBot?.configLoader?.configData ?? {},
    );
  } catch (error) {
    setError(error);
    scheduleReconnect();
    return;
  }

  state.bridge.url = bridgeSettings.url.replace(/\/[^/]*$/, "/qq-voice-call");
  const socket = new WebSocket(bridgeSettings.url, {
    headers: {
      Authorization: `Bearer ${bridgeSettings.token}`,
      "X-Self-ID": String(context.core?.selfInfo?.uin ?? ""),
      "User-Agent": `QQ-Voice-Call/${PLUGIN_VERSION}`,
    },
    handshakeTimeout: 5000,
    maxPayload: 1024 * 1024,
    perMessageDeflate: false,
  });
  bridgeSocket = socket;

  socket.on("open", () => {
    if (bridgeSocket !== socket) return;
    state.bridge.connected = true;
    state.bridge.lastConnectedAt = new Date().toISOString();
    state.lastError = null;
    sendHello();
    sendStatus();
    logger?.info(`[QQVoiceCall] connected to ${state.bridge.url}`);
  });
  socket.on("message", (raw) => {
    bridgeCommandQueue = bridgeCommandQueue
      .then(() => handleBridgeMessage(raw.toString("utf8")))
      .catch(setError);
  });
  socket.on("error", (error) => {
    if (bridgeSocket === socket) setError(`bridge connection failed: ${error?.message ?? error}`);
  });
  socket.on("close", () => {
    if (bridgeSocket !== socket) return;
    bridgeSocket = null;
    state.bridge.connected = false;
    state.bridge.lastDisconnectedAt = new Date().toISOString();
    recordDiagnosticCaptureEvent("bridge_disconnected", { phase: state.call.phase });
    void finalizeDiagnosticCapture("cancelled", "bridge_disconnected");
    bridgeCommandQueue = bridgeCommandQueue
      .then(() => deactivateRuntime("onebot_disconnected"))
      .catch(setError)
      .finally(scheduleReconnect);
    logger?.warn("[QQVoiceCall] OneBot disconnected; AVSDK runtime stopped");
  });
}

async function handleBridgeMessage(rawMessage) {
  const message = JSON.parse(rawMessage);
  if (!message || typeof message !== "object" || Array.isArray(message)) {
    throw new Error("bridge message must be an object");
  }
  if (message.type === "hello_request") {
    sendHello();
  } else if (message.type === "activate") {
    if (Number(message.protocolVersion) !== PROTOCOL_VERSION) {
      throw new Error(`unsupported bridge protocol: ${message.protocolVersion}`);
    }
    await activateRuntime(message.config);
  } else if (message.type === "deactivate") {
    await deactivateRuntime(String(message.reason ?? "onebot_requested"));
  } else if (message.type === "control") {
    await handleCallControl(message);
  } else if (message.type === "diagnostic_capture_start") {
    handleDiagnosticCaptureStart(message);
  } else if (message.type === "diagnostic_visible_surface_start") {
    await handleVisibleSurfaceDiagnosticStart(message);
  } else if (message.type === "diagnostic_avsdk_service_surface_start") {
    await handleServiceSurfaceDiagnosticStart(message);
  } else if (message.type === "diagnostic_static_artifacts_start") {
    await handleStaticArtifactsDiagnosticStart(message);
  } else if (message.type === "status_request") {
    sendStatus();
  } else if (message.type === "ping") {
    sendBridge({ type: "pong" });
  }
}

async function handleCallControl(message) {
  const requestId = String(message.requestId ?? "").trim();
  const action = String(message.action ?? "").trim().toLowerCase();
  const respond = (result) => {
    sendBridge({
      type: "control_result",
      protocolVersion: PROTOCOL_VERSION,
      requestId,
      action,
      phase: state.call.phase,
      ...result,
    });
  };
  if (!requestId || !CALL_CONTROL_ACTIONS.has(action)) {
    respond({
      status: "invalid_action",
      supported: false,
      message: "invalid call control action",
    });
    return;
  }
  state.call.lastControlAction = action;
  state.call.lastControlRequestId = requestId;
  state.call.lastControlStatus = "pending";
  sendStatus();
  try {
    if (action === "accept") {
      if (state.call.phase !== "ringing") {
        respond({
          status: state.call.phase === "idle" || state.call.phase === "ended"
            ? "no_active_call"
            : "invalid_state",
          supported: true,
          message: `cannot accept during ${state.call.phase}`,
        });
        return;
      }
      clearAcceptTimer();
      const result = await acceptActiveInvite();
      const submitted = result?.posted === true || result?.reason === "already_submitted";
      state.call.lastControlStatus = submitted ? "submitted" : "failed";
      sendStatus();
      respond({
        status: submitted ? "submitted" : "failed",
        supported: true,
        message: submitted ? "accept command submitted" : String(result?.reason || "accept failed"),
      });
      return;
    }

    // QQ/AVSDK 的拒接、挂断、静音原生方法和参数仍需按实际版本验证。
    // 先把协议和 AI 工具打通，禁止误调用 quit 等可能退出 Host 的方法。
    state.call.lastControlStatus = "unsupported";
    sendStatus();
    respond({
      status: "unsupported",
      supported: false,
      message: `${action} 的 QQ AVSDK 原生方法尚未在当前版本验证`,
    });
  } catch (error) {
    state.call.lastControlStatus = "failed";
    sendStatus();
    respond({
      status: "failed",
      supported: action === "accept",
      message: String(error?.message ?? error).slice(0, 300),
    });
  }
}

function mapValue(value, key) {
  if (!value) return null;
  if (typeof value.get === "function") return value.get(key) ?? null;
  return typeof value === "object" ? value[key] ?? null : null;
}

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

async function resolveCallerIdentity(uid, inviteAt) {
  let uin = null;
  let profile = null;
  try {
    const result = await context?.core?.context?.session?.getUixConvertService?.().getUin([uid]);
    const resolved = mapValue(result?.uinInfo, uid);
    if (resolved !== null && String(resolved) !== "0") uin = String(resolved);
  } catch {
    // 继续尝试 Profile 服务，身份解析失败不能中断普通来电状态机。
  }
  try {
    const profiles = await context?.core?.eventWrapper?.callNoListenerEvent?.(
      "NodeIKernelProfileService/getCoreAndBaseInfo",
      "nodeStore",
      [uid],
    );
    profile = mapValue(profiles, uid);
    const profileUin = profile?.uin ?? profile?.coreInfo?.uin;
    if (!uin && profileUin !== null && profileUin !== undefined && String(profileUin) !== "0") {
      uin = String(profileUin);
    }
  } catch {
    // 名称只用于状态展示，不影响未配置名单时的自动接听。
  }
  if (state.call.inviteAt !== inviteAt || state.call.callerUid !== uid) return;
  state.call.callerUin = uin;
  state.call.callerName = firstString(
    profile?.remark,
    profile?.displayName,
    profile?.nick,
    profile?.nickname,
    profile?.coreInfo?.remark,
    profile?.coreInfo?.nick,
  );
  sendStatus();
}

function createListener() {
  const target = {};
  for (const name of [
    "onS2CActionToAVSDK",
    "onActionToAVSDK",
    "onAVSDKData",
    "onAVSdkCrash",
    "onReceiveInvite",
    "onInviteActionToAVSDK",
  ]) {
    target[name] = (...args) => recordKernelEvent(name, args);
  }
  return new Proxy(target, {
    get(object, property, receiver) {
      if (Reflect.has(object, property)) return Reflect.get(object, property, receiver);
      if (typeof property === "string") {
        const callback = (...args) => recordKernelEvent(property, args);
        Reflect.set(object, property, callback);
        return callback;
      }
      return Reflect.get(object, property, receiver);
    },
  });
}

function summarizeKernelArg(value) {
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  if (Array.isArray(value)) return `array(${value.length})`;
  if (typeof value === "string") return `string(${value.length})`;
  if (typeof value === "number" || typeof value === "boolean") return typeof value;
  if (typeof value === "object") {
    return `object{${Object.keys(value).slice(0, 12).join(",")}}`;
  }
  return typeof value;
}

function recordKernelEvent(name, args) {
  recordDiagnosticCaptureEvent("kernel_event", { name, args });
  const lowerName = name.toLowerCase();
  state.avsdk.kernelEventCount += 1;
  state.avsdk.lastKernelEvent = {
    name,
    at: new Date().toISOString(),
    argCount: args.length,
    argSummary: args.slice(0, 8).map(summarizeKernelArg),
  };
  if (lowerName === "oninviteactiontoavsdk" || lowerName === "onactiontoavsdk") {
    logger?.info(
      `[QQVoiceCall] kernel event: ${name} ` +
      `args=${args.length} [${state.avsdk.lastKernelEvent.argSummary.join(", ")}]`,
    );
  }
  if (lowerName === "oninviteactiontoavsdk") {
    activeInvite = null;
    state.call = {
      ...idleCall(),
      phase: "ringing",
      inviteAt: new Date().toISOString(),
      inviteReceivedAt: new Date().toISOString(),
    };
    sendStatus();
  } else if (lowerName === "ons2cactiontoavsdk" && typeof args[0]?.destroyReason === "number") {
    clearAcceptTimer();
    activeInvite = null;
    state.call.phase = "ended";
    state.call.endedAt = new Date().toISOString();
    state.call.endReason = args[0].destroyReason;
    sendStatus();
    recordDiagnosticCaptureEvent("call_state_terminal", {
      eventName: name,
      phase: state.call.phase,
      endReason: state.call.endReason,
    });
    scheduleDiagnosticTerminalFinalize("call_terminated");
  }

  let actionType = null;
  let payload = null;
  if (lowerName === "oninviteactiontoavsdk") {
    actionType = args[1];
    payload = args[2];
  } else if (lowerName === "onactiontoavsdk") {
    actionType = args[0];
    payload = args[1];
  }
  if (typeof actionType === "number" && typeof payload === "string") {
    logger?.info(
      `[QQVoiceCall] kernel action forwarding: event=${name} ` +
      `actionType=${actionType} payloadLength=${payload.length}`,
    );
    const forward = invokeAVSDK(55, [actionType, payload]);
    forward
      .then(() => {
        state.avsdk.kernelActionForwardCount += 1;
        state.avsdk.kernelActionForwardError = null;
        sendStatus();
        logger?.info(`[QQVoiceCall] kernel action forwarded: event=${name}`);
        // 旧 OneBot 仍使用计时接听时保留 75ms 兼容补偿；新版准备完成
        // 驱动模式必须继续保持振铃，等待 OneBot 主动提交 accept。
        if (
          lowerName === "onactiontoavsdk" &&
          activeInvite &&
          state.call.phase === "ringing" &&
          !runtimeConfig.acceptWhenReady
        ) {
          scheduleAccept(75);
        }
      })
      .catch((error) => {
        state.avsdk.kernelActionForwardError = String(error?.message ?? error).slice(0, 300);
        sendStatus();
        setError(`kernel action forward failed: ${error?.message ?? error}`);
      });
  } else if (lowerName === "oninviteactiontoavsdk" || lowerName === "onactiontoavsdk") {
    logger?.warn(
      `[QQVoiceCall] kernel action skipped: event=${name} ` +
      `actionType=${summarizeKernelArg(actionType)} payload=${summarizeKernelArg(payload)}`,
    );
  }
}

function clearAcceptTimer() {
  if (acceptTimer) clearTimeout(acceptTimer);
  acceptTimer = null;
}

function scheduleAccept(delayMs = runtimeConfig.acceptDelayMs) {
  clearAcceptTimer();
  logger?.info(`[QQVoiceCall] legacy accept timer scheduled: ${delayMs}ms`);
  acceptTimer = setTimeout(() => {
    acceptTimer = null;
    acceptActiveInvite().catch(setError);
  }, delayMs);
  acceptTimer.unref?.();
}

async function acceptActiveInvite() {
  if (!state.active || !activeInvite || state.call.phase === "ended") {
    return { posted: false, reason: "no_active_invite" };
  }
  if (state.call.phase === "accepting" || state.call.phase === "accepted" || state.call.phase === "connected") {
    return { posted: false, reason: "already_submitted" };
  }
  state.call.phase = "accepting";
  state.call.blockedReason = null;
  state.call.acceptPostState = "pending";
  sendStatus();
  try {
    const result = await invokeAVSDK(5, buildAcceptParams(activeInvite));
    state.call.acceptCommandPostedAt = new Date().toISOString();
    state.call.acceptPostState = result?.posted === true ? "posted" : "unknown";
    logger?.info(
      `[QQVoiceCall] accept command posted: ` +
      `posted=${String(result?.posted === true)}`,
    );
    sendStatus();
    return {
      posted: result?.posted === true,
      command: 5,
      reason: result?.posted === true ? "submitted" : "unknown",
    };
  } catch (error) {
    state.call.phase = "ringing";
    state.call.acceptPostState = "failed";
    sendStatus();
    throw new Error(`native accept failed: ${error?.message ?? error}`);
  }
}

function normalizeAVSDKOutput(value) {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

async function handleAVSDKOutput(_event, incoming) {
  recordDiagnosticCaptureEvent("avsdk_output", { incoming });
  if (!state.active || !incoming || typeof incoming !== "object") return;
  const command = Number(incoming.cmd ?? incoming.command);
  const rawValue = incoming.value ?? incoming.param ?? incoming.data ?? incoming.payload;
  const value = normalizeAVSDKOutput(rawValue);
  if (!Number.isInteger(command)) {
    logger?.warn(
      `[QQVoiceCall] AVSDK output skipped: invalid cmd ` +
      `keys=${Object.keys(incoming).slice(0, 12).join(",")}`,
    );
    return;
  }
  state.avsdk.lastOutputCommand = command;
  state.avsdk.outputCount += 1;
  const commandKey = String(command);
  state.avsdk.commandCounts[commandKey] = (state.avsdk.commandCounts[commandKey] ?? 0) + 1;
  logger?.info(
    `[QQVoiceCall] AVSDK output received: cmd=${command} ` +
    `value=${summarizeKernelArg(rawValue)}`,
  );

  if (command === 20001) {
    if (!Array.isArray(value) || typeof value[0] !== "number" || typeof value[1] !== "string") {
      throw new Error("invalid AVSDK network output");
    }
    if (!avsdkService || typeof avsdkService.setActionFromAVSDK !== "function") {
      throw new Error("setActionFromAVSDK is unavailable");
    }
    await avsdkService.setActionFromAVSDK(value[0], value[1]);
  } else if (command === 20006 && Array.isArray(value)) {
    activeInvite = value;
    const callerUid = typeof value[1] === "string" ? value[1] : null;
    state.call.callerUid = callerUid;
    state.call.inviteReceivedAt = new Date().toISOString();
    logger?.info(
      `[QQVoiceCall] invite received: ` +
      `items=${value.length}, callerUid=${callerUid ? "present" : "missing"}`,
    );

    const decideAndSchedule = () => {
      const decision = shouldAcceptCaller(state.call.callerUin, runtimeConfig);
      if (decision.accept && runtimeConfig.admissionByOneBot) {
        // 只完成本地静态名单判断；真正是否接听交给 OneBot 的当前私聊模型。
        // 不能在这里提交 AVSDK accept，否则模型和开场白仍在准备时就会接通。
        state.call.acceptDecision = "pending";
        state.call.acceptDecisionReason = "onebot_ai";
        state.call.blockedReason = null;
        clearAcceptTimer();
        logger?.info("[QQVoiceCall] waiting for OneBot AI admission decision");
      } else if (decision.accept) {
        state.call.acceptDecision = "accept";
        state.call.acceptDecisionReason = decision.reason;
        state.call.blockedReason = null;
        if (runtimeConfig.acceptWhenReady) {
          clearAcceptTimer();
          logger?.info("[QQVoiceCall] waiting for OneBot call preparation before accept");
        } else {
          scheduleAccept();
        }
      } else {
        state.call.acceptDecision = "block";
        state.call.acceptDecisionReason = decision.reason;
        state.call.blockedReason = decision.reason;
      }
      logger?.info(
        `[QQVoiceCall] accept decision: ${state.call.acceptDecision} ` +
        `reason=${state.call.acceptDecisionReason}`,
      );
      sendStatus();
    };

    // 名单为空时不需要等待身份查询；查询仅用于补齐状态展示。
    // 否则一次缓慢的 QQ Profile 查询会耗尽来电的十几秒振铃窗口。
    const requiresIdentity = runtimeConfig.admissionByOneBot
      || runtimeConfig.allowUsers.length || runtimeConfig.denyUsers.length;
    if (callerUid) {
      const identityTask = resolveCallerIdentity(callerUid, state.call.inviteAt);
      if (requiresIdentity) {
        await identityTask;
        decideAndSchedule();
      } else {
        decideAndSchedule();
        void identityTask;
      }
    } else {
      decideAndSchedule();
    }
  } else if (command === 20006) {
    logger?.warn(
      `[QQVoiceCall] invite output has invalid payload; auto-accept skipped ` +
      `value=${summarizeKernelArg(rawValue)}`,
    );
  } else if (command === 5) {
    const resultCode = Array.isArray(value) && Number.isFinite(Number(value[0]))
      ? Number(value[0])
      : null;
    state.call.acceptResultCode = resultCode;
    state.call.phase = resultCode === 0 ? "accepted" : "ringing";
    logger?.info(`[QQVoiceCall] accept result: code=${String(resultCode)}`);
  } else if (command === 20004) {
    const resultCode = Array.isArray(value) && Number.isFinite(Number(value[0]))
      ? Number(value[0])
      : null;
    state.call.enterRoomResultCode = resultCode;
    if (resultCode === 0) {
      state.call.phase = "connected";
      state.call.connectedAt = new Date().toISOString();
    } else {
      state.call.phase = "ringing";
    }
    logger?.info(`[QQVoiceCall] enter-room result: code=${String(resultCode)}`);
  } else if ((command === 20050 || command === 120043) && state.avsdk.loginPosted) {
    scheduleLogin(100);
  }
  state.lastError = null;
  sendStatus();
}

async function createAVSDKHost() {
  hostSettings = normalizeHostSettings();
  if (!fs.existsSync(hostSettings.qqExecutable)) {
    throw new Error(`QQ 可执行文件不存在：${hostSettings.qqExecutable}`);
  }
  if (!fs.existsSync(hostSettings.avsdkPath)) {
    throw new Error(`QQ AVSDK 文件不存在：${hostSettings.avsdkPath}`);
  }
  let loaderText = "";
  try {
    loaderText = fs.readFileSync(hostSettings.loaderPath, "utf8");
  } catch {
    throw new Error(
      `NapCat Linux Loader 不存在或不可读：${hostSettings.loaderPath}；请确认 QQ 从 NapCat Shell 启动`,
    );
  }
  const usesNapcatLauncher = loaderText.includes("NAPCAT_BOOTMAIN");
  const usesLegacyLoaderHook = loaderText.includes("QQ_VOICE_CALL_LOADER_HOOK_V1");
  if (!usesNapcatLauncher && !usesLegacyLoaderHook) {
    throw new Error(
      `当前 Loader 不支持 NapCat Host 启动：${hostSettings.loaderPath}；请更新 NapCat launcher 或重新部署插件`,
    );
  }
  logger?.info(
    `[QQVoiceCall] AVSDK Host loader=${hostSettings.loaderPath} ` +
      `mode=${usesNapcatLauncher ? "napcat-launcher" : "physical-hook"}`,
  );
  const hostEntry = path.join(context.pluginPath, "host.cjs");
  if (!fs.existsSync(hostEntry)) throw new Error(`AVSDK Host 文件不存在：${hostEntry}`);
  const hostBootstrap = path.join(context.pluginPath, "host-bootstrap");
  const hostBootstrapEntry = path.join(hostBootstrap, "napcat", "napcat.mjs");
  if (!fs.existsSync(hostBootstrapEntry)) {
    throw new Error(`AVSDK Host 引导文件不存在：${hostBootstrapEntry}`);
  }

  await startHostControlServer();
  const env = {
    ...process.env,
    QQ_VOICE_CALL_AV_HOST: "1",
    QQ_VOICE_CALL_AV_HOST_ENTRY: hostEntry,
    QQ_VOICE_CALL_AVSDK_PATH: hostSettings.avsdkPath,
    QQ_VOICE_CALL_AV_HOST_HOST: hostSettings.controlHost,
    QQ_VOICE_CALL_AV_HOST_PORT: String(hostSettings.hostPort),
    QQ_VOICE_CALL_BRIDGE_HOST: hostSettings.controlHost,
    QQ_VOICE_CALL_BRIDGE_PORT: String(hostSettings.controlPort),
    QQ_VOICE_CALL_BRIDGE_TOKEN: hostToken,
    QQ_VOICE_CALL_QQ_DIR: path.dirname(hostSettings.qqExecutable),
    QQ_VOICE_CALL_USER_DATA_DIR: hostSettings.userDataDir,
    NAPCAT_BOOTMAIN: hostBootstrap,
  };
  // NapCat 启动器可能把该变量带入子进程；Host 必须以真正的 Electron 进程运行。
  delete env.ELECTRON_RUN_AS_NODE;
  if (usesNapcatLauncher && process.platform === "linux" && !String(env.LD_PRELOAD ?? "").trim()) {
    throw new Error(
      "NapCat Linux launcher 的 LD_PRELOAD 未传入 AVSDK Host；请从 NapCat Shell 启动 QQ",
    );
  }
  // 同时把 PPAPI 开关放进 QQ 子进程参数，确保 launcher 创建的 Renderer 也继承注册配置。
  const args = [
    "--no-sandbox",
    "--allow-command-line-plugins",
    `--register-pepper-plugins=${hostSettings.avsdkPath};application/x-ppapi-avSDK`,
  ];
  if (hostSettings.userDataDir) args.push(`--user-data-dir=${hostSettings.userDataDir}`);
  hostProcess = spawn(hostSettings.qqExecutable, args, {
    cwd: path.dirname(hostSettings.loaderPath),
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  hostProcess.stdout?.on("data", (chunk) => {
    logger?.info(`[QQVoiceCall][AVSDK Host] ${String(chunk).trim().slice(0, 500)}`);
  });
  hostProcess.stderr?.on("data", (chunk) => {
    logger?.warn(`[QQVoiceCall][AVSDK Host] ${String(chunk).trim().slice(0, 500)}`);
  });
  hostProcess.on("error", (error) => {
    state.avsdk.hostReady = false;
    setError(`AVSDK Host 启动失败: ${error?.message ?? error}`);
  });
  hostProcess.on("exit", (code, signal) => {
    state.avsdk.hostReady = false;
    if (state.active) {
      setError(`AVSDK Host 已退出: code=${code ?? "null"}, signal=${signal ?? "null"}`);
      void cleanupAVSDKResources("host_exit").then(sendStatus).catch(setError);
    }
  });

  const deadline = Date.now() + 15000;
  while (!state.avsdk.hostReady && Date.now() < deadline) {
    try {
      const response = await requestHost("/v1/status");
      const incoming = response?.data ?? response;
      state.avsdk.hostReady = Boolean(incoming?.ready);
      state.avsdk.pluginFound = Boolean(incoming?.pluginFound);
      state.avsdk.hostDiagnostics = incoming?.hostDiagnostics ?? null;
      state.avsdk.rendererDiagnostics = incoming?.diagnostics ?? null;
      state.avsdk.hostMessageCount = Number(incoming?.messageCount ?? 0) || 0;
      state.avsdk.hostForwardedCount = Number(incoming?.forwardedCount ?? 0) || 0;
      state.avsdk.hostMissingPayloadCount = Number(incoming?.missingPayloadCount ?? 0) || 0;
      state.avsdk.hostLastMessageShape = incoming?.lastMessageShape ?? null;
      state.avsdk.hostForwardError = incoming?.forwardError ?? null;
      if (state.avsdk.hostReady) {
        const diagnosticText = JSON.stringify({
          pluginFound: state.avsdk.pluginFound,
          hostDiagnostics: state.avsdk.hostDiagnostics,
          rendererDiagnostics: state.avsdk.rendererDiagnostics,
          error: incoming?.error ?? null,
        });
        logger?.info(
          `[QQVoiceCall][AVSDK Host] status ${diagnosticText.slice(0, 4000)}`,
        );
        sendStatus();
        break;
      }
    } catch {
      // Host 尚未完成 Electron 初始化，继续短暂轮询。
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (!state.avsdk.hostReady) {
    throw new Error(
      `AVSDK Host 未就绪；请确认 NapCat Shell Loader 存在且支持 NAPCAT_BOOTMAIN：${hostSettings.loaderPath}`,
    );
  }
  if (!state.avsdk.pluginFound) {
    throw new Error(
      `QQ AVSDK PPAPI 插件未加载；请检查 ${hostSettings.avsdkPath} 和 NapCat Shell Loader 启动链`,
    );
  }
}

async function invokeAVSDK(command, params) {
  if (!hostProcess || hostProcess.exitCode !== null) {
    recordDiagnosticCaptureEvent("avsdk_invoke", {
      stage: "request",
      command,
      invocationId: null,
      params,
    });
    recordDiagnosticCaptureEvent("avsdk_invoke", {
      stage: "error",
      command,
      invocationId: null,
      error: new Error("AVSDK Host 未运行"),
    });
    throw new Error("AVSDK Host 未运行");
  }
  const invocationId = nextInvocationId++;
  recordDiagnosticCaptureEvent("avsdk_invoke", {
    stage: "request",
    command,
    invocationId,
    params,
  });
  try {
    const response = await requestHost("/v1/invoke", "POST", {
      command,
      id: invocationId,
      params,
    });
    const result = response?.data ?? response;
    recordDiagnosticCaptureEvent("avsdk_invoke", {
      stage: "result",
      command,
      invocationId,
      result,
    });
    return result;
  } catch (error) {
    recordDiagnosticCaptureEvent("avsdk_invoke", {
      stage: "error",
      command,
      invocationId,
      error,
    });
    throw error;
  }
}

function scheduleLogin(delayMs = 500) {
  if (loginTimer) clearTimeout(loginTimer);
  loginTimer = setTimeout(() => {
    loginTimer = null;
    postLogin().catch((error) => {
      setError(`AVSDK login failed: ${error?.message ?? error}`);
      if (state.active) scheduleLogin(Math.min(delayMs * 2, 5000));
    });
  }, delayMs);
  loginTimer.unref?.();
}

async function postLogin() {
  if (!state.active) return;
  const session = context.core?.context?.session;
  const selfUid = String(context.core?.selfInfo?.uid ?? "");
  const selfUin = String(context.core?.selfInfo?.uin ?? "");
  const accountPath = String(
    session?.getAccountPath?.(Number.parseInt(selfUin, 10)) || context.core?.dataPath || "",
  );
  if (!selfUid || !selfUin || !accountPath) throw new Error("QQ identity/account path is unavailable");
  await invokeAVSDK(1, [selfUid, selfUin, selfUin, accountPath, ""]);
  state.avsdk.loginPosted = true;
  sendStatus();
}

async function activateRuntime(incomingConfig) {
  runtimeConfig = normalizeRuntimeConfig(incomingConfig);
  if (state.active) {
    sendStatus();
    return;
  }
  if (activationPromise) return activationPromise;
  activationPromise = (async () => {
    state.lastError = null;
    state.call = idleCall();
    state.avsdk = idleAVSDK();
    visibleSurfaceDiagnostic = null;
    serviceSurfaceDiagnostic = null;
    // 静态文件扫描按 NapCat 插件生命周期限一次，而非按短暂 Host 生命周期重置。
    state.avsdk.staticArtifactsDiagnostic = staticArtifactsDiagnostic;
    avsdkService = context.core?.context?.session?.getAVSDKService?.() ?? null;
    state.avsdk.serviceAvailable = Boolean(avsdkService);
    if (!avsdkService) throw new Error("NapCat getAVSDKService() returned no service");

    await createAVSDKHost();
    listener = createListener();
    listenerId = avsdkService.addKernelAVSDKListener(listener);
    state.avsdk.listenerRegistered = true;
    state.active = true;
    scheduleLogin();
    sendStatus();
    logger?.info("[QQVoiceCall] AVSDK runtime activated by OneBot");
  })();
  try {
    await activationPromise;
  } catch (error) {
    state.call.phase = "error";
    setError(`AVSDK activation failed: ${error?.message ?? error}`);
    await cleanupAVSDKResources("activation_failed");
  } finally {
    activationPromise = null;
  }
}

async function cleanupAVSDKResources(reason) {
  if (diagnosticCapture) {
    recordDiagnosticCaptureEvent("call_state_terminal", {
      phase: state.call.phase,
      reason,
      source: "runtime_cleanup",
    });
    const capture = diagnosticCapture;
    await finalizeDiagnosticCapture(
      capture.terminalDetected ? "completed" : "cancelled",
      capture.terminalDetected ? capture.terminalReason : reason,
    );
  } else if (diagnosticFinalizePromise) {
    await diagnosticFinalizePromise;
  }
  state.active = false;
  clearAcceptTimer();
  if (loginTimer) clearTimeout(loginTimer);
  loginTimer = null;
  activeInvite = null;

  if (avsdkService && listenerId !== null && typeof avsdkService.removeKernelAVSDKListener === "function") {
    try {
      avsdkService.removeKernelAVSDKListener(listenerId);
    } catch (error) {
      logger?.warn(`[QQVoiceCall] listener cleanup failed: ${error?.message ?? error}`);
    }
  }
  listener = null;
  listenerId = null;
  avsdkService = null;

  await stopHostProcess();
  if (hostControlServer) {
    const server = hostControlServer;
    hostControlServer = null;
    await new Promise((resolve) => server.close(resolve));
  }
  hostSettings = null;
  hostToken = null;
  visibleSurfaceDiagnostic = null;
  serviceSurfaceDiagnostic = null;
  state.avsdk = idleAVSDK();
  state.avsdk.staticArtifactsDiagnostic = staticArtifactsDiagnostic;
  if (!new Set(["idle", "ended", "error"]).has(state.call.phase)) {
    state.call.phase = "ended";
    state.call.endedAt = new Date().toISOString();
    state.call.endReason = reason;
  }
}

async function deactivateRuntime(reason = "deactivated") {
  if (activationPromise) {
    try {
      await activationPromise;
    } catch {
      // 激活失败也继续执行幂等清理。
    }
  }
  await cleanupAVSDKResources(reason);
  sendStatus();
}

export const plugin_init = async (ctx) => {
  context = ctx;
  logger = ctx.logger;
  shuttingDown = false;
  loadPluginConfig(ctx);
  connectBridge();
  logger.info("[QQVoiceCall] plugin loaded; AVSDK remains idle until OneBot activates it");
};

export const plugin_cleanup = async () => {
  shuttingDown = true;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (bridgeSocket) {
    const socket = bridgeSocket;
    bridgeSocket = null;
    socket.removeAllListeners();
    socket.close();
  }
  await deactivateRuntime("napcat_plugin_unloaded");
  context = null;
  logger = null;
};

export const plugin_get_config = async () => ({ ...pluginConfig, token: "" });

export const plugin_set_config = async (ctx, incoming) => {
  pluginConfig = {
    ...pluginConfig,
    serverUrl: String(incoming?.serverUrl ?? "").trim(),
    token: String(incoming?.token ?? "").trim() || pluginConfig.token,
    voicePath: String(incoming?.voicePath ?? "/qq-voice-call").trim(),
    reconnectIntervalMs: Number(incoming?.reconnectIntervalMs ?? 5000),
  };
  persistPluginConfig(ctx);
  if (bridgeSocket) {
    const socket = bridgeSocket;
    bridgeSocket = null;
    socket.removeAllListeners();
    socket.close();
  }
  bridgeSettings = null;
  connectBridge();
};

export const plugin_config_ui = [];
