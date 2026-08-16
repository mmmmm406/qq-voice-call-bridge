"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");

const STATIC_ARTIFACT_REPORT_SCHEMA = "qq_voice_call.avsdk_static_artifacts.v1";
const STATIC_ARTIFACTS = Object.freeze([
  { id: "avsdk_plugin" },
  { id: "napcat_default_loader" },
]);
const STATIC_ARTIFACT_IDS = new Set(STATIC_ARTIFACTS.map((item) => item.id));
const STATIC_KEYWORDS = Object.freeze([
  { id: "on_action_to_avsdk", value: "onactiontoavsdk" },
  { id: "on_invite_action_to_avsdk", value: "oninviteactiontoavsdk" },
  { id: "action_type", value: "actiontype" },
  { id: "hangup", value: "hangup" },
  { id: "reject", value: "reject" },
  { id: "leave_room", value: "leaveroom" },
  { id: "clear_room", value: "clearroom" },
  { id: "call_terminated", value: "call_terminated" },
  { id: "avsdk", value: "avsdk" },
  { id: "cmd_20001", value: "20001" },
]);
const DEFAULT_LIMITS = Object.freeze({
  maxBytesPerArtifact: 128 * 1024 * 1024,
  readChunkBytes: 64 * 1024,
});
const MAX_KEYWORD_BYTES = Math.max(...STATIC_KEYWORDS.map((item) => item.value.length));
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : fallback;
}

function resolveLimits(options = {}) {
  return {
    maxBytesPerArtifact: boundedInteger(
      options.maxBytesPerArtifact,
      DEFAULT_LIMITS.maxBytesPerArtifact,
      1,
      DEFAULT_LIMITS.maxBytesPerArtifact,
    ),
    readChunkBytes: boundedInteger(
      options.readChunkBytes,
      DEFAULT_LIMITS.readChunkBytes,
      1024,
      DEFAULT_LIMITS.readChunkBytes,
    ),
  };
}

function emptyKeywordHits() {
  return STATIC_KEYWORDS.map((item) => ({ id: item.id, count: 0 }));
}

function artifactResult(id, status, overrides = {}) {
  return {
    id,
    status,
    byteLength: 0,
    scannedBytes: 0,
    scanTruncated: false,
    sha256: null,
    sha256Scope: null,
    keywordHits: emptyKeywordHits(),
    errorCode: null,
    ...overrides,
  };
}

function errorCodeFor(error) {
  if (error?.code === "ENOENT") return "not_found";
  if (error?.code === "EACCES" || error?.code === "EPERM") return "permission_denied";
  return "read_failed";
}

/**
 * 统计固定 ASCII 关键词，保留上一块末尾以覆盖跨块字符串。
 *
 * @param {Buffer} chunk 本次从受限文件读取的数据块。
 * @param {Buffer} carry 上一个数据块的短尾。
 * @param {Map<string, number>} counts 固定关键词计数表。
 * @returns {Buffer} 需要带入下一个数据块的短尾。
 */
function countKeywordHits(chunk, carry, counts) {
  const merged = carry.byteLength ? Buffer.concat([carry, chunk]) : chunk;
  const searchable = merged.toString("latin1").toLowerCase();
  const carryLength = carry.byteLength;
  for (const keyword of STATIC_KEYWORDS) {
    let offset = 0;
    while (offset < searchable.length) {
      const foundAt = searchable.indexOf(keyword.value, offset);
      if (foundAt < 0) break;
      // 只跳过完全属于上一块的命中；跨块命中应在本轮计入一次。
      if (foundAt + keyword.value.length > carryLength) {
        counts.set(keyword.id, (counts.get(keyword.id) || 0) + 1);
      }
      offset = foundAt + keyword.value.length;
    }
  }
  const tailLength = Math.max(0, MAX_KEYWORD_BYTES - 1);
  return tailLength ? merged.subarray(Math.max(0, merged.byteLength - tailLength)) : Buffer.alloc(0);
}

async function scanArtifact(id, filePath, limits) {
  let stat;
  try {
    stat = await fs.promises.lstat(filePath);
  } catch (error) {
    return artifactResult(id, "missing", { errorCode: errorCodeFor(error) });
  }
  if (stat.isSymbolicLink() || !stat.isFile()) {
    return artifactResult(id, "not_regular_file", { errorCode: "not_regular_file" });
  }

  const byteLength = Math.max(0, Number(stat.size) || 0);
  const plannedBytes = Math.min(byteLength, limits.maxBytesPerArtifact);
  const hash = crypto.createHash("sha256");
  const counts = new Map(STATIC_KEYWORDS.map((item) => [item.id, 0]));
  let fileHandle = null;
  let scannedBytes = 0;
  let carry = Buffer.alloc(0);
  try {
    fileHandle = await fs.promises.open(filePath, "r");
    const buffer = Buffer.allocUnsafe(limits.readChunkBytes);
    while (scannedBytes < plannedBytes) {
      const remaining = plannedBytes - scannedBytes;
      const { bytesRead } = await fileHandle.read(
        buffer,
        0,
        Math.min(buffer.byteLength, remaining),
        scannedBytes,
      );
      if (!bytesRead) break;
      const chunk = buffer.subarray(0, bytesRead);
      hash.update(chunk);
      carry = countKeywordHits(chunk, carry, counts);
      scannedBytes += bytesRead;
    }
  } catch (error) {
    return artifactResult(id, "read_failed", {
      byteLength,
      scannedBytes,
      scanTruncated: true,
      errorCode: errorCodeFor(error),
    });
  } finally {
    if (fileHandle) await fileHandle.close();
  }

  const scanTruncated = scannedBytes < byteLength;
  return artifactResult(id, "scanned", {
    byteLength,
    scannedBytes,
    scanTruncated,
    sha256: hash.digest("hex"),
    sha256Scope: scanTruncated ? "prefix" : "full",
    keywordHits: STATIC_KEYWORDS.map((item) => ({ id: item.id, count: counts.get(item.id) || 0 })),
    errorCode: scanTruncated ? "byte_limit_reached" : null,
  });
}

/**
 * 读取两个固定安装目标的有限字节，生成不含路径或原文的静态线索摘要。
 *
 * 该函数本身不读取目录、不运行 Shell、不调用 QQ/AVSDK；生产调用方只传入
 * 已写死的 QQ AVSDK 与 NapCat Loader 路径。可选参数仅供本地 Node 回归夹具使用，
 * 从未通过 WebSocket 或管理接口接收。
 *
 * @param {{avsdkPluginPath?: string, napcatLoaderPath?: string}} targets 固定目标路径。
 * @param {{maxBytesPerArtifact?: number, readChunkBytes?: number}} [options] 本地测试读取上限。
 * @returns {Promise<object>} 仅含固定类别、计数、哈希和限制状态的报告。
 */
async function collectStaticArtifactReport(targets = {}, options = {}) {
  const limits = resolveLimits(options);
  const artifacts = await Promise.all([
    scanArtifact("avsdk_plugin", String(targets.avsdkPluginPath || ""), limits),
    scanArtifact("napcat_default_loader", String(targets.napcatLoaderPath || ""), limits),
  ]);
  const scannedCount = artifacts.filter((item) => item.status === "scanned").length;
  const complete = scannedCount === artifacts.length && artifacts.every((item) => !item.scanTruncated);
  return {
    schema: STATIC_ARTIFACT_REPORT_SCHEMA,
    status: scannedCount === 0 ? "unavailable" : complete ? "complete" : "partial",
    artifacts,
  };
}

function sanitizeKeywordHits(value) {
  const source = Array.isArray(value) ? value : [];
  const values = new Map();
  for (const item of source.slice(0, STATIC_KEYWORDS.length)) {
    if (!item || typeof item !== "object" || !STATIC_KEYWORDS.some((keyword) => keyword.id === item.id)) {
      continue;
    }
    values.set(item.id, boundedInteger(item.count, 0, 0, 1000000));
  }
  return STATIC_KEYWORDS.map((keyword) => ({ id: keyword.id, count: values.get(keyword.id) || 0 }));
}

function sanitizeArtifact(value) {
  if (!value || typeof value !== "object" || !STATIC_ARTIFACT_IDS.has(value.id)) return null;
  const status = ["scanned", "missing", "not_regular_file", "read_failed"].includes(value.status)
    ? value.status
    : "read_failed";
  const byteLength = boundedInteger(value.byteLength, 0, 0, DEFAULT_LIMITS.maxBytesPerArtifact);
  const scannedBytes = Math.min(
    byteLength,
    boundedInteger(value.scannedBytes, 0, 0, DEFAULT_LIMITS.maxBytesPerArtifact),
  );
  const sha256 = typeof value.sha256 === "string" && SHA256_PATTERN.test(value.sha256)
    ? value.sha256
    : null;
  const scope = sha256 && ["full", "prefix"].includes(value.sha256Scope)
    ? value.sha256Scope
    : null;
  const errorCode = ["not_found", "permission_denied", "not_regular_file", "read_failed", "byte_limit_reached"]
    .includes(value.errorCode)
    ? value.errorCode
    : null;
  return {
    id: value.id,
    status,
    byteLength,
    scannedBytes,
    scanTruncated: Boolean(value.scanTruncated),
    sha256,
    sha256Scope: scope,
    keywordHits: sanitizeKeywordHits(value.keywordHits),
    errorCode,
  };
}

/**
 * 跨进程回传前再白名单化扫描报告，拒绝路径、原文、未知类别和错误详情。
 *
 * @param {unknown} report 扫描器产生的报告。
 * @returns {object} 可安全发送给 OneBot 的固定字段。
 */
function sanitizeStaticArtifactReport(report) {
  const source = report && typeof report === "object" && !Array.isArray(report) ? report : {};
  const byId = new Map();
  for (const item of Array.isArray(source.artifacts) ? source.artifacts.slice(0, 8) : []) {
    const sanitized = sanitizeArtifact(item);
    if (sanitized && !byId.has(sanitized.id)) byId.set(sanitized.id, sanitized);
  }
  const artifacts = STATIC_ARTIFACTS.map((item) => byId.get(item.id) || artifactResult(item.id, "missing"));
  const scannedCount = artifacts.filter((item) => item.status === "scanned").length;
  const complete = scannedCount === artifacts.length && artifacts.every((item) => !item.scanTruncated);
  return {
    schema: STATIC_ARTIFACT_REPORT_SCHEMA,
    // 顶层状态由已白名单化的固定目标重新计算，不能信任远端自报的 complete。
    status: scannedCount === 0 ? "unavailable" : complete ? "complete" : "partial",
    artifacts,
  };
}

module.exports = {
  STATIC_ARTIFACT_REPORT_SCHEMA,
  STATIC_KEYWORDS,
  collectStaticArtifactReport,
  sanitizeStaticArtifactReport,
};
