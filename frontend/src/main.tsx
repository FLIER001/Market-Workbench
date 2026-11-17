import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { Toaster } from "sonner";
import { ErrorBoundary } from "./components/common/ErrorBoundary";
import { router } from "./router";
import { completeWorkBuddyLogin } from "./lib/auth";
import "./index.css";

// WorkBuddy 授权回调落在站点根路径（带 code/state）。先在挂载前换会话，
// 否则路由守卫会先把用户弹到登录页，回调参数就丢了。
// 参数可能出现在 query 或 hash 里，两种都要认。
const hasOAuthCallback = /[?&#](code|state|error)=/.test(
  window.location.search + window.location.hash,
);

async function bootstrap() {
  if (hasOAuthCallback) {
    try {
      const ok = await completeWorkBuddyLogin();
      if (!ok) sessionStorage.setItem("vr-cloud-login-error", "授权回调缺少有效参数，请重试");
    } catch (e) {
      console.error("[cloud-login]", e);
      sessionStorage.setItem(
        "vr-cloud-login-error",
        e instanceof Error ? e.message : "WorkBuddy 登录失败",
      );
    }
    window.history.replaceState({}, "", "/");
  }
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <ErrorBoundary>
        <RouterProvider router={router} />
        <Toaster position="bottom-right" theme="dark" richColors closeButton duration={3500} />
      </ErrorBoundary>
    </StrictMode>
  );
}

void bootstrap();
