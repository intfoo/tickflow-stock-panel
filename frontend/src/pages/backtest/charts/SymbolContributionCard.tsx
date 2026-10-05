import { useMemo, useState } from 'react'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'
import type { StrategyBacktestTrade } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import {
  analyzeContribution,
  buildTreemapTiles,
  buildYearlyRows,
  fmtAmount,
  fmtReturnContrib,
  OTHER_KEY,
  type ContribMetric,
} from '../contributionAnalysis'

const YEARLY_TOP_N = 10
const TREEMAP_TOP_N = 50

interface SegDatum {
  value: number
  segName: string
  pnl: number
  itemStyle: { color: string; borderColor: string; borderWidth: number }
  label: { color: string }
}

interface Props {
  trades: StrategyBacktestTrade[]
  /** 贡献收益率分母 (config.initial_capital); 缺失时退化为 Σ|净额| */
  initialCapital?: number | null
  /** 回测覆盖的完整年份序列 — 无交易年份补空行 */
  spanYears?: number[]
}

/**
 * 标的贡献度 — 两个模式:
 *   模式一(按年度, 默认): 每年一行带符号堆叠横条, 正贡献 0 轴向右/负贡献向左,
 *     横坐标 = 年度收益率(pp) 或年度收益金额(万), 段间白描边 + 段内 名称+数值,
 *     无交易年份保留空行; 全期 |pnl| 总榜前 10 + 其他;
 *   模式二(按标的): treemap 无坐标, 面积 ∝ |贡献|, 总榜前 50 + 其他。
 */
export function SymbolContributionCard({ trades, initialCapital, spanYears }: Props) {
  const analysis = useMemo(() => analyzeContribution(trades), [trades])
  // 无数据(0 交易 / 全零盈亏)直接不渲染; 图表主体独立组件,
  // 保证 null ↔ 有数据 切换时整体重挂载, ECharts 正常初始化。
  if (!analysis) return null
  return <ContributionBody analysis={analysis} initialCapital={initialCapital} spanYears={spanYears} />
}

function ContributionBody({ analysis, initialCapital, spanYears }: {
  analysis: NonNullable<ReturnType<typeof analyzeContribution>>
  initialCapital?: number | null
  spanYears?: number[]
}) {
  const ct = useChartTheme()
  const [yearly, setYearly] = useState(true)
  const [metric, setMetric] = useState<ContribMetric>('return')

  const capital = initialCapital != null && Number.isFinite(initialCapital) && initialCapital > 0
    ? initialCapital
    : analysis.totalAbs
  const conv = (pnl: number) => (metric === 'amount' ? pnl / 1e4 : (pnl / capital) * 100)
  const fmtVal = (pnl: number) => (metric === 'amount' ? fmtAmount(pnl) : fmtReturnContrib(pnl, capital))

  const yearlyOption = useMemo<EChartsOption>(() => {
    const rows = [...buildYearlyRows(analysis, YEARLY_TOP_N, spanYears)].reverse() // 最新年在上
    const cats = rows.map(r => `${r.year}`)
    // 排位槽堆叠: 每行段按 |贡献| 从高到低填入槽位 (正贡献 0 轴向右, 负贡献向左),
    // 槽位数 = 各行最大段数; series 无图例语义, 段名由段内标签/tooltip 承载
    const slotCount = Math.max(...rows.map(r => r.segs.length), 0)

    // 轴范围按实际正负贡献动态取: 无负贡献不显示负半轴, 无正贡献不显示正半轴
    let maxPos = 0
    let maxNeg = 0
    let maxSegAbs = 1e-9
    for (const r of rows) {
      const pos = r.segs.filter(s => s.pnl > 0).reduce((s, x) => s + conv(x.pnl), 0)
      const neg = r.segs.filter(s => s.pnl < 0).reduce((s, x) => s + conv(x.pnl), 0)
      maxPos = Math.max(maxPos, pos)
      maxNeg = Math.max(maxNeg, Math.abs(neg))
      for (const s of r.segs) maxSegAbs = Math.max(maxSegAbs, Math.abs(conv(s.pnl)))
    }
    const labelThreshold = Math.max(maxPos, maxNeg) * 0.06

    // 颜色深浅对齐月度收益热力图: alpha 按 |贡献| 在全表最大值上归一 (sqrt 拉伸小贡献)
    const segColor = (pnl: number, other: boolean): { color: string; alpha: number } => {
      const t = Math.min(Math.abs(conv(pnl)) / maxSegAbs, 1)
      const alpha = (other ? 0.10 : 0.15) + (other ? 0.45 : 0.70) * Math.sqrt(t)
      return { color: pnl >= 0 ? ct.bullAlpha(alpha) : ct.bearAlpha(alpha), alpha }
    }
    const labelColor = (alpha: number) => (alpha >= 0.5 ? '#fff' : ct.textStrong)

    const series = Array.from({ length: slotCount }, (_, slot) => ({
      name: `rank-${slot + 1}`,
      type: 'bar' as const,
      stack: 'contrib',
      barWidth: '62%',
      data: rows.map(r => {
        const seg = r.segs[slot]
        const pnl = seg?.pnl ?? 0
        const { color, alpha } = seg ? segColor(pnl, seg.key === OTHER_KEY) : { color: 'transparent', alpha: 1 }
        return {
          value: seg ? Number(conv(pnl).toFixed(3)) : 0,
          segName: seg?.name ?? '',
          pnl,
          itemStyle: { color, borderColor: ct.tooltipBg, borderWidth: 1 },
          label: { color: labelColor(alpha) },
        } as SegDatum
      }),
      label: {
        show: true,
        position: 'inside' as const,
        fontSize: 9,
        lineHeight: 11,
        formatter: (p: any) => {
          const d = p.data as SegDatum
          // 段宽不足隐藏标签, tooltip 仍可见
          return Math.abs(p.value as number) >= labelThreshold ? `${d.segName}\n${fmtVal(d.pnl)}` : ''
        },
      },
      labelLayout: { hideOverlap: true },
    }))

    return {
      grid: { left: 92, right: 24, top: 8, bottom: 34 },
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
          if (row.totalAbs === 0) return `${row.year}年<br/>无交易`
          const lines = row.segs.map(
            s =>
              `${s.name}${s.key !== OTHER_KEY ? ` ${s.key}` : ''}: ${fmtAmount(s.pnl)} / 收益率 ${fmtReturnContrib(s.pnl, capital)}`,
          )
          return [`${row.year}年`, ...lines].join('<br/>')
        },
      },
      xAxis: {
        type: 'value',
        min: maxNeg > 0 ? Number((-maxNeg * 1.15).toFixed(3)) : 0,
        max: maxPos > 0 ? Number((maxPos * 1.15).toFixed(3)) : 0,
        name: metric === 'amount' ? '年度收益金额(万)' : '年度收益率(%)',
        nameLocation: 'middle',
        nameGap: 22,
        nameTextStyle: { color: ct.text, fontSize: 10 },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          formatter: (v: number) => (metric === 'amount' ? `${v}` : `${v}%`),
        },
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- conv/fmtVal/capital 由 metric/analysis 派生
  }, [analysis, spanYears, metric, capital, ct])

  const tiles = useMemo(() => buildTreemapTiles(analysis, TREEMAP_TOP_N), [analysis])

  const treemapOption = useMemo<EChartsOption>(() => {
    // 色阶对齐月度收益热力图: alpha 按占比在最大块上归一
    const maxShare = Math.max(...tiles.map(t => t.share), 1e-9)
    return {
      tooltip: {
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        formatter: (p: any) => {
          const d = p.data as { name: string; key: string; pnl: number; nTrades: number }
          return [
            d.key !== OTHER_KEY ? `${d.name} ${d.key}` : d.name,
            `金额: ${fmtAmount(d.pnl)}`,
            `贡献收益率: ${fmtReturnContrib(d.pnl, capital)}`,
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
          // 面积过小的块自动隐藏标签 (两行文字至少 ~40×30), hover 仍可读
          visibleMin: 1200,
          label: {
            show: true,
            fontSize: 11,
            overflow: 'truncate',
            formatter: (p: any) => `${p.name}\n${fmtVal((p.data as any).pnl)}`,
          },
          upperLabel: { show: false },
          itemStyle: { borderColor: ct.tooltipBg, borderWidth: 2, gapWidth: 2 },
          data: tiles.map(t => {
            const alpha =
              (t.key === OTHER_KEY ? 0.10 : 0.15) +
              (t.key === OTHER_KEY ? 0.45 : 0.70) * Math.sqrt(t.share / maxShare)
            return {
              name: t.name,
              value: Number((t.share * 100).toFixed(3)),
              key: t.key,
              pnl: t.pnl,
              nTrades: t.nTrades,
              itemStyle: { color: t.pnl >= 0 ? ct.bullAlpha(alpha) : ct.bearAlpha(alpha) },
              label: { color: alpha >= 0.5 ? '#fff' : ct.textStrong },
            }
          }),
        },
      ],
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fmtVal 由 metric/capital 派生
  }, [tiles, analysis, metric, capital, ct])

  const option = yearly ? yearlyOption : treemapOption
  const chartRef = useECharts(option, [yearly, analysis, metric, ct])

  const height = yearly
    // 行高 68px: 段内两行标签 (名称 + 数值)
    ? Math.max(160, (spanYears?.length ?? analysis.years.length) * 68 + 52)
    // 面积图高度随标的数量伸缩, 上限 420 防止 50 块时过高
    : Math.min(420, Math.max(160, tiles.length * 14))
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
          {(['return', 'amount'] as const).map(m => (
            <button
              key={m}
              type="button"
              onClick={() => setMetric(m)}
              className={`px-2 text-[10px] transition-colors cursor-pointer ${
                metric === m ? 'bg-accent/15 text-accent' : 'text-muted hover:text-secondary'
              }`}
            >
              {m === 'return' ? '贡献收益率' : '贡献收益金额'}
            </button>
          ))}
        </div>
      </div>
      <div ref={chartRef} className="w-full" style={{ height }} />
    </div>
  )
}
