"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  STATIC_ARTIFACT_REPORT_SCHEMA,
  collectStaticArtifactReport,
  sanitizeStaticArtifactReport,
} = require("../static-artifact-report.cjs");

async function createFixture() {
  const cacheDirectory = path.resolve(process.cwd(), "temp", "codex-cache");
  await fs.promises.mkdir(cacheDirectory, { recursive: true });
  const directory = await fs.promises.mkdtemp(path.join(cacheDirectory, "qq-static-artifact-"));
  const avsdkPluginPath = path.join(directory, "libAVSDKPlugin.so");
  const napcatLoaderPath = path.join(directory, "loadNapCat.js");
  await fs.promises.writeFile(
    avsdkPluginPath,
    "onActionToAVSDK actionType hangup SECRET_STATIC_ARTIFACT_CONTENT",
    "utf8",
  );
  await fs.promises.writeFile(
    napcatLoaderPath,
    "onInviteActionToAVSDK reject leaveRoom postMessage",
    "utf8",
  );
  return { directory, avsdkPluginPath, napcatLoaderPath };
}

test("static artifact report reads only supplied fixed artifacts and omits paths and content", async () => {
  const fixture = await createFixture();
  try {
    const report = await collectStaticArtifactReport(fixture);
    assert.equal(report.schema, STATIC_ARTIFACT_REPORT_SCHEMA);
    assert.equal(report.status, "complete");
    assert.deepEqual(report.artifacts.map((item) => item.id), [
      "avsdk_plugin",
      "napcat_default_loader",
    ]);
    const plugin = report.artifacts[0];
    const loader = report.artifacts[1];
    assert.equal(plugin.sha256Scope, "full");
    assert.equal(plugin.keywordHits.find((item) => item.id === "hangup").count, 1);
    assert.equal(loader.keywordHits.find((item) => item.id === "reject").count, 1);
    const serialized = JSON.stringify(report);
    assert.equal(serialized.includes(fixture.directory), false);
    assert.equal(serialized.includes("SECRET_STATIC_ARTIFACT_CONTENT"), false);
  } finally {
    await fs.promises.rm(fixture.directory, { recursive: true, force: true });
  }
});

test("static artifact report bounds reads and its sanitizer drops unexpected fields", async () => {
  const fixture = await createFixture();
  try {
    const report = await collectStaticArtifactReport(fixture, { maxBytesPerArtifact: 16 });
    assert.equal(report.status, "partial");
    assert.equal(report.artifacts[0].scanTruncated, true);
    assert.equal(report.artifacts[0].sha256Scope, "prefix");

    const sanitized = sanitizeStaticArtifactReport({
      schema: STATIC_ARTIFACT_REPORT_SCHEMA,
      status: "complete",
      artifacts: [
        {
          id: "avsdk_plugin",
          status: "scanned",
          byteLength: 4,
          scannedBytes: 4,
          sha256: "a".repeat(64),
          sha256Scope: "full",
          keywordHits: [{ id: "hangup", count: 1 }],
          absolutePath: fixture.avsdkPluginPath,
          rawContent: "SECRET_STATIC_ARTIFACT_CONTENT",
          token: "private",
        },
        { id: "unknown_file", rawContent: "private" },
      ],
    });
    const serialized = JSON.stringify(sanitized);
    assert.equal(serialized.includes(fixture.directory), false);
    assert.equal(serialized.includes("SECRET_STATIC_ARTIFACT_CONTENT"), false);
    assert.equal(serialized.includes("unknown_file"), false);
    assert.equal(sanitized.artifacts[0].keywordHits.find((item) => item.id === "hangup").count, 1);
    assert.equal(sanitized.status, "partial");
  } finally {
    await fs.promises.rm(fixture.directory, { recursive: true, force: true });
  }
});
