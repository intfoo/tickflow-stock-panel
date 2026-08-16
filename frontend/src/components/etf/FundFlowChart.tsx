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

export function FundFlowChart({ flow, overlayIndex, onOverlayChange, statDays, onStatDaysChange }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)
  const ct = useChartTheme()

  const overlayName = OVERLAY_INDEXES.find(i => i.symbol === overlayIndex)?.name ?? overlayIndex

  // 拉叠加指数日K
  const overlayQuery = useQuery({
    queryKey: ['etf-fund', 'overlay-index', overlayIndex] as const,
    queryFn: () => api.klineDaily(overlayIndex, 300),
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
  })

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    let chart = chartRef.current
    if (!chart) {
      chart = echarts.init(el, undefined, { renderer: 'canvas' })
      chartRef.current = chart
      roRef.current = new ResizeObserver(() => chart!.resize())
      roRef.current.observe(el)
    }

    const series = flow.series
    const dates = series.map(s => s.trade_date)
    const amounts = series.map(s => s.amount)
    const overlayRows = overlayQuery.data?.rows ?? []
    const overlayMap = new Map(overlayRows.map(r => [r.date.slice(0, 10), r.close]))
    const overlayData = dates.map(d => overlayMap.get(d) ?? null)

    // 拆两个堆叠系列, 图例红/绿与柱体精确一致 (单系列逐点上色时图例只能取默认色)
    const inflowPos = amounts.map(v => (v >= 0 ? v : null))
    const inflowNeg = amounts.map(v => (v < 0 ? v : null))

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
      },
      legend: {
        data: ['宽基ETF申购净流入', '宽基ETF申购净流出', overlayName],
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
          type: 'value',
          name: '净流入(亿)',
          nameTextStyle: { color: ct.text, fontSize: 9 },
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: ct.text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace' },
          splitLine: { lineStyle: { color: ct.grid } },
        },
        {
          type: 'value',
          name: overlayName,
          nameTextStyle: { color: ct.text, fontSize: 9 },
          scale: true,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: ct.text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace' },
          splitLine: { show: false },
        },
      ],
      dataZoom: [
        { type: 'inside', start: 0, end: 100 },
        { type: 'slider', height: 18, bottom: 4, brushSelect: false, dataBackground: { lineStyle: { color: ct.border }, areaStyle: { color: ct.zoomFill } } },
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
