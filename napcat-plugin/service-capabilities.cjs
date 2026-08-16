"use strict";

const SERVICE_SURFACE_REPORT_SCHEMA = "qq_voice_call.avsdk_service_surface.v1";
const CONTROL_METHOD_PATTERN = /(?:accept|answer|reject|decline|hang(?:up)?|leave|close|quit|end|disconnect|terminate|cancel|room|call|invite)/i;
const PUBLIC_METHOD_NAME = /^[A-Za-z_$][A-Za-z0-9_$]{0,127}$/;
const DEFAULT_LIMITS = Object.freeze({
  maxPrototypeDepth: 3,
  maxPropertiesPerLayer: 128,
  maxControlCandidates: 16,
  maxErrors: 8,
});

function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : fallback;
}

function resolveLimits(options = {}) {
  return {
    maxPrototypeDepth: boundedInteger(options.maxPrototypeDepth, DEFAULT_LIMITS.maxPrototypeDepth, 0, 8),
    maxPropertiesPerLayer: boundedInteger(
      options.maxPropertiesPerLayer,
      DEFAULT_LIMITS.maxPropertiesPerLayer,
      1,
      512,
    ),
    maxControlCandidates: boundedInteger(
      options.maxControlCandidates,
      DEFAULT_LIMITS.maxControlCandidates,
      1,
      32,
    ),
    maxErrors: boundedInteger(options.maxErrors, DEFAULT_LIMITS.maxErrors, 1, 16),
  };
}

function appendError(errors, limits, operation, depth) {
  if (errors.length < limits.maxErrors) errors.push({ operation, depth });
}

function safeOwnPropertyNames(value, errors, limits, depth) {
  try {
    return Object.getOwnPropertyNames(value);
  } catch {
    appendError(errors, limits, "own_property_names_failed", depth);
    return [];
  }
}

function safeDescriptor(value, name, errors, limits, depth) {
  try {
    return Object.getOwnPropertyDescriptor(value, name) || null;
  } catch {
    appendError(errors, limits, "property_descriptor_failed", depth);
    return null;
  }
}

function safePrototype(value, errors, limits, depth) {
  try {
    return Object.getPrototypeOf(value);
  } catch {
    appendError(errors, limits, "prototype_lookup_failed", depth);
    return null;
  }
}

function isControlCandidate(name) {
  return typeof name === "string" && PUBLIC_METHOD_NAME.test(name) && CONTROL_METHOD_PATTERN.test(name);
}

function candidateMetadata(name, descriptor, depth) {
  const accessor = !Object.hasOwn(descriptor, "value");
  return {
    name,
    kind: accessor ? "accessor" : "method",
    owner: depth === 0 ? "own" : "prototype",
    depth,
    enumerable: Boolean(descriptor.enumerable),
    configurable: Boolean(descriptor.configurable),
    writable: accessor ? null : Boolean(descriptor.writable),
    hasGetter: accessor ? typeof descriptor.get === "function" : false,
    hasSetter: accessor ? typeof descriptor.set === "function" : false,
  };
}

/**
 * 受限反射已由 NapCat 取得的 AVSDK Service。
 *
 * 只在显式诊断请求中调用。若 Service 是 Proxy，Object.* 反射仍可能触发其元操作
 * trap，因此所有异常均被限制为固定状态码；本函数不会读取属性值、调用方法、读取
 * 函数参数个数或序列化 Service/QQ 数据。
 *
 * @param {object | Function | null | undefined} service 当前激活运行时缓存的 AVSDK Service。
 * @param {object} [options] 仅供本地测试收紧反射上限。
 * @returns {object} 仅包含受限控制候选和层级元数据的可序列化报告。
 */
function collectAVSDKServiceSurface(service, options = {}) {
  const limits = resolveLimits(options);
  const reflectionErrors = [];
  const controlCandidates = [];
  const layers = [];
  let truncated = false;
  let prototypeDepthLimited = false;

  if (!service || (typeof service !== "object" && typeof service !== "function")) {
    return {
      schema: SERVICE_SURFACE_REPORT_SCHEMA,
      status: "unavailable",
      serviceAvailable: false,
      controlCandidates,
      layers,
      truncated: false,
      prototypeDepthLimited: false,
      reflectionErrors,
    };
  }

  let current = service;
  let depth = 0;
  while (current && depth <= limits.maxPrototypeDepth) {
    const names = safeOwnPropertyNames(current, reflectionErrors, limits, depth);
    const selectedNames = names.slice(0, limits.maxPropertiesPerLayer);
    const layerTruncated = names.length > selectedNames.length;
    truncated ||= layerTruncated;
    layers.push({
      depth,
      owner: depth === 0 ? "own" : "prototype",
      propertyCount: names.length,
      truncated: layerTruncated,
    });

    for (const name of selectedNames) {
      if (!isControlCandidate(name)) continue;
      const descriptor = safeDescriptor(current, name, reflectionErrors, limits, depth);
      if (!descriptor) continue;
      const isMethod = Object.hasOwn(descriptor, "value") && typeof descriptor.value === "function";
      const isAccessor = typeof descriptor.get === "function" || typeof descriptor.set === "function";
      if (!isMethod && !isAccessor) continue;
      if (controlCandidates.length >= limits.maxControlCandidates) {
        truncated = true;
        continue;
      }
      controlCandidates.push(candidateMetadata(name, descriptor, depth));
    }

    const next = safePrototype(current, reflectionErrors, limits, depth);
    if (!next || next === Object.prototype) {
      current = null;
      break;
    }
    current = next;
    depth += 1;
  }
  if (current) {
    prototypeDepthLimited = true;
    truncated = true;
  }

  return {
    schema: SERVICE_SURFACE_REPORT_SCHEMA,
    status: reflectionErrors.length ? "partial" : "complete",
    serviceAvailable: true,
    controlCandidates,
    layers,
    truncated,
    prototypeDepthLimited,
    reflectionErrors,
  };
}

function sanitizeCandidate(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || !isControlCandidate(value.name)) {
    return null;
  }
  const kind = value.kind === "accessor" ? "accessor" : value.kind === "method" ? "method" : null;
  if (!kind) return null;
  return {
    name: value.name,
    kind,
    owner: value.owner === "own" ? "own" : "prototype",
    depth: boundedInteger(value.depth, 0, 0, 8),
    enumerable: Boolean(value.enumerable),
    configurable: Boolean(value.configurable),
    writable: kind === "method" ? Boolean(value.writable) : null,
    hasGetter: kind === "accessor" && Boolean(value.hasGetter),
    hasSetter: kind === "accessor" && Boolean(value.hasSetter),
  };
}

/**
 * 在桥回传前再次白名单化报告，拒绝 Service、值、路径、参数和异常正文。
 *
 * @param {unknown} report 原始受限反射报告。
 * @returns {object} 可发送给 OneBot 的固定字段报告。
 */
function sanitizeAVSDKServiceSurfaceReport(report) {
  const source = report && typeof report === "object" && !Array.isArray(report) ? report : {};
  const controlCandidates = [];
  const names = new Set();
  for (const item of Array.isArray(source.controlCandidates) ? source.controlCandidates.slice(0, 32) : []) {
    const candidate = sanitizeCandidate(item);
    if (candidate && !names.has(candidate.name)) {
      names.add(candidate.name);
      controlCandidates.push(candidate);
    }
  }
  const layers = [];
  for (const item of Array.isArray(source.layers) ? source.layers.slice(0, 8) : []) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    layers.push({
      depth: boundedInteger(item.depth, 0, 0, 8),
      owner: item.owner === "own" ? "own" : "prototype",
      propertyCount: boundedInteger(item.propertyCount, 0, 0, 512),
      truncated: Boolean(item.truncated),
    });
  }
  const allowedOperations = new Set([
    "own_property_names_failed",
    "property_descriptor_failed",
    "prototype_lookup_failed",
  ]);
  const reflectionErrors = [];
  for (const item of Array.isArray(source.reflectionErrors) ? source.reflectionErrors.slice(0, 16) : []) {
    if (!item || typeof item !== "object" || !allowedOperations.has(item.operation)) continue;
    reflectionErrors.push({
      operation: item.operation,
      depth: boundedInteger(item.depth, 0, 0, 8),
    });
  }
  const status = ["complete", "partial", "unavailable"].includes(source.status)
    ? source.status
    : "partial";
  return {
    schema: SERVICE_SURFACE_REPORT_SCHEMA,
    status,
    serviceAvailable: Boolean(source.serviceAvailable),
    controlCandidates,
    layers,
    truncated: Boolean(source.truncated),
    prototypeDepthLimited: Boolean(source.prototypeDepthLimited),
    reflectionErrors,
  };
}

module.exports = {
  SERVICE_SURFACE_REPORT_SCHEMA,
  collectAVSDKServiceSurface,
  sanitizeAVSDKServiceSurfaceReport,
};
