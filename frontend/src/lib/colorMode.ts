import { useEffect, useState } from 'react'

export type ColorMode = 'classic' | 'green-red' | 'warm-cool' | 'mono-blue' | 'mono-grey'

export interface ColorModePreset {
  id: ColorMode
  label: string
  desc: string
  /** CSS HSL 值（供 index.css 变量，不带 hsl() 包装），如 "4 87% 60%" */
  bullHsl: string
  bearHsl: string
  /** 十六进制色值（供 ECharts/lightweight-charts 画布） */
  bullHex: string
  bearHex: string
}

/** 配色预设常量 — 设置页预览卡片和 theme.ts 共享的单一数据源 */
export const COLOR_MODES: ColorModePreset[] = [
  { id: 'classic',     label: '经典红绿', desc: 'A 股默认配色',
    bullHsl: '4 87% 60%',  bearHsl: '152 67% 45%',  bullHex: '#C74040', bearHex: '#2D9B65' },
  { id: 'green-red',   label: '绿涨红跌', desc: '美股/港股惯例，与 A 股相反',
    bullHsl: '152 67% 45%', bearHsl: '4 87% 60%',  bullHex: '#2D9B65', bearHex: '#C74040' },
  { id: 'warm-cool',   label: '橙蓝', desc: '暖正向/冷负向，避开红绿',
    bullHsl: '34 94% 50%', bearHsl: '215 16% 47%',  bullHex: '#F79009', bearHex: '#64748B' },
  { id: 'mono-blue',   label: '深浅蓝', desc: '单色系明度对比',
    bullHsl: '226 71% 40%', bearHsl: '215 20% 65%', bullHex: '#1E40AF', bearHex: '#94A3B8' },
  { id: 'mono-grey',   label: '摸鱼纯灰', desc: '深灰/浅灰，零色相，像报纸印刷',
    bullHsl: '240 5% 34%', bearHsl: '240 5% 65%', bullHex: '#52525B', bearHex: '#A1A1AA' },
]

const KEY = 'tf-color-mode'
const EVENT = 'tf-color-mode-change'

export function getColorMode(): ColorMode {
  try {
    const v = localStorage.getItem(KEY)
    return COLOR_MODES.some(m => m.id === v) ? (v as ColorMode) : 'classic'
  } catch {
    return 'classic'
  }
}

export function applyColorMode(mode: ColorMode) {
  document.documentElement.dataset.colorMode = mode
}

export function setColorMode(mode: ColorMode) {
  try { localStorage.setItem(KEY, mode) } catch { /* ignore */ }
  applyColorMode(mode)
  window.dispatchEvent(new CustomEvent(EVENT, { detail: mode }))
}

export function useColorMode(): ColorMode {
  const [mode, set] = useState<ColorMode>(getColorMode)
  useEffect(() => {
    // 初始化时同步 DOM data-color-mode 属性，修正 FOUC 脚本可能设置的无效值
    applyColorMode(getColorMode())
    const onChange = () => set(getColorMode())
    window.addEventListener(EVENT, onChange)
    window.addEventListener('storage', onChange)
    return () => {
      window.removeEventListener(EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [])
  return mode
}
