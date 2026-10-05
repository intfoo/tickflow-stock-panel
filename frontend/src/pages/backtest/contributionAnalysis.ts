// 标的贡献度派生工具 — 从回测 trades 聚合各标的的已实现盈亏贡献。
//
// 口径:
//   - 贡献 = 该标的已实现盈亏 Σpnl_amount, 按 exit_date 归年 (与交易明细一致);
//   - 贡献率 = pnl ÷ Σ|pnl| (带符号): 分母取绝对值和, 与图表的面积/宽度分配口径一致,
//     正贡献为正、负贡献为负, 全体 |贡献率| 之和 = 100%;
//   - 缺失/非法 pnl_amount 按 0 计; 全部为零时返回 null (调用方隐藏卡片);
//   - 金额与贡献率两种读数同源 (只差一个常数分母), 切换只影响展示不影响形状。

import type { StrategyBacktestTrade } from '@/lib/api'

/** 读数模式: return = 贡献收益率 (盈亏 ÷ 初始资金, pp); amount = 贡献收益金额 (万元) */
export type ContribMetric = 'return' | 'amount'

export const OTHER_KEY = '__other__'

export interface SymbolPnl {
  symbol: string
  name: string
  /** Σpnl_amount (元) */
  pnl: number
  nTrades: number
}

export interface YearSeg extends SymbolPnl {
  /** |pnl| / 当年 Σ|pnl| (0-1) */
  share: number
}

export interface YearContribution {
  year: number
  totalAbs: number
  /** 正贡献按 pnl 降序在前, 负贡献按 |pnl| 降序在后 */
  segs: YearSeg[]
}

export interface ContributionAnalysis {
  /** 全期榜, 按 |pnl| 降序 */
  symbols: SymbolPnl[]
  /** 升序 */
  years: YearContribution[]
  /** 全期 Σ|pnl| */
  totalAbs: number
}

export interface YearRowSeg {
  key: string
  name: string
  pnl: number
  share: number
}

export interface YearlyRow {
  year: number
  totalAbs: number
  segs: YearRowSeg[]
}

export interface TreemapTile {
  key: string
  name: string
  pnl: number
  nTrades: number
  /** |pnl| / 全期 Σ|pnl| (0-1) */
  share: number
}

/** 金额读数: 万元一位小数, 正数不带符号 (与收益分析页约定一致)。 */
export function fmtAmount(pnl: number): string {
  return `${(pnl / 1e4).toFixed(1)}万`
}

/** 贡献收益率读数: 盈亏 ÷ 初始资金 (pp), 带符号一位小数; 资金非法返回 —。 */
export function fmtReturnContrib(pnl: number, capital: number): string {
  if (!Number.isFinite(capital) || capital <= 0) return '—'
  return `${((pnl / capital) * 100).toFixed(1)}%`
}

/** 贡献占比读数: 盈亏 ÷ Σ|盈亏| (面积/宽度分配的展示值), 带符号一位小数。 */
export function fmtShare(pnl: number, totalAbs: number): string {
  if (totalAbs <= 0) return '—'
  return `${((pnl / totalAbs) * 100).toFixed(1)}%`
}

export function analyzeContribution(trades: StrategyBacktestTrade[]): ContributionAnalysis | null {
  const symMap = new Map<string, SymbolPnl>()
  const yearSym = new Map<number, Map<string, SymbolPnl>>()
  for (const t of trades) {
    const year = Number(String(t.exit_date).slice(0, 4))
    if (!Number.isInteger(year) || year < 1990) continue
    const pnl = Number(t.pnl_amount ?? 0)
    if (!Number.isFinite(pnl)) continue
    const symbol = String(t.symbol)
    const name = String(t.name ?? '').trim() || symbol
    const upsert = (m: Map<string, SymbolPnl>) => {
      let s = m.get(symbol)
      if (!s) {
        s = { symbol, name, pnl: 0, nTrades: 0 }
        m.set(symbol, s)
      }
      s.pnl += pnl
      s.nTrades += 1
    }
    upsert(symMap)
    let ym = yearSym.get(year)
    if (!ym) {
      ym = new Map()
      yearSym.set(year, ym)
    }
    upsert(ym)
  }
  if (symMap.size === 0) return null

  const totalAbs = [...symMap.values()].reduce((s, x) => s + Math.abs(x.pnl), 0)
  if (totalAbs === 0) return null

  const symbols = [...symMap.values()].sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl))
  const years = [...yearSym.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([year, m]) => {
      const all = [...m.values()]
      const totalAbsY = all.reduce((s, x) => s + Math.abs(x.pnl), 0)
      const nonzero = all.filter(s => s.pnl !== 0)
      const pos = nonzero.filter(s => s.pnl > 0).sort((a, b) => b.pnl - a.pnl)
      const neg = nonzero.filter(s => s.pnl < 0).sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl))
      return {
        year,
        totalAbs: totalAbsY,
        segs: [...pos, ...neg].map(s => ({
          ...s,
          share: totalAbsY > 0 ? Math.abs(s.pnl) / totalAbsY : 0,
        })),
      }
    })
  return { symbols, years, totalAbs }
}

/**
 * 模式一(按年度)行数据: 全期 |pnl| 总榜 topN 保持独立段, 其余合并为「其他」
 * (净额求和)。段序: 正贡献降序 → 负贡献(|pnl|)降序; 「其他」按符号归入对应组
 * 并按 |pnl| 参与排名 (不垫底)。
 * spanYears 传入回测覆盖的完整年份序列时, 无交易的年份补空行 (totalAbs=0)。
 */
export function buildYearlyRows(a: ContributionAnalysis, topN: number, spanYears?: number[]): YearlyRow[] {
  const topKeys = new Set(a.symbols.slice(0, Math.max(topN, 0)).map(s => s.symbol))
  const rows = a.years.map(y => {
    const segs: YearRowSeg[] = []
    let restPnl = 0
    for (const s of y.segs) {
      if (topKeys.has(s.symbol)) {
        segs.push({ key: s.symbol, name: s.name, pnl: s.pnl, share: s.share })
      } else {
        restPnl += s.pnl
      }
    }
    if (restPnl !== 0) {
      // 「其他」按符号归入正/负贡献组, 并按 |pnl| 插入对应组的排名位置 (不垫底)
      const other: YearRowSeg = {
        key: OTHER_KEY,
        name: '其他',
        pnl: restPnl,
        share: y.totalAbs > 0 ? Math.abs(restPnl) / y.totalAbs : 0,
      }
      const pos = segs.filter(s => s.pnl > 0)
      const neg = segs.filter(s => s.pnl < 0)
      const group = restPnl > 0 ? pos : neg
      const idx = group.findIndex(s => Math.abs(s.pnl) < Math.abs(restPnl))
      group.splice(idx === -1 ? group.length : idx, 0, other)
      segs.length = 0
      segs.push(...pos, ...neg)
    }
    return { year: y.year, totalAbs: y.totalAbs, segs }
  })
  if (!spanYears?.length) return rows
  const byYear = new Map(rows.map(r => [r.year, r]))
  return [...spanYears]
    .sort((x, z) => x - z)
    .map(y => byYear.get(y) ?? { year: y, totalAbs: 0, segs: [] })
}

/** 模式二(按标的)treemap 块: 全期 |pnl| 总榜 topN + 「其他」(净额求和)。 */
export function buildTreemapTiles(a: ContributionAnalysis, topN: number): TreemapTile[] {
  const n = Math.max(topN, 0)
  const tiles: TreemapTile[] = a.symbols
    .slice(0, n)
    .filter(s => s.pnl !== 0)
    .map(s => ({
      key: s.symbol,
      name: s.name,
      pnl: s.pnl,
      nTrades: s.nTrades,
      share: Math.abs(s.pnl) / a.totalAbs,
    }))
  const rest = a.symbols.slice(n)
  const restPnl = rest.reduce((sum, s) => sum + s.pnl, 0)
  const restN = rest.reduce((sum, s) => sum + s.nTrades, 0)
  if (restPnl !== 0) {
    tiles.push({ key: OTHER_KEY, name: '其他', pnl: restPnl, nTrades: restN, share: Math.abs(restPnl) / a.totalAbs })
  }
  return tiles
}
