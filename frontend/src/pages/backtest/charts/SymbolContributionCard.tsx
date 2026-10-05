import { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'
import type { StrategyBacktestTrade } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import {
  analyzeBackendContributions,
  analyzeContribution,
  buildTreemapTiles,
  buildYearlyRows,
  fmtAmount,
  fmtReturnContrib,
  OTHER_KEY,
  type BackendContributionRow,
  type ContribMetric,
} from '../contributionAnalysis'

const YEARLY_TOP_N = 10
const TREEMAP_TOP_N = 50

interface SegDatum {
  value: number
  pnl: number
  /** 所属年份 — 收益率读数按当年年初资产换算 */
  year: number
  /** 段内文字 (按像素宽分档预生成: 截断/单行/竖排/空) */
  text: string
  itemStyle: { color: string; borderColor?: string; borderWidth?: number; borderType?: 'solid' | 'dashed' | 'dotted' }
  label: { show: boolean; color?: string; fontSize?: number; lineHeight?: number; rotate?: number }
}

interface Props {
  trades: StrategyBacktestTrade[]
  /** 后端市值贡献 (symbol_contributions); 存在时优先于 trades 已实现口径 */
  contributions?: BackendContributionRow[] | null
  /** 贡献收益率分母 (config.initial_capital); 缺失时退化为 Σ|净额| */
  initialCapital?: number | null
  /** 各年年初资产 (returnsAnalysis.yearStartEquity) — 按年度模式收益率读数分母, 使 Σ段收益率 = 年度收益 */
  yearStartEquity?: Map<number, number>
  /** 回测覆盖的完整年份序列 — 无交易年份补空行 */
  spanYears?: number[]
}

/**
 * 标的贡献度 — 两个模式:
 *   模式一(按年度, 默认): 每年一行铺满整行的堆叠横条 (无横坐标), 段宽 ∝ |贡献| 占比,
 *     段序负数由小到大 → 正数由大到小, 段间白描边 + 段内 名称+数值 (段宽不足省略),
 *     tooltip 表头带年度收益; 无交易年份保留空行; 当年 |pnl| 前 10 + 其他;
 *   模式二(按标的): treemap 无坐标, 面积 ∝ |贡献|, 总榜前 50 + 其他。
 */
export function SymbolContributionCard({ trades, contributions, initialCapital, yearStartEquity, spanYears }: Props) {
  // 后端市值口径优先 (与净值曲线严格对账); 缺字段 (旧结果) 回退 trades 已实现口径
  const backendActive = (contributions?.length ?? 0) > 0
  const analysis = useMemo(
    () => (backendActive ? analyzeBackendContributions(contributions!, trades) : analyzeContribution(trades)),
    [backendActive, contributions, trades],
  )
  // 无数据(0 交易 / 全零盈亏)直接不渲染; 图表主体独立组件,
  // 保证 null ↔ 有数据 切换时整体重挂载, ECharts 正常初始化。
  if (!analysis) return null
  return (
    <ContributionBody
      analysis={analysis}
      initialCapital={initialCapital}
      yearStartEquity={yearStartEquity}
      spanYears={spanYears}
      m2m={backendActive}
    />
  )
}

function ContributionBody({ analysis, initialCapital, yearStartEquity, spanYears, m2m }: {
  analysis: NonNullable<ReturnType<typeof analyzeContribution>>
  initialCapital?: number | null
  yearStartEquity?: Map<number, number>
  spanYears?: number[]
  /** true=后端市值口径 (含持仓浮动); false=trades 已实现口径 */
  m2m: boolean
}) {
  const ct = useChartTheme()
  const [yearly, setYearly] = useState(true)
  const [metric, setMetric] = useState<ContribMetric>('return')

  // 容器像素宽 — 按年度模式的段宽布局/文字截断/「其他」固定窄条都依赖真实宽度
  const containerRef = useRef<HTMLDivElement>(null)
  const [rowPx, setRowPx] = useState(0)
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver(entries => {
      const w = entries[0]?.contentRect.width ?? 0
      setRowPx(prev => (Math.abs(prev - w) > 1 ? w : prev))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // hover 色块追踪: 行 tooltip 顶部插入该色块简卡。不设系列级 item tooltip —
  // 它会抑制全局 axis tooltip 导致整行 tooltip 不显示
  const hoveredSegRef = useRef<{ rowIdx: number; slot: number } | null>(null)
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    let inst: echarts.ECharts | undefined
    const over = (p: any) => {
      if (p.componentType === 'series') hoveredSegRef.current = { rowIdx: p.dataIndex, slot: p.seriesIndex }
    }
    const out = () => { hoveredSegRef.current = null }
    // useECharts 的 init effect 排在本 effect 之后, 延时一拍等实例就绪再绑
    const t = window.setTimeout(() => {
      inst = echarts.getInstanceByDom(el) as echarts.ECharts | undefined
      inst?.on('mouseover', over)
      inst?.on('mouseout', out)
      inst?.on('globalout', out)
    }, 0)
    return () => {
      window.clearTimeout(t)
      inst?.off('mouseover', over)
      inst?.off('mouseout', out)
      inst?.off('globalout', out)
    }
  }, [])

  const capital = initialCapital != null && Number.isFinite(initialCapital) && initialCapital > 0
    ? initialCapital
    : analysis.totalAbs
  // treemap (全期) 读数分母 = 初始资金: Σ全期贡献 = 权益全期变动, 与全期收益率对账
  const fmtVal = (pnl: number) => (metric === 'amount' ? fmtAmount(pnl) : fmtReturnContrib(pnl, capital))

  const yearlyOption = useMemo<EChartsOption>(() => {
    const rows = [...buildYearlyRows(analysis, YEARLY_TOP_N, spanYears)].reverse() // 最新年在上
    const cats = rows.map(r => `${r.year}`)
    // 收益率读数分母 = 当年年初资产 (yearStartEquity), 缺失退化初始资金;
    // 市值口径下 Σ段收益率 = 该年年度收益 (与热力图/直方图对账)
    const denomOf = (year: number) => {
      const v = yearStartEquity?.get(year)
      return v != null && Number.isFinite(v) && v > 0 ? v : capital
    }
    const fmtValY = (pnl: number, year: number) => (metric === 'amount' ? fmtAmount(pnl) : fmtReturnContrib(pnl, denomOf(year)))
    // 年度收益 = Σ段净额 ÷ 年初资产 (tooltip 表头读数)
    const yearRetOf = (row: (typeof rows)[number]) => {
      const net = row.segs.reduce((s, x) => s + x.pnl, 0)
      return (net / denomOf(row.year)) * 100
    }
    // 无横坐标: 每行铺满整行, 真实段按 segCmp 序 (负数由小到大 → 正数由大到小) 从左到右,
    // 「其他」不参与排序固定垫底 (最右)。「其他」固定窄条 (OTHER_PX, 不按实际占比),
    // 空心虚线框 + 灰字, 与红绿实心真实段视觉解耦; 真实段在剩余宽度内按 |pnl| 比例分配。
    // 段宽无读数语义, 数值由标签/tooltip 承载
    const OTHER_PX = 64
    const plotW = Math.max(rowPx - 46, 400) // grid left 42 + right 4; 未测量时兜底
    const slotCount = Math.max(...rows.map(r => r.segs.length), 0)
    // 行布局: seg → 像素宽 (「其他」固定, 真实段按 |pnl| 分剩余宽度)
    const layouts = rows.map(r => {
      const m = new Map<(typeof r.segs)[number], number>()
      const other = r.segs.find(s => s.key === OTHER_KEY)
      const otherW = other ? Math.min(OTHER_PX, plotW * 0.2) : 0
      const avail = plotW - otherW
      const realAbs = r.segs.filter(s => s.key !== OTHER_KEY).reduce((s, x) => s + Math.abs(x.pnl), 0)
      for (const s of r.segs) {
        m.set(s, s.key === OTHER_KEY ? otherW : realAbs > 0 ? (Math.abs(s.pnl) / realAbs) * avail : 0)
      }
      return m
    })
    let maxAbsPnl = 1e-9
    for (const r of rows) {
      for (const s of r.segs) if (s.key !== OTHER_KEY) maxAbsPnl = Math.max(maxAbsPnl, Math.abs(s.pnl))
    }

    // 颜色深浅对齐月度收益热力图: alpha 按 |pnl| 在全表最大真实段上归一 (sqrt 拉伸小贡献)
    const segColor = (pnl: number): { color: string; alpha: number } => {
      const t = Math.min(Math.abs(pnl) / maxAbsPnl, 1)
      const alpha = 0.15 + 0.70 * Math.sqrt(t)
      return { color: pnl >= 0 ? ct.bullAlpha(alpha) : ct.bearAlpha(alpha), alpha }
    }
    const labelColor = (alpha: number) => (alpha >= 0.5 ? '#fff' : ct.textStrong)

    // 段内文字按像素宽分档, 数值优先、换行不超过两行:
    // ≥64 名称10px(截断…)+数值两行 / 40~64 同两行 9px / 30~40 名称前 2 字+数值两行 9px /
    // 12~30 仅数值 (旋转 90° 利用段高) / <12 省略。per-datum show 开关, hover emphasis 不复活
    const segLabel = (name: string, pnl: number, year: number, w: number) => {
      const val = fmtValY(pnl, year)
      if (w >= 64) {
        const maxChars = Math.max(1, Math.floor((w - 8) / 10))
        const nm = name.length > maxChars ? `${name.slice(0, Math.max(1, maxChars - 1))}…` : name
        return { show: true, fontSize: 10, lineHeight: 14, rotate: 0, text: `${nm}\n${val}` }
      }
      if (w >= 40) {
        const maxChars = Math.max(1, Math.floor((w - 8) / 9))
        const nm = name.length > maxChars ? `${name.slice(0, Math.max(1, maxChars - 1))}…` : name
        return { show: true, fontSize: 9, lineHeight: 13, rotate: 0, text: `${nm}\n${val}` }
      }
      if (w >= 30) return { show: true, fontSize: 9, lineHeight: 13, rotate: 0, text: `${name.slice(0, 2)}\n${val}` }
      if (w >= 12) return { show: true, fontSize: 9, lineHeight: 13, rotate: 90, text: val }
      return { show: false, fontSize: 9, lineHeight: 13, rotate: 0, text: '' }
    }

    const series = Array.from({ length: slotCount }, (_, slot) => ({
      name: `rank-${slot + 1}`,
      type: 'bar' as const,
      stack: 'contrib',
      barWidth: '62%',
      data: rows.map((r, ri) => {
        const seg = r.segs[slot]
        if (!seg) {
          return {
            value: 0, pnl: 0, year: r.year, text: '',
            itemStyle: { color: 'transparent' }, label: { show: false },
          } as SegDatum
        }
        const other = seg.key === OTHER_KEY
        const w = layouts[ri].get(seg) ?? 0
        const lab = other
          ? {
              show: w >= 40,
              fontSize: 10,
              lineHeight: 14,
              rotate: 0,
              text: `其他${seg.count ? `(${seg.count})` : ''}\n${fmtValY(seg.pnl, r.year)}`,
            }
          : segLabel(seg.name, seg.pnl, r.year, w)
        const { color, alpha } = other
          // 「其他」按净额符号浅着色 + 同色虚线描边: 有红绿语义又与实心真实段区分
          ? { color: seg.pnl >= 0 ? ct.bullAlpha(0.3) : ct.bearAlpha(0.3), alpha: 0.3 }
          : segColor(seg.pnl)
        return {
          value: Number(((w / plotW) * 100).toFixed(3)),
          pnl: seg.pnl,
          year: r.year,
          text: lab.text,
          itemStyle: other
            ? { color, borderColor: seg.pnl >= 0 ? ct.bull : ct.bear, borderWidth: 1, borderType: 'dashed' as const }
            : { color, borderColor: ct.tooltipBg, borderWidth: 1 },
          label: {
            show: lab.show,
            color: other ? ct.textStrong : labelColor(alpha),
            fontSize: lab.fontSize,
            lineHeight: lab.lineHeight,
            rotate: lab.rotate,
          },
        } as SegDatum
      }),
      label: {
        show: true,
        position: 'inside' as const,
        formatter: (p: any) => (p.data as SegDatum).text,
      },
      labelLayout: { hideOverlap: true },
    }))

    return {
      // 与月度收益热力图 (DOM 表格 w-full) 左右边对齐: 年份标签列 ~42px, 条形区铺满
      grid: { left: 42, right: 4, top: 8, bottom: 8 },
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
          // 悬停的色块在表格内行内高亮 (▸ + 底色; hover 追踪见 hoveredSegRef)
          const hov = hoveredSegRef.current
          const hovSeg = hov && hov.rowIdx === ps[0]?.dataIndex ? row.segs[hov.slot] : null
          // 层级: 表头年度收益加粗着色; 每行 名称(代码弱化) | 金额(加粗着色) | 收益率(弱化着色)
          const yr = yearRetOf(row)
          const head =
            `<div style="margin-bottom:4px;font-weight:600">${row.year}年 · 年度收益 ` +
            `<span style="color:${yr >= 0 ? ct.bull : ct.bear}">${yr.toFixed(1)}%</span></div>`
          const lines = row.segs.map(s => {
            const c = s.pnl >= 0 ? ct.bull : ct.bear
            const isHov = s === hovSeg
            const title =
              s.key === OTHER_KEY
                ? `<span style="opacity:.65">其他(${s.count ?? 0}个标的)</span>`
                : `${s.name} <span style="opacity:.55;font-size:10px">${s.key}</span>`
            return (
              `<tr${isHov ? ' style="background:rgba(127,127,127,0.18)"' : ''}>` +
              `<td style="padding-right:14px">${isHov ? '▸ ' : ''}${title}</td>` +
              `<td style="text-align:right;font-weight:600;color:${c}">${fmtAmount(s.pnl)}</td>` +
              `<td style="text-align:right;padding-left:10px;color:${c};opacity:.8">${fmtReturnContrib(s.pnl, denomOf(row.year))}</td></tr>`
            )
          })
          return `${head}<table style="border-collapse:collapse">${lines.join('')}</table>`
        },
      },
      xAxis: { type: 'value', show: false, min: 0, max: 100 },
      yAxis: {
        type: 'category',
        data: cats,
        axisLabel: { color: ct.text, fontSize: 10, fontWeight: 'bold' },
        axisLine: { lineStyle: { color: ct.border } },
        axisTick: { show: false },
      },
      series,
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fmtValY/capital 由 metric/analysis 派生
  }, [analysis, spanYears, metric, capital, yearStartEquity, rowPx, ct])

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
            fontSize: 12,
            lineHeight: 17,
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
  // 超采样渲染 (≥2x DPR): 改善段内 8-9px 小字在 Windows 分数缩放下的 canvas 模糊
  const chartRef = useECharts(
    option,
    [yearly, analysis, metric, rowPx, ct],
    containerRef,
    Math.max(window.devicePixelRatio || 1, 2),
  )

  const height = yearly
    // 行高 68px: 段内两行标签 (名称 + 数值)
    ? Math.max(160, (spanYears?.length ?? analysis.years.length) * 68 + 52)
    // 面积图高度随标的数量伸缩, 上限 420 防止 50 块时过高
    : Math.min(420, Math.max(160, tiles.length * 14))
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-xs font-medium text-secondary">标的贡献</span>
        <span className="text-[10px] text-muted">
          红=正贡献 · 绿=负贡献 · 宽 ∝ |贡献| (虚线框=其他, 固定窄条) · {m2m ? '市值口径(含浮动, 与年度收益对账)' : '已实现口径(按卖出年)'}
        </span>
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
