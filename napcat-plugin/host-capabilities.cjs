"use strict";

const CAPABILITY_REPORT_SCHEMA = "qq_voice_call.avsdk_visible_surface.v1";
const CONTROL_METHOD_PATTERN = /(?:accept|answer|reject|decline|hang(?:up)?|leave|close|quit|end|disconnect|terminate|cancel|room|call|invite)/i;
const PUBLIC_METHOD_NAME = /^[A-Za-z_$][A-Za-z0-9_$]{0,127}$/;
const DEFAULT_LIMITS = Object.freeze({
  maxPrototypeDepth: 4,
  maxPropertiesPerLayer: 256,
  maxCallableMethods: 64,
  maxControlAccessors: 16,
  maxErrors: 8,
});

function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : fallback;
}

function resolveLimits(options = {}) {
  return {
    maxPrototypeDepth: boundedInteger(options.maxPrototypeDepth, DEFAULT_LIMITS.maxPrototypeDepth, 1, 12),
    maxPropertiesPerLayer: boundedInteger(
      options.maxPropertiesPerLayer,
      DEFAULT_LIMITS.maxPropertiesPerLayer,
      1,
      1024,
    ),
    maxCallableMethods: boundedInteger(
      options.maxCallableMethods,
      DEFAULT_LIMITS.maxCallableMethods,
      1,
      128,
    ),
    maxControlAccessors: boundedInteger(
      options.maxControlAccessors,
      DEFAULT_LIMITS.maxControlAccessors,
      1,
      64,
    ),
    maxErrors: boundedInteger(options.maxErrors, DEFAULT_LIMITS.maxErrors, 1, 32),
  };
}

function appendError(errors, limits, operation, depth) {
  if (errors.length >= limits.maxErrors) return;
  errors.push({ operation, depth });
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
    return Object.getOwnPropertyDescriptor(value, name) ?? null;
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

function visibleMethodName(value) {
  return typeof value === "string" && PUBLIC_METHOD_NAME.test(value) ? value : null;
}

function isControlCandidate(name) {
  return CONTROL_METHOD_PATTERN.test(name);
}

/**
 * 读取普通 embed 的固有属性名，用于排除 DOM 基线方法。
 *
 * 这里只读取属性描述符和属性名，不读取 accessor 返回值，也不会调用任何函数。
 *
 * @param {object | null | undefined} baseline 普通 `embed` 对象。
 * @param {ReturnType<typeof resolveLimits>} limits 有界扫描限制。
 * @returns {Set<string>} 普通 DOM 表面已有的属性名。
 */
function collectBaselineNames(baseline, limits) {
  const names = new Set();
  const errors = [];
  let current = baseline;
  for (let depth = 0; current && depth <= limits.maxPrototypeDepth; depth += 1) {
    for (const name of safeOwnPropertyNames(current, errors, limits, depth)) {
      if (typeof name === "string") names.add(name);
    }
    current = safePrototype(current, errors, limits, depth);
  }
  return names;
}

/**
 * 返回从基线对象向上移动指定层数后的原型对象。
 *
 * 此处只比较对象身份，不读取任何属性。若基线无法继续读取，返回 null，
 * 调用方会保守地把未扫描层视为可能包含额外可反射能力。
 *
 * @param {object | null | undefined} baseline 普通 `embed` 对象。
 * @param {number} depth 已扫描的原型层数。
 * @returns {object | null} 与待检查对象同一深度的基线原型。
 */
function baselinePrototypeAtDepth(baseline, depth) {
  let current = baseline;
  for (let index = 0; current && index < depth; index += 1) {
    try {
      current = Object.getPrototypeOf(current);
    } catch {
      return null;
    }
  }
  return current || null;
}

function methodMetadata(name, descriptor, depth) {
  const arity = Number.isInteger(descriptor.value?.length)
    ? Math.max(0, Math.min(64, descriptor.value.length))
    : null;
  return {
    name,
    owner: depth === 0 ? "own" : "prototype",
    depth,
    arity,
    enumerable: Boolean(descriptor.enumerable),
    configurable: Boolean(descriptor.configurable),
    writable: Boolean(descriptor.writable),
  };
}

function accessorMetadata(name, descriptor, depth) {
  return {
    name,
    owner: depth === 0 ? "own" : "prototype",
    depth,
    has_getter: typeof descriptor.get === "function",
    has_setter: typeof descriptor.set === "function",
  };
}

/**
 * 枚举 AVSDK `<embed>` 对象的可见方法表面。
 *
 * 本函数只使用反射 API 读取名称、属性描述符、函数 `length` 和原型链；不会读取
 * accessor 的值、调用候选函数、调用 `postMessage`，也不会序列化原生插件对象。
 *
 * @param {object | null | undefined} plugin AVSDK `<embed>` 对象。
 * @param {{baseline?: object | null, maxPrototypeDepth?: number, maxPropertiesPerLayer?: number, maxCallableMethods?: number, maxControlAccessors?: number, maxErrors?: number}} [options] 诊断基线与上限。
 * @returns {object} 仅包含受限方法元数据的可 JSON 序列化报告。
 */
function collectVisibleSurface(plugin, options = {}) {
  const limits = resolveLimits(options);
  const errors = [];
  const callableMethods = [];
  const controlAccessors = [];
  const layers = [];
  let truncated = false;
  let prototypeDepthLimited = false;
  let skippedBaselinePrototypeTail = false;

  if (!plugin || (typeof plugin !== "object" && typeof plugin !== "function")) {
    return {
      schema: CAPABILITY_REPORT_SCHEMA,
      status: "unavailable",
      pluginFound: false,
      callableMethods,
      controlCandidates: [],
      controlAccessors,
      layers,
      truncated: false,
      prototypeDepthLimited: false,
      skippedBaselinePrototypeTail: false,
      reflectionErrors: errors,
    };
  }

  const baselineNames = collectBaselineNames(options.baseline, limits);
  let current = plugin;
  let depth = 0;
  while (current && depth <= limits.maxPrototypeDepth) {
    const names = safeOwnPropertyNames(current, errors, limits, depth);
    const selectedNames = names.slice(0, limits.maxPropertiesPerLayer);
    if (names.length > selectedNames.length) truncated = true;
    layers.push({
      depth,
      owner: depth === 0 ? "own" : "prototype",
      propertyCount: names.length,
      truncated: names.length > selectedNames.length,
    });

    for (const rawName of selectedNames) {
      const name = visibleMethodName(rawName);
      if (!name) continue;
      const descriptor = safeDescriptor(current, name, errors, limits, depth);
      if (!descriptor) continue;
      const baselineProperty = baselineNames.has(name);
      if (Object.hasOwn(descriptor, "value") && typeof descriptor.value === "function") {
        if (baselineProperty || callableMethods.length >= limits.maxCallableMethods) {
          truncated ||= !baselineProperty;
          continue;
        }
        callableMethods.push(methodMetadata(name, descriptor, depth));
      } else if (
        (typeof descriptor.get === "function" || typeof descriptor.set === "function") &&
        isControlCandidate(name) &&
        !baselineProperty &&
        controlAccessors.length < limits.maxControlAccessors
      ) {
        // accessor 只记录存在性，绝不读取其值，避免触发 PPAPI 原生 getter。
        controlAccessors.push(accessorMetadata(name, descriptor, depth));
      } else if (isControlCandidate(name) && !baselineProperty) {
        truncated ||= controlAccessors.length >= limits.maxControlAccessors;
      }
    }

    current = safePrototype(current, errors, limits, depth);
    depth += 1;
  }
  if (current) {
    prototypeDepthLimited = true;
    // 如果当前未扫描层与普通 embed 的同层原型对象完全相同，剩余内容就是
    // DOM 基线尾部，不应被误报为 QQ 业务能力被截断。
    const baselineTail = baselinePrototypeAtDepth(options.baseline, depth);
    skippedBaselinePrototypeTail = Boolean(baselineTail && current === baselineTail);
    if (!skippedBaselinePrototypeTail) truncated = true;
  }

  const controlCandidates = callableMethods.filter((item) => isControlCandidate(item.name));
  return {
    schema: CAPABILITY_REPORT_SCHEMA,
    status: errors.length ? "partial" : "complete",
    pluginFound: true,
    callableMethods,
    controlCandidates,
    controlAccessors,
    layers,
    truncated,
    prototypeDepthLimited,
    skippedBaselinePrototypeTail,
    reflectionErrors: errors,
  };
}

function sanitizeMethodEntry(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const name = visibleMethodName(value.name);
  if (!name) return null;
  return {
    name,
    owner: value.owner === "own" ? "own" : "prototype",
    depth: boundedInteger(value.depth, 0, 0, 12),
    arity: Number.isInteger(value.arity) ? Math.max(0, Math.min(64, value.arity)) : null,
    enumerable: Boolean(value.enumerable),
    configurable: Boolean(value.configurable),
    writable: Boolean(value.writable),
  };
}

function sanitizeAccessorEntry(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const name = visibleMethodName(value.name);
  if (!name || !isControlCandidate(name)) return null;
  return {
    name,
    owner: value.owner === "own" ? "own" : "prototype",
    depth: boundedInteger(value.depth, 0, 0, 12),
    has_getter: Boolean(value.has_getter),
    has_setter: Boolean(value.has_setter),
  };
}

/**
 * 限制 Renderer 到 Host IPC 的可见面诊断，防止原生对象或环境字段进入状态接口。
 *
 * @param {unknown} report Renderer 返回的诊断对象。
 * @returns {object} 仅由固定字段组成的报告。
 */
function sanitizeVisibleSurfaceReport(report) {
  const source = report && typeof report === "object" && !Array.isArray(report) ? report : {};
  const callableMethods = [];
  const names = new Set();
  for (const item of Array.isArray(source.callableMethods) ? source.callableMethods.slice(0, 128) : []) {
    const sanitized = sanitizeMethodEntry(item);
    if (sanitized && !names.has(sanitized.name)) {
      names.add(sanitized.name);
      callableMethods.push(sanitized);
    }
  }
  const controlAccessors = [];
  const accessorNames = new Set();
  for (const item of Array.isArray(source.controlAccessors) ? source.controlAccessors.slice(0, 64) : []) {
    const sanitized = sanitizeAccessorEntry(item);
    if (sanitized && !accessorNames.has(sanitized.name)) {
      accessorNames.add(sanitized.name);
      controlAccessors.push(sanitized);
    }
  }
  const layers = [];
  for (const item of Array.isArray(source.layers) ? source.layers.slice(0, 16) : []) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    layers.push({
      depth: boundedInteger(item.depth, 0, 0, 12),
      owner: item.owner === "own" ? "own" : "prototype",
      propertyCount: boundedInteger(item.propertyCount, 0, 0, 4096),
      truncated: Boolean(item.truncated),
    });
  }
  const reflectionErrors = [];
  const allowedOperations = new Set([
    "own_property_names_failed",
    "property_descriptor_failed",
    "prototype_lookup_failed",
  ]);
  for (const item of Array.isArray(source.reflectionErrors) ? source.reflectionErrors.slice(0, 32) : []) {
    if (!item || typeof item !== "object" || !allowedOperations.has(item.operation)) continue;
    reflectionErrors.push({
      operation: item.operation,
      depth: boundedInteger(item.depth, 0, 0, 12),
    });
  }
  const status = ["complete", "partial", "unavailable"].includes(source.status)
    ? source.status
    : "partial";
  return {
    schema: CAPABILITY_REPORT_SCHEMA,
    status,
    pluginFound: Boolean(source.pluginFound),
    callableMethods,
    controlCandidates: callableMethods.filter((item) => isControlCandidate(item.name)),
    controlAccessors,
    layers,
    truncated: Boolean(source.truncated),
    prototypeDepthLimited: Boolean(source.prototypeDepthLimited),
    skippedBaselinePrototypeTail: Boolean(source.skippedBaselinePrototypeTail),
    reflectionErrors,
  };
}

module.exports = {
  CAPABILITY_REPORT_SCHEMA,
  collectVisibleSurface,
  sanitizeVisibleSurfaceReport,
};
