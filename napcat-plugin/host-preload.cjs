"use strict";

const { ipcRenderer } = require("electron");
const { collectVisibleSurface } = require("./host-capabilities.cjs");

const STATE_CHANNEL = "qq-voice-call:host-state";
const OUTPUT_CHANNEL = "qq-voice-call:avsdk-output";
const HOST_BUILD_MARKER = "2026-08-15-avsdk-main-world-3";
let avsdkPlugin = null;

function safeRead(read, fallback = null) {
  try {
    const value = read();
    return value == null ? fallback : value;
  } catch {
    return fallback;
  }
}

function safeString(read) {
  const value = safeRead(read);
  return value == null ? null : String(value);
}

function describeEmbed(plugin) {
  if (!plugin) return null;
  return {
    tagName: safeString(() => plugin.tagName),
    typeAttribute: safeString(() => plugin.getAttribute?.("type")),
    typeProperty: safeString(() => plugin.type),
    constructorName: safeString(() => plugin.constructor?.name),
    postMessageType: safeString(() => typeof plugin.postMessage),
    readyState: safeString(() => plugin.readyState),
    width: safeString(() => plugin.width),
    height: safeString(() => plugin.height),
  };
}

function collectNavigatorPlugins() {
  return safeRead(
    () =>
      Array.from(navigator.plugins || [])
        .slice(0, 64)
        .map((plugin) => ({
          name: safeString(() => plugin.name),
          description: safeString(() => plugin.description),
          filename: safeString(() => plugin.filename),
          length: Number.isInteger(plugin.length) ? plugin.length : null,
        })),
    [],
  );
}

function collectNavigatorMimeTypes() {
  return safeRead(
    () =>
      Array.from(navigator.mimeTypes || [])
        .slice(0, 64)
        .map((mime) => ({
          type: safeString(() => mime.type),
          description: safeString(() => mime.description),
          suffixes: safeString(() => mime.suffixes),
          enabledPlugin: safeString(() => mime.enabledPlugin?.name),
        })),
    [],
  );
}

function collectRendererDiagnostics(plugin, errors = []) {
  return {
    buildMarker: HOST_BUILD_MARKER,
    readyState: safeString(() => document.readyState),
    protocol: safeString(() => window.location.protocol),
    contextIsolated: safeRead(() => Boolean(process?.contextIsolated)),
    nodeRequireAvailable: safeRead(() => typeof require === "function"),
    electronVersion: safeString(() => process?.versions?.electron),
    rendererArgv: safeRead(
      () => (Array.isArray(process?.argv) ? process.argv.slice(0, 64) : []),
      [],
    ),
    embed: describeEmbed(plugin),
    navigatorPlugins: collectNavigatorPlugins(),
    navigatorMimeTypes: collectNavigatorMimeTypes(),
    errors: errors.slice(-8),
  };
}

window.qqVoiceCall = {
  /**
   * 向 QQ AVSDK 发送原生命令。
   * @param {number} command AVSDK 命令号。
   * @param {number} id 本次调用的递增标识。
   * @param {unknown[]} params 原生命令参数。
   * @returns {{posted: boolean, command: number, id: number}}
   */
  invoke(command, id, params) {
    if (!avsdkPlugin || typeof avsdkPlugin.postMessage !== "function") {
      throw new Error("AVSDK postMessage is unavailable");
    }
    if (!Number.isInteger(command) || !Number.isInteger(id) || !Array.isArray(params)) {
      throw new Error("invalid AVSDK invocation");
    }
    avsdkPlugin.postMessage({ cmd: command, id, param: params });
    return { posted: true, command, id };
  },

  /**
   * 只读检查当前 AVSDK 对象的可见方法表面。
   *
   * @returns {object} 不含原生对象、payload 或函数返回值的受限元数据报告。
   */
  inspectVisibleSurface() {
    // 以普通 embed 作为 DOM 基线，避免把 click/focus 等通用方法误报为 AVSDK 能力。
    const baseline = safeRead(() => document.createElement("embed"), null);
    return collectVisibleSurface(avsdkPlugin, { baseline });
  },
};

window.addEventListener("load", () => {
  avsdkPlugin = document.getElementById("qq-avsdk");
  const rendererErrors = [];
  avsdkPlugin?.addEventListener("message", (event) => {
    ipcRenderer.send(OUTPUT_CHANNEL, event.data);
  });
  const publishState = () => {
    ipcRenderer.send(STATE_CHANNEL, {
      ready: true,
      pluginFound: Boolean(avsdkPlugin && typeof avsdkPlugin.postMessage === "function"),
      diagnostics: collectRendererDiagnostics(avsdkPlugin, rendererErrors),
    });
  };
  window.addEventListener("error", (event) => {
    rendererErrors.push(
      String(event?.message || "renderer error").slice(0, 300),
    );
    publishState();
  });
  window.addEventListener("unhandledrejection", (event) => {
    rendererErrors.push(
      String(event?.reason?.message || event?.reason || "unhandled rejection").slice(0, 300),
    );
    publishState();
  });
  setTimeout(() => {
    publishState();
  }, 1200);
});
