import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Search, X, Check } from 'lucide-react'
import { etfFundApi, type EtfInstrumentItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

interface Props {
  open: boolean
  onClose: () => void
}

const QUICK_KEYWORDS = [
  '沪深300', '中证500', '中证1000', '上证50',
  '创业板指', '科创50', '深证100', '中证2000',
  '中证A500', '上证综指',
]

/** 按 market_cap 降序，null 排最后 */
function sortByMarketCapDesc(a: EtfInstrumentItem, b: EtfInstrumentItem): number {
  if (a.market_cap == null && b.market_cap == null) return 0
  if (a.market_cap == null) return 1
  if (b.market_cap == null) return -1
  return b.market_cap - a.market_cap
}

function fmtMarketCap(v: number | null | undefined): string {
  if (v == null) return ''
  return v.toFixed(0)
}

export function BroadEtfDialog({ open, onClose }: Props) {
  const qc = useQueryClient()
  const [keyword, setKeyword] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [initialized, setInitialized] = useState(false)

  // 拉全量 instruments
  const instrumentsQuery = useQuery({
    queryKey: QK.etfFundInstruments,
    queryFn: etfFundApi.getInstruments,
    staleTime: 10 * 60_000,
  })

  // 拉当前宽基配置（effective set + is_default + presets）
  const broadQuery = useQuery({
    queryKey: QK.etfFundBroad,
    queryFn: etfFundApi.getBroad,
    staleTime: 30_000,
  })

  // 重新打开时重置初始化标记，使选中集从最新 broadQuery.data 重新加载
  useEffect(() => {
    if (open) setInitialized(false)
  }, [open])

  // 初始化选中集
  useEffect(() => {
    if (!initialized && broadQuery.data) {
      setSelected(new Set(broadQuery.data.symbols))
      setInitialized(true)
    }
  }, [broadQuery.data, initialized])

  const allItems = useMemo(() => instrumentsQuery.data?.items ?? [], [instrumentsQuery.data?.items])

  const presetSymbols = useMemo(() => {
    const set = new Set<string>()
    for (const p of broadQuery.data?.presets ?? []) set.add(p.symbol)
    return set
  }, [broadQuery.data?.presets])

  const hasKeyword = keyword.trim().length > 0

  // 过滤结果（有搜索词时使用）
  const filtered: EtfInstrumentItem[] = useMemo(() => {
    const q = keyword.trim().toLowerCase()
    if (!q) return allItems
    return allItems.filter(i =>
      i.symbol.toLowerCase().includes(q) || i.name.toLowerCase().includes(q),
    )
  }, [allItems, keyword])

  // 无搜索词时三分组数据
  const selectedItems = useMemo(() => {
    return allItems.filter(i => selected.has(i.symbol)).sort(sortByMarketCapDesc)
  }, [allItems, selected])

  const presetItems = useMemo(() => {
    // 推荐宽基中未选中的
    return allItems.filter(i => presetSymbols.has(i.symbol) && !selected.has(i.symbol)).sort(sortByMarketCapDesc)
  }, [allItems, presetSymbols, selected])

  const restItems = useMemo(() => {
    // 全部 ETF 中未选中且不在推荐中的
    return allItems.filter(i => !selected.has(i.symbol) && !presetSymbols.has(i.symbol)).sort(sortByMarketCapDesc)
  }, [allItems, presetSymbols, selected])

  const toggle = (symbol: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(symbol)) next.delete(symbol)
      else next.add(symbol)
      return next
    })
  }

  const selectAllFiltered = () => {
    setSelected(prev => {
      const next = new Set(prev)
      for (const item of filtered) next.add(item.symbol)
      return next
    })
  }

  const selectAllPresets = () => {
    setSelected(prev => {
      const next = new Set(prev)
      for (const s of presetSymbols) next.add(s)
      return next
    })
  }

  const clearAll = () => setSelected(new Set())

  const saveMutation = useMutation({
    mutationFn: () => etfFundApi.putBroad([...selected]),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.etfFundBroad })
      qc.invalidateQueries({ queryKey: ['etf-fund', 'flow'] })
      toast('宽基配置已保存', 'success')
      onClose()
    },
  })

  const resetMutation = useMutation({
    mutationFn: () => etfFundApi.resetBroad(),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: QK.etfFundBroad })
      qc.invalidateQueries({ queryKey: ['etf-fund', 'flow'] })
      // 重新初始化选中集为默认推荐清单
      setSelected(new Set(data.symbols))
      setInitialized(true)
      toast('已恢复默认配置', 'success')
    },
  })

  if (!open) return null

  const isDefault = broadQuery.data?.is_default ?? true

  // 渲染单行 ETF
  const renderItem = (item: EtfInstrumentItem, showPresetBadge = false) => {
    const checked = selected.has(item.symbol)
    const capStr = fmtMarketCap(item.market_cap)
    return (
      <label
        key={item.symbol}
        className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 hover:bg-elevated/50"
      >
        <span
          className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${checked ? 'border-accent bg-accent' : 'border-border'}`}
        >
          {checked && <Check className="h-3 w-3 text-white" />}
        </span>
        <input type="checkbox" checked={checked} onChange={() => toggle(item.symbol)} className="sr-only" />
        {showPresetBadge && (
          <span className="shrink-0 rounded-sm bg-accent/15 px-1 text-[9px] font-medium text-accent">荐</span>
        )}
        <span className="shrink-0 font-mono text-xs text-secondary">{item.symbol}</span>
        <span className="text-xs text-foreground">{item.name}</span>
        {capStr && <span className="ml-auto shrink-0 font-mono text-[10px] text-muted">{capStr}亿</span>}
      </label>
    )
  }

  return (
    <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        className="w-full max-w-2xl rounded-card border border-border bg-surface shadow-xl"
        onClick={e => e.stopPropagation()}
        style={{ maxHeight: '80vh' }}
      >
        {/* 标题栏 */}
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="text-sm font-semibold text-foreground">宽基ETF配置</h3>
          <button onClick={onClose} className="text-muted hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* 搜索 + 操作栏 */}
        <div className="flex items-center gap-2 border-b border-border px-4 py-2">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <input
              value={keyword}
              onChange={e => setKeyword(e.target.value)}
              placeholder="搜索代码/名称"
              className="w-full rounded border border-border bg-base py-1 pl-7 pr-2 text-xs text-foreground outline-none focus:border-accent"
            />
          </div>
          <button onClick={selectAllFiltered} className="rounded bg-elevated px-2 py-1 text-[10px] text-secondary hover:text-foreground">全选</button>
          <button onClick={clearAll} className="rounded bg-elevated px-2 py-1 text-[10px] text-secondary hover:text-foreground">清空</button>
        </div>

        {/* 快捷搜索 chips 行 */}
        <div className="flex flex-wrap items-center gap-1 border-b border-border px-4 py-1.5">
          {QUICK_KEYWORDS.map(kw => (
            <button
              key={kw}
              onClick={() => setKeyword(kw)}
              className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-secondary hover:bg-elevated/70 hover:text-foreground"
            >
              {kw}
            </button>
          ))}
        </div>

        {/* 列表 */}
        <div className="overflow-y-auto px-2 py-2" style={{ maxHeight: '50vh' }}>
          {instrumentsQuery.isLoading && (
            <div className="py-8 text-center text-xs text-muted">加载中…</div>
          )}
          {!instrumentsQuery.isLoading && hasKeyword && filtered.length === 0 && (
            <div className="py-8 text-center text-xs text-muted">无匹配结果</div>
          )}

          {/* 有搜索词：平铺过滤结果 */}
          {hasKeyword && filtered.map(item => renderItem(item, presetSymbols.has(item.symbol)))}

          {/* 无搜索词：三分组 */}
          {!hasKeyword && !instrumentsQuery.isLoading && (
            <>
              {/* 已选 */}
              {selectedItems.length > 0 && (
                <div className="mb-2">
                  <div className="px-2 py-1 text-[10px] font-medium text-muted">已选 ({selectedItems.length})</div>
                  {selectedItems.map(item => renderItem(item, presetSymbols.has(item.symbol)))}
                </div>
              )}

              {/* 推荐宽基 */}
              {presetItems.length > 0 && (
                <div className="mb-2">
                  <div className="flex items-center justify-between px-2 py-1">
                    <span className="text-[10px] font-medium text-muted">推荐宽基 ({presetItems.length})</span>
                    <button
                      onClick={selectAllPresets}
                      className="rounded bg-elevated px-1.5 py-0.5 text-[9px] text-secondary hover:text-foreground"
                    >
                      全选推荐
                    </button>
                  </div>
                  {presetItems.map(item => renderItem(item, true))}
                </div>
              )}

              {/* 全部 ETF */}
              {restItems.length > 0 && (
                <div className="mb-2">
                  <div className="px-2 py-1 text-[10px] font-medium text-muted">全部 ETF ({restItems.length})</div>
                  {restItems.map(item => renderItem(item, false))}
                </div>
              )}

              {/* 三组都空 */}
              {selectedItems.length === 0 && presetItems.length === 0 && restItems.length === 0 && (
                <div className="py-8 text-center text-xs text-muted">暂无 ETF 数据</div>
              )}
            </>
          )}
        </div>

        {/* 底栏 */}
        <div className="flex items-center justify-between border-t border-border px-4 py-2.5">
          <div className="flex items-center gap-3">
            <span className="text-xs text-muted">已选 {selected.size} 只</span>
            {!isDefault && (
              <button
                onClick={() => resetMutation.mutate()}
                disabled={resetMutation.isPending}
                className="rounded border border-border bg-base px-2 py-1 text-[10px] text-secondary hover:text-foreground disabled:opacity-50"
              >
                {resetMutation.isPending ? '恢复中…' : '恢复默认'}
              </button>
            )}
          </div>
          <button
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending}
            className="rounded-btn bg-accent px-4 py-1.5 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50"
          >
            {saveMutation.isPending ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
