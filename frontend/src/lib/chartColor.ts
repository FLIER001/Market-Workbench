/**
 * echarts 图表颜色的统一取色入口 —— 全项目所有 echarts 图表必须从这里取色，禁止本地重写。
 *
 * 根因（2026-08-29 实测定案）：Tailwind 的 CSS 变量存的是空格分隔的 HSL 分量
 * （如 --primary: 24 85% 55%），拼出的 `hsl(24 85% 55% / 0.14)` 浏览器 canvas 原生
 * 认识、初始渲染正常；但 zrender 的颜色解析（tool/color 的 parse）去空格后只按逗号
 * split，该语法直接返回 undefined。echarts 6 悬停增量重绘会重新 parse 颜色并重建
 * 渐变对象，undefined 塞进 addColorStop 抛 SyntaxError，整个 series 绘制中断——
 * 表现为"鼠标移入图表，折线/面积/标记全部消失，移出恢复"。
 *
 * 因此这里统一输出 zrender 必然可解析的逗号语法 `rgb(r, g, b)` / `rgba(r, g, b, a)`。
 * 渐变 colorStops（areaStyle 等）尤其要用本函数。
 */

export const cssVar = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const hslToRgb = (h: number, s: number, l: number): [number, number, number] => {
  const sat = s / 100, lig = l / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sat * Math.min(lig, 1 - lig);
  const f = (n: number) => lig - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)];
};

/** 读主题 CSS 变量并转成 zrender 可解析的 rgb/rgba 字符串。alpha 支持 number 或字符串（如 "0.4"）。 */
export const cssColor = (name: string, alpha?: number | string) => {
  const parts = cssVar(name).split(/[\s,]+/);
  const [r, g, b] = hslToRgb(parseFloat(parts[0]), parseFloat(parts[1]), parseFloat(parts[2]));
  return alpha == null ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${alpha})`;
};
