# OneBot 端插件

这是帮助 OneBot 增加 QQ 私聊原生语音通话能力的一侧插件。

它接收 NapCat 传来的来电和通话状态，按准入规则决定是否处理，再把 ASR、TTS、声纹和文本模型串起来。NapCat 端负责 QQ/AVSDK 一侧；两端需要配套使用。

## 目录中有哪些内容

- plugins/qq_voice_call/：必须安装的通话桥和电话对话运行时。
- plugins/astrbot_plugin_qq_voice_call/：可选的 AstrBot 工具插件，让模型能够查询状态、接听、拒接和挂断。
- config.example.json：脱敏配置模板，默认关闭桥和 AI 对话。
- requirements.txt：通话桥自身的直接 Python 依赖；ASR、TTS、声纹和模型依赖由宿主提供。

## 宿主接入要求

OneBot 是消息协议，不等于每个 OneBot 实现都有相同的运行时接口。当前插件直接使用宿主已有的能力，因此不是“任意 OneBot 下载后即插即用”的通用包。

宿主至少要提供：

1. 日志对象：onebot.logger.logger，并提供 info、warning、error 等方法。
2. 运行时注册接口：onebot.runtime，能够注册、获取和注销 QQ 通话运行时及电话对话服务。
3. 已鉴权的 aiohttp WebSocket 服务，注册 /qq-voice-call 路由。
4. 与普通反向 WS 相同的 Bearer Token 鉴权。
5. 能把 NapCat 的 X-Self-ID 传给运行时，并保留连接来源地址用于状态展示。

如果你的宿主命名不同，请在宿主适配层实现等价接口。不要直接修改通话状态机里的隐式调用，否则后续更新很难合并。

## 第一步：复制插件并安装直接依赖

1. 把 plugins/qq_voice_call/ 整个目录复制到 OneBot 宿主的插件目录，例如：

   ~~~text
   <OneBot 宿主>/plugins/qq_voice_call
   ~~~

2. 使用 OneBot 实际启动时的 Python 解释器安装桥接层直接依赖：

   ~~~powershell
   python -m pip install -r <公开仓库目录>\onebot-plugin\requirements.txt
   ~~~

   requirements.txt 只有 aiohttp 和 loguru。不要因为安装了这两个包，就认为 ASR、TTS、Torch 或模型已经准备好。
3. 如果使用 AstrBot，再把 plugins/astrbot_plugin_qq_voice_call/ 复制到 AstrBot 的插件目录。它只是给模型增加电话工具，不是通话桥运行的必要条件。

## 第二步：准备 OneBot 插件配置

不同 OneBot 宿主的配置文件名和加载方式可能不同。本仓库只约定传给插件的 JSON 结构，不强行规定宿主必须叫 config.json。

第一次只验证电话桥时，建议使用以下最小配置：

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

字段含义：

| 字段 | 作用 | 第一次测试建议 |
| --- | --- | --- |
| enabled | 是否注册 QQ 通话运行时 | true |
| call.auto_accept_private | 允许名单中的私聊来电是否直接接听 | true，先验证原生通话 |
| call.allow_users | 允许处理的 QQ 号字符串列表 | 只填自己的测试号 |
| call.deny_users | 明确拒绝的 QQ 号字符串列表 | 留空 |
| dialogue.enabled | 是否启动电话 ASR/TTS/模型对话 | 先 false |

打开 AI 决策后，将 dialogue.enabled 改为 true，并把 call.auto_accept_private 改为 false。此时当前私聊模型需要调用 qq_voice_call_accept 或 qq_voice_call_reject；普通自然语言不会被当成接听决定。

配置示例中的 dialogue.enabled 默认关闭，模型路径和设备编号是占位符。填写后的配置不要提交回 GitHub。

## 第三步：注册 /qq-voice-call 路由

通话运行时本身不创建 HTTP 服务器。宿主需要在普通反向 WebSocket 服务中增加电话路径，并沿用原有鉴权。

请求处理顺序必须是：

1. 检查请求路径是否为 /qq-voice-call。
2. 按普通反向 WS 的规则检查 Authorization: Bearer Token。
3. 创建并完成 aiohttp WebSocket 握手。
4. 获取当前已注册的 QQVoiceCallRuntime。
5. 调用 runtime.handle_connection，直到连接断开。

下面是宿主适配示意：

~~~python
runtime = get_qq_voice_call_runtime()

if runtime is None:
    # 插件未启用，按宿主自己的方式返回错误
    ...

await runtime.handle_connection(
    websocket,
    remote_address=request.remote or "",
    self_id=request.headers.get("X-Self-ID", ""),
)
~~~

这段代码不能原样适配所有 OneBot。宿主可以使用自己的路由装饰器和鉴权中间件，但必须把已经握手完成的 aiohttp WebSocket 对象传给 handle_connection，并让连接断开后正常返回。

NapCat 连接后，运行时会先发送 hello_request；收到 NapCat 的 hello 后，再发送 activate。NapCat 在收到 activate 前不会注册 AVSDK Listener，也不会自动接听电话。

## 第四步：启动并确认桥接

推荐启动顺序：

1. 启动 OneBot，确认插件已加载。
2. 确认 /qq-voice-call 正在监听，并且 Bearer Token 鉴权有效。
3. 启动或重启 NapCat。
4. 等待两端日志显示连接成功，再发起 QQ 私聊原生语音来电。

成功标志：

- OneBot 日志：QQ 通话插件已就绪，随后出现虚拟机 NapCat 通话桥已连接。
- NapCat 日志：连接到 /qq-voice-call，并收到 activate。
- 运行时状态：bridge_connected 为真；收到 activate 后 runtime_active 为真。

“端口正在监听”只能说明服务器启动了，不能证明路径、Token 和运行时已经正确连接。

## 第五步：从桥接模式进入 AI 对话

桥接模式验证通过后，再按以下顺序打开 AI：

1. 确定电话输入设备和输出设备的索引。设备索引以 OneBot 进程实际看到的列表为准。
2. 填写 SenseVoice 和 VAD 模型路径。
3. 填写 TTS 配置，确认宿主的文本模型服务和语音服务可以独立工作。
4. 如需声纹，再填写 CAM++/3D-Speaker 模型和声纹目录。
5. 将 dialogue.enabled 改为 true。
6. 先进行一通短电话，确认识别、回复和 TTS 后，再打开总结、监听和更复杂的插话策略。

完整对话配置位于 dialogue 下，主要字段如下：

| 配置区域 | 作用 |
| --- | --- |
| dialogue.admission | 模型接听/拒接决策超时和忙线提示 |
| dialogue.audio | 输入、TTS 输出、采样率和本地路由 |
| dialogue.vad | 说话起止判断和最长语音时长 |
| dialogue.sensevoice | ASR 和 VAD 模型路径 |
| dialogue.voiceprint | 全局声纹目录、模型和阈值 |
| dialogue.llm | 模型配置继承方式、超时和最大回复长度 |
| dialogue.tts | TTS 配置继承方式和播放策略 |
| dialogue.summary | 通话总结以及写回原私聊的模板 |

这些模型、设备和数据目录属于宿主环境，仓库只提供结构示例，不提供模型权重。

## 控制工具的含义

如果安装了 AstrBot 工具插件，模型可以使用：

| 工具 | 含义 |
| --- | --- |
| qq_voice_call_status | 查询当前活动电话状态 |
| qq_voice_call_accept | 接听当前正在振铃的来电 |
| qq_voice_call_reject | 拒接当前正在振铃的来电 |
| qq_voice_call_hangup | 登记当前电话在本轮回复播放完成后本地结束 |

qq_voice_call_hangup 返回 locally_ended，只代表 OneBot 本地媒体和任务已停止，不代表 QQ 对端已经断线。NapCat 尚未确认的 reject、hangup、mute、unmute 动作会返回 unsupported，不能把提交状态说成 QQ 已经执行。

## 数据和安全

- Token 只放在本机配置或环境变量中，不放进 Git、截图、日志或 issue。
- transcript、通话总结、声纹登记、录音和诊断数据都是运行数据，应放在宿主数据目录。
- 通话桥建议只监听回环地址或可信局域网，不要直接暴露到公网。
- 不要把真实 IP、QQ 号、用户名、模型路径和音频设备编号提交到公开仓库。

## 常见问题

### 普通消息能收，QQ 电话桥不通

普通消息使用 /ws，电话使用 /qq-voice-call。检查宿主是否真的注册了第二条路由，并确认两条路径使用同一个 Bearer Token。

### OneBot 已连接，但 NapCat 没有 activate

检查运行时是否在收到 hello 后被正确获取，路由是否把 X-Self-ID 和已鉴权的 WebSocket 传给 handle_connection。

### 来电没有自动接听

桥接模式检查 call.auto_accept_private 和 allow_users。AI 模式检查 dialogue.enabled、dialogue.admission，以及模型是否调用了接听工具。

### 接通后没有识别或回复

先确认 bridge_connected 和 runtime_active，再检查 OneBot 进程的音频设备、ASR/TTS 模型路径、宿主依赖和文本模型服务。桥接成功不等于完整 AI 环境已就绪。

## 宿主测试

本目录没有复制依赖宿主私有 onebot 和 utils 的完整测试宿主。安装到目标 OneBot 后，至少执行：

~~~powershell
python -m py_compile plugins/qq_voice_call/*.py plugins/astrbot_plugin_qq_voice_call/main.py
~~~

然后运行宿主已有的 OneBot、音频和模型测试，最后做一次真实私聊电话复测。

## 许可证

本插件按 GPL-3.0-only 发布。上游来源和未包含的第三方运行时见仓库根目录的 THIRD_PARTY_NOTICES.md。
