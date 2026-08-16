# QQ Voice Call Bridge

一个帮助 OneBot 实现 QQ 私聊原生语音通话的开源配套项目。

它把 NapCat 的 QQ 通话能力接入 OneBot，让 OneBot 可以接收 QQ 语音来电、按规则处理通话，并在宿主具备相应音频与模型服务时进行语音对话。

可以把它理解成两个人一起接电话：**NapCat 端负责接起 QQ 电话、拿到音频并把电话状态传出来；OneBot 端负责决定要不要接听、把对方的话转成文字、生成回答，再把声音送回电话。**

所以，这个仓库里有两个需要配合使用的插件。它们不是同一个插件的两个安装包，也不能只装其中一边就完成通话。

## 先看安装位置

| 仓库里的文件夹 | 放到哪里 | 主要负责什么 |
| --- | --- | --- |
| `napcat-plugin/` | NapCat 的插件目录 | QQ/AVSDK 侧的来电、音频和通话信令 |
| `onebot-plugin/plugins/qq_voice_call/` | OneBot 宿主的插件目录 | 通话桥、准入规则和对话运行时 |
| `onebot-plugin/plugins/astrbot_plugin_qq_voice_call/` | AstrBot 插件目录（可选） | 给模型提供接听、拒接、挂断和状态查询工具 |

两端通过 `/qq-voice-call` 这条反向 WebSocket 通道连接，并使用同一个 Bearer Token。请始终从同一个发布版本成套安装，不要把公开版的一端和旧的私有构建混在一起。

## 这个仓库适合什么情况

它主要面向已经有 OneBot、NapCat 和自己的音频/模型服务，并希望增加 QQ 私聊语音通话的人。NapCat 端可以按 NapCat 插件安装；OneBot 端目前是**带宿主接口约定的实现**，不是所有 OneBot 实现都能复制后直接运行。

OneBot 宿主至少要能提供：

- 一个可以接收 `/qq-voice-call` 反向 WebSocket 的服务；
- 日志、运行时注册和获取接口；
- 如果要做完整 AI 通话，还要有 ASR、TTS、声纹、音频设备和文本模型服务。

这些接口来自宿主项目，本仓库没有偷偷复制一套“看起来能跑、实际不接真实服务”的替代实现。具体接口和依赖列在 [OneBot 端说明](onebot-plugin/README.md) 里。

## 最短安装路线

### 普通单机

NapCat 和 OneBot 在同一台机器时，连接关系大致是：

```text
NapCat -> ws://127.0.0.1:6199/qq-voice-call -> OneBot 宿主
```

1. 先在 OneBot 宿主中安装 `onebot-plugin/plugins/qq_voice_call/`。
2. 以 `onebot-plugin/config.example.json` 为起点创建实际配置。先只打开通话桥，设备编号、模型路径和令牌按本机情况填写。
3. 如果使用 AstrBot，再额外安装 `astrbot_plugin_qq_voice_call/`；它是可选的，不影响通话桥本身。
4. 把整个 `napcat-plugin/` 复制到 NapCat 的插件目录，目录名保持为 `qq-voice-call`。
5. 在 NapCat 插件配置中填写 OneBot 的反向 WebSocket 地址和同一个 Token。插件会把普通路径改成 `/qq-voice-call`。
6. 先启动 OneBot，再启动或重启 NapCat。第一次只验证“来电出现、允许接听、能听到声音、能播出声音”这条最短链路。

### NapCat 在虚拟机里

虚拟机只改变地址，不改变安装方式：

```text
NapCat 虚拟机 -> ws://<ONEBOT_HOST_IP>:6199/qq-voice-call -> OneBot 宿主机
```

把 `<ONEBOT_HOST_IP>` 换成“虚拟机可以访问到的 OneBot 宿主地址”，并确保防火墙只允许可信网络访问。不要把真实 IP、用户名、绝对路径、Token 或 QQ 号写进公开文档、截图和 issue。这个仓库保留了虚拟机部署作为经验示例，但所有具体信息都使用占位符。

## 配置时记住三件事

1. `onebot-plugin/config.example.json` 是示例，不是可以直接上线的完整配置。它默认关闭 AI 对话，模型路径和设备编号都是占位符。
2. Token 只放在本机配置中，不要提交到 Git、截图、日志或公开 issue。通话桥尽量只监听回环地址或可信局域网，不要直接暴露到公网。
3. 录音、transcript、总结、声纹、诊断捕获和运行日志都是本机数据，不属于仓库内容；请把它们放在宿主自己的数据目录，并加入 Git 忽略。

## 常见困惑

### 为什么要装两个插件？

因为 NapCat 和 OneBot 本来就是两个不同的程序。NapCat 能看到 QQ 的来电和 AVSDK，OneBot 能接入你的规则、模型和音频服务；任何一边都不能替另一边完成全部工作。

### 为什么不是任意 OneBot 都能用？

“OneBot”描述的是消息协议，不代表每个实现都提供相同的运行时、音频和模型接口。本项目目前直接使用宿主已有的 `onebot.runtime`、音频服务和文本模型服务，所以需要按 [宿主契约](onebot-plugin/README.md) 对照检查。

### 本地挂断是不是 QQ 对端已经挂断？

不一定。当前版本能可靠收口本地媒体和播放状态；某些 QQ/AVSDK 版本还没有经过验证的原生拒接、挂断、静音命令，因此这些动作会明确返回 `unsupported`，不会假装已经在 QQ 端执行。

## 进阶资料

- [NapCat 端安装与 AVSDK Host 说明](napcat-plugin/README.md)
- [OneBot 端安装、配置和宿主接口](onebot-plugin/README.md)
- [许可证](LICENSE)
- [第三方来源与运行时说明](THIRD_PARTY_NOTICES.md)

## 当前范围

- 支持 QQ 私聊原生语音来电。
- 不承诺主动拨打、群语音、视频通话或并发多通电话。
- NapCat、QQ、Electron 或 AVSDK 升级后，需要重新验证 Host 加载、接听信令和音频路由。

## 许可证

本仓库按 `GPL-3.0-only` 发布。你可以使用、修改和分发本项目，也可以将其用于收费项目；分发修改版或衍生作品时，应继续遵守 GPL-3.0 的条款，保留版权与许可证信息，并向接收者提供相应源代码。

NapCat 端的信令映射参考了 GPL-3.0-only 上游实现，因此当前版本以 GPL-3.0-only 作为整个仓库的许可证。完整条款见 [LICENSE](LICENSE)，代码来源和未包含的第三方运行时见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

QQ、NapCat、Electron、AVSDK、模型和音频运行时均不包含在本仓库中，使用者需要自行安装并遵守各自的软件和服务条款。

## 测试

NapCat 端：

```bash
cd napcat-plugin
node --test tests/*.test.*
```

OneBot 端的完整测试需要目标宿主提供 `onebot` 和 `utils` 模块。仓库不会伪造一套测试宿主来掩盖真实依赖；至少应先做 Python 语法检查，再在目标 OneBot 宿主中执行原有测试和一次真实私聊电话复测。
