import { useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { loadToken, loadUser, clearSession, auth } from "@/lib/auth";
import { pullBackendToLocal } from "@/lib/userData";
import { Loader2 } from "lucide-react";

// 每次会话只 pull 一次（模块级标记，避免每个路由切换都拉一遍）
let pulledThisSession = false;

// 登录守卫：有 token 先校验（/auth/me），通过则放行；无效则清会话跳登录页。
// 后端临时不可达时，用本地缓存的用户放行（离线可用），不强制登出。
export function RequireAuth({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const [state, setState] = useState<"checking" | "ok" | "deny">("checking");

  useEffect(() => {
    const token = loadToken();
    if (!token) {
      setState("deny");
      return;
    }
    let alive = true;
    auth
      .me()
      .then(() => {
        if (!alive) return;
        setState("ok");
        // 已登录用户每次打开页面时拉一次后端数据（含深度分析结果恢复 + 本地已完成的反向迁移）
        if (!pulledThisSession) {
          pulledThisSession = true;
          pullBackendToLocal().catch(() => { /* 拉取失败不影响使用 */ });
        }
      })
      .catch((e) => {
        if (!alive) return;
        // 401 已在 auth 层清会话并发事件；这里区分网络错误 vs 真未授权
        if (e instanceof Error && /401|未登录|过期/.test(e.message)) {
          clearSession();
          setState("deny");
        } else {
          // 后端没起来：本地有用户就先放行，数据读写会回退 localStorage
          setState(loadUser() ? "ok" : "deny");
        }
      });
    return () => {
      alive = false;
    };
  }, [loc.pathname]);

  // 全局未授权事件（任意接口 401 时触发）→ 回到登录页
  useEffect(() => {
    const onUnauth = () => setState("deny");
    window.addEventListener("vr:unauthorized", onUnauth);
    return () => window.removeEventListener("vr:unauthorized", onUnauth);
  }, []);

  if (state === "checking") {
    return (
      <div className="flex h-screen items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }
  if (state === "deny") {
    return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  }
  return <>{children}</>;
}
