import "server-only";
import { createTuantuantongClient } from "@tuantuantong/sdk";

function requiredEnv(name: "TT_STUDIO_ORIGIN" | "TT_APP_KEY" | "TT_APP_SECRET"): string {
  const value = process.env[name];
  if (!value) throw new Error("缺少工作室服务端配置 " + name);
  return value;
}

export const studio = createTuantuantongClient({
  origin: requiredEnv("TT_STUDIO_ORIGIN"),
  appKey: requiredEnv("TT_APP_KEY"),
  appSecret: requiredEnv("TT_APP_SECRET"),
});
