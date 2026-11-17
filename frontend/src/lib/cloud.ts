// WorkBuddy 云服务客户端（单例）。
// endpoint / publishableKey / oauthRelayBaseUrl 均来自应用开通云服务时返回的 publicConfig，
// 属于可公开下发的配置；真正的权限由服务端按 Origin + 用户会话判定。
import { createWorkBuddyCloud } from "@tencent-ai/workbuddy-cloud-sdk";

export const CLOUD_CONFIG = {
  endpoint: "https://wbapp-qcx1g7oby75wan75knpmpk.app.workbuddy.host",
  oauthRelayBaseUrl: "https://www.workbuddy.cn/v2/as/genie-baas/oauth",
  publishableKey: "wbpk_QCx1g7ObY75WAn75KNpmPK_1cKyvI5qoZ2AHDL562cnmHM1FVwdOsH2",
};

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

export const cloud = createWorkBuddyCloud<CloudDatabase>(CLOUD_CONFIG);
