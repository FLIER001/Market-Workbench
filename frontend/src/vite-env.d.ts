/// <reference types="vite/client" />

// 云服务配置只在构建时注入：未注入的构建（本地/自建部署）自动关闭云登录与云数据层。
interface ImportMetaEnv {
  readonly VITE_CLOUD_ENDPOINT?: string;
  readonly VITE_CLOUD_PUBLISHABLE_KEY?: string;
  readonly VITE_CLOUD_OAUTH_RELAY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
