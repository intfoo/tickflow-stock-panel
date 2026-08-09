// 主题管理 — 暗色(默认) / 亮色切换
//
// 机制:
//   - 状态存 localStorage('tf-theme'), 默认 dark (保持老用户体验不变)
//   - 生效方式: html.dark class (index.css 的 CSS variables + Tailwind darkMode:class)
//   - index.html 里有预渲染内联脚本, 首屏前就设好 class, 避免闪烁 (FOUC)
//   - UI token (bg-surface/text-foreground 等) 自动跟随;
//     图表画布不吃 CSS 变量, 统一走 useChartTheme() 取调色板
import { useEffect, useState } from 'react'
import { getColorMode, useColorMode, type ColorMode, COLOR_MODES } from './colorMode'

const KEY = 'tf-theme'
const EVENT = 'tf-theme-change'

export type Theme = 'dark' | 'light'

export function getTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export function setTheme(theme: Theme) {
  try { localStorage.setItem(KEY, theme) } catch { /* ignore */ }
  document.documentElement.classList.toggle('dark', theme === 'dark')
  window.dispatchEvent(new CustomEvent(EVENT, { detail: theme }))
}

export function toggleTheme(): Theme {
  const next: Theme = getTheme() === 'dark' ? 'light' : 'dark'
  setTheme(next)
  return next
}

/** 订阅当前主题 (本页切换 + 其他标签页切换均同步)。 */
export function useTheme(): Theme {
  const [theme, set] = useState<Theme>(getTheme)
  useEffect(() => {
    const onChange = () => set(getTheme())
    window.addEventListener(EVENT, onChange)
    window.addEventListener('storage', onChange)  // 跨标签页同步
    return () => {
      window.removeEventListener(EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [])
  return theme
}

// ================================================================
// 图表调色板 — ECharts / lightweight-charts 画布不吃 CSS 变量,
// 所有图表组件统一从这里取色, 主题切换时依赖 useTheme 重建 option。
// bull/bear/accent 等语义色双主题一致, 不在此重复定义。
// ================================================================

function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const r = parseInt(h.slice(0, 2), 16)
  const g = parseInt(h.slice(2, 4), 16)
  const b = parseInt(h.slice(4, 6), 16)
  return `rgba(${r},${g},${b},${alpha})`
}

function presetColors(mode: ColorMode): { bull: string; bear: string } {
  const p = COLOR_MODES.find(m => m.id === mode) ?? COLOR_MODES[0]
  return { bull: p.bullHex, bear: p.bearHex }
}

export interface ChartTheme {
  /** 轴刻度/图例等常规文字 */
  text: string
  /** 信息条/图例里的强调文字 */
  textStrong: string
  /** 网格线 */
  grid: string
  /** 轴线/边框 */
  border: string
  /** 十字光标线 */
  crosshair: string
  /** 十字光标轴标签背景 */
  crosshairLabelBg: string
  /** tooltip 背景 */
  tooltipBg: string
  /** tooltip 边框 */
  tooltipBorder: string
  /** tooltip 文字 */
  tooltipText: string
  /** 半透明信息条背景 (K线图左上角 OHLC 条) */
  infoBarBg: string
  /** dataZoom 滑块填充 */
  zoomFill: string
  /** 分时图均价线以外的弱填充 */
  fillSubtle: string
  /** 涨色 hex（画布用） */
  bull: string
  /** 跌色 hex（画布用） */
  bear: string
  /** 涨色半透明 rgba（alpha 0-1） */
  bullAlpha: (alpha: number) => string
  /** 跌色半透明 rgba（alpha 0-1） */
  bearAlpha: (alpha: number) => string
}

function darkTheme(cm: ColorMode): ChartTheme {
  const c = presetColors(cm)
  return {
  text: '#A1A1AA',
  textStrong: '#E4E4E7',
  grid: 'rgba(255,255,255,0.06)',
  border: '#27272A',
  crosshair: 'rgba(255,255,255,0.25)',
  crosshairLabelBg: '#333',
  tooltipBg: 'rgba(24,24,27,0.95)',
  tooltipBorder: 'rgba(255,255,255,0.1)',
  tooltipText: '#E4E4E7',
  infoBarBg: 'rgba(39,39,42,0.6)',
  zoomFill: 'rgba(255,255,255,0.06)',
  fillSubtle: 'rgba(255,255,255,0.04)',
    bull: c.bull,
    bear: c.bear,
    bullAlpha: (a: number) => hexToRgba(c.bull, a),
    bearAlpha: (a: number) => hexToRgba(c.bear, a),
  }
}

function lightTheme(cm: ColorMode): ChartTheme {
  const c = presetColors(cm)
  return {
  text: '#71717A',
  textStrong: '#27272A',
  grid: 'rgba(0,0,0,0.06)',
  border: '#E4E4E7',
  crosshair: 'rgba(0,0,0,0.3)',
  crosshairLabelBg: '#52525B',
  tooltipBg: 'rgba(255,255,255,0.97)',
  tooltipBorder: 'rgba(0,0,0,0.1)',
  tooltipText: '#27272A',
  infoBarBg: 'rgba(244,244,245,0.85)',
  zoomFill: 'rgba(0,0,0,0.06)',
  fillSubtle: 'rgba(0,0,0,0.04)',
    bull: c.bull,
    bear: c.bear,
    bullAlpha: (a: number) => hexToRgba(c.bull, a),
    bearAlpha: (a: number) => hexToRgba(c.bear, a),
  }
}

export function chartTheme(theme: Theme, colorMode: ColorMode = getColorMode()): ChartTheme {
  return theme === 'dark' ? darkTheme(colorMode) : lightTheme(colorMode)
}

/** hook: 当前主题的图表调色板 (主题切换自动触发重渲染)。 */
export function useChartTheme(): ChartTheme {
  return chartTheme(useTheme(), useColorMode())
}
