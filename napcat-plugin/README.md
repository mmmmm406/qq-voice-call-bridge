# NapCat 端插件

这一边是“接电话的人”：它运行在 NapCat 里，负责看到 QQ 原生私聊语音来电、启动 AVSDK Host、拿到音频，并把通话状态传给配套的 OneBot 插件。

如果你只想知道怎么安装，先做下面三步：

1. 把整个 `napcat-plugin/` 文件夹复制到 NapCat 的插件目录。
2. 在 NapCat 中启用 `qq-voice-call`，填好 OneBot 反向 WebSocket 和 Token。
3. 先启动 OneBot，再重启 NapCat，确认状态显示已连接。

## 放到哪里

必须复制整个目录，目标目录名保持为 `qq-voice-call`：

```text
<NapCat.Shell>/napcat/plugins/qq-voice-call
```

不要只复制 `index.mjs`。`host.cjs`、`host-preload.cjs`、`host.html`、`host-bootstrap/` 和 capability 模块都是运行时的一部分。

本插件不包含 QQ、NapCat、Electron 或 AVSDK 本体；这些程序需要按自己的版本和安装方式准备。

## 连接 OneBot

插件会读取 NapCat 已启用的反向 WebSocket 设置，并把通话连接放到专用路径：

```text
普通消息：ws://<ONEBOT_HOST_IP>:6199/ws
语音通话：ws://<ONEBOT_HOST_IP>:6199/qq-voice-call
```

两条连接使用同一个 Bearer Token。OneBot 还没有发送 `activate` 之前，插件不会注册 AVSDK Listener，也不会自动接听来电。

### 同一台机器

把 `<ONEBOT_HOST_IP>` 换成 `127.0.0.1` 即可。

### 虚拟机

NapCat 在虚拟机里时，把 `<ONEBOT_HOST_IP>` 换成虚拟机能够访问到的 OneBot 宿主地址，并检查宿主防火墙和虚拟机网络模式。真实 IP、用户名、绝对路径和 Token 不要写进公开仓库。

## AVSDK Host 路径

当前代码优先适配会通过 `NAPCAT_BOOTMAIN` 分流的 NapCat Linux launcher。插件会在独立 Electron 进程中加载 `host.cjs`，不会启动第二套普通 NapCat；`host-bootstrap/` 必须随插件一起保留。

默认 AVSDK 文件位置是：

```text
/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so
```

如果 QQ 安装位置不同，请在插件配置的 `avHost.qqExecutable`、`avHost.avsdkPath` 中填写当前机器的绝对路径；只有确实需要时才填写 `avHost.loaderPath`。这些路径只属于本机配置，不要提交到 GitHub。

## NapCat 4.18.13 白名单

部分 NapCat Core 版本默认只显示官方插件。仓库提供的脚本默认只读扫描，确认只找到一个候选文件且状态为 `[patchable]` 后，才使用 `--apply` 修改：

```bash
cd <NapCat.Shell>/napcat/plugins/qq-voice-call
python3 patch_napcat_voice_whitelist.py --root <NapCat.Shell 的绝对路径>
python3 patch_napcat_voice_whitelist.py --root <NapCat.Shell 的绝对路径> --apply
```

脚本会在原文件旁创建带时间戳的备份。扫描到多个候选文件时应停止，并用 `--file` 指定已经人工核对过的唯一文件；不要手工修改未知的 Loader。

回滚时使用脚本输出的备份路径：

```bash
python3 patch_napcat_voice_whitelist.py \
  --root <NapCat.Shell 的绝对路径> \
  --rollback <备份文件绝对路径>
```

如果你的版本确实存在物理 `loadNapCat.js`，可以先只读检查旧版兼容脚本：

```bash
python3 patch_qq_av_host_loader.py --root <NapCat 或 QQ 根目录>
```

当前 launcher 已经提供 `NAPCAT_BOOTMAIN` 时通常不需要这个脚本；扫描结果不确定就先停下来，不要覆盖 Loader。

## 能做什么，不能做什么

- 已验证的原生控制是 `accept`。
- `reject`、`hangup`、`mute`、`unmute` 在没有当前 QQ/AVSDK 版本可验证契约时会返回 `unsupported`，不会猜测原生命令。
- 只支持 QQ 私聊原生语音来电；不承诺主动拨打、群语音、视频或多通电话。
- 诊断功能只返回白名单摘要，不上传 AVSDK 原始对象、Token、QQ 房间或会话数据。

## 排查顺序

1. 看 NapCat 插件是否启用，以及是否已经连接 `/qq-voice-call`。
2. 看 OneBot 是否先启动并发送了 `activate`。
3. 检查 Token、主机地址和端口是否与 OneBot 完全一致。
4. 再检查 QQ/AVSDK 路径、Electron Host 和本机音频设备。

NapCat、QQ、Electron、launcher 或 AVSDK 升级后，必须重新验证 Host 加载、接听信令和音频路由。静态 Node 测试不能替代真实 QQ 电话复测。

## 测试

在包含 Node.js 22 或兼容版本的环境中运行：

```bash
node --test tests/*.test.*
```

## 许可证

本插件按 `GPL-3.0-only` 发布。QQ、NapCat、Electron 和 QQ AVSDK 不随本仓库发布；使用者需要自行遵守对应项目、软件和服务条款。上游信令来源见仓库根目录的 `THIRD_PARTY_NOTICES.md`。
