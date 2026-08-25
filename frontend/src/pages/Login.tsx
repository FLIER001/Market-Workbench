import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { LineChart, LogIn, UserPlus, Loader2 } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { auth, saveSession } from "@/lib/auth";
import { api } from "@/lib/api";
import { pendingLocalData, importLocalToAccount, pullBackendToLocal, clearLocalUserData } from "@/lib/userData";
import { toast } from "sonner";
import { cn } from "@/lib/utils";

const inputCls =
  "w-full rounded-lg border border-border bg-black/20 px-3 py-2.5 text-sm outline-none transition-colors focus:border-primary/50 placeholder:text-muted-foreground/40";

export function Login() {
  const nav = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [regOpen, setRegOpen] = useState(false); // 注册默认关闭；空库/显式开关时后端才放行
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [pendingData, setPendingData] = useState<{ key: string; count: number; preview: string }[]>([]);
  const [loggedInName, setLoggedInName] = useState("");

  useEffect(() => {
    // 后端不可达时静默保持只登录（注册按钮不出现），不影响登录使用
    api.authConfig().then((c) => setRegOpen(c.registration_open)).catch(() => {});
  }, []);

  const submit = async () => {
    setErr("");
    if (!username.trim() || !password) {
      setErr("请填写用户名和密码");
      return;
    }
    if (mode === "register") {
      if (password.length < 6) {
        setErr("密码至少 6 位");
        return;
      }
      if (password !== confirm) {
        setErr("两次输入的密码不一致");
        return;
      }
    }
    setBusy(true);
    try {
      const res =
        mode === "register"
          ? await auth.register(username.trim(), password)
          : await auth.login(username.trim(), password);
      const userId = "user_id" in res && typeof res.user_id === "number" ? res.user_id : 0;
      saveSession(res.token, { id: userId, username: res.username });
      // 检测本地是否有可导入的数据，由用户确认后再导入（不静默搬数据）
      const pending = await pendingLocalData();
      if (pending.length > 0) {
        setPendingData(pending);
        setLoggedInName(res.username);
        return; // 停在确认页，由用户选择导入或跳过
      }
      clearLocalUserData();
      await pullBackendToLocal();
      toast.success(mode === "register" ? `欢迎，${res.username}！账号已创建` : `欢迎回来，${res.username}`);
      nav("/daily-review", { replace: true });
    } catch (e) {
      setErr(e instanceof Error ? e.message : "操作失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm">
        {/* Brand */}
        <div className="mb-6 flex flex-col items-center">
          <LineChart className="mb-2 h-10 w-10 text-primary text-glow" />
          <h1 className="text-2xl font-extrabold tracking-tight">
            Market <span className="text-primary">Workbench</span>
          </h1>
          <p className="mt-1 text-xs text-muted-foreground">本地市场研究工作台 · 数据跟随账号，不再因换浏览器/域名丢失</p>
        </div>

        <GlassCard className="p-6">
          {pendingData.length > 0 ? (
            /* 登录/注册成功后发现本地有数据 → 由用户决定是否导入账号 */
            <div>
              <p className="mb-1 text-sm font-semibold text-foreground">检测到本机已有数据</p>
              <p className="mb-4 text-xs leading-relaxed text-muted-foreground">
                {loggedInName}，要把这台机器浏览器里的数据导入你的账号吗？导入后换浏览器/换电脑也能看到。
              </p>
              <div className="mb-5 space-y-1.5">
                {pendingData.map((d) => (
                  <div key={d.key} className="flex items-center justify-between rounded-lg bg-muted/20 px-3 py-2 text-xs">
                    <span className="text-foreground">{d.preview}</span>
                    <span className="font-mono text-muted-foreground">{d.count} 条</span>
                  </div>
                ))}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={async () => {
                    setBusy(true);
                    const { migrated } = await importLocalToAccount();
                    await pullBackendToLocal();
                    setBusy(false);
                    toast.success(`已导入 ${migrated.length} 项数据到账号`);
                    nav("/daily-review", { replace: true });
                  }}
                  disabled={busy}
                  className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2.5 text-sm font-medium text-primary shadow-glow transition-colors hover:bg-primary/25 disabled:opacity-50"
                >
                  {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                  导入到账号
                </button>
                <button
                  onClick={async () => {
                    clearLocalUserData();
                    await pullBackendToLocal();
                    toast.success("已跳过，未导入本地数据");
                    nav("/daily-review", { replace: true });
                  }}
                  className="inline-flex items-center justify-center rounded-lg px-4 py-2.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
                >
                  跳过
                </button>
              </div>
            </div>
          ) : (
          <>
          {/* 模式切换：注册关闭时只显示登录（后端 /api/auth/config 控制） */}
          {regOpen ? (
          <div className="mb-5 grid grid-cols-2 gap-1 rounded-lg bg-black/20 p-1">
            {(["login", "register"] as const).map((m) => (
              <button
                key={m}
                onClick={() => { setMode(m); setErr(""); }}
                className={cn(
                  "rounded-md py-1.5 text-sm font-medium transition-colors",
                  mode === m ? "bg-primary/20 text-primary" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {m === "login" ? "登录" : "注册"}
              </button>
            ))}
          </div>
          ) : (
          <p className="mb-5 text-center text-xs text-muted-foreground">账号由管理员开通，如需账号请联系管理员</p>
          )}

          <div className="space-y-3">
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="用户名"
              autoComplete="username"
              className={inputCls}
              onKeyDown={(e) => e.key === "Enter" && submit()}
            />
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="密码"
              autoComplete={mode === "register" ? "new-password" : "current-password"}
              className={inputCls}
              onKeyDown={(e) => e.key === "Enter" && submit()}
            />
            {mode === "register" && regOpen && (
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder="确认密码"
                autoComplete="new-password"
                className={inputCls}
                onKeyDown={(e) => e.key === "Enter" && submit()}
              />
            )}

            {err && <p className="text-xs text-danger">{err}</p>}

            <button
              onClick={submit}
              disabled={busy}
              className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-primary/15 px-4 py-2.5 text-sm font-medium text-primary shadow-glow transition-colors hover:bg-primary/25 disabled:opacity-50"
            >
              {busy ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : mode === "login" ? (
                <LogIn className="h-4 w-4" />
              ) : (
                <UserPlus className="h-4 w-4" />
              )}
              {busy ? "处理中…" : mode === "login" ? "登录" : "创建账号"}
            </button>
          </div>

          <p className="mt-4 text-center text-[11px] leading-relaxed text-muted-foreground/50">
            数据只存在你自己的机器上（~/.vibe-research），不会上传到任何服务器。
          </p>
          </>
          )}
        </GlassCard>
      </div>
    </div>
  );
}
