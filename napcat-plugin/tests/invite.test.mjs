import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import {
  extractCallerUid,
  normalizeInvitePayload,
  normalizeRuntimeConfig,
  shouldAcceptCaller,
} from "../protocol.mjs";

const source = readFileSync(new URL("../index.mjs", import.meta.url), "utf8");
const runtimeSource = source.slice(source.indexOf("const PLUGIN_VERSION")).replace(/^export /gm, "");
const invite = [3, "caller-uid", "self-uid", 1, "room", 0, 0, 0, 0, 0, 1, "extra"];

/** 执行真实来电入口，身份服务使用合成返回值；保持控制端准备后再接听。 */
function fixture({ config = {}, getUin = async () => ({ uinInfo: new Map([["caller-uid", { uin: "12345" }]]) }) } = {}) {
  return runInNewContext(`${runtimeSource}
    context = syntheticContext;
    state.active = true;
    state.call.phase = "ringing";
    state.call.inviteAt = "synthetic-invite";
    runtimeConfig = normalizeRuntimeConfig({ acceptWhenReady: true, ...config });
    invokeAVSDK = async () => { throw new Error("unexpected native call"); };
    ({ emit: (value) => handleAVSDKOutput(null, { cmd: 20006, value }), snapshot: publicStatus });
  `, {
    normalizeRuntimeConfig, normalizeInvitePayload, extractCallerUid, shouldAcceptCaller, config,
    syntheticContext: {
      core: {
        context: { session: { getUixConvertService: () => ({ getUin }) } },
        eventWrapper: { callNoListenerEvent: async () => new Map([["caller-uid", { nick: "Synthetic caller" }]]) },
      },
    },
  });
}

test("来电包装通过真实入口归一化，名单使用解析后的 QQ 号", async () => {
  for (const payload of [invite, { payload: { value: invite } }, Object.assign({}, invite)]) {
    const runtime = fixture({ config: { allowUsers: ["12345"] } });
    await runtime.emit(payload);
    const { call } = runtime.snapshot();
    assert.equal(call.callerUid, "caller-uid");
    assert.equal(call.callerUin, "12345");
    assert.equal(call.callerName, "Synthetic caller");
    assert.equal(call.acceptDecision, "accept");
    assert.equal(call.acceptDecisionReason, "allowed");
    assert.equal(call.acceptPostState, null);
  }
});

test("OneBot 准入沿用身份解析完成后再通知控制端的契约", async () => {
  let completeIdentity;
  const runtime = fixture({
    config: { admissionByOneBot: true },
    getUin: () => new Promise((resolve) => { completeIdentity = resolve; }),
  });
  const pending = runtime.emit(invite);
  assert.equal(runtime.snapshot().call.acceptDecision, null);
  completeIdentity({ uinInfo: new Map([["caller-uid", "12345"]]) });
  await pending;
  const { call } = runtime.snapshot();
  assert.equal(call.callerUin, "12345");
  assert.equal(call.acceptDecision, "pending");
  assert.equal(call.acceptDecisionReason, "onebot_ai");
});

test("非法来电载荷保持准入未决", async () => {
  const runtime = fixture();
  await runtime.emit({ payload: "invalid-invite" });
  const { call } = runtime.snapshot();
  assert.equal(call.callerUid, null);
  assert.equal(call.acceptDecision, null);
  assert.equal(call.acceptPostState, null);
});
