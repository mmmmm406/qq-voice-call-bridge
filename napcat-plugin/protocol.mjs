// QQ AVSDK 的纯协议函数。此文件不依赖 Electron，便于用 Node 内置测试验证。

export const PROTOCOL_VERSION = 1;
export const DEFAULT_VOICE_PATH = "/qq-voice-call";
export const MANUAL_HANGUP_CAPTURE_CAPABILITY = "manual_hangup_capture_v1";
export const AVSDK_VISIBLE_SURFACE_DIAGNOSTIC_CAPABILITY = "avsdk_visible_surface_diagnostic_v1";
export const AVSDK_STATIC_ARTIFACTS_DIAGNOSTIC_CAPABILITY = "avsdk_static_artifacts_diagnostic_v1";
export const AVSDK_SERVICE_SURFACE_DIAGNOSTIC_CAPABILITY = "avsdk_service_surface_diagnostic_v1";
export const DIAGNOSTIC_CAPTURE_LIMITS = Object.freeze({
  minTimeoutMs: 30000,
  maxTimeoutMs: 45000,
  maxEvents: 512,
  maxBytes: 8 * 1024 * 1024,
  maxDepth: 10,
  maxItems: 512,
  maxStringBytes: 1024 * 1024,
  maxBinaryBytes: 1024 * 1024,
});

function integer(value, fallback, minimum, maximum, name) {
  const parsed = value === undefined || value === null || value === "" ? fallback : Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${name} must be an integer between ${minimum} and ${maximum}`);
  }
  return parsed;
}

function qqNumbers(value, name) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error(`${name} must be an array`);
  const result = [];
  for (const item of value) {
    const number = String(item ?? "").trim();
    if (!/^[1-9]\d{4,19}$/.test(number)) throw new Error(`${name} contains an invalid QQ number`);
    if (!result.includes(number)) result.push(number);
  }
  return result;
}

function captureIdentifier(value, name) {
  const normalized = String(value ?? "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(normalized)) {
    throw new Error(`invalid_${name}`);
  }
  return normalized;
}

/** 校验并规范化 OneBot 发来的一次性手动挂断捕获请求。 */
export function normalizeDiagnosticCaptureStart(value = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid_request");
  }
  if (Number(value.protocolVersion) !== PROTOCOL_VERSION) {
    throw new Error("unsupported_protocol_version");
  }
  if (value.kind !== "manual_hangup") throw new Error("unsupported_capture_kind");
  if (value.mode !== "raw") throw new Error("unsupported_capture_mode");
  return {
    protocolVersion: PROTOCOL_VERSION,
    requestId: captureIdentifier(value.requestId, "request_id"),
    captureId: captureIdentifier(value.captureId, "capture_id"),
    callId: captureIdentifier(value.callId, "call_id"),
    kind: "manual_hangup",
    mode: "raw",
    timeoutMs: integer(
      value.timeoutMs,
      DIAGNOSTIC_CAPTURE_LIMITS.maxTimeoutMs,
      DIAGNOSTIC_CAPTURE_LIMITS.minTimeoutMs,
      DIAGNOSTIC_CAPTURE_LIMITS.maxTimeoutMs,
      "timeoutMs",
    ),
  };
}

/** 校验并规范化一次性 AVSDK 可见方法表面诊断请求。 */
export function normalizeVisibleSurfaceDiagnosticStart(value = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid_request");
  }
  if (Number(value.protocolVersion) !== PROTOCOL_VERSION) {
    throw new Error("unsupported_protocol_version");
  }
  if (value.kind !== "avsdk_visible_surface") throw new Error("unsupported_diagnostic_kind");
  return {
    protocolVersion: PROTOCOL_VERSION,
    requestId: captureIdentifier(value.requestId, "request_id"),
    kind: "avsdk_visible_surface",
  };
}

/**
 * 校验一次性 AVSDK 静态资源诊断请求。
 *
 * 请求不接收路径、关键词、正则、读取上限或命令参数，避免 OneBot 桥成为
 * 虚拟机任意文件读取入口。实际扫描目标由 NapCat 插件内的固定常量决定。
 */
export function normalizeStaticArtifactsDiagnosticStart(value = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid_request");
  }
  if (Number(value.protocolVersion) !== PROTOCOL_VERSION) {
    throw new Error("unsupported_protocol_version");
  }
  if (value.kind !== "avsdk_static_artifacts") throw new Error("unsupported_diagnostic_kind");
  return {
    protocolVersion: PROTOCOL_VERSION,
    requestId: captureIdentifier(value.requestId, "request_id"),
    kind: "avsdk_static_artifacts",
  };
}

/**
 * 校验一次性 AVSDK Service 可见面诊断请求。
 *
 * 请求不接收方法名、路径、命令或参数，避免诊断链成为对 QQ 内部服务的
 * 任意调用入口。实际被观察的对象固定为当前激活运行时缓存的 AVSDK Service。
 */
export function normalizeAVSDKServiceSurfaceDiagnosticStart(value = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid_request");
  }
  if (Number(value.protocolVersion) !== PROTOCOL_VERSION) {
    throw new Error("unsupported_protocol_version");
  }
  if (value.kind !== "avsdk_service_surface") throw new Error("unsupported_diagnostic_kind");
  return {
    protocolVersion: PROTOCOL_VERSION,
    requestId: captureIdentifier(value.requestId, "request_id"),
    kind: "avsdk_service_surface",
  };
}

function truncateUtf8(value, maximumBytes) {
  const encoded = Buffer.from(value, "utf8");
  if (encoded.byteLength <= maximumBytes) {
    return { text: value, byteLength: encoded.byteLength, truncated: false };
  }
  return {
    text: encoded.subarray(0, maximumBytes).toString("utf8"),
    byteLength: encoded.byteLength,
    truncated: true,
  };
}

/**
 * 把 AVSDK/Kernel 的任意原始值转换成可写入 JSON 的有界结构。
 * Buffer 与 TypedArray 使用 base64；循环引用和越界内容使用显式占位符。
 */
export function serializeCaptureValue(value, options = {}) {
  const limits = {
    maxDepth: integer(options.maxDepth, DIAGNOSTIC_CAPTURE_LIMITS.maxDepth, 1, 32, "maxDepth"),
    maxItems: integer(options.maxItems, DIAGNOSTIC_CAPTURE_LIMITS.maxItems, 1, 4096, "maxItems"),
    maxStringBytes: integer(
      options.maxStringBytes,
      DIAGNOSTIC_CAPTURE_LIMITS.maxStringBytes,
      1,
      4 * 1024 * 1024,
      "maxStringBytes",
    ),
    maxBinaryBytes: integer(
      options.maxBinaryBytes,
      DIAGNOSTIC_CAPTURE_LIMITS.maxBinaryBytes,
      1,
      4 * 1024 * 1024,
      "maxBinaryBytes",
    ),
  };
  const seen = new WeakMap();

  const binaryValue = (input, type) => {
    const originalLength = input.byteLength;
    const bounded = input.subarray(0, limits.maxBinaryBytes);
    return {
      $type: type,
      encoding: "base64",
      byteLength: originalLength,
      truncated: originalLength > bounded.byteLength,
      data: bounded.toString("base64"),
    };
  };

  const encode = (input, currentPath, depth) => {
    if (input === null) return null;
    if (input === undefined) return { $type: "Undefined" };
    if (typeof input === "string") {
      const bounded = truncateUtf8(input, limits.maxStringBytes);
      return bounded.truncated
        ? {
            $type: "TruncatedString",
            byteLength: bounded.byteLength,
            data: bounded.text,
          }
        : input;
    }
    if (typeof input === "number") {
      return Number.isFinite(input) ? input : { $type: "Number", value: String(input) };
    }
    if (typeof input === "boolean") return input;
    if (typeof input === "bigint") return { $type: "BigInt", value: String(input) };
    if (typeof input === "symbol") return { $type: "Symbol", value: input.description ?? "" };
    if (typeof input === "function") return { $type: "Function", name: input.name || null };

    if (Buffer.isBuffer(input)) return binaryValue(input, "Buffer");
    if (input instanceof ArrayBuffer) {
      return binaryValue(Buffer.from(input), "ArrayBuffer");
    }
    if (ArrayBuffer.isView(input)) {
      return binaryValue(
        Buffer.from(input.buffer, input.byteOffset, input.byteLength),
        input.constructor?.name || "TypedArray",
      );
    }
    if (input instanceof Date) {
      return { $type: "Date", value: Number.isNaN(input.valueOf()) ? null : input.toISOString() };
    }
    if (input instanceof Error) {
      return {
        $type: input.name || "Error",
        message: encode(String(input.message ?? ""), `${currentPath}.message`, depth + 1),
        stack: encode(String(input.stack ?? ""), `${currentPath}.stack`, depth + 1),
      };
    }
    if (seen.has(input)) return { $type: "Circular", path: seen.get(input) };
    if (depth >= limits.maxDepth) {
      return { $type: "DepthLimit", tag: Object.prototype.toString.call(input) };
    }
    seen.set(input, currentPath);

    if (Array.isArray(input)) {
      const output = input
        .slice(0, limits.maxItems)
        .map((item, index) => encode(item, `${currentPath}[${index}]`, depth + 1));
      if (input.length > limits.maxItems) {
        output.push({ $type: "ItemsLimit", omitted: input.length - limits.maxItems });
      }
      return output;
    }
    if (input instanceof Map) {
      const entries = [];
      let index = 0;
      for (const [key, item] of input) {
        if (index >= limits.maxItems) break;
        entries.push([
          encode(key, `${currentPath}.mapKey[${index}]`, depth + 1),
          encode(item, `${currentPath}.mapValue[${index}]`, depth + 1),
        ]);
        index += 1;
      }
      return {
        $type: "Map",
        size: input.size,
        truncated: input.size > entries.length,
        entries,
      };
    }
    if (input instanceof Set) {
      const values = [];
      let index = 0;
      for (const item of input) {
        if (index >= limits.maxItems) break;
        values.push(encode(item, `${currentPath}.set[${index}]`, depth + 1));
        index += 1;
      }
      return {
        $type: "Set",
        size: input.size,
        truncated: input.size > values.length,
        values,
      };
    }

    const output = Object.create(null);
    let keys;
    try {
      keys = Object.keys(input);
    } catch (error) {
      return { $type: "UnreadableObject", error: String(error?.message ?? error).slice(0, 500) };
    }
    for (const [index, key] of keys.slice(0, limits.maxItems).entries()) {
      const boundedKey = truncateUtf8(key, 1024);
      const outputKey = boundedKey.truncated ? `${boundedKey.text}<truncated:${index}>` : key;
      try {
        const descriptor = Object.getOwnPropertyDescriptor(input, key);
        output[outputKey] = descriptor && Object.hasOwn(descriptor, "value")
          ? encode(descriptor.value, `${currentPath}.${key}`, depth + 1)
          : { $type: "Accessor" };
      } catch (error) {
        output[outputKey] = {
          $type: "UnreadableProperty",
          error: String(error?.message ?? error).slice(0, 500),
        };
      }
    }
    if (keys.length > limits.maxItems) {
      output.$itemsLimit = { omitted: keys.length - limits.maxItems };
    }
    return output;
  };

  return encode(value, "$", 0);
}

/**
 * 从 NapCat 已启用的反向 WebSocket 配置推导 QQ 通话桥地址和令牌。
 * 用户可在插件配置中显式覆盖，但默认不需要维护第二份连接信息。
 */
export function deriveBridgeSettings(pluginConfig = {}, oneBotConfig = {}) {
  const clients = oneBotConfig?.network?.websocketClients;
  const candidates = Array.isArray(clients)
    ? clients.filter((item) => item && item.enable !== false && typeof item.url === "string")
    : [];
  const preferred =
    candidates.find((item) => {
      try {
        return new URL(item.url).pathname === "/ws";
      } catch {
        return false;
      }
    }) ?? candidates[0] ?? null;

  const explicitUrl = String(pluginConfig.serverUrl ?? "").trim();
  const baseUrl = explicitUrl || String(preferred?.url ?? "").trim();
  if (!baseUrl) throw new Error("no enabled NapCat reverse WebSocket client was found");
  const url = new URL(baseUrl);
  if (!new Set(["ws:", "wss:"]).has(url.protocol)) {
    throw new Error("voice bridge URL must use ws:// or wss://");
  }
  const configuredPath = String(pluginConfig.voicePath ?? DEFAULT_VOICE_PATH).trim();
  url.pathname = configuredPath.startsWith("/") ? configuredPath : `/${configuredPath}`;
  url.search = "";
  url.hash = "";

  const token = String(pluginConfig.token ?? "").trim() || String(preferred?.token ?? "").trim();
  if (!token) throw new Error("voice bridge requires the OneBot reverse WebSocket token");
  return {
    url: url.toString(),
    token,
    reconnectIntervalMs: integer(
      pluginConfig.reconnectIntervalMs,
      5000,
      1000,
      60000,
      "reconnectIntervalMs",
    ),
  };
}

/** 规范化 OneBot 下发的私聊自动接听策略。 */
export function normalizeRuntimeConfig(value = {}) {
  return {
    autoAcceptPrivate: value.autoAcceptPrivate !== false,
    // 第三阶段由 OneBot 私聊模型决定是否接听；字段缺失时仍走旧版名单策略。
    admissionByOneBot: value.admissionByOneBot === true,
    // 新 OneBot 会在本机模型和音频准备完成后主动提交 accept。
    // 字段缺失时保留旧版固定计时行为，避免旧 OneBot 无法接听。
    acceptWhenReady: value.acceptWhenReady === true,
    acceptDelayMs: integer(value.acceptDelayMs, 500, 0, 10000, "acceptDelayMs"),
    allowUsers: qqNumbers(value.allowUsers, "allowUsers"),
    denyUsers: qqNumbers(value.denyUsers, "denyUsers"),
  };
}

/**
 * 把 AVSDK 20006 邀请转换为原生命令 5 的接听参数。
 * 该映射来自 maibot-qq-voice-call，并针对 QQ AVSDK 的真实回调签名保留。
 */
export function buildAcceptParams(invite) {
  if (!Array.isArray(invite) || invite.length < 12) {
    throw new Error("AVSDK invite callback is incomplete");
  }
  const params = [
    invite[0],
    invite[1],
    [invite[1]],
    invite[3],
    invite[4],
    Boolean(invite[10]),
    invite[11],
  ];
  const valid =
    typeof params[0] === "number" &&
    typeof params[1] === "string" &&
    params[2].every((item) => typeof item === "string") &&
    typeof params[3] === "number" &&
    typeof params[4] === "string" &&
    typeof params[5] === "boolean" &&
    typeof params[6] === "string";
  if (!valid) throw new Error("AVSDK invite does not match the native Accept signature");
  return params;
}

/** 根据 QQ 号白名单和拒绝名单判断是否自动接听。 */
export function shouldAcceptCaller(callerUin, runtimeConfig) {
  const config = normalizeRuntimeConfig(runtimeConfig);
  if (!config.autoAcceptPrivate) return { accept: false, reason: "auto_accept_disabled" };
  const number = String(callerUin ?? "").trim();
  if (!number && (config.allowUsers.length || config.denyUsers.length)) {
    return { accept: false, reason: "caller_identity_unresolved" };
  }
  if (config.denyUsers.includes(number)) return { accept: false, reason: "caller_denied" };
  if (config.allowUsers.length && !config.allowUsers.includes(number)) {
    return { accept: false, reason: "caller_not_allowed" };
  }
  return { accept: true, reason: "allowed" };
}
