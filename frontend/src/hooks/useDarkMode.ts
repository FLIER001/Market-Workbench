import { useEffect, useState } from "react";

import { storageGet, storageSet } from "@/lib/storage";

// 默认浅色纸面，用户可切低眩光暗色；选择存 localStorage。
// <html> 同时使用 Tailwind 的 .dark 与主题变量对应的 .light。
export function useDarkMode() {
  const [dark, setDark] = useState(() => {
    const saved = storageGet("vr-theme");
    if (saved) return saved === "dark";
    return false; // 日常桌面研究默认浅色
  });

  useEffect(() => {
    document.documentElement.classList.toggle("light", !dark);
    document.documentElement.classList.toggle("dark", dark);
    storageSet("vr-theme", dark ? "dark" : "light");
  }, [dark]);

  return { dark, toggle: () => setDark((d) => !d) };
}
