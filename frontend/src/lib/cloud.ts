// WorkBuddy 云服务客户端（单例）。
// endpoint / publishableKey / oauthRelayBaseUrl 都来自应用开通云服务时返回的 publicConfig，
// 属于可公开下发的配置；真正的权限由服务端按 Origin + 用户会话判定。
//
// 这三个值在**构建时**通过 VITE_CLOUD_* 注入。未注入的构建（本地开发、自建云主机）
// 自动关闭云登录与云数据层，代码里不再写死某个发布域名。
import { createWorkBuddyCloud, type WorkBuddyCloudClient } from "@tencent-ai/workbuddy-cloud-sdk";

const env = import.meta.env;

const ENDPOINT = (env.VITE_CLOUD_ENDPOINT || "").replace(/\/+$/, "");
const PUBLISHABLE_KEY = env.VITE_CLOUD_PUBLISHABLE_KEY || "";

export const CLOUD_CONFIG = {
  endpoint: ENDPOINT,
  oauthRelayBaseUrl:
    env.VITE_CLOUD_OAUTH_RELAY || "https://www.workbuddy.cn/v2/as/genie-baas/oauth",
  publishableKey: PUBLISHABLE_KEY,
};

/** 只有 endpoint 与 publishableKey 都注入时，云登录/云数据才启用。 */
export const CLOUD_ENABLED = Boolean(ENDPOINT && PUBLISHABLE_KEY);

// 只声明本应用实际用到的表，让 database.from() 有强类型（不传泛型时 insert/update 会退化成 never）。
type UserDataRow = {
  id: number;
  owner_id: string;
  key: string;
  value: unknown;
  updated_at: string;
};

export type CloudDatabase = {
  public: {
    Tables: {
      user_data: {
        Row: UserDataRow;
        // owner_id 由数据库 DEFAULT auth.uid() 填，应用不得传。
        Insert: { id?: never; owner_id?: never; key: string; value: unknown; updated_at?: string };
        Update: { value?: unknown; updated_at?: string };
        Relationships: [];
      };
    };
    Views: Record<string, never>;
    Functions: Record<string, never>;
  };
};

// 未配置时保持 null：createWorkBuddyCloud 缺配置会直接抛错，会让整页白屏。
// 调用方都必须先过 CLOUD_ENABLED / cloudDataActive()，这两处已挡住。
export const cloud: WorkBuddyCloudClient<CloudDatabase> = CLOUD_ENABLED
  ? createWorkBuddyCloud<CloudDatabase>(CLOUD_CONFIG)
  : (null as unknown as WorkBuddyCloudClient<CloudDatabase>);
