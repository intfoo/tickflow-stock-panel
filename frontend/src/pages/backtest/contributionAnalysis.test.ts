import { describe, expect, it } from 'vitest'
import type { StrategyBacktestTrade } from '@/lib/api'
import {
  analyzeBackendContributions,
  analyzeContribution,
  buildTreemapTiles,
  buildYearlyRows,
  fmtAmount,
  fmtReturnContrib,
  fmtShare,
  OTHER_KEY,
} from './contributionAnalysis'

function trade(symbol: string, exitDate: string, pnlAmount: number | undefined, name?: string): StrategyBacktestTrade {
  return {
    symbol,
    name,
    entry_date: '2025-01-01',
    exit_date: exitDate,
    entry_price: 1,
    exit_price: 1,
    pnl_pct: 0,
    duration: 1,
    exit_reason: 'signal',
    pnl_amount: pnlAmount,
  }
}

const SAMPLE = [
  trade('A', '2024-03-10', 2000, '甲'),
  trade('B', '2024-06-10', 8000, '乙'),
  trade('A', '2025-02-10', -3000, '甲'),
  trade('C', '2025-05-10', 1000, '丙'),
  trade('D', '2025-07-10', 500, '丁'),
]

describe('analyzeContribution', () => {
  it('空 trades / 全部盈亏为 0 → null', () => {
    expect(analyzeContribution([])).toBeNull()
    expect(analyzeContribution([trade('A', '2025-01-10', 0), trade('B', '2025-02-10', undefined)])).toBeNull()
  })

  it('按标的聚合全期盈亏与交易数, 缺失 pnl_amount 按 0 计', () => {
    const a = analyzeContribution([...SAMPLE, trade('E', '2025-08-10', undefined, '戊')])!
    const bySym = new Map(a.symbols.map(s => [s.symbol, s]))
    expect(bySym.get('A')).toMatchObject({ pnl: -1000, nTrades: 2, name: '甲' })
    expect(bySym.get('B')).toMatchObject({ pnl: 8000, nTrades: 1 })
    expect(bySym.get('E')).toMatchObject({ pnl: 0, nTrades: 1 })
    // Σ|净额| = |A:-1000| + |B:8000| + |C:1000| + |D:500| + |E:0|
    expect(a.totalAbs).toBe(10_500)
  })

  it('name 缺失时回退为 symbol', () => {
    const a = analyzeContribution([trade('X', '2025-01-10', 100)])!
    expect(a.symbols[0].name).toBe('X')
  })

  it('全期榜按 |pnl| 降序', () => {
    const a = analyzeContribution(SAMPLE)!
    expect(a.symbols.map(s => s.symbol)).toEqual(['B', 'A', 'C', 'D'])
  })

  it('年度聚合: 按 exit 年归集, 段序 = 负数由小到大 → 正数由大到小, share 按当年 Σ|pnl|', () => {
    const a = analyzeContribution(SAMPLE)!
    expect(a.years.map(y => y.year)).toEqual([2024, 2025])
    const y2024 = a.years[0]
    expect(y2024.totalAbs).toBe(10_000)
    expect(y2024.segs.map(s => s.symbol)).toEqual(['B', 'A'])
    expect(y2024.segs[0].share).toBeCloseTo(0.8, 10)
    const y2025 = a.years[1]
    // 2025: A -3000 (负组), C +1000, D +500 (正组降序) → 负组在前
    expect(y2025.segs.map(s => s.symbol)).toEqual(['A', 'C', 'D'])
    expect(y2025.totalAbs).toBe(4_500)
    expect(y2025.segs[0].share).toBeCloseTo(3000 / 4500, 10)
  })

  it('段序锁定: 负数由小到大在前, 正数由大到小在后 (非 |pnl| 混排)', () => {
    const a = analyzeContribution([
      trade('P1', '2025-01-10', 9000),
      trade('N1', '2025-02-10', -500),
      trade('P2', '2025-03-10', 3000),
      trade('N2', '2025-04-10', -2000),
    ])!
    // |pnl| 混排会是 P1,P2,N2,N1; 期望 N2(-2000) → N1(-500) → P1(9000) → P2(3000)
    expect(a.years[0].segs.map(s => s.symbol)).toEqual(['N2', 'N1', 'P1', 'P2'])
  })

  it('非法日期/年份的交易被跳过', () => {
    expect(analyzeContribution([trade('A', 'bad', 1000)])).toBeNull()
    expect(analyzeContribution([trade('A', '0001-01-01', 500)])).toBeNull()
    const a = analyzeContribution([trade('A', 'bad', 1000), trade('A', '2025-03-10', 500)])!
    expect(a.symbols[0].pnl).toBe(500)
  })
})

describe('buildYearlyRows', () => {
  it('当年 |pnl| 前 topN 独立成段, 其余合并「其他」; share 按行内显示段归一', () => {
    const a = analyzeContribution(SAMPLE)!
    const rows = buildYearlyRows(a, 2)
    const y2024 = rows[0]
    expect(y2024.segs.map(s => s.key)).toEqual(['B', 'A'])
    // share 行内归一: Σ|显示段| = 10000 → B 0.8 / A 0.2, 行铺满整宽
    expect(y2024.segs[0].share).toBeCloseTo(0.8, 10)
    expect(y2024.segs.reduce((s, x) => s + x.share, 0)).toBeCloseTo(1, 10)
    const y2025 = rows[1]
    // 当年 top2 = A(-3000), C(+1000); D(+500) 并入其他 — 与全期总榜无关
    expect(y2025.segs.map(s => s.key)).toEqual(['A', 'C', OTHER_KEY])
    // share 只在真实段内归一: A 0.75 / C 0.25; 其他固定窄条 share=0, 带并入标的数
    expect(y2025.segs[0].share).toBeCloseTo(0.75, 10)
    expect(y2025.segs[1].share).toBeCloseTo(0.25, 10)
    const other = y2025.segs[2]
    expect(other.pnl).toBe(500)
    expect(other.share).toBe(0)
    expect(other.count).toBe(1)
  })

  it('当年某标的进入 topN 与否互不影响: 全期小标的在当年也可独立成段', () => {
    const a = analyzeContribution([
      trade('BIG', '2024-01-10', 100_000),
      trade('S1', '2025-01-10', 300),
      trade('S2', '2025-02-10', 200),
    ])!
    const rows = buildYearlyRows(a, 1)
    // 2025 年 BIG 未交易, 当年 top1 = S1, S2 并入其他 (而非全期 top1=BIG 导致全并入其他)
    expect(rows[1].segs.map(s => s.key)).toEqual(['S1', OTHER_KEY])
  })

  it('「其他」可为负贡献 (净额为负时 share 取绝对值)', () => {
    const a = analyzeContribution([
      trade('A', '2025-01-10', 10_000, '甲'),
      trade('X', '2025-02-10', -6000, '辛'),
      trade('Y', '2025-03-10', -1000, '癸'),
    ])!
    const rows = buildYearlyRows(a, 1)
    // 其他不参与排序, 固定垫底 (即使净额为负且 |pnl| 更大): A(+10000) → 其他(-7000)
    expect(rows[0].segs.map(s => s.key)).toEqual(['A', OTHER_KEY])
    expect(rows[0].segs[0].share).toBe(1)
    expect(rows[0].segs[1].pnl).toBe(-7000)
    expect(rows[0].segs[1].share).toBe(0)
    expect(rows[0].segs[1].count).toBe(2)
  })
})

describe('buildTreemapTiles', () => {
  it('topN + 其他, share 按全期 Σ|pnl|', () => {
    const a = analyzeContribution(SAMPLE)!
    const tiles = buildTreemapTiles(a, 2) // top: B(8000), A(-1000)
    expect(tiles.map(t => t.key)).toEqual(['B', 'A', OTHER_KEY])
    expect(tiles[0].share).toBeCloseTo(8000 / 10_500, 10)
    expect(tiles[1].share).toBeCloseTo(1000 / 10_500, 10)
    // 其他 = C + D = 1500, 2 笔
    expect(tiles[2]).toMatchObject({ pnl: 1500, nTrades: 2 })
    expect(tiles[2].share).toBeCloseTo(1500 / 10_500, 10)
  })

  it('pnl=0 的标的独立成块被过滤, 但不影响其他', () => {
    const a = analyzeContribution([trade('A', '2025-01-10', 1000, '甲'), trade('B', '2025-02-10', 0, '乙')])!
    const tiles = buildTreemapTiles(a, 5)
    expect(tiles.map(t => t.key)).toEqual(['A'])
  })
})

describe('buildYearlyRows spanYears', () => {
  it('无交易年份补空行 (totalAbs=0, segs=[])', () => {
    const a = analyzeContribution([
      trade('A', '2024-03-10', 2000, '甲'),
      trade('A', '2026-05-10', 1000, '甲'),
    ])!
    const rows = buildYearlyRows(a, 10, [2024, 2025, 2026])
    expect(rows.map(r => r.year)).toEqual([2024, 2025, 2026])
    expect(rows[1]).toMatchObject({ totalAbs: 0, segs: [] })
    expect(rows[0].segs[0].pnl).toBe(2000)
    expect(rows[2].segs[0].pnl).toBe(1000)
  })

  it('不传 spanYears 时只含有交易的年份', () => {
    const a = analyzeContribution([
      trade('A', '2024-03-10', 2000, '甲'),
      trade('A', '2026-05-10', 1000, '甲'),
    ])!
    expect(buildYearlyRows(a, 10).map(r => r.year)).toEqual([2024, 2026])
  })
})

describe('analyzeBackendContributions', () => {
  const ROWS = [
    { symbol: 'A', name: '甲', year: 2023, pnl: 5000 },
    { symbol: 'A', name: '甲', year: 2024, pnl: 15_000 },
    { symbol: 'B', name: '乙', year: 2024, pnl: -3000 },
  ]

  it('按 (symbol, year) 归集, 名称/nTrades 从 trades 兜底', () => {
    const a = analyzeBackendContributions(ROWS, [
      trade('A', '2024-01-03', 20_000, '甲'),
      trade('B', '2024-02-01', -3000, '乙'),
    ])!
    const bySym = new Map(a.symbols.map(s => [s.symbol, s]))
    // A 跨年净额 20000, nTrades=1 (仅 2024 一笔卖出)
    expect(bySym.get('A')).toMatchObject({ pnl: 20_000, nTrades: 1, name: '甲' })
    expect(bySym.get('B')).toMatchObject({ pnl: -3000, nTrades: 1 })
    expect(a.years.map(y => y.year)).toEqual([2023, 2024])
    // 2023 年 A 有贡献但该年无卖出 → nTrades=0
    expect(a.years[0].segs[0]).toMatchObject({ symbol: 'A', pnl: 5000, nTrades: 0 })
    // 2024 段序负组在前: B(-3000) → A(+15000)
    expect(a.years[1].segs.map(s => s.symbol)).toEqual(['B', 'A'])
    expect(a.totalAbs).toBe(23_000)
  })

  it('name 缺失回退 trades, 再回退 symbol', () => {
    const a = analyzeBackendContributions(
      [{ symbol: 'X', year: 2025, pnl: 100 }],
      [trade('X', '2025-01-10', 100, '未知股')],
    )!
    expect(a.symbols[0].name).toBe('未知股')
    const b = analyzeBackendContributions([{ symbol: 'Y', year: 2025, pnl: 100 }], [])!
    expect(b.symbols[0].name).toBe('Y')
  })

  it('空行 / 全零 / 非法行 → null', () => {
    expect(analyzeBackendContributions([], [])).toBeNull()
    expect(analyzeBackendContributions([{ symbol: 'A', year: 2025, pnl: 0 }], [])).toBeNull()
    expect(analyzeBackendContributions([{ symbol: 'A', year: 99, pnl: 100 }], [])).toBeNull()
  })
})

describe('fmtAmount / fmtReturnContrib / fmtShare', () => {
  it('金额: ≥1000 万元一位小数, 不足 1000 用元整数, 正数不带符号', () => {
    expect(fmtAmount(12_500)).toBe('1.3万')
    expect(fmtAmount(-20_000)).toBe('-2.0万')
    expect(fmtAmount(1000)).toBe('0.1万')
    expect(fmtAmount(850)).toBe('850元')
    expect(fmtAmount(-320)).toBe('-320元')
    expect(fmtAmount(0)).toBe('0元')
  })

  it('贡献收益率: 盈亏 ÷ 初始资金 pp, 带符号; 资金非法返回 —', () => {
    expect(fmtReturnContrib(2000, 100_000)).toBe('2.0%')
    expect(fmtReturnContrib(-3000, 100_000)).toBe('-3.0%')
    expect(fmtReturnContrib(1, 0)).toBe('—')
    expect(fmtReturnContrib(1, NaN)).toBe('—')
  })

  it('贡献占比: 盈亏 ÷ Σ|盈亏|, 带符号; 分母为 0 返回 —', () => {
    expect(fmtShare(2000, 10_000)).toBe('20.0%')
    expect(fmtShare(-3000, 10_000)).toBe('-30.0%')
    expect(fmtShare(1, 0)).toBe('—')
  })
})
