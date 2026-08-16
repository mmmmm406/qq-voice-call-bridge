# OneBot 端插件

这是帮助 OneBot 增加 QQ 私聊原生语音通话能力的一侧插件。它接收 NapCat 传来的来电和音频状态，按准入规则决定是否处理，再把 ASR、TTS、声纹和文本模型串起来。

如果你的 OneBot 宿主已经具备本文后面列出的运行时、音频和模型接口，安装这部分后就可以接入 QQ 语音通话；NapCat 端插件则负责 QQ/AVSDK 一侧，两端需要配套使用。

本目录有两个部分：

- `plugins/qq_voice_call/`：必须安装的通话桥和电话对话运行时。
- `plugins/astrbot_plugin_qq_voice_call/`：可选的 AstrBot 工具插件，让模型能够查询状态、接听、拒接和挂断。

## 放到哪里

把 `plugins/qq_voice_call/` 复制到你的 OneBot 宿主插件目录，例如：

```text
<OneBot 宿主>/plugins/qq_voice_call
```

如果使用 AstrBot，再把 `plugins/astrbot_plugin_qq_voice_call/` 复制到 AstrBot 的插件目录。两者都来自同一份公开版，但 AstrBot 工具不是通话桥的必需组件。

## 先确认你的宿主能不能接

这里有一个容易被忽略的事实：**OneBot 是消息协议，不等于每个 OneBot 实现都有相同的运行时接口。**当前插件直接使用宿主已有的能力，因此不是“任意 OneBot 下载后即插即用”的通用包。

宿主至少要有：

1. `onebot.logger.logger`，并提供 `info`、`warning`、`error` 等日志方法。
2. `onebot.runtime`，能够注册、获取和注销通话运行时、电话对话服务和文本模型服务。
3. 一个 `aiohttp` WebSocket 服务，把 `/qq-voice-call` 路由接到这个插件，并使用与普通反向 WS 相同的 Bearer Token 鉴权。
4. 能处理 `activate`、`deactivate`，以及 NapCat 发来的 `invite_received`、`connected`、`ended`、`error`、`status` 和诊断摘要。

如果你的宿主命名不同，请在宿主适配层实现等价接口。不要直接改通话状态机里的隐式调用，否则后续更新很难合并。

## 只想先试通话桥

建议先不要一上来打开 AI 对话：

1. 复制 `config.example.json`，把它作为宿主实际配置的起点。
2. 保持 `enabled` 和 `dialogue.enabled` 的默认关闭状态，先确认 WebSocket、来电状态和准入规则能跑通。
3. 配好 OneBot 的 `/qq-voice-call` 路由后，再安装并启动 NapCat 端插件。
4. 能稳定看到来电和连接状态后，再逐项配置音频设备、ASR、TTS、声纹和模型。

这样出问题时能先判断是“电话没接上”，还是“电话接上了但 AI 服务没准备好”。

## 配置要点

- `call.allow_users` / `call.deny_users` 使用 QQ 号字符串。空列表不等于允许所有人，仍会经过宿主自己的准入策略。
- `dialogue.enabled` 默认关闭。打开前要先准备 ASR、TTS、音频输入/输出设备、声纹目录和模型路径。
- `dialogue.audio.input_device_index`、`output_device_index` 必须填写目标宿主实际的设备编号。
- `dialogue.sensevoice`、`dialogue.voiceprint` 里的模型路径由使用者填写；仓库不包含模型权重。
- `dialogue.llm` 和 `dialogue.tts` 默认继承宿主的模型与语音配置，具体名称以宿主实现为准。
- 通话 transcript、总结和声纹登记属于运行数据，应放在宿主自己的数据目录，并加入 Git 忽略。

示例配置里没有真实设备编号、模型路径、Token 或个人账号；填写后的配置不要提交回 GitHub。

## 控制语义

- `qq_voice_call_accept` / `qq_voice_call_reject` 只表示当前来电的接听决策。
- `qq_voice_call_hangup` 会绑定当前电话回复的播放收尾；返回 `locally_ended` 只说明 OneBot 本地媒体已经停止。
- NapCat 端尚未确认的原生动作会返回 `unsupported`，宿主不能把提交状态伪装成 QQ 已执行。

## 音频和模型依赖

如果启用完整电话 AI，对话运行时还需要宿主提供：

- `utils.gpt_model.file_lock.cross_process_lock`；
- `utils.voice_input` 的 SenseVoice 结果解析；
- `utils.audio_handle.my_tts.MY_TTS`、`utils.config.Config`；
- `utils.sv.SV_Manager`、`utils.sv_3dspeaker.SV_3DSpeaker`；
- 宿主自己的 `get_qq_ai_service()` 文本模型服务；
- 运行时可用的 PyAudio、FunASR、Torch、CAM++/3D-Speaker 和 Voicemeeter（如果启用对应功能）。

这些依赖来自宿主项目，本仓库不会重复打包。缺少其中一项时，通话桥可能仍能连接，但完整 AI 对话不会正常工作。

## 排查顺序

1. 先看 OneBot 是否启动了 `/qq-voice-call` WebSocket 路由。
2. 再看 Token、端口和 NapCat 地址是否一致。
3. 确认 NapCat 已发送 `hello`/状态，OneBot 也已经发送 `activate`。
4. 最后再检查 ASR、TTS、设备编号、声纹模型和文本模型服务。

## 宿主测试

本目录没有复制依赖宿主私有 `utils` 的完整测试宿主。安装到目标 OneBot 后，至少执行：

```bash
python -m py_compile plugins/qq_voice_call/*.py plugins/astrbot_plugin_qq_voice_call/main.py
```

然后运行宿主已有的 OneBot/音频/模型测试，最后做一次真实私聊电话复测。

## 许可证

本插件按 `GPL-3.0-only` 发布。上游来源和未包含的第三方运行时见仓库根目录的 `THIRD_PARTY_NOTICES.md`。
