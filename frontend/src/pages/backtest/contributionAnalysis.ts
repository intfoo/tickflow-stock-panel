// 标的贡献度派生工具 — 从回测 trades 聚合各标的的已实现盈亏贡献。
//
// 口径:
//   - 贡献 = 该标的已实现盈亏 Σpnl_amount, 按 exit_date 归年 (与交易明细一致);
//   - 贡献率 = pnl ÷ Σ|pnl| (带符号): 分母取绝对值和, 与图表的面积/宽度分配口径一致,
//     正贡献为正、负贡献为负, 全体 |贡献率| 之和 = 100%;
//   - 收益率读数分母: 按年度模式 = 当年年初资产 (Σ段收益率 = 年度收益, 与热力图/直方图对账),
//     按标的全期模式 = 初始资金 (Σ = 全期收益); 分母取值在 SymbolContributionCard;
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
  /** 段序: 负数由小到大 (最负在前) → 正数由大到小; 堆叠时正负两侧 |贡献| 均由 0 轴向外递减, tooltip 同序 */
  segs: YearSeg[]
}

/** 段序比较器: 负数由小到大在前, 正数由大到小在后 (符号分组内均按 |pnl| 降序)。 */
function segCmp(a: { pnl: number }, b: { pnl: number }): number {
  const na = a.pnl < 0
  const nb = b.pnl < 0
  if (na !== nb) return na ? -1 : 1
  return na ? a.pnl - b.pnl : b.pnl - a.pnl
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
  /** 真实段: |pnl| 在行内真实段 Σ|pnl| 的占比 (0-1); 「其他」恒 0 (固定窄条, 不占比例) */
  share: number
  /** 仅「其他」段: 并入的标的数 */
  count?: number
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

/** 金额读数: |金额| ≥ 1000 元用万元一位小数; 不足 1000 元用元整数 (避免 0.0万 无信息)。 */
export function fmtAmount(pnl: number): string {
  if (Math.abs(pnl) < 1000) return `${Math.round(pnl)}元`
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
  return _assemble(symMap, yearSym)
}

/** 后端 symbol_contributions 行 (市值口径)。 */
export interface BackendContributionRow {
  symbol: string
  name?: string
  year: number
  pnl: number
}

/**
 * 后端市值贡献 (symbol_contributions) → 统一分析结构。
 * 与 analyzeContribution (trades 已实现口径) 同形; nTrades 从 trades 统计
 * (按 exit 年计数, 持仓未卖年份为 0), name 优先取后端行再回退 trades/symbol。
 */
export function analyzeBackendContributions(
  rows: BackendContributionRow[],
  trades: StrategyBacktestTrade[],
): ContributionAnalysis | null {
  const tradeNames = new Map<string, string>()
  const tradeCount = new Map<string, number>() // `${symbol}|${year}` 与 symbol 两级
  for (const t of trades) {
    const symbol = String(t.symbol)
    const name = String(t.name ?? '').trim()
    if (name && !tradeNames.has(symbol)) tradeNames.set(symbol, name)
    const year = String(t.exit_date).slice(0, 4)
    tradeCount.set(symbol, (tradeCount.get(symbol) ?? 0) + 1)
    const key = `${symbol}|${year}`
    tradeCount.set(key, (tradeCount.get(key) ?? 0) + 1)
  }

  const symMap = new Map<string, SymbolPnl>()
  const yearSym = new Map<number, Map<string, SymbolPnl>>()
  for (const r of rows) {
    const year = Number(r.year)
    const pnl = Number(r.pnl)
    if (!Number.isInteger(year) || year < 1990 || !Number.isFinite(pnl)) continue
    const symbol = String(r.symbol)
    const name = String(r.name ?? '').trim() || tradeNames.get(symbol) || symbol
    if (!symMap.has(symbol)) {
      symMap.set(symbol, { symbol, name, pnl: 0, nTrades: tradeCount.get(symbol) ?? 0 })
    }
    symMap.get(symbol)!.pnl += pnl
    let ym = yearSym.get(year)
    if (!ym) {
      ym = new Map()
      yearSym.set(year, ym)
    }
    if (!ym.has(symbol)) {
      ym.set(symbol, { symbol, name, pnl: 0, nTrades: tradeCount.get(`${symbol}|${year}`) ?? 0 })
    }
    ym.get(symbol)!.pnl += pnl
  }
  return _assemble(symMap, yearSym)
}

/** 共享装配: 排序 / 份额 / 分母。 */
function _assemble(
  symMap: Map<string, SymbolPnl>,
  yearSym: Map<number, Map<string, SymbolPnl>>,
): ContributionAnalysis | null {
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
      return {
        year,
        totalAbs: totalAbsY,
        segs: nonzero.sort(segCmp).map(s => ({
          ...s,
          share: totalAbsY > 0 ? Math.abs(s.pnl) / totalAbsY : 0,
        })),
      }
    })
  return { symbols, years, totalAbs }
}

/**
 * 模式一(按年度)行数据: 当年 |pnl| 前 topN 保持独立段, 其余合并为「其他」(净额求和, 带 count)。
 * share 在真实段内归一 (Σ|真实段| = 100%); 「其他」固定窄条不占比例, share 恒 0。
 * 真实段段序同 segCmp (负数由小到大 → 正数由大到小); 「其他」不参与排序, 固定垫底 (最右)。
 * spanYears 传入回测覆盖的完整年份序列时, 无交易的年份补空行 (totalAbs=0)。
 */
export function buildYearlyRows(a: ContributionAnalysis, topN: number, spanYears?: number[]): YearlyRow[] {
  const n = Math.max(topN, 0)
  const rows = a.years.map(y => {
    const sorted = [...y.segs].sort((x, z) => Math.abs(z.pnl) - Math.abs(x.pnl))
    const rest = sorted.slice(n)
    const segs: YearRowSeg[] = sorted
      .slice(0, n)
      .map(s => ({ key: s.symbol, name: s.name, pnl: s.pnl, share: 0 }))
    const realAbs = segs.reduce((s, x) => s + Math.abs(x.pnl), 0)
    for (const s of segs) s.share = realAbs > 0 ? Math.abs(s.pnl) / realAbs : 0
    segs.sort(segCmp)
    const restPnl = rest.reduce((s, x) => s + x.pnl, 0)
    if (restPnl !== 0) {
      segs.push({ key: OTHER_KEY, name: '其他', pnl: restPnl, share: 0, count: rest.length })
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
