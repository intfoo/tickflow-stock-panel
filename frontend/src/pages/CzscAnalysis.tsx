import { useState, useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, LineChart, Loader2, AlertTriangle, PackageOpen, ListChecks } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { LastStockChip } from '@/components/LastStockChip'
import { CzscKChart } from '@/components/czsc/CzscKChart'
import { api } from '@/lib/api'
import type { CzscFreq } from '@/lib/api'
import { useLastStock } from '@/lib/useLastStock'

const FREQ_OPTIONS: CzscFreq[] = ['日线', '周线', '月线', '季线', '1分钟', '5分钟', '15分钟', '30分钟', '60分钟']

const MINUTE_FREQS: CzscFreq[] = ['1分钟', '5分钟', '15分钟', '30分钟', '60分钟']

/**
 * 缠论分析页 —— 多频率 K 线 + 分型 / 笔 / 中枢 / 买卖点（基于 czsc）。
 *
 * 复刻 StockAnalysis.tsx 的页面壳：PageHeader + 搜索栏 + 主体（左图表 + 右侧栏）。
 * 搜索栏旁加频率选择器（9 档）；右侧栏分两部分：上方信号勾选面板，下方结构摘要。
 * czsc 未安装时显示安装提示，无数据时显示空状态。
 */
export function CzscAnalysis() {
  const [symbol, setSymbol] = useState<string>('')
  const [name, setName] = useState<string>('')
  const [freq, setFreq] = useState<CzscFreq>('日线')
  const { last: lastStock, remember: rememberStock } = useLastStock('czsc-analysis')

  // 自动恢复上次选中的股票
  useEffect(() => {
    if (!symbol && lastStock) {
      setSymbol(lastStock.symbol)
      setName(lastStock.name)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onSelect = (sym: string, nm: string) => {
    setSymbol(sym)
    setName(nm)
    rememberStock(sym, nm)
  }

  return (
    <>
      <PageHeader
        title="缠论分析"
        subtitle="分型 · 笔 · 中枢 · 买卖点（基于 czsc，多频率 + 信号勾选）"
        right={
          <div className="flex items-center gap-2">
            <LastStockChip stock={lastStock} onSelect={onSelect} />
          </div>
        }
      />

      <div className="w-full px-8 py-6 space-y-6">
        {/* 搜索栏 + 频率选择器 */}
        <div className="flex items-center gap-3 flex-wrap">
          <div className="w-72">
            <StockFinancialSearch onSelect={onSelect} assetTypes="stock,etf,index" />
          </div>
          {/* 频率选择器 */}
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-muted">频率</span>
            <select
              value={freq}
              onChange={(e) => setFreq(e.target.value as CzscFreq)}
              className="h-8 rounded-lg border border-border/60 bg-elevated/40 px-2 text-xs text-foreground outline-none focus:border-sky-400/60 cursor-pointer"
            >
              {FREQ_OPTIONS.map((f) => (
                <option key={f} value={f}>{f}</option>
              ))}
            </select>
          </div>
          {symbol && (
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-medium text-foreground">{name || symbol}</span>
              <span className="text-[10px] font-mono text-muted">{symbol}</span>
            </div>
          )}
        </div>

        {/* 主体: 左侧图表 + 右侧侧栏（信号勾选 + 结构摘要） */}
        {!symbol ? (
          <EmptyState
            icon={LineChart}
            title="选择一只股票开始缠论分析"
            hint="搜索代码或名称，查看分型、笔、中枢与买卖点。支持股票 / ETF / 指数。"
          />
        ) : (
          <CzscAnalysisBoard symbol={symbol} freq={freq} />
        )}
      </div>
    </>
  )
}

// ===== 分析看板: 缠论图表 + 信号勾选面板 + 摘要侧栏 =====
function CzscAnalysisBoard({ symbol, freq }: { symbol: string; freq: CzscFreq }) {
  // --- 信号目录 + 默认信号 ---
  const signalsQuery = useQuery({
    queryKey: ['czsc-signals'],
    queryFn: () => api.czscSignals(),
    staleTime: 5 * 60_000,
  })
  const statusQuery = useQuery({
    queryKey: ['czsc-status'],
    queryFn: () => api.czscStatus(),
    staleTime: 5 * 60_000,
  })

  const defaultSignals = statusQuery.data?.default_signals ?? []
  const allSignalNames = useMemo(() => {
    const groups = signalsQuery.data?.groups ?? {}
    return Object.values(groups).flat().map((s) => s.name)
  }, [signalsQuery.data])

  // selectedSignals 初始化为 default_signals；symbol/freq 变化时保持
  const [selectedSignals, setSelectedSignals] = useState<string[]>([])
  const [initialized, setInitialized] = useState(false)

  // 当 default_signals 首次加载完成时初始化 selectedSignals
  useEffect(() => {
    if (!initialized && defaultSignals.length > 0) {
      setSelectedSignals(defaultSignals)
      setInitialized(true)
    }
  }, [defaultSignals, initialized])

  // --- 缠论分析 query ---
  const signalsKey = selectedSignals.join(',')
  const query = useQuery({
    queryKey: ['czsc-analyze', symbol, freq, signalsKey],
    queryFn: () => api.czscAnalyze(symbol, freq, undefined, selectedSignals),
    enabled: !!symbol,
    staleTime: 60_000,
  })

  // --- 信号勾选操作 ---
  const toggleSignal = (name: string) => {
    setSelectedSignals((prev) =>
      prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name],
    )
  }
  const selectAll = () => setSelectedSignals(allSignalNames)
  const clearAll = () => setSelectedSignals([])
  const restoreDefault = () => setSelectedSignals(defaultSignals)

  const isMinuteFreq = MINUTE_FREQS.includes(freq)

  // --- 渲染 ---
  if (query.isLoading) {
    return (
      <div className="grid grid-cols-[1fr_300px] gap-6 items-start">
        <div className="flex items-center justify-center py-20">
          <Loader2 className="h-5 w-5 animate-spin text-muted" />
        </div>
      </div>
    )
  }

  if (query.isError) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title="缠论分析加载失败"
        hint="请检查网络或后端配置后重试。"
      />
    )
  }

  const data = query.data
  if (!data || !data.available) {
    return (
      <EmptyState
        icon={PackageOpen}
        title="czsc 扩展未安装"
        hint={data?.message || '缠论分析需要 czsc 扩展，请运行: uv sync --extra czsc'}
      />
    )
  }

  const bars = data.bars ?? []
  if (bars.length === 0) {
    return (
      <EmptyState
        icon={LineChart}
        title={isMinuteFreq ? '暂无分钟K数据' : '暂无日K数据'}
        hint={isMinuteFreq
          ? '该标的未同步分钟K（Pro+ 功能），或非交易日。'
          : '该标的尚未同步日K，请先在数据页或自选页同步。'}
      />
    )
  }

  const fxList = data.fx_list ?? []
  const biList = data.bi_list ?? []
  const zsList = data.zs_list ?? []
  const signalMarkers = data.signal_markers ?? []

  return (
    <div className="grid grid-cols-[1fr_300px] gap-6 items-start">
      {/* 左侧: 缠论图表 */}
      <div className="min-w-0 rounded-card border border-border/60 bg-surface/40 overflow-hidden">
        <div className="px-4 py-3 border-b border-border/40">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 min-w-0">
              <Activity className="h-4 w-4 text-sky-400 shrink-0" />
              <span className="text-sm font-medium text-foreground">缠论结构图</span>
              <span className="text-[10px] text-muted shrink-0">{data.freq || freq}</span>
            </div>
            <div className="flex items-baseline gap-2 shrink-0">
              <span className="text-[10px] text-muted">{bars.length} 根K线</span>
            </div>
          </div>
        </div>
        <div className="p-3">
          <CzscKChart
            bars={bars}
            fxList={fxList}
            biList={biList}
            zsList={zsList}
            signalMarkers={signalMarkers}
            height={480}
          />
        </div>
      </div>

      {/* 右侧: 信号勾选面板 + 摘要侧栏 */}
      <aside className="self-start sticky top-0 space-y-4 max-h-[calc(100vh-120px)] overflow-y-auto">
        {/* 信号勾选面板 */}
        <SignalPanel
          signalsQuery={signalsQuery}
          statusQuery={statusQuery}
          selectedSignals={selectedSignals}
          onToggle={toggleSignal}
          onSelectAll={selectAll}
          onClearAll={clearAll}
          onRestoreDefault={restoreDefault}
        />

        {/* 结构摘要 */}
        <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
          <div className="px-3 py-2.5 border-b border-border/40 flex items-center gap-2">
            <Activity className="h-3.5 w-3.5 text-sky-400 shrink-0" />
            <span className="text-xs font-medium text-foreground">结构摘要</span>
          </div>

          <div className="p-3 space-y-3">
            {/* 统计数字 */}
            <div className="grid grid-cols-3 gap-2">
              <StatCard label="分型" value={fxList.length} color="#EAB308" />
              <StatCard label="笔" value={biList.length} color="#F97316" />
              <StatCard label="中枢" value={zsList.length} color="#3B82F6" />
            </div>

            {/* 买卖点列表 */}
            <div>
              <div className="text-[10px] text-muted mb-1.5 flex items-center justify-between">
                <span>买卖点</span>
                <span className="opacity-60">{signalMarkers.length}</span>
              </div>
              {signalMarkers.length === 0 ? (
                <div className="text-[11px] text-muted/60 py-2 text-center">暂无买卖点信号</div>
              ) : (
                <div className="space-y-1 max-h-[300px] overflow-y-auto">
                  {signalMarkers.map((m, i) => (
                    <div
                      key={`${m.dt}-${i}`}
                      className="flex items-center gap-2 py-1 px-1.5 -mx-1.5 rounded text-[11px] hover:bg-elevated/40 transition-colors"
                    >
                      <span
                        className="shrink-0 inline-flex items-center justify-center h-4 w-4 rounded text-[9px] font-bold text-white"
                        style={{ backgroundColor: m.kind === 'buy' ? '#2D9B65' : '#C74040' }}
                      >
                        {m.kind === 'buy' ? '买' : '卖'}
                      </span>
                      <span className="text-secondary shrink-0 font-mono">{m.dt}</span>
                      <span className="text-foreground truncate flex-1">{m.label}</span>
                      <span className="text-muted font-mono shrink-0">{(m.price ?? 0).toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </aside>
    </div>
  )
}

// ===== 信号勾选面板 =====
interface SignalPanelProps {
  signalsQuery: ReturnType<typeof useQuery>
  statusQuery: ReturnType<typeof useQuery>
  selectedSignals: string[]
  onToggle: (name: string) => void
  onSelectAll: () => void
  onClearAll: () => void
  onRestoreDefault: () => void
}

function SignalPanel({
  signalsQuery,
  statusQuery,
  selectedSignals,
  onToggle,
  onSelectAll,
  onClearAll,
  onRestoreDefault,
}: SignalPanelProps) {
  const catalog = signalsQuery.data
  const groups = catalog?.groups ?? {}
  const groupEntries = Object.entries(groups)
  const selectedCount = selectedSignals.length

  return (
    <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
      <div className="px-3 py-2.5 border-b border-border/40 flex items-center gap-2">
        <ListChecks className="h-3.5 w-3.5 text-sky-400 shrink-0" />
        <span className="text-xs font-medium text-foreground">信号勾选</span>
        <span className="text-[10px] text-muted ml-auto">{selectedCount} / {catalog?.total ?? 0}</span>
      </div>

      <div className="p-3 space-y-2">
        {/* 操作按钮 */}
        <div className="flex items-center gap-1.5">
          <button
            onClick={onSelectAll}
            className="flex-1 h-6 rounded text-[10px] bg-elevated/40 hover:bg-elevated/70 text-secondary transition-colors"
          >
            全选
          </button>
          <button
            onClick={onClearAll}
            className="flex-1 h-6 rounded text-[10px] bg-elevated/40 hover:bg-elevated/70 text-secondary transition-colors"
          >
            清空
          </button>
          <button
            onClick={onRestoreDefault}
            className="flex-1 h-6 rounded text-[10px] bg-elevated/40 hover:bg-elevated/70 text-secondary transition-colors"
          >
            默认
          </button>
        </div>

        {/* 性能警告 */}
        {selectedCount > 30 && (
          <div className="flex items-start gap-1.5 rounded-lg bg-amber-500/10 border border-amber-500/30 px-2 py-1.5">
            <AlertTriangle className="h-3 w-3 text-amber-400 shrink-0 mt-0.5" />
            <span className="text-[10px] text-amber-300 leading-tight">
              已选 {selectedCount} 个信号，分钟级 × 大量信号可能数秒，请耐心等待。
            </span>
          </div>
        )}

        {/* 信号列表 */}
        {signalsQuery.isLoading ? (
          <div className="flex items-center justify-center py-4">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />
          </div>
        ) : signalsQuery.isError || !catalog || !catalog.available ? (
          <div className="text-[10px] text-muted/60 py-3 text-center">
            信号目录不可用（需安装 czsc 扩展）
          </div>
        ) : groupEntries.length === 0 ? (
          <div className="text-[10px] text-muted/60 py-3 text-center">暂无信号</div>
        ) : (
          <div className="space-y-2 max-h-[400px] overflow-y-auto pr-0.5">
            {groupEntries.map(([groupName, entries]) => (
              <div key={groupName}>
                <div className="text-[10px] font-medium text-muted/80 px-0.5 py-1 sticky top-0 bg-surface/80 backdrop-blur-sm">
                  {groupName}
                  <span className="opacity-50 ml-1">({entries.length})</span>
                </div>
                {entries.map((sig) => {
                  const checked = selectedSignals.includes(sig.name)
                  return (
                    <label
                      key={sig.name}
                      className="flex items-start gap-1.5 py-0.5 px-1 -mx-1 rounded cursor-pointer hover:bg-elevated/30 transition-colors"
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => onToggle(sig.name)}
                        className="mt-0.5 h-3 w-3 shrink-0 accent-sky-400 cursor-pointer"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="text-[10px] text-foreground truncate font-mono leading-tight">
                          {sig.name}
                        </div>
                        {sig.desc && (
                          <div className="text-[9px] text-muted/70 truncate leading-tight">
                            {sig.desc}
                          </div>
                        )}
                      </div>
                    </label>
                  )
                })}
              </div>
            ))}
          </div>
        )}

        {/* 状态提示 */}
        {statusQuery.isLoading && (
          <div className="text-[9px] text-muted/50 text-center">加载默认信号…</div>
        )}
      </div>
    </div>
  )
}

function StatCard({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="rounded-lg border border-border/40 bg-elevated/20 px-2 py-1.5 text-center">
      <div className="text-[10px] text-muted">{label}</div>
      <div className="text-base font-mono font-bold" style={{ color }}>
        {value}
      </div>
    </div>
  )
}
