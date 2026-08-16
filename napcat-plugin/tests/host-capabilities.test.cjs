"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CAPABILITY_REPORT_SCHEMA,
  collectVisibleSurface,
  sanitizeVisibleSurfaceReport,
} = require("../host-capabilities.cjs");

test("visible-surface inspection reads descriptors without invoking methods or accessors", () => {
  let methodCalls = 0;
  let getterReads = 0;
  const nativeSurface = {};
  Object.defineProperty(nativeSurface, "hangup", {
    value: function hangup(first, second) {
      methodCalls += 1;
      return [first, second];
    },
    configurable: false,
    enumerable: false,
    writable: false,
  });
  Object.defineProperty(nativeSurface, "acceptInvite", {
    value: function acceptInvite() {
      methodCalls += 1;
    },
    configurable: true,
    enumerable: true,
    writable: true,
  });
  Object.defineProperty(nativeSurface, "leaveRoom", {
    get() {
      getterReads += 1;
      return () => undefined;
    },
    configurable: true,
  });
  const plugin = Object.create(nativeSurface);
  const report = collectVisibleSurface(plugin, { baseline: {} });

  assert.equal(report.schema, CAPABILITY_REPORT_SCHEMA);
  assert.equal(report.status, "complete");
  assert.equal(methodCalls, 0);
  assert.equal(getterReads, 0);
  assert.deepEqual(
    report.controlCandidates.map((item) => [item.name, item.arity]),
    [
      ["hangup", 2],
      ["acceptInvite", 0],
    ],
  );
  assert.deepEqual(report.controlAccessors, [
    {
      name: "leaveRoom",
      owner: "prototype",
      depth: 1,
      has_getter: true,
      has_setter: false,
    },
  ]);
  assert.equal(report.callableMethods.some((item) => item.name === "toString"), false);
});

test("visible-surface inspection bounds failures and only keeps whitelisted report fields", () => {
  const inaccessible = new Proxy(
    {},
    {
      ownKeys() {
        throw new Error("do not expose this error text");
      },
    },
  );
  const partial = collectVisibleSurface(inaccessible, { baseline: {} });
  assert.equal(partial.status, "partial");
  assert.deepEqual(partial.reflectionErrors[0], {
    operation: "own_property_names_failed",
    depth: 0,
  });

  const sanitized = sanitizeVisibleSurfaceReport({
    schema: CAPABILITY_REPORT_SCHEMA,
    status: "complete",
    pluginFound: true,
    callableMethods: [
      { name: "hangup", owner: "own", depth: 0, arity: 0, rawPayload: "private" },
      { name: "../../invalid", owner: "own", depth: 0, arity: 0 },
    ],
    controlAccessors: [
      { name: "closeRoom", owner: "prototype", depth: 1, has_getter: true, raw: "private" },
    ],
    layers: [{ depth: 0, owner: "own", propertyCount: 1, privateValue: "private" }],
    prototypeDepthLimited: true,
    skippedBaselinePrototypeTail: true,
    reflectionErrors: [{ operation: "own_property_names_failed", depth: 0, message: "private" }],
    token: "private",
  });

  assert.deepEqual(sanitized.callableMethods, [
    {
      name: "hangup",
      owner: "own",
      depth: 0,
      arity: 0,
      enumerable: false,
      configurable: false,
      writable: false,
    },
  ]);
  assert.deepEqual(sanitized.controlCandidates.map((item) => item.name), ["hangup"]);
  assert.equal(sanitized.prototypeDepthLimited, true);
  assert.equal(sanitized.skippedBaselinePrototypeTail, true);
  assert.equal(JSON.stringify(sanitized).includes("private"), false);
  assert.equal("token" in sanitized, false);
});

test("visible-surface inspection separates the shared embed tail from a real depth truncation", () => {
  const sharedEmbedTail = Object.create(null);
  const baseline = Object.create(Object.create(sharedEmbedTail));
  const sameTailPlugin = Object.create(Object.create(sharedEmbedTail));
  const distinctTailPlugin = Object.create(Object.create(Object.create(null)));

  const sharedTailReport = collectVisibleSurface(sameTailPlugin, {
    baseline,
    maxPrototypeDepth: 1,
  });
  assert.equal(sharedTailReport.prototypeDepthLimited, true);
  assert.equal(sharedTailReport.skippedBaselinePrototypeTail, true);
  assert.equal(sharedTailReport.truncated, false);

  const distinctTailReport = collectVisibleSurface(distinctTailPlugin, {
    baseline,
    maxPrototypeDepth: 1,
  });
  assert.equal(distinctTailReport.prototypeDepthLimited, true);
  assert.equal(distinctTailReport.skippedBaselinePrototypeTail, false);
  assert.equal(distinctTailReport.truncated, true);
});
