# NapCat 语音桥协议 v1

本协议供机器人或 AI 宿主控制 NapCat 语音插件。仓库中的 [OneBot 接入参考](../onebot-plugin/README.md) 是一种实现；其它宿主可以实现相同协议。

## 建立连接

NapCat 插件作为 WebSocket 客户端连接控制端，默认路径 `/qq-voice-call`，握手携带：

- `Authorization: Bearer <Token>`：控制端应校验。
- `X-Self-ID`：当前机器人 QQ 号。
- `User-Agent: QQ-Voice-Call/<插件版本>`。

使用 JSON 对象交换文本消息。插件复用普通反向 WS 的地址与 Token，也支持在插件配置中显式填写；公开版要求 Token 非空。

## 握手与激活

连接后插件主动发送：

```json
{
  "type": "hello",
  "protocolVersion": 1,
  "pluginVersion": "0.2.1",
  "capabilities": [],
  "selfId": "123456789",
  "platform": "linux",
  "qqVersion": "..."
}
```

这里只展示字段形状；实际 `capabilities` 包含插件支持的诊断能力。控制端也可发送 `{"type":"hello_request"}` 再次获取握手信息。

确认协议版本为 1 后发送 `activate`。下面的 QQ 号仅用于展示格式，应改成实际允许的测试来电账号：

```json
{
  "type": "activate",
  "protocolVersion": 1,
  "config": {
    "autoAcceptPrivate": true,
    "allowUsers": ["123456789"],
    "denyUsers": [],
    "admissionByOneBot": false,
    "acceptWhenReady": true,
    "acceptDelayMs": 500
  }
}
```

| 字段 | 默认行为与作用 |
| --- | --- |
| `autoAcceptPrivate` | 缺省为 `true`；为 `false` 时静态准入不自动同意来电 |
| `allowUsers` / `denyUsers` | QQ 号字符串数组，缺省为空；名单需要可解析的来电身份，拒绝名单优先 |
| `admissionByOneBot` | 缺省为 `false`；静态准入通过后等待控制端的接听决策 |
| `acceptWhenReady` | 缺省为 `false`；为 `true` 时等待控制端显式发送 `accept` |
| `acceptDelayMs` | 缺省为 `500`，范围 `0..10000` 毫秒；仅用于旧式自动接听路径 |

这组字段随 `activate` 更新。首次激活创建 Host 并注册 Listener，重复激活更新配置；从停用状态重新激活会先等待上一代资源清理。

## 状态与接听

插件推送 `{"type":"status","data":{...}}`。控制端可发送 `{"type":"status_request"}` 获取当前快照。主要观察：

- `data.active`：运行时是否激活。
- `data.avsdk.hostReady`、`listenerRegistered`、`loginPosted`：Host、监听与登录命令提交状态。
- `data.avsdk.lastLoginAt`、`lastLoginResult`：AVSDK 初始化调用时间和返回结构摘要。0.2.2 起，原生日志/提示通知 `20050`、`120043` 只累计到 `commandCounts`，不再触发初始化；`loginSuppressed` 随原有通知重登逻辑一起移除。
- `data.call.phase`：例如 `idle`、`ringing`、`accepted`、`connected`、`ended`、`error`。
- `data.call.callerUid`、`callerUin`、`callerName`：来电身份，部分字段可能尚未解析。

来电进入 `ringing` 后，控制端在音频、ASR、TTS 等准备好时提交：

```json
{
  "type": "control",
  "protocolVersion": 1,
  "requestId": "accept-1",
  "action": "accept"
}
```

插件用 `control_result` 回应同一个 `requestId`，携带 `action`、`phase`、`status`、`supported` 和 `message`。`status: "submitted"` 表示原生命令已提交；实际接通还要结合后续状态和通话确认。

`reject`、`hangup`、`mute`、`unmute` 当前返回 `unsupported`。控制端据此向自己的业务层报告能力状态。

## 停用、连接存活与音频

控制端可发送 `{"type":"deactivate","reason":"host_stopping"}`，插件会取消待执行任务、移除 Listener 并等待 Host 退出。WebSocket 断开也触发清理；重新建立连接后，需要再次完成握手与激活。

`{"type":"ping"}` 返回 `{"type":"pong"}`。

音频由 QQ/AVSDK 所在环境的设备承载；本协议的 WebSocket 消息提供控制、信令和状态。ASR 输入与 TTS 输出应通过实际音频路由接入。诊断消息的详细数据结构见 `protocol.mjs` 与接入参考运行时。
