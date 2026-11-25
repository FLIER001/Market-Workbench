import { useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { loadToken, loadUser, clearSession, auth, runOncePerSession } from "@/lib/auth";
import { storageGet, storageRemove } from "@/lib/storage";
import { pullBackendToLocal } from "@/lib/userData";
import { Loader2 } from "lucide-react";

// 登录守卫：有 token 先校验（/auth/me），通过则放行；无效则清会话跳登录页。
// 后端临时不可达时，用本地缓存的用户放行（离线可用），不强制登出。
// 校验与「拉取后端数据」每个会话只做一次（标记在 auth.ts，登录/登出时复位）；
// 此前每次路由切换都重新校验一次，而后端每个鉴权请求都要查一次会话库。
export function RequireAuth({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const [state, setState] = useState<"checking" | "ok" | "deny">("checking");

  useEffect(() => {
    const token = loadToken();
    if (!token) {
      setState("deny");
      return;
    }
    const pull = () => runOncePerSession("pulled", () => {
      // 已登录用户每次打开页面时拉一次后端数据（含深度分析结果恢复 + 本地已完成的反向迁移）
      pullBackendToLocal().catch(() => { /* 拉取失败不影响使用 */ });
    });
    // 刚完成登录：登录响应本身已证明凭据有效，不再用 /auth/me 二次校验。
    // 某些部署前面有网关/反代，可能改写或不再透传 Authorization，导致 /auth/me 误判 401，
    // 表现就是「登录成功但立刻被弹回登录页」。后续任何接口真 401 仍会通过全局事件登出。
    if (storageGet("vr-just-logged-in") === "1") {
      storageRemove("vr-just-logged-in");
      runOncePerSession("verified", () => { /* 登录响应即为校验 */ });
      setState("ok");
      pull();
      return;
    }
    let alive = true;
    const verified = runOncePerSession("verified", () => {
      auth
        .me()
        .then(() => {
          if (!alive) return;
          setState("ok");
          pull();
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
    });
    if (!verified) {
      // 本会话已校验过：直接放行，并确保数据拉取已触发
      setState("ok");
      pull();
    }
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
