# NapCat 端插件

这一端负责 QQ/AVSDK：发现 QQ 私聊原生语音来电、启动独立的 AVSDK Host、收发通话音频，并把状态交给 OneBot 端。

它不能单独完成 AI 对话。OneBot 端负责准入、模型、ASR、TTS 和通话运行时；两端必须来自同一个发布版本。

## 安装前确认

开始前请确认：

1. NapCat 和 QQ 已经可以正常登录。
2. NapCat 使用的 Core/launcher 版本允许第三方插件加载。
3. OneBot 已经启动，且虚拟机能够访问 OneBot 的监听地址和端口。
4. 你已经准备好普通反向 WebSocket 的地址、端口和 Bearer Token。
5. 如果 NapCat 在虚拟机中，虚拟机网络不是只允许访问本机回环地址。

当前公开版优先适配通过 NAPCAT_BOOTMAIN 分流的 NapCat Linux launcher。默认 AVSDK 路径也是 Linux 路径；其他平台或其他 launcher 需要先核对路径和加载方式。

## 第一步：复制完整目录

必须复制整个 napcat-plugin/ 目录，目标目录名保持为 qq-voice-call：

~~~text
<NapCat.Shell>/napcat/plugins/qq-voice-call
~~~

不要只复制 index.mjs。下面这些文件都是运行时的一部分：

- host.cjs、host-preload.cjs、host.html；
- host-bootstrap/；
- host-capabilities.cjs、service-capabilities.cjs、static-artifact-report.cjs；
- protocol.mjs 和三个诊断/协议测试所依赖的模块。

本插件不包含 QQ、NapCat、Electron 或 QQ AVSDK 本体，使用者需要自行准备并遵守对应软件和服务条款。插件运行时不需要在插件目录执行 npm install。

## 第二步：配置与 OneBot 连接

### 推荐方式：复用 NapCat 普通反向 WS 配置

插件会读取 NapCat 已启用的普通反向 WebSocket 客户端。普通消息和 QQ 电话共用地址、端口和 Token，只把路径区分开：

~~~text
普通消息：ws://<ONEBOT_HOST_IP>:6199/ws
QQ 电话：ws://<ONEBOT_HOST_IP>:6199/qq-voice-call
~~~

推荐先在 NapCat 的普通反向 WS 设置中确认：

1. 客户端已启用。
2. 地址、端口和 Token 正确。
3. 普通路径是 /ws，或者至少存在一个可用的反向 WS 客户端。

在这种方式下，插件配置中的 serverUrl 和 token 可以留空，插件会从 NapCat 的普通反向 WS 配置推导电话地址和 Token。这样可以减少维护两份令牌的机会。

### 显式方式：填写插件自己的连接字段

如果你的 NapCat 版本没有可读取的普通反向 WS 配置，可以在插件配置中填写：

~~~json
{
  "serverUrl": "ws://<ONEBOT_HOST_IP>:6199/ws",
  "token": "<与OneBot相同的Token>",
  "voicePath": "/qq-voice-call",
  "reconnectIntervalMs": 5000
}
~~~

字段说明：

| 字段 | 是否必须 | 说明 |
| --- | --- | --- |
| serverUrl | 通常不需要 | 普通反向 WS 的地址；插件会把路径改成 voicePath |
| token | 通常不需要 | 只有普通 WS 配置没有 Token 时才填写 |
| voicePath | 否 | 默认是 /qq-voice-call，不建议改动 |
| reconnectIntervalMs | 否 | 重连间隔，默认 5000 毫秒 |

不要把填写过 Token 的配置文件提交回 GitHub，也不要在 issue 或截图中展示它。

### 配置保存位置

插件配置由 NapCat 插件宿主管理，公开仓库不规定你的 NapCat 配置文件绝对路径。能看到插件设置界面的版本，直接在插件管理页面保存；没有设置界面的版本，使用 NapCat 为该插件生成的本地配置文件，不要修改仓库里的示例文件。

## 第三步：检查 AVSDK Host 路径

插件会在独立 Electron 进程中加载 host.cjs，不会启动第二套普通 NapCat。host-bootstrap/ 必须随插件一起保留。

默认配置：

~~~text
QQ 可执行文件：/opt/QQ/qq
AVSDK 文件：/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so
~~~

如果 QQ 安装位置不同，在本机插件配置中调整：

| 配置字段 | 作用 |
| --- | --- |
| avHost.qqExecutable | QQ 可执行文件的绝对路径 |
| avHost.avsdkPath | libAVSDKPlugin.so 的绝对路径 |
| avHost.loaderPath | 只有需要兼容旧版 loader 时才填写 |
| avHost.controlHost | 仅允许 127.0.0.1 或 ::1 |
| avHost.controlPort | Host 控制端口，默认 6110 |
| avHost.hostPort | Host 页面端口，默认 6111 |

这些是本机路径和本机端口，不要照抄其他人的路径，也不要提交到公开仓库。controlHost 不是 OneBot 地址，不能填虚拟机宿主机 IP。

## 第四步：启用插件并启动

1. 先启动 OneBot，让 /qq-voice-call 路由已经监听。
2. 在 NapCat 插件管理中启用 qq-voice-call。
3. 如果插件配置已经存在，保存后重启或重新加载插件。
4. 如果 NapCat 使用独立 QQ/AVSDK Host，确认 Host 进程能够读取 QQ 和 AVSDK 文件。
5. 等待 OneBot 和 NapCat 两端都显示桥接成功，再发起测试来电。

成功时通常能看到：

- NapCat 日志：连接到 /qq-voice-call。
- OneBot 日志：虚拟机 NapCat 通话桥已连接。
- OneBot 状态：bridge_connected 为真；收到 activate 后 runtime_active 为真。

只看到普通 /ws 已连接，不代表 QQ 电话桥已经连接。

## NapCat 4.18.13 白名单

部分 NapCat Core 版本默认只显示官方插件。仓库提供的脚本默认只读扫描，确认只找到一个候选文件且状态为 [patchable] 后，才使用 --apply：

~~~powershell
cd <NapCat.Shell>\napcat\plugins\qq-voice-call
python patch_napcat_voice_whitelist.py --root <NapCat.Shell的绝对路径>
python patch_napcat_voice_whitelist.py --root <NapCat.Shell的绝对路径> --apply
~~~

脚本会在原文件旁创建带时间戳的备份。扫描到多个候选文件时应停止，并用 --file 指定已经人工核对过的唯一文件；不要手工修改未知的 Loader。

回滚使用脚本输出的备份路径：

~~~powershell
python patch_napcat_voice_whitelist.py --root <NapCat.Shell的绝对路径> --rollback <备份文件绝对路径>
~~~

如果你的版本确实存在物理 loadNapCat.js，可以先只读检查旧版兼容脚本：

~~~powershell
python patch_qq_av_host_loader.py --root <NapCat 或 QQ 根目录>
~~~

当前 launcher 已经提供 NAPCAT_BOOTMAIN 时通常不需要这个脚本；扫描结果不确定就先停下来，不要覆盖 Loader。

## 虚拟机网络示例

同一台机器：

~~~text
NapCat -> ws://127.0.0.1:6199/qq-voice-call -> OneBot
~~~

NapCat 在虚拟机，OneBot 在宿主机：

~~~text
NapCat 虚拟机 -> ws://<ONEBOT_HOST_IP>:6199/qq-voice-call -> OneBot 宿主机
~~~

虚拟机部署时按顺序检查：

1. 从虚拟机测试宿主机 6199 端口是否可达。
2. OneBot 是否监听了虚拟机可访问的网卡，而不是只监听 127.0.0.1。
3. 宿主机防火墙是否只放行可信虚拟机网段。
4. 两端 Token 是否完全一致。
5. 路径是否是 /qq-voice-call，而不是普通消息的 /ws。

不要把真实 IP、用户名、绝对路径和 Token 写进公开仓库。本仓库只使用占位符保留虚拟机部署经验。

## 能做什么，不能做什么

- 已验证的原生控制是 accept。
- reject、hangup、mute、unmute 在没有当前 QQ/AVSDK 版本可验证契约时会返回 unsupported，不会猜测原生命令。
- 只支持 QQ 私聊原生语音来电；不承诺主动拨打、群语音、视频或多通电话。
- 诊断功能只返回白名单摘要，不上传 AVSDK 原始对象、Token、QQ 房间或会话数据。

## 排查顺序

1. 先看插件是否真的启用，以及 NapCat 是否读取到了插件目录。
2. 再看普通反向 WS 配置是否启用、Token 是否存在。
3. 查看 OneBot 是否注册了 /qq-voice-call 路由。
4. 查看两端日志中的地址、路径和认证错误。
5. 最后再检查 QQ/AVSDK 路径、Electron Host 和本机音频设备。

NapCat、QQ、Electron、launcher 或 AVSDK 升级后，必须重新验证 Host 加载、接听信令和音频路由。静态 Node 测试不能替代真实 QQ 电话复测。

## 测试

在包含 Node.js 22 或兼容版本的环境中运行：

~~~powershell
node --test tests/*.test.*
~~~

## 许可证

本插件按 GPL-3.0-only 发布。QQ、NapCat、Electron 和 QQ AVSDK 不随本仓库发布；使用者需要自行遵守对应项目、软件和服务条款。上游信令来源见仓库根目录的 THIRD_PARTY_NOTICES.md。
