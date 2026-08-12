import { useEffect } from "react";

// 模块级「上次成功重取时间」（按 SWR key）：供「点进页面时距上次刷新不足 1 分钟就
// 立刻补刷」判断。注意 1 分钟短窗是为了真正的「刚看过又回来」场景不重复打后端；
// 后端有自己的半小时后台预热，这里的补刷是兜底，保证盘中收益不旧。
const lastFetchAt = new Map<string, number>();

const nowMs = () => Date.now();

/** 距上次刷新超过 windowMs 时触发一次 force 重取；windowMs 内直接看缓存。挂载即记录时间戳防双挂载连打。 */
export function useFreshOnEnter(
  key: string,
  revalidate: (force?: boolean) => Promise<unknown>,
  windowMs = 5 * 60_000,
) {
  useEffect(() => {
    const last = lastFetchAt.get(key) ?? 0;
    if (nowMs() - last >= windowMs) {
      // 超过窗口：强制真重算（fetcher 带 fresh 直打后端，拿到最新收益）
      lastFetchAt.set(key, nowMs());
      void revalidate(true);
    }
    // 仅在挂载（点进页面）时判断一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}
