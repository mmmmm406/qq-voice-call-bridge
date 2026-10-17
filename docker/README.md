# Docker 安装

本目录给 NapCat 语音插件提供独立的 Docker 安装入口。控制端和音频服务按你的机器人项目部署，容器启动包装只负责插件安装及 QQ/AVSDK Host 入口。

## 适配布局与验证范围

安装器要求容器中存在以下文件：

```text
/app/napcat/napcat.mjs
/app/entrypoint.sh
/opt/QQ/qq
/opt/QQ/resources/app/package.json
/opt/QQ/resources/app/loadNapCat.js
/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so
```

全新容器还需要镜像自带的 `/app/NapCat.Shell.zip`。包装脚本在首次启动时先解压代码并保留配置目录，再执行插件安装；已有 NapCat 代码继续使用当前版本。

示例 Compose 从 `mlikiowa/napcat-docker` 的固定镜像摘要构建，其中自带 Python 3.10。`Dockerfile` 先补齐基础 QQ 包依赖，再安装 `libpulse-mainloop-glib0`、`libegl1` 和 `libopengl0`，最后用 `ldd` 检查 AVSDK 动态依赖。可以通过构建参数 `NAPCAT_IMAGE` 指定自己的兼容基础镜像；镜像需要 Python 3.10 或兼容版本、APT 包管理器，路径布局和 Loader 结构需要符合安装器检查。

0.2.2 已完成独立镜像构建与 Compose 配置检查，固定基础镜像的 AVSDK 缺失动态库由 3 项降为 0 项。安装器回归覆盖合成目录下的首次安装、重复安装、发布包覆盖后的重新安装、配置保留、未知结构拒绝，以及隔离 Shell 替身下的启动顺序和失败传播。

此前 0.2.1 使用示例固定镜像、空账号目录和 `--network none` 实际启动：镜像自带 NapCat **4.18.37**，插件安装成功，QQ/Electron 启动后 WebUI 返回 **HTTP 200**。同一安装实现也在 NapCat **4.18.38** 官方发布包上完成了隔离容器启动验证。账号在线、Host 激活和双向音频需在实际部署中分别验收。

## 新部署

在仓库根目录执行：

```bash
docker compose -f docker/compose.yaml config --quiet
docker compose -f docker/compose.yaml up -d --build
docker compose -f docker/compose.yaml logs -f napcat
```

安装成功时会出现类似信息：

```text
[QQVoiceCall] Docker plugin v0.2.2 installed; enable it in NapCat plugin management
```

打开 `http://127.0.0.1:6099`，按镜像原有方式登录 QQ，再到插件管理启用 `qq-voice-call`。安装器保留现有插件启用状态，首次启用由使用者操作。

示例通过命名卷保存 `/app/.config/QQ` 账号目录和 `/app/napcat/config` 配置目录。维护容器时应保留这两个卷。

## 接入已有容器

在原有 Compose 的 NapCat 服务中追加以下内容，保留原来的端口、账号卷和配置卷。将下面三个占位路径改成仓库的实际绝对路径：

```yaml
services:
  napcat:
    image: qq-voice-call-napcat:local
    build:
      context: /absolute/path/qq-voice-call-bridge/docker
    entrypoint: ["bash", "/opt/qq-voice-call-docker/entrypoint.sh"]
    volumes:
      - /absolute/path/qq-voice-call-bridge/napcat-plugin:/opt/qq-voice-call:ro
      - /absolute/path/qq-voice-call-bridge/docker/entrypoint.sh:/opt/qq-voice-call-docker/entrypoint.sh:ro
```

这里展示的是要并入原配置的片段；保留原有的 `volumes` 条目。使用 `up -d --build` 构建并更新原服务。若已有镜像已经补齐这些 AVSDK 动态依赖，可以沿用它，只加入入口与挂载；使用其它兼容基础镜像构建时，通过 `build.args.NAPCAT_IMAGE` 指定。包装脚本最终执行 `/app/entrypoint.sh`，适用于该入口的镜像。

如果原来已有自定义启动脚本，请在它完成 NapCat 发布包解压或更新之后、真正启动 QQ 之前加入：

```bash
python3 /opt/qq-voice-call/install-docker.py
```

让这一步失败时结束启动，避免在未完成 Host 分流的状态下运行 QQ。随后使用原来的 Compose 文件更新原 NapCat 服务。同一账号保持一个普通 QQ/NapCat 实例，避免并行部署造成会话冲突。

## 安装器修改的内容

`napcat-plugin/install-docker.py` 使用镜像自带的 Python 3 执行，只依赖 Python 标准库：

1. 校验 QQ 的 `main` 指向待适配的物理 Loader，并检查 AVSDK 与插件文件。
2. 将只读挂载的插件部署到 `/app/napcat/plugins/qq-voice-call`。
3. 在 NapCat 的唯一官方插件白名单中增加 `qq-voice-call`，保留其它加载规则。
4. 备份物理 Loader，再按 `QQ_VOICE_CALL_AV_HOST` 标志分流：普通进程运行原 NapCat 入口，AVSDK Host 子进程只加载 `host.cjs`。
5. 重复安装时检查备份与现有补丁，保留账号、插件启用状态及插件配置。

修改前会保存 `napcat.mjs.qq-voice-call.original` 和 `loadNapCat.qq-voice-call.original.cjs`。未知白名单、未知 Loader、错误的 QQ `main` 或残缺备份会在写入安装文件前报错。

安装器修改的是容器中的 NapCat 文件与 QQ Loader。源码挂载保持只读；控制端设置通过 NapCat 插件配置保存，账号与配置通过原有数据卷持久化。

## 控制端网络

控制端运行在 Docker 宿主机时，普通反向 WS 可以配置为：

```text
ws://host.docker.internal:6199/ws
```

插件使用同一地址、端口和 Token，实际连接 `/qq-voice-call`。示例 Compose 已为 Linux Docker 添加 `host.docker.internal:host-gateway`；Docker Desktop 通常直接支持这个名称。

控制端应监听容器可达的网卡，防火墙允许对应来源。`127.0.0.1` 在容器内指向容器自身。若控制端也在同一个 Compose 网络，使用控制端服务名和容器端口。

Host 控制端口 `6110`、页面端口 `6111` 仅在容器回环接口使用，保持原有绑定即可。

插件配置页可以编辑控制端地址、电话桥路径、令牌、重连间隔及 Host 路径和端口。保存后重新启用插件或重启 NapCat 生效；字段说明见 [NapCat 插件配置](../napcat-plugin/README.md#配置保存位置)。

## 双向音频

桥接 WebSocket 传输控制、信令和状态；声音走 QQ/AVSDK 使用的音频设备。实际部署还要接通：

```text
QQ 来电声音 -> 容器音频输出 -> 控制端的 ASR 输入
控制端的 TTS 输出 -> 容器音频输入 -> QQ 对端
```

音频路由依赖宿主系统、音频服务器和控制端运行位置。构建时补齐动态库只解决原生库加载依赖；PulseAudio 服务、设备或音频服务器挂载仍按实际环境配置。若日志出现 PulseAudio 连接错误或断言，应检查音频服务与路由，插件可见或桥接成功分别只证明对应阶段完成。

## 更新、回退与排查

更新仓库后，用 `docker compose -f docker/compose.yaml up -d --build` 更新服务，启动包装会再次部署当前源码并补齐安装状态；已有容器使用自己的 Compose 路径。更新镜像或 NapCat 发布包时，保留账号与配置卷，并重新验证 Loader、来电与音频。

需要恢复镜像原有启动方式时，恢复原来的 `entrypoint`、移除本插件的两个挂载，再用原镜像重新创建服务，继续挂载原账号卷和配置卷。容器可写层的安装补丁随容器重建恢复为镜像内容。

安装失败时先检查 `docker compose logs napcat` 中的首个错误。请核对实际文件布局、白名单是否唯一、QQ `main` 与 Loader 是否一致；确认原因后再重新启动。示例使用 `restart: "no"`，便于发现启动阶段的结构不兼容。

安装成功后按顺序验收：插件可见并启用、控制端收到 `hello`、激活后 Host 就绪、来电进入、接听成功、双方分别听到声音。
