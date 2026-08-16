# QQ 语音通话桥接

一个帮助 OneBot 接入 QQ 私聊原生语音通话的开源配套项目。

它把 NapCat 看到的 QQ 来电和音频接入 OneBot，让 OneBot 可以接收 QQ 语音来电、按规则决定是否接听，并在宿主准备好 ASR、TTS、声纹和文本模型服务后进行语音对话。

可以把整套系统理解成两端配合接电话：

- NapCat 端负责 QQ/AVSDK：发现来电、维持通话、收发音频。
- OneBot 端负责业务逻辑：准入判断、接听决策、语音识别、模型回复和语音合成。

两端必须使用同一发布版本配套安装。只安装其中一端，不能完成 QQ 语音通话。

## 先判断是否适合你

这个项目适合已经具备以下条件的使用者：

1. 已经有可以运行 NapCat 的 QQ 环境。
2. 已经有 OneBot 宿主，并且宿主允许插件注册运行时和 WebSocket 路由。
3. 能够让 NapCat 所在机器访问 OneBot 的监听地址和端口。
4. 如果要使用 AI 对话，宿主还要有可用的音频设备、ASR、TTS、声纹和文本模型服务。

这里的 OneBot 端不是脱离宿主就能独立运行的通用程序。OneBot 是消息协议，不同宿主提供的运行时和音频接口并不完全相同；请先阅读 [OneBot 端安装说明](onebot-plugin/README.md) 的“宿主接入要求”。

## 文件夹分别放在哪里

| 仓库中的文件夹 | 复制到哪里 | 负责什么 |
| --- | --- | --- |
| napcat-plugin/ | NapCat 的插件目录 | QQ/AVSDK 来电、音频和通话信令 |
| onebot-plugin/plugins/qq_voice_call/ | OneBot 宿主的插件目录 | 通话桥、准入规则和电话运行时 |
| onebot-plugin/plugins/astrbot_plugin_qq_voice_call/ | AstrBot 插件目录（可选） | 给模型提供查询状态、接听、拒接和挂断工具 |

仓库中的 onebot-plugin/config.example.json 只是配置模板，不是已经填好的真实配置。不要把填过 Token、QQ 号、设备编号或模型路径的配置重新提交到 GitHub。

## 安装前准备

### 两端都要准备的信息

请先记录以下信息，安装时会用到：

- OneBot 监听地址：同机使用 127.0.0.1，虚拟机使用 OneBot 宿主对虚拟机可达的地址。
- OneBot 反向 WebSocket 端口，例如 6199。
- 普通反向 WebSocket 路径，通常是 /ws。
- 反向 WebSocket Bearer Token。两端必须使用同一个 Token。
- 来电准入范围。建议第一次只允许自己的测试 QQ 号。

不要把真实 IP、Token、QQ 号、用户名、绝对路径、录音、日志或模型目录写进公开 README、截图、issue 或提交记录。

### 只接通电话，还是要 AI 对话

建议先按“只接通电话”模式验证网络和信令，再打开 AI 对话：

| 模式 | OneBot 配置重点 | 用途 |
| --- | --- | --- |
| 只接通电话 | enabled: true、dialogue.enabled: false、call.auto_accept_private: true | 先验证来电、接听和音频链路 |
| AI 决定是否接听 | dialogue.enabled: true、call.auto_accept_private: false | 由当前私聊模型调用接听或拒接工具 |

无论哪种模式，都建议先填写 call.allow_users，不要在第一次测试时放开所有来电。

## 完整安装流程

### 第一步：安装 OneBot 端

1. 在 OneBot 宿主的插件目录中创建或确认目标目录：

   onebot-plugin/plugins/qq_voice_call/

2. 将本仓库的 onebot-plugin/plugins/qq_voice_call/ 整个目录复制进去。不要只复制某一个 .py 文件。
3. 在 OneBot 宿主使用的 Python 环境中安装桥接层直接依赖：

   ~~~powershell
   python -m pip install -r <公开仓库目录>\onebot-plugin\requirements.txt
   ~~~

   如果宿主使用虚拟环境，请使用宿主实际启动 OneBot 的那个 Python 解释器。
4. 将 onebot-plugin/config.example.json 复制为宿主实际使用的插件配置。宿主框架的配置文件名可能不同，关键是把这份 JSON 对象传给 qq_voice_call 插件。
5. 第一次测试可以使用下面的最小配置：

   ~~~json
   {
     "enabled": true,
     "call": {
       "auto_accept_private": true,
       "allow_users": ["<你的测试QQ号>"],
       "deny_users": []
     },
     "dialogue": {
       "enabled": false
     }
   }
   ~~~

   dialogue.enabled 关闭时，系统只验证电话桥和原生接听；不要先填写不存在的模型路径。

### 第二步：把通话 WebSocket 路由接到运行时

这个仓库的 OneBot 插件会注册 QQVoiceCallRuntime，但不会替你的宿主创建 HTTP/WebSocket 服务。宿主需要在已经鉴权的反向 WebSocket 服务中增加一条 /qq-voice-call 路由。

路由必须复用普通反向 WebSocket 的 Bearer Token 鉴权。鉴权通过、WebSocket 握手完成后，把连接交给运行时的 handle_connection：

~~~python
runtime = get_qq_voice_call_runtime()

if runtime is None:
    # 插件未启用，返回宿主自己的错误或关闭连接
    ...

await runtime.handle_connection(
    websocket,
    remote_address=request.remote or "",
    self_id=request.headers.get("X-Self-ID", ""),
)
~~~

这是宿主适配示意，不是可以原样复制到所有 OneBot 的完整服务器代码。你的宿主如果使用不同的路由注册方式，只要保证以下契约即可：

- 路径是 /qq-voice-call。
- 使用与普通反向 WS 相同的 Bearer Token。
- 将已完成握手的 aiohttp WebSocket 对象传给 handle_connection。
- 连接断开时让 handle_connection 正常返回，插件会执行本地清理。

OneBot 启动后，日志中应能看到插件已加载并等待 NapCat 连接。此时还没有电话是正常的。

### 第三步：安装 NapCat 端

1. 将本仓库的 napcat-plugin/ 整个目录复制到：

   ~~~text
   <NapCat.Shell>/napcat/plugins/qq-voice-call
   ~~~

2. 目录名必须是 qq-voice-call。不要只复制 index.mjs；host.cjs、host-preload.cjs、host.html、host-bootstrap/ 和 capability 模块都是运行时文件。
3. 在 NapCat 中启用 qq-voice-call 插件。
4. NapCat 端会优先读取已经启用的普通反向 WebSocket 配置，并把路径自动改成 /qq-voice-call。最简单的做法是：

   - 普通反向 WS 配置保持启用。
   - 普通路径使用 /ws。
   - 普通 WS 的地址、端口和 Token 填写正确。
   - NapCat 插件的 serverUrl 和 token 可以留空，让它复用普通反向 WS 配置。

5. 如果你的 NapCat 版本不能提供普通反向 WS 配置，才在插件配置中显式填写：

   ~~~json
   {
     "serverUrl": "ws://<ONEBOT_HOST_IP>:6199/ws",
     "token": "<与OneBot相同的Token>",
     "voicePath": "/qq-voice-call",
     "reconnectIntervalMs": 5000
   }
   ~~~

   serverUrl 可以写普通的 /ws 路径，插件会在实际连接时替换成 /qq-voice-call。不要把 Token 写进仓库文件。
6. 先启动 OneBot，再重启 NapCat。NapCat 连接成功后，OneBot 日志应出现“通话桥已连接”，NapCat 日志应出现已连接到 /qq-voice-call。

### 第四步：如果插件没有显示

部分 NapCat Core 版本默认只显示官方插件。仓库提供的白名单脚本会先只读扫描，再由你确认后修改：

~~~powershell
cd <NapCat.Shell>\napcat\plugins\qq-voice-call
python patch_napcat_voice_whitelist.py --root <NapCat.Shell的绝对路径>
python patch_napcat_voice_whitelist.py --root <NapCat.Shell的绝对路径> --apply
~~~

扫描到多个候选文件时不要继续执行，先用 --file 指定已经人工确认的唯一文件。脚本会生成备份，回滚方法见 [NapCat 端说明](napcat-plugin/README.md)。

## 虚拟机部署

### 同一台机器

~~~text
NapCat -> ws://127.0.0.1:6199/qq-voice-call -> OneBot
~~~

### NapCat 在虚拟机，OneBot 在宿主机

~~~text
NapCat 虚拟机 -> ws://<ONEBOT_HOST_IP>:6199/qq-voice-call -> OneBot 宿主机
~~~

只需要改变 ONEBOT_HOST_IP，两端插件的目录和协议不变。请同时检查：

1. 虚拟机网络模式允许访问 OneBot 宿主机。
2. OneBot 监听地址不是只绑定 127.0.0.1。
3. 宿主机防火墙允许来自虚拟机网段的 6199 端口。
4. Token、端口和路径与普通反向 WS 配置一致。
5. 不要为了测试把 6199 暴露到公网。

## 第一次通话怎么验证

建议按下面顺序检查，不要一开始就打开所有 AI 功能：

1. OneBot 插件加载成功，/qq-voice-call 路由正在监听。
2. NapCat 插件已启用，并且桥状态为已连接。
3. 使用 call.allow_users 中的测试 QQ 发起一通私聊原生语音来电。
4. 只接通电话模式下，确认来电进入、电话被接听、双方能听到声音。
5. 如果声音正常，再打开 dialogue.enabled，逐项配置 ASR、TTS、设备和模型。
6. 最后再测试模型接听、插话、挂断和通话总结。

### 看到什么才算连接成功

- OneBot 日志：插件已就绪，随后显示 NapCat 通话桥已连接。
- NapCat 日志：连接到 /qq-voice-call，并收到 OneBot 的 activate。
- OneBot 状态：bridge_connected 为真，且 runtime_active 在收到 activate 后变为真。

只看到端口在监听，不能证明插件已经连通；要以日志和桥状态为准。

## 常见问题

### 普通消息能收，QQ 电话桥却不通

普通消息使用 /ws，电话使用 /qq-voice-call。请确认宿主不是只注册了普通路径，也请确认两条路径都使用相同的 Token。

### 来电没有自动接听

检查 call.auto_accept_private。如果它为 false，并且 dialogue.enabled 也为 false，系统不会凭空替你做接听决策；这属于安全的默认行为。

### AI 对话启动后没有声音

先确认桥已经连接，再检查 OneBot 进程实际使用的音频设备编号、ASR/TTS 模型路径和宿主依赖。桥连接成功不代表音频和模型服务已经准备好。

### 本地挂断后 QQ 对端仍显示通话中

当前版本可靠保证的是 OneBot 本地媒体和任务收口；部分 QQ/AVSDK 版本没有经过验证的远端挂断命令，因此状态可能暂时不同步。插件会返回 locally_ended 或 unsupported，不会把本地状态伪装成远端已挂断。

## 当前范围

- 支持 QQ 私聊原生语音来电。
- 不承诺主动拨打、群语音、视频通话、并发多通电话或多人重叠语音的精确分离。
- NapCat、QQ、Electron 或 AVSDK 升级后，需要重新验证 Host 加载、接听信令和音频路由。

## 许可证与第三方

本仓库按 GPL-3.0-only 发布。完整条款见 [LICENSE](LICENSE)，代码来源和未包含的运行时见 [第三方来源与声明](THIRD_PARTY_NOTICES.md)。

QQ、NapCat、Electron、AVSDK、ASR、TTS、声纹模型和文本模型均不包含在本仓库中，使用者需要自行安装并遵守相应软件和服务条款。

## 测试

NapCat 端：

~~~powershell
cd napcat-plugin
node --test tests/*.test.*
~~~

OneBot 端的完整测试需要目标宿主提供 onebot 和 utils 模块。至少先执行 Python 语法检查，再在目标宿主中运行原有测试并做一次真实私聊电话复测。
