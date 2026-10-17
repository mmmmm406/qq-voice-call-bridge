# 为 QQ AVSDK 补齐原生依赖；NAPCAT_IMAGE 可指定相同布局的兼容基础镜像。
ARG NAPCAT_IMAGE=mlikiowa/napcat-docker@sha256:f611790ecffa23c3c182017d5cf68179dc24612403608cd835ad13d79642f1bd
FROM ${NAPCAT_IMAGE}

USER root
# 基础 QQ 包存在未满足的依赖，先修复包依赖，再安装 AVSDK 所需的库。
RUN apt-get update \
    && apt-get install -y --fix-broken --no-install-recommends --no-remove \
    && apt-get install -y --no-install-recommends --no-remove libpulse-mainloop-glib0 libegl1 libopengl0 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -c 'import subprocess; output = subprocess.check_output(["ldd", "/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so"], text=True); missing = [line.strip() for line in output.splitlines() if "not found" in line]; assert not missing, "AVSDK dependencies missing: " + "; ".join(missing)'
