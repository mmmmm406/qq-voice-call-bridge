#!/usr/bin/env bash
# 在普通 QQ 启动前完成插件部署与 Host 入口分流；失败时结束本次启动。
set -euo pipefail

# 上游镜像首次启动才解压 NapCat；先准备代码，才能在普通 QQ 运行前打补丁。
# 保留已有 NapCat 文件；config 由原入口按缺失情况补齐，沿用现有配置卷。
cd /app
if [ ! -f /app/napcat/napcat.mjs ]; then
  mkdir -p /app/napcat
  unzip -qo /app/NapCat.Shell.zip -d /app/napcat -x 'config/*'
fi

# 使用基础镜像自带的 Python 3 完成文件部署，QQ/Electron 留给原入口启动。
python3 /opt/qq-voice-call/install-docker.py

# 账号登录、权限及运行目录继续由上游镜像的启动脚本处理。
exec bash /app/entrypoint.sh "$@"
