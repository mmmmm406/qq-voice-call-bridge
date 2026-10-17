# QQ Voice Call — NapCat 语音插件

为 Linux NapCat 提供 QQ 私聊原生语音来电能力，支持 Linux Shell 和 Docker 部署。插件使用独立 QQ/Electron AVSDK Host 处理原生通话，通过已鉴权的 WebSocket 向控制端提供来电状态和接听控制。

当前版本：**0.2.0**。本次维护包含登录并发与生命周期修复、来电载荷兼容，以及 Docker 启动前自动安装。

## 从这里开始

| 目标 | 入口 |
| --- | --- |
| 在 Docker 中安装，或给已有容器加入插件 | [Docker 安装说明](docker/README.md) |
| 在 Linux NapCat Shell 中安装、配置插件 | [NapCat 插件说明](napcat-plugin/README.md) |
| 为自己的机器人接入来电与接听控制 | [桥接协议 v1](docs/bridge-protocol.md) |
| 参考已有 OneBot 宿主的 AI 通话接入 | [OneBot 接入参考](onebot-plugin/README.md) |
| 查看本次修复和验证范围 | [更新记录](CHANGELOG.md) |

仓库主要交付 `napcat-plugin/`。`docker/` 提供该插件的容器安装入口；`onebot-plugin/` 保留一种宿主接入实现，使用它之前需要适配对应宿主接口。

## Docker 快速安装

已有 NapCat 容器时，请先按 [接入已有容器](docker/README.md#接入已有容器) 修改原来的 Compose 服务，沿用账号和配置卷。下面的命令用于新部署：

```bash
git clone https://github.com/mmmmm406/qq-voice-call-bridge.git
cd qq-voice-call-bridge
docker compose -f docker/compose.yaml up -d
```

启动包装脚本会在 QQ 启动前部署插件、补上插件白名单并适配物理 Loader，然后交给镜像原有入口启动 NapCat。源码以只读方式挂载，每次创建或启动容器都会重新确认安装状态。

打开 NapCat WebUI，在插件管理中启用 **qq-voice-call**，再配置到控制端的反向 WebSocket。示例仅将 WebUI 发布到本机 `127.0.0.1:6099`。

Docker 安装器适配 `/app/napcat` 与 `/opt/QQ` 的标准 Linux 布局；遇到未知 Loader、白名单或残缺的备份会结束本次启动，错误会出现在容器日志中。镜像与验证范围见 [Docker 说明](docker/README.md)。

## Linux Shell 安装

将 `napcat-plugin/` 整个目录复制为：

```text
<NapCat.Shell>/napcat/plugins/qq-voice-call/
```

保留 `host.cjs`、`host-preload.cjs`、`host.html`、`host-bootstrap/` 以及协议和 capability 模块，然后在 NapCat 插件管理中启用。运行插件不需要执行 `npm install`。

Linux Shell 使用支持 `NAPCAT_BOOTMAIN` 的 launcher；插件不可见或使用物理 Loader 时，按 [NapCat 插件说明](napcat-plugin/README.md) 检查相应补丁。

## 连接控制端

插件需要一个实现 [桥接协议 v1](docs/bridge-protocol.md) 的 WebSocket 控制端。可以使用仓库提供的 OneBot 接入参考，也可以在自己的机器人中实现该协议。

默认复用 NapCat 已启用的普通反向 WebSocket 地址、端口和 Bearer Token，只把路径改为 `/qq-voice-call`。控制端需注册这条路径并校验 Token。

连接建立后，插件先报告 `hello` 和 `status`；收到有效 `activate` 后才启动 AVSDK Host。控制端可以读取来电状态，并在准备完成后发送 `accept`。双方按 `protocolVersion: 1` 协商，插件版本号不要求与控制端包版本相同。

## 通话与音频范围

- 支持 QQ 私聊原生语音来电，以及已经验证的原生接听控制。
- 原生 `reject`、`hangup`、`mute`、`unmute` 在契约尚未验证时返回 `unsupported`。
- 当前支持范围为单通私聊语音来电；主动拨打、群语音和视频需要另行实现与验证。
- WebSocket 承载控制、信令和状态。通话音频通过 QQ/AVSDK 所在环境的音频设备传输。
- AI 对话需要控制端另行提供音频路由、ASR、TTS 和模型。Docker 跨宿主机的双向音频需要实际配置，桥连接成功只证明控制链路建立。

## 登录与进程生命周期

`0.2.0` 合并了重复回调的登录任务，在同一插件生命周期内共享在途登录请求，并在重新激活前等待旧 Host 清理完成。旧进程迟到的错误和退出事件按进程身份过滤。

AVSDK 登录命令的最小间隔为 60 秒，首次尝试、失败重试和桥重连均遵守；该间隔是保护策略。AVSDK 初始化与 QQ 主账号登录属于不同环节，本项目不保证账号在线时长，也不能据此判断腾讯账号风控原因。

## 验证

本次行为回归使用合成身份、虚拟时钟和隔离文件夹，覆盖重复回调、长耗时登录、桥重连、Host 退出交接和 Docker 安装幂等。白名单定位逻辑沿用本地已在 NapCat 4.18.38 官方发布包上验证的实现。

需要 Node.js 22 或兼容版本及 Bash。Windows 运行 Docker 启动脚本测试时，用 `QQ_VOICE_CALL_TEST_BASH` 环境变量指定 Git for Windows 的 `bin/bash.exe`。

```bash
node --test napcat-plugin/tests/login.test.mjs napcat-plugin/tests/host-process.test.mjs napcat-plugin/tests/install-docker.test.cjs napcat-plugin/tests/protocol.test.mjs napcat-plugin/tests/invite.test.mjs napcat-plugin/tests/docker-entrypoint.test.cjs
```

容器内真实 QQ/Electron 启动、长期在线和双向通话音频仍需在实际部署中验收。升级 QQ、NapCat 或镜像后，需要重新检查 Host 加载、来电、接听和音频。

## 许可证与来源

按 GPL-3.0-only 发布，见 [LICENSE](LICENSE) 与 [第三方来源](THIRD_PARTY_NOTICES.md)。QQ、NapCat、Electron、AVSDK 及 AI 模型由使用者自行准备，并遵守对应软件和服务条款。
