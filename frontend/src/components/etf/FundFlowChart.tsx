import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { keepPreviousData } from '@tanstack/react-query'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { api, type EtfFlowResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'

const OVERLAY_INDEXES = [
  { symbol: '000001.SH', name: '上证指数' },
  { symbol: '000300.SH', name: '沪深300' },
  { symbol: '399006.SZ', name: '创业板指' },
  { symbol: '000688.SH', name: '科创50' },
  { symbol: '399001.SZ', name: '深证成指' },
]

interface Props {
  flow: EtfFlowResult
  overlayIndex: string
  onOverlayChange: (symbol: string) => void
  statDays: 5 | 20 | 60
  onStatDaysChange: (days: 5 | 20 | 60) => void
}

const RED = '#EF4444'
const GREEN = '#10B981'

function fmtVal(v: number | null | undefined): string {
  if (v == null) return '--'
  return v.toFixed(2)
}

function valColor(v: number | null | undefined): string {
  if (v == null) return 'text-muted'
  return v >= 0 ? 'text-bull' : 'text-bear'
}

/** 从 startIdx 起逐日累加 (之前的点返回 null, 线从可见第一天开始) */
function cumFrom(amounts: number[], startIdx: number): (number | null)[] {
  let cum = 0
  return amounts.map((v, i) => {
    if (i < startIdx) return null
    cum = Math.round((cum + v) * 1e4) / 1e4
    return cum
  })
}

/** 可见窗口内柱轴范围 (含 0 基线 + 10% 余量), 让柱子始终撑满可视高度 */
function barRange(amounts: number[], startIdx: number, endIdx: number): { min: number; max: number } {
  let lo = 0
  let hi = 0
  for (let i = startIdx; i <= endIdx && i < amounts.length; i++) {
    const v = amounts[i]
    if (v < lo) lo = v
    if (v > hi) hi = v
  }
  const pad = Math.max((hi - lo) * 0.1, 0.01)
  return {
    min: Math.floor((lo - pad) * 100) / 100,
    max: Math.ceil((hi + pad) * 100) / 100,
  }
}

export function FundFlowChart({ flow, overlayIndex, onOverlayChange, statDays, onStatDaysChange }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)
  const amountsRef = useRef<number[]>([])
  const datesRef = useRef<string[]>([])
  const cumStartDateRef = useRef('')
  const ct = useChartTheme()

  const overlayName = OVERLAY_INDEXES.find(i => i.symbol === overlayIndex)?.name ?? overlayIndex

  // 拉叠加指数日K (与 flow 全量窗口对齐)
  const overlayQuery = useQuery({
    queryKey: ['etf-fund', 'overlay-index', overlayIndex] as const,
    queryFn: () => api.klineDaily(overlayIndex, 750),
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
  })

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const series = flow.series
    const dates = series.map(s => s.trade_date)
    const amounts = series.map(s => s.amount)
    amountsRef.current = amounts
    datesRef.current = dates

    let chart = chartRef.current
    if (!chart) {
      chart = echarts.init(el, undefined, { renderer: 'canvas' })
      chartRef.current = chart
      roRef.current = new ResizeObserver(() => chart!.resize())
      roRef.current.observe(el)
      // 拖动/缩放 dataZoom 时: 1) 累计净流入从可见窗口第一天起重算
      // 2) 柱轴按可见窗口内柱值动态缩放 (柱子在任何区间都撑满可视高度)
      chart.on('dataZoom', () => {
        const c = chartRef.current
        if (!c) return
        const dz = (c.getOption().dataZoom as { start?: number; end?: number }[] | undefined)?.[0]
        const start = dz?.start ?? 0
        const endPct = dz?.end ?? 100
        const arr = amountsRef.current
        const n = arr.length
        if (n === 0) return
        const startIdx = Math.round((start / 100) * (n - 1))
        const endIdx = Math.min(n - 1, Math.round((endPct / 100) * (n - 1)))
        cumStartDateRef.current = datesRef.current[startIdx] ?? ''
        const range = barRange(arr, startIdx, endIdx)
        c.setOption({
          series: [{ id: 'cum', data: cumFrom(arr, startIdx) }],
          yAxis: [{ min: range.min, max: range.max }],
        })
      })
    }

    const overlayRows = overlayQuery.data?.rows ?? []
    const overlayMap = new Map(overlayRows.map(r => [r.date.slice(0, 10), r.close]))
    const overlayData = dates.map(d => overlayMap.get(d) ?? null)

    // 拆两个堆叠系列, 图例红/绿与柱体精确一致 (单系列逐点上色时图例只能取默认色)
    const inflowPos = amounts.map(v => (v >= 0 ? v : null))
    const inflowNeg = amounts.map(v => (v < 0 ? v : null))

    // dataZoom 默认聚焦最近 120 个交易日 (全量数据已加载, 可拖回更早)
    const zoomStartIdx = dates.length > 120 ? dates.length - 120 : 0
    const zoomStart = dates.length > 1 ? (zoomStartIdx / (dates.length - 1)) * 100 : 0
    cumStartDateRef.current = dates[zoomStartIdx] ?? ''

    // 累计净流入: 从可见窗口第一天起逐日累加 (亿元, 独立隐藏轴)
    const cumulative = cumFrom(amounts, zoomStartIdx)

    // 柱轴初始范围按默认窗口内柱值 (之后随 dataZoom 动态缩放)
    const initBarRange = barRange(amounts, zoomStartIdx, dates.length - 1)

    const option: EChartsOption = {
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        borderWidth: 1,
        textStyle: { color: ct.tooltipText, fontSize: 11 },
        formatter: (params: unknown) => {
          const items = params as {
            axisValueLabel?: string
            axisValue?: string
            marker?: string
            seriesName?: string
            seriesId?: string
            value?: number | null
          }[]
          if (!Array.isArray(items) || items.length === 0) return ''
          const title = items[0].axisValueLabel ?? items[0].axisValue ?? ''
          const lines = items.map(p => {
            const v = p.value == null ? '--' : Number(p.value).toFixed(2)
            if (p.seriesId === 'cum') {
              return `${p.marker ?? ''} 累计净流入(自${cumStartDateRef.current || '--'}起): ${v}亿`
            }
            return `${p.marker ?? ''} ${p.seriesName}: ${v}`
          })
          return [title, ...lines].join('<br/>')
        },
      },
      legend: {
        data: ['宽基ETF申购净流入', '宽基ETF申购净流出', '累计净流入', overlayName],
        top: 0,
        textStyle: { color: ct.text, fontSize: 10 },
        itemWidth: 12,
        itemHeight: 8,
      },
      grid: { left: 56, right: 56, top: 32, bottom: 48 },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: ct.border } },
        axisLabel: { color: ct.text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace' },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: [
        {
          // 左轴: 每日净流入柱 (随可见窗口动态缩放, 见 dataZoom 处理)
          type: 'value',
          name: '净流入(亿)',
          nameTextStyle: { color: ct.text, fontSize: 9 },
          min: initBarRange.min,
          max: initBarRange.max,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: ct.text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace' },
          splitLine: { lineStyle: { color: ct.grid } },
        },
        {
          // 右轴: 叠加指数
          type: 'value',
          name: overlayName,
          nameTextStyle: { color: ct.text, fontSize: 9 },
          scale: true,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: ct.text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace' },
          splitLine: { show: false },
        },
        {
          // 累计净流入独立轴: 隐藏刻度, 只作独立缩放 (避免千亿级累计值压扁每日柱)
          type: 'value',
          show: false,
          scale: true,
        },
      ],
      dataZoom: [
        // 默认窗口聚焦最近 120 个交易日, 拖滑块/滚轮可回看全部已同步历史
        { type: 'inside', start: zoomStart, end: 100 },
        { type: 'slider', height: 18, bottom: 4, brushSelect: false, start: zoomStart, end: 100, dataBackground: { lineStyle: { color: ct.border }, areaStyle: { color: ct.zoomFill } } },
      ],
      series: [
        {
          name: '宽基ETF申购净流入',
          type: 'bar',
          stack: 'flow',
          data: inflowPos,
          itemStyle: { color: RED },
          yAxisIndex: 0,
        },
        {
          name: '宽基ETF申购净流出',
          type: 'bar',
          stack: 'flow',
          data: inflowNeg,
          itemStyle: { color: GREEN },
          yAxisIndex: 0,
        },
        {
          id: 'cum',
          name: '累计净流入',
          type: 'line',
          data: cumulative,
          yAxisIndex: 2,
          symbol: 'none',
          lineStyle: { width: 1.4, color: '#F59E0B' },
          itemStyle: { color: '#F59E0B' },
          z: 3,
        },
        {
          name: overlayName,
          type: 'line',
          data: overlayData,
          yAxisIndex: 1,
          symbol: 'none',
          lineStyle: { width: 1.2, color: '#9CA3AF' },
          itemStyle: { color: '#9CA3AF' },
          connectNulls: true,
        },
      ],
    }

    if (series.length > 0) {
      chart.setOption(option, true)
    } else {
      chart.clear()
    }
  }, [flow, overlayQuery.data, overlayName, ct])

  useEffect(() => {
    return () => {
      roRef.current?.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
      roRef.current = null
    }
  }, [])

  const stats = flow.stats
  const statVal = statDays === 5 ? stats.d5 : statDays === 20 ? stats.d20 : stats.d60

  return (
    <div className="w-full">
      {/* 统计头 */}
      <div className="flex items-center gap-x-4 px-1 pb-2 text-xs">
        <span className="text-muted">昨日净流入:</span>
        <span className={`font-mono font-semibold ${valColor(stats.yesterday)}`}>
          {stats.yesterday != null ? (stats.yesterday >= 0 ? '+' : '') + fmtVal(stats.yesterday) : '--'}亿
        </span>
        <span className="text-muted">近{statDays}日净流入:</span>
        <span className={`font-mono font-semibold ${valColor(statVal)}`}>
          {statVal != null ? (statVal >= 0 ? '+' : '') + fmtVal(statVal) : '--'}亿
        </span>
        <select
          value={statDays}
          onChange={e => onStatDaysChange(Number(e.target.value) as 5 | 20 | 60)}
          className="rounded border border-border bg-base px-1 py-0.5 text-[10px] text-secondary outline-none"
        >
          <option value={5}>5日</option>
          <option value={20}>20日</option>
          <option value={60}>60日</option>
        </select>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-muted">叠加指数:</span>
          <select
            value={overlayIndex}
            onChange={e => onOverlayChange(e.target.value)}
            className="rounded border border-border bg-base px-1.5 py-0.5 text-[10px] text-secondary outline-none"
          >
            {OVERLAY_INDEXES.map(idx => (
              <option key={idx.symbol} value={idx.symbol}>{idx.name}</option>
            ))}
          </select>
        </div>
      </div>
      <div className="mb-1 px-1 text-[10px] text-muted">
        数据截至 {stats.data_end_date ?? '--'}
      </div>
      {flow.series.length === 0 ? (
        <div className="flex h-[360px] flex-col items-center justify-center gap-1 text-xs text-muted">
          {flow.broad_count === 0 ? (
            flow.is_default ? (
              <>
                <span>ETF 维表为空</span>
                <span className="text-[10px] text-muted/60">请先在「数据」页同步 ETF 标的与日K</span>
              </>
            ) : (
              <>
                <span>宽基清单为空（已主动清空）</span>
                <span className="text-[10px] text-muted/60">请到「宽基配置」重新选择，或点「恢复默认」</span>
              </>
            )
          ) : (
            <>
              <span>暂无净流入数据，请先在「数据同步」中回填</span>
            </>
          )}
        </div>
      ) : (
        <div ref={containerRef} style={{ width: '100%', height: 360 }} />
      )}
    </div>
  )
}
