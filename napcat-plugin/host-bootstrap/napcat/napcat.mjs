/**
 * NapCat Linux launcher 的 AVSDK Host 引导入口。
 *
 * launcher 会把 NAPCAT_BOOTMAIN 指向本目录，随后按固定约定加载
 * ``napcat/napcat.mjs``。这里不启动第二套 NapCat，只转入同一插件目录中的
 * Electron Host；实际入口由环境变量传入，避免在生成的 Loader 上打持久补丁。
 */

import { createRequire } from "node:module";

const entry = String(process.env.QQ_VOICE_CALL_AV_HOST_ENTRY ?? "").trim();
if (!entry) {
  throw new Error("QQ_VOICE_CALL_AV_HOST_ENTRY is required for the QQ AVSDK Host");
}

createRequire(import.meta.url)(entry);
