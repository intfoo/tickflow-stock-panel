import { useEffect, useRef, useMemo, useState } from 'react'
import { chartTheme, getTheme, useTheme } from '@/lib/theme'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { Maximize2, Minimize2 } from 'lucide-react'

/**
 * 缠论分析专用 K 线图表。
 *
 * 复刻 AnalysisKChart 的 ECharts 构建模式（candlestick + volume bar + dataZoom），
 * 叠加缠论结构：分型(markPoint) / 笔(line) / 中枢(markArea) / 买卖点(markPoint)。
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
  signalMarkers: { dt: string; kind: 'buy' | 'sell'; label: string; price: number }[]
  signals?: Record<string, string>[]
  height?: number
}

const VOL_PANE_H = 90

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

  // 全屏时图表高度撑满视口 (留 80px 给开关行 + padding)
  const effectiveHeight = isFs ? Math.max(window.innerHeight - 80, 300) : height

  // 数据预处理
  // date 用完整字符串：日线族 "YYYY-MM-DD"，分钟族 "YYYY-MM-DD HH:MM"（含空格）。
  // 不再 slice(0,10)，否则分钟族 dt 无法匹配 dateIndex。
  const { dates, candle, vols, dateIndex, zoomStart, fxByDate, markerByDate, signalsByDate } = useMemo(() => {
    const dates = bars.map(r => (typeof r.date === 'string' ? r.date : String(r.date)))
    const candle = bars.map(r => [r.open, r.close, r.low, r.high])
    const vols = bars.map(r => ({
      value: r.volume ?? 0,
      itemStyle: { color: r.close >= r.open ? THEME.volUp : THEME.volDown },
    }))
    const dateIndex = new Map(dates.map((d, i) => [d, i]))
    const showBars = 120
    const zoomStart = dates.length > showBars ? Math.round((1 - showBars / dates.length) * 100) : 0
    // date → 分型/买卖点 查找表，供 tooltip formatter 上下文追加
    const fxByDate = new Map(fxList.map(fx => [fx.dt, fx]))
    const markerByDate = new Map(signalMarkers.map(m => [m.dt, m]))
    // date → 信号值 dict (供 tooltip 展示结构状态信号, 如笔表里关系/分型强弱)
    const signalsByDate = new Map((signals ?? []).map(s => [String(s.dt ?? ''), s] as [string, Record<string, string>]))
    return { dates, candle, vols, dateIndex, zoomStart, fxByDate, markerByDate, signalsByDate }
  }, [bars, fxList, signalMarkers, signals])

  const buildOption = (): EChartsOption => {
    // 布局: 主图 / 成交量 / 缩放条
    const SLIDER_H = 22
    const PAD_TOP = 16
    const GAP_MAIN_VOL = 8
    const GAP_VOL_SLIDER = 12
    const PAD_BOTTOM = 8
    const volH = VOL_PANE_H
    const mainH = effectiveHeight - PAD_TOP - GAP_MAIN_VOL - volH - GAP_VOL_SLIDER - SLIDER_H - PAD_BOTTOM
    const volTop = PAD_TOP + mainH + GAP_MAIN_VOL
    const sliderBottom = PAD_BOTTOM

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
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: vols,
        animation: false,
      },
    ]

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

    return {
      animation: false,
      backgroundColor: 'transparent',
      grid: [
        { left: 56, right: 24, top: 16, height: mainH },
        { left: 56, right: 24, top: volTop, height: volH },
      ],
      // axisLabel 格式化：分钟族显示 "MM-DD HH:MM"，日线族显示 "MM-DD"
      // （ECharts category 轴的 axisLabel formatter 接收原始 category 值）
      xAxis: [
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
        {
          type: 'category',
          gridIndex: 1,
          data: dates,
          boundaryGap: true,
          axisLabel: { show: false },
          axisLine: { show: false },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          // 预留上下边距: 分型/买卖点标注位于数据极值外侧, 不扩大轴范围会被裁剪看不到。
          // 默认 6%, 买卖点偏移较大时按其实际需求 (topExtra/bottomExtra) 自适应加宽
          min: (v: { min: number; max: number }) => v.min - Math.max((v.max - v.min) * 0.06, bottomExtra),
          max: (v: { min: number; max: number }) => v.max + Math.max((v.max - v.min) * 0.06, topExtra),
          splitLine: { lineStyle: { color: CT().grid } },
          axisLabel: { color: CT().text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
        },
        {
          scale: true,
          gridIndex: 1,
          splitNumber: 2,
          splitLine: { show: false },
          axisLabel: {
            color: CT().text,
            fontSize: 10,
            fontFamily: 'JetBrains Mono, monospace',
            formatter: (v: number) => fmtVol(v),
          },
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100 },
        {
          type: 'slider',
          xAxisIndex: [0, 1],
          bottom: sliderBottom,
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
          const up = bar.close >= bar.open
          let html = `<div style="font-weight:600;margin-bottom:2px">${date}</div>`
          html += `<div style="display:flex;gap:8px">`
          html += `<span style="color:${up ? THEME.bull : THEME.bear}">开 ${bar.open.toFixed(2)}</span>`
          html += `<span style="color:${up ? THEME.bull : THEME.bear}">收 ${bar.close.toFixed(2)}</span>`
          html += `<span>低 ${bar.low.toFixed(2)}</span>`
          html += `<span>高 ${bar.high.toFixed(2)}</span>`
          html += `</div>`
          // 当天有分型 → 追加显示 (含分型确认时间, 分型有滞后: 极值点+2根K线才确认)
          const fx = fxByDate.get(date)
          if (fx) {
            const label = fx.mark === 'top' ? '顶分型' : '底分型'
            const color = fx.mark === 'top' ? THEME.bear : THEME.bull
            const confirm = fx.confirm_dt && fx.confirm_dt !== fx.dt ? ` 确认 ${fx.confirm_dt}` : ` ${fx.dt}`
            const power = fx.power ? `(${fx.power})` : ''
            html += `<div style="margin-top:2px;color:${color}">▲ ${label}${power} ${fx.price.toFixed(2)} <span style="opacity:0.65">${confirm}</span></div>`
          }
          // 当天有买卖点 → 追加显示
          const marker = markerByDate.get(date)
          if (marker) {
            const color = marker.kind === 'buy' ? THEME.bull : THEME.bear
            html += `<div style="margin-top:2px;color:${color}">● ${marker.label} ${(marker.price ?? 0).toFixed(2)}</div>`
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
  }, [bars, fxList, biList, zsList, signalMarkers, signals, effectiveHeight, theme, activeToggles])

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

function fmtVol(v: number): string {
  if (!v) return '0'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v.toFixed(0)
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
