import assert from "node:assert/strict";
import test from "node:test";

import {
  AVSDK_SERVICE_SURFACE_DIAGNOSTIC_CAPABILITY,
  AVSDK_STATIC_ARTIFACTS_DIAGNOSTIC_CAPABILITY,
  AVSDK_VISIBLE_SURFACE_DIAGNOSTIC_CAPABILITY,
  DIAGNOSTIC_CAPTURE_LIMITS,
  buildAcceptParams,
  deriveBridgeSettings,
  normalizeDiagnosticCaptureStart,
  normalizeAVSDKServiceSurfaceDiagnosticStart,
  normalizeStaticArtifactsDiagnosticStart,
  normalizeVisibleSurfaceDiagnosticStart,
  normalizeRuntimeConfig,
  serializeCaptureValue,
  shouldAcceptCaller,
} from "../protocol.mjs";

test("derives voice bridge from the enabled NapCat reverse WebSocket", () => {
  const settings = deriveBridgeSettings(
    {},
    {
      network: {
        websocketClients: [
          {
            enable: true,
            url: "ws://192.0.2.10:6199/ws?ignored=1",
            token: "test-token",
          },
        ],
      },
    },
  );
  assert.equal(settings.url, "ws://192.0.2.10:6199/qq-voice-call");
  assert.equal(settings.token, "test-token");
});

test("builds the native AVSDK accept parameters", () => {
  const invite = [3, "caller-uid", "self-uid", 1, "room", 0, 0, 0, 0, 0, 1, "extra"];
  assert.deepEqual(buildAcceptParams(invite), [
    3,
    "caller-uid",
    ["caller-uid"],
    1,
    "room",
    true,
    "extra",
  ]);
});

test("private caller filters are deterministic", () => {
  const config = normalizeRuntimeConfig({
    autoAcceptPrivate: true,
    allowUsers: ["12345"],
    denyUsers: ["54321"],
  });
  assert.deepEqual(shouldAcceptCaller("12345", config), { accept: true, reason: "allowed" });
  assert.deepEqual(shouldAcceptCaller("54321", config), { accept: false, reason: "caller_denied" });
  assert.deepEqual(shouldAcceptCaller("99999", config), { accept: false, reason: "caller_not_allowed" });
  assert.deepEqual(shouldAcceptCaller(null, config), { accept: false, reason: "caller_identity_unresolved" });
});

test("new runtime waits for OneBot readiness while legacy runtime keeps its fallback", () => {
  const readyDriven = normalizeRuntimeConfig({ acceptWhenReady: true });
  const legacy = normalizeRuntimeConfig({ acceptDelayMs: 900 });

  assert.equal(readyDriven.acceptWhenReady, true);
  assert.equal(legacy.acceptWhenReady, false);
  assert.equal(legacy.acceptDelayMs, 900);
});

test("normalizes one-shot manual hangup capture requests", () => {
  const request = normalizeDiagnosticCaptureStart({
    protocolVersion: 1,
    requestId: "request-1",
    captureId: "capture-1",
    callId: "call-1",
    kind: "manual_hangup",
    mode: "raw",
    timeoutMs: 30000,
  });

  assert.equal(request.timeoutMs, 30000);
  assert.equal(request.kind, "manual_hangup");
  assert.throws(
    () => normalizeDiagnosticCaptureStart({ ...request, timeoutMs: 29999 }),
    /timeoutMs must be an integer/,
  );
  assert.throws(
    () => normalizeDiagnosticCaptureStart({ ...request, captureId: "../escape" }),
    /invalid_capture_id/,
  );
  assert.equal(DIAGNOSTIC_CAPTURE_LIMITS.maxTimeoutMs, 45000);
});

test("normalizes explicit AVSDK visible-surface diagnostic requests", () => {
  const request = normalizeVisibleSurfaceDiagnosticStart({
    protocolVersion: 1,
    requestId: "visible-surface-1",
    kind: "avsdk_visible_surface",
  });

  assert.equal(request.kind, "avsdk_visible_surface");
  assert.equal(AVSDK_VISIBLE_SURFACE_DIAGNOSTIC_CAPABILITY, "avsdk_visible_surface_diagnostic_v1");
  assert.throws(
    () => normalizeVisibleSurfaceDiagnosticStart({ ...request, kind: "hangup" }),
    /unsupported_diagnostic_kind/,
  );
});

test("normalizes static-artifacts diagnostics without accepting file-system inputs", () => {
  const request = normalizeStaticArtifactsDiagnosticStart({
    protocolVersion: 1,
    requestId: "static-artifacts-1",
    kind: "avsdk_static_artifacts",
    path: "/private/file",
    keywords: ["hangup"],
    command: "cat",
  });

  assert.deepEqual(request, {
    protocolVersion: 1,
    requestId: "static-artifacts-1",
    kind: "avsdk_static_artifacts",
  });
  assert.equal(
    AVSDK_STATIC_ARTIFACTS_DIAGNOSTIC_CAPABILITY,
    "avsdk_static_artifacts_diagnostic_v1",
  );
  assert.throws(
    () => normalizeStaticArtifactsDiagnosticStart({ ...request, kind: "file_read" }),
    /unsupported_diagnostic_kind/,
  );
});

test("normalizes AVSDK Service diagnostics without accepting method inputs", () => {
  assert.deepEqual(
    normalizeAVSDKServiceSurfaceDiagnosticStart({
      protocolVersion: 1,
      requestId: "service-surface-1",
      kind: "avsdk_service_surface",
      method: "hangup",
      params: ["private"],
    }),
    {
      protocolVersion: 1,
      requestId: "service-surface-1",
      kind: "avsdk_service_surface",
    },
  );
  assert.equal(
    AVSDK_SERVICE_SURFACE_DIAGNOSTIC_CAPABILITY,
    "avsdk_service_surface_diagnostic_v1",
  );
});

test("serializes buffers and circular values without exposing unbounded objects", () => {
  const source = {
    payload: Buffer.from([0, 1, 254, 255]),
    text: "abcdef",
  };
  source.self = source;

  const serialized = serializeCaptureValue(source, {
    maxStringBytes: 4,
    maxBinaryBytes: 3,
  });

  assert.deepEqual(serialized.payload, {
    $type: "Buffer",
    encoding: "base64",
    byteLength: 4,
    truncated: true,
    data: "AAH+",
  });
  assert.deepEqual(serialized.text, {
    $type: "TruncatedString",
    byteLength: 6,
    data: "abcd",
  });
  assert.deepEqual(serialized.self, { $type: "Circular", path: "$" });
});
