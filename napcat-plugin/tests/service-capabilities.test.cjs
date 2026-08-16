"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  SERVICE_SURFACE_REPORT_SCHEMA,
  collectAVSDKServiceSurface,
  sanitizeAVSDKServiceSurfaceReport,
} = require("../service-capabilities.cjs");

test("service-surface inspection keeps only control candidates without reading accessors", () => {
  let accessorReads = 0;
  const prototype = {
    leaveRoom() {},
    ordinaryMethod() {},
  };
  const service = Object.create(prototype);
  Object.defineProperty(service, "reject", {
    get() {
      accessorReads += 1;
      return () => {};
    },
    configurable: true,
  });
  service.hangup = () => {};

  const report = collectAVSDKServiceSurface(service);

  assert.equal(report.schema, SERVICE_SURFACE_REPORT_SCHEMA);
  assert.equal(report.status, "complete");
  assert.equal(accessorReads, 0);
  assert.deepEqual(
    report.controlCandidates.map((item) => [item.name, item.kind, item.owner]),
    [
      ["reject", "accessor", "own"],
      ["hangup", "method", "own"],
      ["leaveRoom", "method", "prototype"],
    ],
  );
  assert.equal(report.controlCandidates.some((item) => item.name === "ordinaryMethod"), false);
});

test("service-surface inspection bounds reflection failures and sanitizer drops unknown fields", () => {
  const proxy = new Proxy({}, {
    ownKeys() {
      throw new Error("private service trap");
    },
  });
  const report = collectAVSDKServiceSurface(proxy);
  assert.equal(report.status, "partial");
  assert.equal(report.reflectionErrors[0].operation, "own_property_names_failed");

  const sanitized = sanitizeAVSDKServiceSurfaceReport({
    schema: SERVICE_SURFACE_REPORT_SCHEMA,
    status: "complete",
    serviceAvailable: true,
    controlCandidates: [
      {
        name: "clearRoom",
        kind: "method",
        owner: "own",
        depth: 0,
        rawValue: "must-not-leave-plugin",
      },
      { name: "../invalid", kind: "method" },
    ],
    layers: [{ depth: 0, owner: "own", propertyCount: 2, secret: "private" }],
    reflectionErrors: [{ operation: "prototype_lookup_failed", depth: 1, message: "private" }],
    token: "private",
  });

  assert.deepEqual(sanitized.controlCandidates.map((item) => item.name), ["clearRoom"]);
  assert.equal(JSON.stringify(sanitized).includes("must-not-leave-plugin"), false);
  assert.equal(JSON.stringify(sanitized).includes("private"), false);
});
