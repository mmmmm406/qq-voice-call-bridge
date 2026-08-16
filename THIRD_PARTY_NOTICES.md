# 第三方来源与声明

## maibot-qq-voice-call

NapCat 端的 QQ AVSDK 信令状态机和部分事件映射参考：

<https://github.com/ClaudiaGardner/maibot-qq-voice-call>

上游项目声明为 GPL-3.0-only。本仓库保留 GPL-3.0-only，并在 `napcat-plugin/index.mjs`、`napcat-plugin/protocol.mjs` 中保留来源说明。这里没有重新分发上游项目之外的 QQ/AVSDK 二进制文件。

## NapCatQQ

插件运行在 NapCat 的插件宿主中，NapCat 项目地址：

<https://github.com/NapNeko/NapCatQQ>

NapCat、QQ、Electron 和 QQ AVSDK 不包含在本仓库中。使用者需要自行安装并遵守对应项目、软件和服务条款。

## 宿主依赖

OneBot 端的 `onebot`、ASR、TTS、声纹、模型和音频服务不是本仓库复制的第三方包。它们由使用者的 OneBot 宿主提供，具体接口见 `onebot-plugin/README.md`。
