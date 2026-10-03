import { useMemo, useState } from 'react'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'
import type { StrategyBacktestTrade } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import {
  analyzeContribution,
  buildTreemapTiles,
  buildYearlyRows,
  fmtContrib,
  OTHER_KEY,
  type ContribMetric,
} from '../contributionAnalysis'

const YEARLY_TOP_N = 10
const TREEMAP_TOP_N = 50

interface SegDatum {
  value: number
  segName: string
  pnl: number
  itemStyle: { color: string; opacity?: number }
}

/**
 * 标的贡献度 — 两个模式:
 *   模式一(按年度, 默认): 每年一行 100% 堆叠横条, 段宽 ∝ |贡献|, 红=正贡献 绿=负贡献, 总榜前 10 + 其他;
 *   模式二(按标的): treemap 无坐标, 面积 ∝ |贡献|, 总榜前 50 + 其他。
 * 读数可在 金额(万) / 贡献率(%) 间切换 (同源, 只影响展示)。
 */
export function SymbolContributionCard({ trades }: { trades: StrategyBacktestTrade[] }) {
  const analysis = useMemo(() => analyzeContribution(trades), [trades])
  // 无数据(0 交易 / 全零盈亏)直接不渲染; 图表主体独立组件,
  // 保证 null ↔ 有数据 切换时整体重挂载, ECharts 正常初始化。
  if (!analysis) return null
  return <ContributionBody analysis={analysis} />
}

function ContributionBody({ analysis }: { analysis: NonNullable<ReturnType<typeof analyzeContribution>> }) {
  const ct = useChartTheme()
  const [yearly, setYearly] = useState(true)
  const [metric, setMetric] = useState<ContribMetric>('share')

  const yearlyOption = useMemo<EChartsOption | null>(() => {
    if (!analysis) return null
    const rows = [...buildYearlyRows(analysis, YEARLY_TOP_N)].reverse() // 最新年在上
    const cats = rows.map(r => `${r.year}`)
    // 段序: 全期榜 topN 顺序 + 其他垫底 (跨年固定, 行内可比)
    const keys: { key: string; name: string }[] = analysis.symbols
      .slice(0, YEARLY_TOP_N)
      .map(s => ({ key: s.symbol, name: s.name }))
    if (rows.some(r => r.segs.some(s => s.key === OTHER_KEY))) keys.push({ key: OTHER_KEY, name: '其他' })

    const series = keys.map(k => ({
      name: k.name,
      type: 'bar' as const,
      stack: 'contrib',
      barWidth: '62%',
      data: rows.map(r => {
        const seg = r.segs.find(s => s.key === k.key)
        const pnl = seg?.pnl ?? 0
        return {
          value: seg ? Number((seg.share * 100).toFixed(2)) : 0,
          segName: seg?.name ?? k.name,
          pnl,
          itemStyle: {
            color: pnl >= 0 ? ct.bull : ct.bear,
            ...(k.key === OTHER_KEY ? { opacity: 0.5 } : {}),
          },
        } as SegDatum
      }),
      label: {
        show: true,
        fontSize: 9,
        color: '#fff',
        formatter: (p: any) => ((p.value as number) >= 8 ? (p.data as SegDatum).segName : ''),
      },
      labelLayout: { hideOverlap: true },
    }))

    return {
      grid: { left: 56, right: 16, top: 8, bottom: 22 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        formatter: (params: any) => {
          const ps = Array.isArray(params) ? params : [params]
          const row = rows[ps[0]?.dataIndex]
          if (!row) return ''
          const lines = row.segs.map(
            s =>
              `${s.name}: ${fmtContrib(s.pnl, 'amount', row.totalAbs)} / ${fmtContrib(s.pnl, 'share', row.totalAbs)}`,
          )
          return [`${row.year}年`, ...lines].join('<br/>')
        },
      },
      xAxis: {
        type: 'value',
        max: 100,
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => `${v}%` },
        splitLine: { lineStyle: { color: ct.grid } },
        axisLine: { show: false },
      },
      yAxis: {
        type: 'category',
        data: cats,
        axisLabel: { color: ct.text, fontSize: 10, fontWeight: 'bold' },
        axisLine: { lineStyle: { color: ct.border } },
        axisTick: { show: false },
      },
      series,
    }
  }, [analysis, ct])

  const treemapOption = useMemo<EChartsOption | null>(() => {
    if (!analysis) return null
    const tiles = buildTreemapTiles(analysis, TREEMAP_TOP_N)
    return {
      tooltip: {
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        formatter: (p: any) => {
          const d = p.data as { name: string; pnl: number; nTrades: number }
          return [
            d.name,
            `金额: ${fmtContrib(d.pnl, 'amount', analysis.totalAbs)}`,
            `贡献率: ${fmtContrib(d.pnl, 'share', analysis.totalAbs)}`,
            `交易: ${d.nTrades} 笔`,
          ].join('<br/>')
        },
      },
      series: [
        {
          type: 'treemap',
          roam: false,
          nodeClick: false,
          breadcrumb: { show: false },
          width: '100%',
          height: '100%',
          top: 0,
          left: 0,
          label: {
            show: true,
            fontSize: 11,
            color: '#fff',
            overflow: 'truncate',
            formatter: (p: any) => `${p.name} ${fmtContrib((p.data as any).pnl, metric, analysis.totalAbs)}`,
          },
          upperLabel: { show: false },
          itemStyle: { borderColor: ct.tooltipBg, borderWidth: 2, gapWidth: 2 },
          data: tiles.map(t => ({
            name: t.name,
            value: Number((t.share * 100).toFixed(3)),
            pnl: t.pnl,
            nTrades: t.nTrades,
            itemStyle: {
              color: t.pnl >= 0 ? ct.bull : ct.bear,
              opacity: t.key === OTHER_KEY ? 0.45 : 0.85,
            },
          })),
        },
      ],
    }
  }, [analysis, metric, ct])

  const option = yearly ? yearlyOption : treemapOption
  const chartRef = useECharts(option, [yearly, analysis, metric, ct])

  const height = yearly ? Math.max(120, analysis.years.length * 34 + 40) : 420
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-xs font-medium text-secondary">标的贡献</span>
        <span className="text-[10px] text-muted">红=正贡献 · 绿=负贡献 · 宽/面积 ∝ |贡献|</span>
        <label className="flex cursor-pointer items-center gap-1 text-[10px] text-secondary">
          <input
            type="checkbox"
            checked={yearly}
            onChange={e => setYearly(e.target.checked)}
            className="h-3 w-3 cursor-pointer accent-[#3b82f6]"
          />
          按年度
        </label>
        <div className="ml-auto inline-flex h-6 overflow-hidden rounded-btn border border-border">
          {(['share', 'amount'] as const).map(m => (
            <button
              key={m}
              type="button"
              onClick={() => setMetric(m)}
              className={`px-2 text-[10px] transition-colors cursor-pointer ${
                metric === m ? 'bg-accent/15 text-accent' : 'text-muted hover:text-secondary'
              }`}
            >
              {m === 'share' ? '贡献率%' : '金额(万)'}
            </button>
          ))}
        </div>
      </div>
      <div ref={chartRef} className="w-full" style={{ height }} />
    </div>
  )
}
