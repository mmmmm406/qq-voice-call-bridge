"use strict";

/**
 * QQ 启动前部署 Docker 电话桥。由 QQ 的 Node 模式执行，只使用内置模块。
 * 先校验固定入口与白名单，再写入容器文件；未知结构会在 QQ 启动前报错。
 */
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const PLUGIN_ID = "qq-voice-call";
const HOST_MARKER = "QQ_VOICE_CALL_LOADER_HOOK_V1";
const ORIGINAL_LOADER = "loadNapCat.qq-voice-call.original.cjs";
const OFFICIAL_IDS = [
  "napcat-plugin-builtin", "napcat-plugin-cleaner", "napcat-plugin-ssqq", "napcat-plugin-qce",
];

/** 只修改拒绝提示之前、包含完整官方 ID 的唯一 Set，保留其余加载规则。 */
function patchPluginWhitelist(source) {
  const marker = source.indexOf("not in official plugin whitelist");
  if (marker < 0) throw new Error("无法识别 NapCat 插件白名单");
  const candidates = [...source.matchAll(/new Set\(\[([^\]]*)\]\)/g)].filter((match) => {
    const ids = [...match[1].matchAll(/(["'`])([^"'`]+)\1/g)].map((item) => item[2]);
    return match.index < marker && OFFICIAL_IDS.every((id) => ids.includes(id));
  });
  if (candidates.length !== 1) throw new Error("NapCat 官方插件白名单不唯一");
  const candidate = candidates[0];
  if (new RegExp(`(["'\x60])${PLUGIN_ID}\\1`).test(candidate[1])) return source;
  const end = candidate.index + candidate[0].lastIndexOf("]");
  return `${source.slice(0, end)},"${PLUGIN_ID}"${source.slice(end)}`;
}

/** Docker 的物理 Loader 只为带 Host 标志的子进程分流，普通 QQ 沿原入口运行。 */
function patchHostLoader(source, napcatRoot) {
  if (source.includes(HOST_MARKER)) return source;
  const expectedUrl = pathToFileURL(path.join(napcatRoot, "napcat.mjs")).href;
  const match = source.trim().match(
    /^\(async\s*\(\)\s*=>\s*\{\s*await\s+import\((["'])([^"']+)\1\);?\s*\}\)\(\);?$/,
  );
  if (!match || match[2] !== expectedUrl) throw new Error("无法识别 Docker QQ 的物理 Loader");
  return `"use strict";\n// ${HOST_MARKER}\n` +
    'if (process.env.QQ_VOICE_CALL_AV_HOST === "1") {\n' +
    '  require(process.env.QQ_VOICE_CALL_AV_HOST_ENTRY);\n' +
    '} else {\n' +
    `  require('./${ORIGINAL_LOADER}');\n}\n`;
}

/** 在原目录中替换文件，保留权限；新文件只用于受控容器目录。 */
function writeAtomic(filename, content) {
  const temporary = `${filename}.qq-voice-call-tmp-${process.pid}`;
  try {
    fs.writeFileSync(temporary, content, {
      mode: fs.existsSync(filename) ? fs.statSync(filename).mode : 0o644,
    });
    fs.renameSync(temporary, filename);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

/** 参数只供离线夹具指定空目录；生产启动使用默认的两个固定容器根目录。 */
function installDockerPlugin({ napcatRoot = "/app/napcat", qqRoot = "/opt/QQ", sourceRoot = __dirname } = {}) {
  const loader = path.join(qqRoot, "resources/app/loadNapCat.js");
  const appRoot = path.dirname(loader);
  const appPackage = JSON.parse(fs.readFileSync(path.join(appRoot, "package.json"), "utf8"));
  if (path.resolve(appRoot, appPackage.main || "") !== path.resolve(loader)) {
    throw new Error("QQ main 与待适配的物理 Loader 不一致");
  }
  const metadata = JSON.parse(fs.readFileSync(path.join(sourceRoot, "package.json"), "utf8"));
  if (metadata.name !== PLUGIN_ID) throw new Error("电话插件 ID 不匹配");
  for (const entry of ["index.mjs", "host.cjs", "host-preload.cjs", "host.html", "host-bootstrap/napcat/napcat.mjs"]) {
    if (!fs.statSync(path.join(sourceRoot, entry)).isFile()) throw new Error(`电话插件文件缺失：${entry}`);
  }
  if (!fs.statSync(path.join(appRoot, "avsdk/libAVSDKPlugin.so")).isFile()) {
    throw new Error("QQ AVSDK 原生库缺失");
  }
  const bundle = path.join(napcatRoot, "napcat.mjs");
  const bundleSource = fs.readFileSync(bundle, "utf8");
  const patchedBundle = patchPluginWhitelist(bundleSource);
  const loaderSource = fs.readFileSync(loader, "utf8");
  const patchedLoader = patchHostLoader(loaderSource, napcatRoot);
  const original = path.join(appRoot, ORIGINAL_LOADER);
  if (loaderSource.includes(HOST_MARKER)) {
    // 已安装时也校验普通 QQ 的原入口，防止残缺备份导致启动另一套客户端。
    const originalSource = fs.readFileSync(original, "utf8");
    if (originalSource.includes(HOST_MARKER)) throw new Error("QQ 原入口备份包含 Host 分流逻辑");
    patchHostLoader(originalSource, napcatRoot);
  } else if (fs.existsSync(original) && fs.readFileSync(original, "utf8") !== loaderSource) {
    throw new Error("QQ Loader 备份与当前原入口不一致");
  }

  const target = path.join(napcatRoot, "plugins", PLUGIN_ID);
  fs.cpSync(sourceRoot, target, {
    recursive: true,
    filter: (entry) => !["data", "tests", "__pycache__"].includes(path.basename(entry)),
  });
  if (patchedBundle !== bundleSource) {
    writeAtomic(`${bundle}.qq-voice-call.original`, bundleSource);
    writeAtomic(bundle, patchedBundle);
  }
  if (patchedLoader !== loaderSource) {
    writeAtomic(original, loaderSource);
    writeAtomic(loader, patchedLoader);
  }
  return { pluginId: PLUGIN_ID, version: metadata.version, target, loader };
}

module.exports = { installDockerPlugin, patchHostLoader, patchPluginWhitelist };

if (require.main === module) {
  try {
    const installed = installDockerPlugin();
    console.log(`[QQVoiceCall] Docker plugin v${installed.version} installed; enable it in NapCat plugin management`);
  } catch (error) {
    console.error(`[QQVoiceCall] Docker installation failed: ${error.message}`);
    process.exitCode = 1;
  }
}
