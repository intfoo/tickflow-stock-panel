import { useEffect, useRef, useMemo, useState } from 'react'
import { chartTheme, getTheme, useTheme } from '@/lib/theme'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { Maximize2, Minimize2 } from 'lucide-react'
import { storage } from '@/lib/storage'
import {
  SUB_CHARTS,
  fmtVol,
  volumeRatioAt,
  fmtVolumeRatio,
  INFO_BAR_H,
  SUB_GAP_PX,
  type OHLC,
  type VolumeCompareConfig,
} from '@/components/EChartsCandlestick'

/**
 * 缠论分析专用 K 线图表。
 *
 * 复刻 AnalysisKChart 的 ECharts 构建模式（candlestick + volume bar + dataZoom），
 * 叠加缠论结构：分型(markPoint) / 笔(line) / 中枢(markArea) / 买卖点(markPoint)。
 *
 * 同时复用 EChartsCandlestick 的副图机制（SUB_CHARTS），支持均线 MA5/10/20/60、
 * MACD / RSI / KDJ 副图、量比（成交量柱顶标签 + tooltip 数值）。
 *
 * 独立组件，不 import AnalysisKChart，避免耦合。
 */

// ===== 配色(红涨绿跌, 双主题通用) =====
const THEME = {
  bull: '#C74040',
  bear: '#2D9B65',
  bi: '#F59E0B',
  volUp: 'rgba(240,68,56,0.5)',
  volDown: 'rgba(18,183,106,0.5)',
  zs: 'rgba(59,130,246,0.12)',
  zsBorder: 'rgba(59,130,246,0.35)',
  ma5: '#A1A1AA',
  ma10: '#3B82F6',
  ma20: '#F97316',
  ma60: '#8B5CF6',
}

/** 当前主题的图表调色板 */
const CT = () => chartTheme(getTheme())

// ===== 开关组配置 =====
interface ToggleKey {
  key: string
  label: string
  color: string
}
const TOGGLE_GROUPS: ToggleKey[] = [
  { key: 'fx', label: '分型', color: '#EAB308' },
  { key: 'bi', label: '笔', color: '#F97316' },
  { key: 'zs', label: '中枢', color: '#3B82F6' },
  { key: 'signal', label: '买卖点', color: '#8B5CF6' },
]

interface CzscChartProps {
  bars: { date: string; open: number; high: number; low: number; close: number; volume: number }[]
  fxList: { dt: string; confirm_dt?: string; price: number; mark: 'top' | 'bottom'; power?: string }[]
  biList: { a_dt: string; a_price: number; b_dt: string; b_price: number; direction: 'up' | 'down' }[]
  zsList: { sdt: string; edt: string; zd: number; zg: number }[]
  signalMarkers: { dt: string; confirm_dt?: string; kind: 'buy' | 'sell'; label: string; price: number }[]
  signals?: Record<string, string>[]
  height?: number
}

const DEFAULT_VOLUME_COMPARE: VolumeCompareConfig = { enabled: true, days: 1 }

// 缠论页副图白名单: 仅渲染有数据支撑的副图 (vol 来自 bars.volume, macd 前端计算)。
// RSI/KDJ/BOLL 后端未提供, 不渲染按钮避免空副图。
const CZSC_SUB_KEYS = ['vol', 'macd'] as const

function normalizeVolumeCompare(config: VolumeCompareConfig): VolumeCompareConfig {
  return {
    enabled: config.enabled !== false,
    days: Math.max(1, Math.min(20, Math.round(Number(config.days) || 1))),
  }
}

// ===== 指标计算 (前端, 因后端 czscAnalyze bars 仅 OHLCV) =====
function calcMA(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = []
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) { out.push(null); continue }
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) sum += values[j]
    out.push(sum / period)
  }
  return out
}

function calcEMA(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = []
  const k = 2 / (period + 1)
  let prev: number | null = null
  for (const v of values) {
    if (prev == null) { prev = v; out.push(v) }
    else { prev = v * k + prev * (1 - k); out.push(prev) }
  }
  return out
}

function calcMACD(closes: number[]): {
  dif: (number | null)[]
  dea: (number | null)[]
  hist: (number | null)[]
} {
  const ema12 = calcEMA(closes, 12)
  const ema26 = calcEMA(closes, 26)
  const dif = closes.map((_, i) =>
    ema12[i] != null && ema26[i] != null ? ema12[i]! - ema26[i]! : null,
  )
  const deaRaw = calcEMA(dif.map(d => d ?? 0), 9)
  const dea = dif.map((d, i) => (d != null ? deaRaw[i] : null))
  const hist = closes.map((_, i) =>
    dif[i] != null && dea[i] != null ? 2 * (dif[i]! - dea[i]!) : null,
  )
  return { dif, dea, hist }
}

/** bars → OHLC[]，填入前端计算的 MA/MACD（供 SUB_CHARTS buildSeries 消费） */
function barsToOHLC(bars: CzscChartProps['bars']): OHLC[] {
  const closes = bars.map(b => b.close)
  const ma5 = calcMA(closes, 5)
  const ma10 = calcMA(closes, 10)
  const ma20 = calcMA(closes, 20)
  const ma60 = calcMA(closes, 60)
  const { dif, dea, hist } = calcMACD(closes)
  return bars.map((b, i) => ({
    date: typeof b.date === 'string' ? b.date : String(b.date),
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
    volume: b.volume ?? 0,
    ma5: ma5[i],
    ma10: ma10[i],
    ma20: ma20[i],
    ma60: ma60[i],
    macd_dif: dif[i],
    macd_dea: dea[i],
    macd_hist: hist[i],
  }))
}

export function CzscKChart({
  bars,
  fxList,
  biList,
  zsList,
  signalMarkers,
  signals,
  height = 460,
}: CzscChartProps) {
  const chartRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const chartInstRef = useRef<ECharts | null>(null)
  const theme = useTheme()
  const [activeToggles, setActiveToggles] = useState<Set<string>>(new Set(['fx', 'bi', 'zs', 'signal']))
  const [isFs, setIsFs] = useState(false)
  const [activeIndicators, setActiveIndicators] = useState<string[]>(['vol'])
  const [volumeCompare, setVolumeCompare] = useState<VolumeCompareConfig>(() =>
    normalizeVolumeCompare(storage.stockVolumeCompare.get(DEFAULT_VOLUME_COMPARE)),
  )

  // 全屏状态跟踪 (浏览器 Fullscreen API)
  useEffect(() => {
    const onFsChange = () => setIsFs(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', onFsChange)
    return () => document.removeEventListener('fullscreenchange', onFsChange)
  }, [])

  const toggleFullscreen = () => {
    if (!document.fullscreenElement) wrapRef.current?.requestFullscreen?.().catch(() => {})
    else document.exitFullscreen?.().catch(() => {})
  }

  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is typeof SUB_CHARTS[number] => !!d)
  // 副图额外高度 (信息栏 + 子图高 + 间距)
  const subExtraH = activeSubDefs.reduce(
    (sum, def) => sum + INFO_BAR_H + def.height + SUB_GAP_PX,
    0,
  )

  // 全屏时图表高度撑满视口 (留 80px 给开关行 + padding)；副图额外加高
  const effectiveHeight = (isFs ? Math.max(window.innerHeight - 80, 300) : height) + subExtraH

  // 数据预处理
  // date 用完整字符串：日线族 "YYYY-MM-DD"，分钟族 "YYYY-MM-DD HH:MM"（含空格）。
  // 不再 slice(0,10)，否则分钟族 dt 无法匹配 dateIndex。
  const { dates, candle, dateIndex, zoomStart, fxByDate, markerByDate, signalsByDate, ohlcData } = useMemo(() => {
    const dates = bars.map(r => (typeof r.date === 'string' ? r.date : String(r.date)))
    const candle = bars.map(r => [r.open, r.close, r.low, r.high])
    const dateIndex = new Map(dates.map((d, i) => [d, i]))
    const showBars = 120
    const zoomStart = dates.length > showBars ? Math.round((1 - showBars / dates.length) * 100) : 0
    // date → 分型/买卖点 查找表，供 tooltip formatter 上下文追加
    const fxByDate = new Map(fxList.map(fx => [fx.dt, fx]))
    const markerByDate = new Map(signalMarkers.map(m => [m.dt, m]))
    // date → 信号值 dict (供 tooltip 展示结构状态信号, 如笔表里关系/分型强弱)
    const signalsByDate = new Map((signals ?? []).map(s => [String(s.dt ?? ''), s] as [string, Record<string, string>]))
    const ohlcData = barsToOHLC(bars)
    return { dates, candle, dateIndex, zoomStart, fxByDate, markerByDate, signalsByDate, ohlcData }
  }, [bars, fxList, signalMarkers, signals])

  const buildOption = (): EChartsOption => {
    // 布局: 主图 / [动态副图] / 缩放条
    const SLIDER_H = 22
    const PAD_TOP = 16
    const GAP_MAIN_SUB = 8
    const GAP_SUB_SLIDER = 12
    const PAD_BOTTOM = 8
    const LEFT = 56
    const RIGHT = 24

    const subZoneH = activeSubDefs.length > 0
      ? GAP_MAIN_SUB + subExtraH
      : 0
    const mainH = effectiveHeight - PAD_TOP - subZoneH - GAP_SUB_SLIDER - SLIDER_H - PAD_BOTTOM

    // 价格区间缓冲 (供分型/买卖点标注的纵向偏移基准)
    let pricePad = 0
    if (bars.length > 0) {
      let hi = -Infinity, lo = Infinity
      for (const b of bars) { if (b.high > hi) hi = b.high; if (b.low < lo) lo = b.low }
      pricePad = (hi - lo) * 0.01 || 0
    }
    // 买卖点标注实际需要的轴外空间 (供 yAxis 自适应扩边, 防止 K 线贴边时标注被裁剪)
    let topExtra = 0
    let bottomExtra = 0

    // ===== 分型 markPoint =====
    // 缩小图标并移出 K 线: 顶分型挂在最高价上方, 底分型挂在最低价下方
    const fxMarkPoints: any[] = activeToggles.has('fx')
      ? fxList
          .filter(fx => dateIndex.has(fx.dt))
          .map(fx => ({
            coord: [fx.dt, fx.mark === 'top' ? fx.price + pricePad * 1.2 : fx.price - pricePad * 1.2],
            // 细杆箭头: 底分型↑(看涨红), 顶分型↓(看跌绿, 旋转180°)
            // 路径宽高比 80:100, symbolSize 用数组保持比例 (标量会被拉伸变粗), 短杆箭头
            symbol: 'path://M50,0 L90,50 L62,50 L62,100 L38,100 L38,50 L10,50 Z',
            symbolSize: [8, 10],
            symbolRotate: fx.mark === 'top' ? 180 : 0,
            // 方向语义配色: 底分型红(看涨, 同买点), 顶分型绿(看跌, 同卖点)
            itemStyle: { color: fx.mark === 'top' ? THEME.bear : THEME.bull },
            // 分型强度标注 (强/中/弱), 顶分型标在上方, 底分型标在下方
            label: {
              show: !!fx.power,
              formatter: fx.power ?? '',
              fontSize: 9,
              color: fx.mark === 'top' ? THEME.bear : THEME.bull,
              position: fx.mark === 'top' ? 'top' : 'bottom',
              distance: 1
            },
          }))
      : []

    // ===== 中枢 markArea =====
    const zsMarkAreas: any[] = activeToggles.has('zs') && zsList.length > 0
      ? zsList
          .filter(zs => dateIndex.has(zs.sdt) && dateIndex.has(zs.edt))
          .map(zs => [
            {
              xAxis: zs.sdt,
              yAxis: zs.zg,
              name: '',
              itemStyle: { color: THEME.zs, borderColor: THEME.zsBorder, borderWidth: 1 },
            },
            {
              xAxis: zs.edt,
              yAxis: zs.zd,
            },
          ])
      : []

    // ===== 买卖点 markPoint + 点状指引线 =====
    // 设计: 不加图标, 仅文字 (1B/2S 等), 边框无底色; 点状线从 K 线极值指向文字锚点。
    // 同一天多个信号纵向堆叠 (递增偏移), 避免文字互相 / 与 K 线重叠。
    const baseOffset = pricePad * 12
    const stepOffset = pricePad * 2
    const stackCount = new Map<string, number>()
    const signalMarkPoints: any[] = []
    const signalMarkLines: any[] = []
    if (activeToggles.has('signal')) {
      for (const m of signalMarkers) {
        if (!dateIndex.has(m.dt)) continue
        const bar = bars[dateIndex.get(m.dt)!]
        const n = stackCount.get(m.dt) ?? 0
        stackCount.set(m.dt, n + 1)
        const dist = baseOffset + n * stepOffset
        // 记录该侧最大外扩需求: 锚点距离 + 文字行高余量 (pricePad*4 ≈ 12px)
        if (m.kind === 'buy') bottomExtra = Math.max(bottomExtra, dist + pricePad * 4)
        else topExtra = Math.max(topExtra, dist + pricePad * 4)
        const extreme = m.kind === 'buy' ? bar.low : bar.high
        const anchor = m.kind === 'buy' ? extreme - dist : extreme + dist
        const color = m.kind === 'buy' ? THEME.bull : THEME.bear
        signalMarkPoints.push({
          coord: [m.dt, anchor],
          // 用 1px 透明圆点作 label 锚点 (symbol:'none' 会让 label 不渲染)
          symbol: 'circle',
          symbolSize: 1,
          itemStyle: { color: 'transparent' },
          label: {
            show: true,
            formatter: bsShort(m.label),
            fontSize: 9,
            fontWeight: 600,
            color,
            borderColor: color,
            borderWidth: 0.5,
            backgroundColor: 'transparent',
            borderRadius: 2,
            padding: [0.5, 1],
            position: m.kind === 'buy' ? 'bottom' : 'top',
            distance: 1,
          },
        })
        // 点状线从 K 线极值指向文字锚点, 起点留出空白不贴 K 线
        const lineStart = m.kind === 'buy' ? extreme - pricePad * 1 : extreme + pricePad
        signalMarkLines.push([
          { coord: [m.dt, lineStart], lineStyle: { type: 'dotted', color, width: 1 } },
          { coord: [m.dt, anchor] },
        ])
      }
    }

    const series: any[] = [
      {
        name: 'K',
        type: 'candlestick',
        data: candle,
        animation: false,
        z: 2,
        itemStyle: {
          color: THEME.bull,
          color0: THEME.bear,
          borderColor: THEME.bull,
          borderColor0: THEME.bear,
        },
        markPoint: (fxMarkPoints.length > 0 || signalMarkPoints.length > 0)
          ? { data: [...fxMarkPoints, ...signalMarkPoints], animation: false }
          : undefined,
        markLine: signalMarkLines.length > 0
          ? { symbol: ['none', 'none'], silent: true, animation: false, data: signalMarkLines, label: { show: false } }
          : undefined,
        markArea: zsMarkAreas.length > 0
          ? { silent: true, data: zsMarkAreas }
          : undefined,
      },
    ]

    // ===== 均线 MA5/10/20/60 (主图叠加) =====
    const hasMA = ohlcData.some(d => d.ma5 != null || d.ma10 != null || d.ma20 != null || d.ma60 != null)
    if (hasMA) {
      const maLine = (key: keyof OHLC, color: string, name: string) => ({
        name, type: 'line',
        data: ohlcData.map(d => (d[key] != null ? Number(d[key]) : '-')),
        smooth: true, symbol: 'none', animation: false,
        silent: true,
        lineStyle: { width: 1, color }, itemStyle: { color },
      })
      series.push(maLine('ma5', THEME.ma5, 'MA5'))
      series.push(maLine('ma10', THEME.ma10, 'MA10'))
      series.push(maLine('ma20', THEME.ma20, 'MA20'))
      series.push(maLine('ma60', THEME.ma60, 'MA60'))
    }

    // ===== 笔 line series =====
    // 把 bi 端点展平为 [date, price] 数组，画成一条折线
    // 涨笔红色，跌笔绿色 —— 但 ECharts line series 的每段颜色无法直接按段区分，
    // 这里用分段策略：每条 bi 作为一个独立 line series（两点的折线），统一暖黄色
    if (activeToggles.has('bi')) {
      for (const bi of biList) {
        if (!dateIndex.has(bi.a_dt) || !dateIndex.has(bi.b_dt)) continue
        const aIdx = dateIndex.get(bi.a_dt)!
        const bIdx = dateIndex.get(bi.b_dt)!
        const data: (number | string)[] = dates.map(() => '-')
        data[aIdx] = bi.a_price
        data[bIdx] = bi.b_price
        series.push({
          name: '笔',
          type: 'line',
          data,
          animation: false,
          symbol: 'circle',
          symbolSize: 4,
          z: 3,
          zlevel: 0,
          silent: true,  // 不参与 tooltip / hover，避免 13 条笔全部刷屏
          lineStyle: {
            width: 1.5,
            color: THEME.bi,
          },
          itemStyle: {
            color: THEME.bi,
          },
          connectNulls: true,  // 端点间有 '-' 占位，需连接才能画出笔线段
        })
      }
    }

    // ===== 动态副图 (复用 EChartsCandlestick SUB_CHARTS) =====
    const grids: any[] = [
      { left: LEFT, right: RIGHT, top: PAD_TOP, height: mainH },
    ]
    const xAxes: any[] = [
      {
        type: 'category',
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: CT().grid } },
        axisLabel: {
          color: CT().text,
          fontSize: 10,
          // 分钟族 "YYYY-MM-DD HH:MM" → "MM-DD HH:MM"；日线族 "YYYY-MM-DD" → "MM-DD"
          formatter: (val: string) => val ? val.slice(5) : '',
        },
        splitLine: { show: false },
        axisPointer: { show: true, label: { show: false } },
      },
    ]
    const yAxes: any[] = [
      {
        scale: true,
        // 预留上下边距: 分型/买卖点标注位于数据极值外侧, 不扩大轴范围会被裁剪看不到。
        // 默认 6%, 买卖点偏移较大时按其实际需求 (topExtra/bottomExtra) 自适应加宽
        min: (v: { min: number; max: number }) => v.min - Math.max((v.max - v.min) * 0.06, bottomExtra),
        max: (v: { min: number; max: number }) => v.max + Math.max((v.max - v.min) * 0.06, topExtra),
        splitLine: { lineStyle: { color: CT().grid } },
        axisLabel: { color: CT().text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
      },
    ]
    const xAxisIndices = [0]

    let curTop = PAD_TOP + mainH + GAP_MAIN_SUB
    activeSubDefs.forEach((def, i) => {
      const gridIdx = i + 1
      const xAxisIdx = i + 1
      const yAxisIdx = i + 1
      const chartTop = curTop + INFO_BAR_H

      grids.push({
        left: LEFT, right: RIGHT,
        top: chartTop, height: def.height,
        show: true, borderColor: CT().grid, borderWidth: 1,
      })
      xAxes.push({
        type: 'category', gridIndex: gridIdx, data: dates, boundaryGap: true,
        axisLine: { show: false }, axisLabel: { show: false },
        axisTick: { show: false }, splitLine: { show: false },
        axisPointer: { label: { show: false } },
      })
      const isFixedRange = !!def.yAxisConfig
      yAxes.push({
        scale: !isFixedRange,
        ...(isFixedRange ? def.yAxisConfig : {}),
        gridIndex: gridIdx,
        splitNumber: 2,
        axisLine: { show: false }, axisTick: { show: false },
        splitLine: { lineStyle: { color: CT().grid } },
        axisLabel: {
          show: true, color: CT().text, fontSize: 9,
          fontFamily: 'JetBrains Mono, monospace',
        },
      })
      xAxisIndices.push(xAxisIdx)

      const subSeries = def.buildSeries(ohlcData, { compact: false, volumeCompare })
      subSeries.forEach((s: any) => {
        series.push({ ...s, xAxisIndex: xAxisIdx, yAxisIndex: yAxisIdx })
      })

      curTop += INFO_BAR_H + def.height + SUB_GAP_PX
    })

    return {
      animation: false,
      backgroundColor: 'transparent',
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [
        { type: 'inside', xAxisIndex: xAxisIndices, start: zoomStart, end: 100 },
        {
          type: 'slider',
          xAxisIndex: xAxisIndices,
          bottom: PAD_BOTTOM,
          height: SLIDER_H,
          start: zoomStart,
          end: 100,
          borderColor: 'transparent',
          fillerColor: CT().zoomFill,
          handleStyle: { color: '#52525B' },
          textStyle: { color: CT().text, fontSize: 10 },
          labelFormatter: (val: number) => {
            const dt = dates[Math.round((val / 100) * (dates.length - 1))]
            if (!dt) return ''
            if (dt.includes(' ')) {
              // 分钟族 "YYYY-MM-DD HH:MM" → "MM-DD HH:MM"
              return dt.slice(5)
            }
            // 日线族 "YYYY-MM-DD" → "MM-DD"
            return dt.slice(5)
          },
        },
      ],
      // tooltip 保留但自定义: 只显示日期 + K线 OHLC, 跳过笔 series (13 条会刷屏)
      tooltip: {
        show: true,
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        backgroundColor: CT().tooltipBg,
        borderColor: CT().tooltipBorder,
        textStyle: { color: CT().tooltipText, fontSize: 11 },
        formatter: (params: any) => {
          if (!Array.isArray(params) || params.length === 0) return ''
          const date = params[0]?.axisValue ?? ''
          // 直接从 bars 查原始 OHLC，不依赖 ECharts 内部变换后的 p.data
          // (candlestick 的 p.data 可能被 ECharts 重排/追加字段，导致取值错位)
          const idx = dateIndex.get(date)
          if (idx == null) return `<div style="font-weight:600">${date}</div>`
          const bar = bars[idx]
          const ohlc = ohlcData[idx]
          const up = bar.close >= bar.open
          let html = `<div style="font-weight:600;margin-bottom:2px">${date}</div>`
          html += `<div style="display:flex;gap:8px">`
          html += `<span style="color:${up ? THEME.bull : THEME.bear}">开 ${bar.open.toFixed(2)}</span>`
          html += `<span style="color:${up ? THEME.bull : THEME.bear}">收 ${bar.close.toFixed(2)}</span>`
          html += `<span>低 ${bar.low.toFixed(2)}</span>`
          html += `<span>高 ${bar.high.toFixed(2)}</span>`
          html += `</div>`
          // 均线 MA5/10/20/60
          if (ohlc) {
            const maSpan = (label: string, v: number | null | undefined, color: string) =>
              v != null ? `<span style="color:${color}">${label} ${v.toFixed(2)}</span>` : ''
            const maHtml = [
              maSpan('MA5', ohlc.ma5, THEME.ma5),
              maSpan('MA10', ohlc.ma10, THEME.ma10),
              maSpan('MA20', ohlc.ma20, THEME.ma20),
              maSpan('MA60', ohlc.ma60, THEME.ma60),
            ].filter(Boolean).join(' ')
            if (maHtml) html += `<div style="margin-top:2px;display:flex;gap:8px">${maHtml}</div>`
          }
          // 成交量 + 量比
          const ratio = volumeRatioAt(ohlcData, idx, volumeCompare.days)
          html += `<div style="margin-top:2px;display:flex;gap:8px">`
          html += `<span>量 ${fmtVol(bar.volume)}</span>`
          html += `<span style="color:${ratio != null && ratio >= 1 ? THEME.bull : THEME.bear}">量比 ${fmtVolumeRatio(ratio)}</span>`
          html += `</div>`
          // MACD (激活时显示)
          if (activeIndicators.includes('macd') && ohlc) {
            html += `<div style="margin-top:2px;display:flex;gap:8px">`
            html += `<span style="color:#FACC15">DIF ${ohlc.macd_dif != null ? ohlc.macd_dif.toFixed(3) : '—'}</span>`
            html += `<span style="color:#8B5CF6">DEA ${ohlc.macd_dea != null ? ohlc.macd_dea.toFixed(3) : '—'}</span>`
            html += `<span style="color:${ohlc.macd_hist != null && ohlc.macd_hist >= 0 ? THEME.bull : THEME.bear}">MACD ${ohlc.macd_hist != null ? ohlc.macd_hist.toFixed(3) : '—'}</span>`
            html += `</div>`
          }
          // 当天有分型 → 追加显示 (含分型确认时间, 分型有滞后: 极值点+2根K线才确认)
          const fx = fxByDate.get(date)
          if (fx) {
            const label = fx.mark === 'top' ? '顶分型' : '底分型'
            const color = fx.mark === 'top' ? THEME.bear : THEME.bull
            const confirm = fx.confirm_dt && fx.confirm_dt !== fx.dt ? ` 确认 ${fx.confirm_dt}` : ` ${fx.dt}`
            const power = fx.power ? `(${fx.power})` : ''
            html += `<div style="margin-top:2px;color:${color}">▲ ${label}${power} ${fx.price.toFixed(2)} <span style="opacity:0.65">${confirm}</span></div>`
          }
          // 当天有买卖点 → 追加显示 (含确认时间, 买卖点锚定的分型滞后2根K线确认)
          const marker = markerByDate.get(date)
          if (marker) {
            const color = marker.kind === 'buy' ? THEME.bull : THEME.bear
            const confirm = marker.confirm_dt && marker.confirm_dt !== marker.dt
              ? `<span style="opacity:0.65"> 确认 ${marker.confirm_dt}</span>` : ''
            html += `<div style="margin-top:2px;color:${color}">● ${marker.label} ${(marker.price ?? 0).toFixed(2)}${confirm}</div>`
          }
          // 当天信号值 (结构状态信号, 如笔表里关系/分型强弱; 跳过未触发的买卖点"其他")
          const sigs = signalsByDate.get(date)
          if (sigs) {
            const BAR_KEYS = new Set(['dt', 'close', 'open', 'high', 'low', 'vol', 'amount', 'symbol', 'freq', 'id'])
            const lines: string[] = []
            for (const [k, v] of Object.entries(sigs)) {
              if (BAR_KEYS.has(k) || typeof v !== 'string') continue
              if (v.startsWith('其他')) continue  // 跳过未触发的买卖点
              // key 形如 "日线_D1_表里关系V230102" → 去掉频率前缀, 留可读部分
              const idx = k.indexOf('_')
              const label = idx >= 0 ? k.slice(idx + 1) : k
              // value 形如 "向上_顶分_任意_0" → 取 v1_v2_v3 (去 score), 过滤"任意"占位段
              const valShort = v.split('_').slice(0, 3).filter(seg => seg !== '任意').join(' ')
              lines.push(`${label}: ${valShort}`)
            }
            if (lines.length > 0) {
              html += `<div style="margin-top:2px;padding-top:2px;border-top:1px dashed ${CT().grid};font-size:10px;color:${CT().tooltipText};opacity:0.85">`
              html += lines.slice(0, 20).map(l => `<div>${l}</div>`).join('')
              if (lines.length > 20) html += `<div style="opacity:0.6">…+${lines.length - 20}</div>`
              html += `</div>`
            }
          }
          return html
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      // 隐藏默认 legend: 每个 bi 单独一个 line series 会产生 N 个重复图例条目,
      // 而 toggle 按钮已控制各层显隐, legend 多余。
      legend: { show: false },
      series,
    }
  }

  // 初始化 + 数据更新
  useEffect(() => {
    if (!chartRef.current) return
    if (!chartInstRef.current) {
      chartInstRef.current = echarts.init(chartRef.current, undefined, { renderer: 'canvas' })
    }
    chartInstRef.current.resize()
    chartInstRef.current.setOption(buildOption(), true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bars, fxList, biList, zsList, signalMarkers, signals, effectiveHeight, theme, activeToggles, activeIndicators, volumeCompare])

  // resize: 窗口尺寸 + 容器尺寸 (侧栏收起/展开改变容器宽度时重绘图表)
  useEffect(() => {
    const inst = chartInstRef.current
    if (!inst) return
    const onResize = () => inst.resize()
    window.addEventListener('resize', onResize)
    const el = chartRef.current
    const ro = new ResizeObserver(() => inst.resize())
    if (el) ro.observe(el)
    return () => {
      window.removeEventListener('resize', onResize)
      ro.disconnect()
      inst.dispose()
      chartInstRef.current = null
    }
  }, [])

  const toggleType = (t: string) => {
    setActiveToggles(prev => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  const toggleIndicator = (key: string) => {
    setActiveIndicators(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }

  const updateVolumeCompare = (patch: Partial<VolumeCompareConfig>) => {
    setVolumeCompare(prev => {
      const next = normalizeVolumeCompare({ ...prev, ...patch })
      storage.stockVolumeCompare.set(next)
      return next
    })
  }

  const zsEmpty = zsList.length === 0

  return (
    <div ref={wrapRef} className={isFs ? 'bg-base p-4' : undefined}>
      {/* 开关按钮组 */}
      <div className="flex flex-wrap items-center gap-1.5 mb-2">
        <span className="text-[10px] text-muted mr-1">缠论结构</span>
        {TOGGLE_GROUPS.map(g => {
          const active = activeToggles.has(g.key)
          const disabled = g.key === 'zs' && zsEmpty
          return (
            <button
              key={g.key}
              onClick={() => toggleType(g.key)}
              disabled={disabled}
              title={disabled ? '暂无中枢数据' : g.label}
              className={`inline-flex items-center gap-1 h-6 px-2 rounded-md text-[10px] font-medium border transition-all disabled:opacity-30 disabled:cursor-not-allowed ${
                active
                  ? 'text-foreground'
                  : 'text-muted bg-base/40 border-border/30 hover:border-border/60'
              }`}
              style={active ? { borderColor: g.color + '66', backgroundColor: g.color + '1a' } : undefined}
            >
              <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: active ? g.color : '#52525B' }} />
              {g.label}
            </button>
          )
        })}

        {/* 副图切换 */}
        <span className="text-[10px] text-muted ml-2 mr-1">副图</span>
        {CZSC_SUB_KEYS.map(key => {
          const ind = SUB_CHARTS.find(s => s.key === key)
          if (!ind) return null
          const active = activeIndicators.includes(ind.key)
          return (
            <button
              key={ind.key}
              onClick={() => toggleIndicator(ind.key)}
              className={`px-2 h-6 rounded-md text-[10px] font-mono cursor-pointer transition-colors border ${
                active
                  ? 'text-accent border-accent/40 bg-accent/10'
                  : 'text-muted bg-base/40 border-border/30 hover:border-border/60'
              }`}
            >
              {ind.label}
            </button>
          )
        })}

        {/* 量比控件 (成交量激活时) */}
        {activeIndicators.includes('vol') && (
          <div className="ml-0.5 flex h-6 items-center gap-1.5 border-l border-border/70 pl-2">
            <span className="text-[10px] text-muted">量比</span>
            <button
              type="button"
              role="switch"
              aria-checked={volumeCompare.enabled}
              aria-label="开启量能对比"
              title={volumeCompare.enabled ? '关闭量能对比' : '开启量能对比'}
              onClick={() => updateVolumeCompare({ enabled: !volumeCompare.enabled })}
              className={`relative h-3.5 w-6 shrink-0 rounded-full transition-colors ${
                volumeCompare.enabled ? 'bg-accent' : 'bg-elevated'
              }`}
            >
              <span className={`absolute left-0 top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-transform ${
                volumeCompare.enabled ? 'translate-x-3' : 'translate-x-0.5'
              }`} />
            </button>
            <select
              aria-label="量能对比周期"
              value={volumeCompare.days}
              disabled={!volumeCompare.enabled}
              onChange={event => updateVolumeCompare({ days: Number(event.target.value) })}
              className="h-5 rounded border border-border bg-base px-1 text-[10px] text-secondary outline-none disabled:opacity-40"
            >
              {Array.from({ length: 20 }, (_, index) => index + 1).map(days => (
                <option key={days} value={days}>前{days}日均量</option>
              ))}
            </select>
          </div>
        )}

        {/* 全屏切换 */}
        <button
          onClick={toggleFullscreen}
          title={isFs ? '退出全屏' : '全屏'}
          className="ml-auto inline-flex items-center justify-center h-6 w-6 rounded-md border border-border/30 text-muted hover:text-foreground hover:border-border/60 transition-all"
        >
          {isFs ? <Minimize2 className="h-3 w-3" /> : <Maximize2 className="h-3 w-3" />}
        </button>
      </div>
      <div ref={chartRef} style={{ width: '100%', height: effectiveHeight }} />
    </div>
  )
}

/** 买卖点标签简写: "一类买点" → "1B", "二类卖点" → "2S", "三类买点" → "3B" */
function bsShort(label: string): string {
  const m = label.match(/([一二三])类([买卖])点/)
  if (m) {
    const num = (m[1] === '一' ? 1 : m[1] === '二' ? 2 : 3)
    const bs = m[2] === '买' ? 'B' : 'S'
    return `${num}${bs}`
  }
  return label.replace(/[类点]/g, '')
}
