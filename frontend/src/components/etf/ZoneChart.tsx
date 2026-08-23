import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { etfFundApi, type EtfZonePoint } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'

const OPP_AREA = 'rgba(16,185,129,0.12)' // 机会区底色 (绿)
const RISK_AREA = 'rgba(239,68,68,0.12)' // 风险区底色 (红)
const OPP_SOLID = '#10B981'
const RISK_SOLID = '#EF4444'
const RED = '#EF4444'   // 净流入 (A股配色)
const GREEN = '#10B981' // 净流出

interface Props {
  overlayIndex: string
}

/** 连续同区段合并为 markArea 区间 */
function zoneSegments(pts: EtfZonePoint[], kind: 'opp' | 'risk') {
  const segs: [{ xAxis: string; itemStyle?: { color: string } }, { xAxis: string }][] = []
  let start = -1
  const color = kind === 'opp' ? OPP_AREA : RISK_AREA
  for (let i = 0; i < pts.length; i++) {
    if (pts[i].zone === kind) {
      if (start < 0) start = i
    } else if (start >= 0) {
      segs.push([{ xAxis: pts[start].trade_date, itemStyle: { color } }, { xAxis: pts[i - 1].trade_date }])
      start = -1
    }
  }
  if (start >= 0) segs.push([{ xAxis: pts[start].trade_date, itemStyle: { color } }, { xAxis: pts[pts.length - 1].trade_date }])
  return segs
}

export function ZoneChart({ overlayIndex }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)
  const ct = useChartTheme()

  const query = useQuery({
    queryKey: QK.etfFundZones(overlayIndex),
    queryFn: () => etfFundApi.getZones(overlayIndex, 750),
    staleTime: 5 * 60_000,
  })

  const pts = query.data?.series ?? []

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (pts.length === 0) {
      chartRef.current?.clear()
      return
    }

    let chart = chartRef.current
    if (!chart) {
      chart = echarts.init(el, undefined, { renderer: 'canvas' })
      chartRef.current = chart
      roRef.current = new ResizeObserver(() => chart!.resize())
      roRef.current.observe(el)
    }

    const dates = pts.map(p => p.trade_date)
    const close = pts.map(p => p.close)
    // 拆两个堆叠系列, 图例红/绿与柱体精确一致 (单系列逐点上色时图例只能取默认色)
    const i20Pos = pts.map(p => (p.i20 != null && p.i20 >= 0 ? p.i20 : null))
    const i20Neg = pts.map(p => (p.i20 != null && p.i20 < 0 ? p.i20 : null))
    const pct = pts.map(p => (p.i20_pct == null ? null : Math.round(p.i20_pct * 1000) / 10))
    const pxPct = pts.map(p => p.px_pct)
    const zones = pts.map(p => p.zone)
    const divs = pts.map(p => p.div)

    // 底背离标记 (三角, 位于价格线下方)
    const divPoints = pts
      .map((p, i) => (p.div && p.close != null ? { name: 'div', coord: [p.trade_date, p.close] as (string | number)[], i } : null))
      .filter(Boolean) as { name: string; coord: (string | number)[] }[]

    // 默认聚焦最近 250 个交易日
    const zoomStartIdx = dates.length > 250 ? dates.length - 250 : 0
    const zoomStart = dates.length > 1 ? (zoomStartIdx / (dates.length - 1)) * 100 : 0

    const option: EChartsOption = {
      animation: false,
      backgroundColor: 'transparent',
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis',
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        borderWidth: 1,
        textStyle: { color: ct.tooltipText, fontSize: 11 },
        formatter: (params: unknown) => {
          const items = params as { dataIndex?: number; axisValueLabel?: string; axisValue?: string }[]
          if (!Array.isArray(items) || items.length === 0) return ''
          const i = items[0].dataIndex ?? 0
          const title = items[0].axisValueLabel ?? items[0].axisValue ?? ''
          const zoneTxt = zones[i] === 'opp' ? '<span style="color:#10B981">机会区</span>'
            : zones[i] === 'risk' ? '<span style="color:#EF4444">风险区</span>' : '中性'
          const fmt = (v: number | null | undefined, d = 2) => (v == null ? '--' : v.toFixed(d))
          const pctTxt = (v: number | null | undefined) => (v == null ? '--' : `${(v * 100).toFixed(0)}%`)
          return [
            `${title} ${zoneTxt}${divs[i] ? ' <span style="color:#10B981">▲底背离</span>' : ''}`,
            `指数: ${fmt(close[i])}`,
            `20日净流入: ${fmt(pts[i]?.i20)}亿`,
            `流入分位: ${pctTxt(pct[i] == null ? null : pct[i] / 100)} | 价格分位: ${pctTxt(pxPct[i])}`,
          ].join('<br/>')
        },
      },
      legend: {
        data: ['指数', '净流入(20日)', '净流出(20日)', '流入分位'],
        top: 0,
        textStyle: { color: ct.text, fontSize: 10 },
        itemWidth: 12,
        itemHeight: 8,
      },
      grid: [
        { left: 56, right: 56, top: 30, height: '44%' },
        { left: 56, right: 56, top: '60%', height: '22%' },
      ],
      xAxis: [
        {
          type: 'category', gridIndex: 0, data: dates,
          axisLine: { lineStyle: { color: ct.border } },
          axisLabel: { color: ct.text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace' },
          axisTick: { show: false }, splitLine: { show: false },
        },
        {
          type: 'category', gridIndex: 1, data: dates,
          axisLine: { lineStyle: { color: ct.border } },
          axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false },
        },
      ],
      yAxis: [
        {
          type: 'value', gridIndex: 0, scale: true,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: ct.text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace' },
          splitLine: { lineStyle: { color: ct.grid } },
        },
        {
          type: 'value', gridIndex: 1, name: '净流入(亿)',
          nameTextStyle: { color: ct.text, fontSize: 9 },
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: ct.text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace' },
          splitLine: { lineStyle: { color: ct.grid } },
        },
        {
          type: 'value', gridIndex: 1, min: 0, max: 100, name: '流入分位%',
          nameTextStyle: { color: ct.text, fontSize: 9 },
          position: 'right',
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: ct.text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace' },
          splitLine: { show: false },
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100 },
        {
          type: 'slider', xAxisIndex: [0, 1], height: 18, bottom: 4, brushSelect: false,
          start: zoomStart, end: 100,
          dataBackground: { lineStyle: { color: ct.border }, areaStyle: { color: ct.zoomFill } },
        },
      ],
      series: [
        {
          name: '指数',
          type: 'line',
          xAxisIndex: 0, yAxisIndex: 0,
          data: close,
          symbol: 'none',
          lineStyle: { width: 1.4, color: '#9CA3AF' },
          itemStyle: { color: '#9CA3AF' },
          z: 3,
          markArea: {
            silent: true,
            data: [...zoneSegments(pts, 'opp'), ...zoneSegments(pts, 'risk')],
          },
          markPoint: {
            symbol: 'triangle',
            symbolSize: 9,
            symbolOffset: [0, 14],
            itemStyle: { color: OPP_SOLID },
            label: { show: false },
            data: divPoints,
          },
        },
        {
          name: '净流入(20日)',
          type: 'bar',
          stack: 'i20',
          xAxisIndex: 1, yAxisIndex: 1,
          data: i20Pos,
          itemStyle: { color: RED },
        },
        {
          name: '净流出(20日)',
          type: 'bar',
          stack: 'i20',
          xAxisIndex: 1, yAxisIndex: 1,
          data: i20Neg,
          itemStyle: { color: GREEN },
        },
        {
          name: '流入分位',
          type: 'line',
          xAxisIndex: 1, yAxisIndex: 2,
          data: pct,
          symbol: 'none',
          lineStyle: { width: 1.2, color: '#F59E0B' },
          itemStyle: { color: '#F59E0B' },
          connectNulls: true,
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { type: 'dashed', width: 1 },
            data: [
              { yAxis: 67, lineStyle: { color: RISK_SOLID, type: 'dashed' } },
              { yAxis: 33, lineStyle: { color: OPP_SOLID, type: 'dashed' } },
            ],
          },
        },
      ],
    }
    chart.setOption(option, true)
  }, [pts, ct])

  useEffect(() => {
    return () => {
      roRef.current?.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
      roRef.current = null
    }
  }, [])

  const latest = pts.length > 0 ? pts[pts.length - 1] : null
  const latestZone = latest?.zone === 'opp' ? { txt: '机会区', cls: 'text-bear' }
    : latest?.zone === 'risk' ? { txt: '风险区', cls: 'text-bull' }
      : { txt: '中性', cls: 'text-muted' }

  return (
    <div className="w-full">
      {/* 说明头 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-1 pb-2 text-xs">
        <span className="font-medium text-secondary">指数风险/机会区</span>
        <span className="text-muted">
          当前: <span className={`font-semibold ${latestZone.cls}`}>{latestZone.txt}</span>
          {latest?.i20_pct != null && (
            <span className="ml-1 text-[10px]">
              (流入分位 {(latest.i20_pct * 100).toFixed(0)}% / 价格分位 {latest.px_pct != null ? (latest.px_pct * 100).toFixed(0) : '--'}%)
            </span>
          )}
        </span>
        <span className="ml-auto flex items-center gap-2 text-[10px] text-muted">
          <span><span className="inline-block h-2 w-2 rounded-sm" style={{ background: OPP_AREA, border: `1px solid ${OPP_SOLID}` }} /> 机会区(价低+流入高)</span>
          <span><span className="inline-block h-2 w-2 rounded-sm" style={{ background: RISK_AREA, border: `1px solid ${RISK_SOLID}` }} /> 风险区(价高+流入高)</span>
          <span style={{ color: OPP_SOLID }}>▲ 底背离</span>
        </span>
      </div>

      {/* 指标说明 (可折叠) */}
      <details className="px-1 pb-2 text-[10px] leading-relaxed text-muted">
        <summary className="cursor-pointer select-none text-secondary hover:text-foreground">指标口径与判定规则（实证区间 2023-01~2026-08，沪深300/上证互证）</summary>
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          <li><b>价格分位</b>：当日收盘价在过去 250 个交易日中的位置（0%=一年最低，100%=一年最高）。</li>
          <li><b>流入分位</b>：宽基ETF 20 日净申购合计在过去 250 个交易日中的位置；衡量申购资金相对自身历史的强弱。</li>
          <li><span className="text-bear font-medium">机会区（绿底）</span>：价格分位 ≤33% 且流入分位 ≥67%——低位有大额申购承接（托底/左侧资金），历史未来 60 日均值 <b>+5.7%</b>、胜率 62%（n=172）。</li>
          <li><span className="text-bull font-medium">风险区（红底）</span>：价格分位 ≥67% 且流入分位 ≥67%——高位天量申购多为情绪追高，历史未来 60 日均值 ≈0%、胜率 50%。</li>
          <li><span className="text-bear font-medium">▲底背离</span>：指数创 60 日新低、但累计净流入高于 60 日前——历史未来 60 日均值 <b>+8.6%</b>、胜率 69%（n=55）。</li>
          <li><b>反直觉要点</b>：价低但流入分位 &lt;67% 属下跌中继（历史胜率仅 18~24%），低位本身不是机会，承接资金才是；中性区（分位 33%~67%）该指标无预测力。</li>
          <li><b>局限</b>：信号在 V 形反转年（如 2024）最强，单边阴跌年仅为相对改善；依赖 2024 年后国家队/机构托底型申赎结构，政策行为变化会使其失效。数据 T+1。</li>
        </ul>
      </details>

      {query.isLoading ? (
        <div className="py-10 text-center text-sm text-muted">区域数据加载中…</div>
      ) : query.isError ? (
        <div className="py-4 text-center text-sm text-danger">区域数据加载失败</div>
      ) : pts.length === 0 ? (
        <div className="py-10 text-center text-sm text-muted">暂无区域数据（需同步资金数据并积累 60+ 交易日）</div>
      ) : (
        <div ref={containerRef} className="w-full" style={{ height: 420 }} />
      )}
    </div>
  )
}
