// 安全的 localStorage 读写。
//
// 浏览器在隐私模式、嵌入式 WebView、禁用 cookie、或配额写满时，访问 localStorage
// 会**直接抛异常**（不是返回 null）。如果这个异常发生在组件初始化里，整个页面会白屏——
// 侧栏折叠状态、主题这类外壳级设置尤其致命：一崩就是全站打不开。
//
// 这里统一兜底：存不下就算了，本次会话内功能照常，只是关掉页面后不被记住。
// 但「登不上」不能只靠这条规则——token 落不了盘时，跳转会被守卫弹回登录页，
// 所以额外加一层进程内内存兜底：localStorage 不可用时，本次会话内读写照常工作。

const memory = new Map<string, string>();

export function storageGet(key: string): string | null {
  try {
    const v = localStorage.getItem(key);
    if (v !== null) return v;
  } catch {
    /* 存储不可用，走内存 */
  }
  return memory.get(key) ?? null;
}

export function storageSet(key: string, value: string): void {
  memory.set(key, value);
  try {
    localStorage.setItem(key, value);
  } catch {
    /* 存储不可用：本次会话仍可正常使用，只是不持久化 */
  }
}

export function storageRemove(key: string): void {
  memory.delete(key);
  try {
    localStorage.removeItem(key);
  } catch {
    /* 同上 */
  }
}
