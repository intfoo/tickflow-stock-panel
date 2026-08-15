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

  // 拉当前已保存的宽基列表
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

  const filtered: EtfInstrumentItem[] = useMemo(() => {
    const items = instrumentsQuery.data?.items ?? []
    const q = keyword.trim().toLowerCase()
    if (!q) return items
    return items.filter(i =>
      i.symbol.toLowerCase().includes(q) || i.name.toLowerCase().includes(q),
    )
  }, [instrumentsQuery.data?.items, keyword])

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

  if (!open) return null

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

        {/* 列表 */}
        <div className="overflow-y-auto px-2 py-2" style={{ maxHeight: '50vh' }}>
          {instrumentsQuery.isLoading && (
            <div className="py-8 text-center text-xs text-muted">加载中…</div>
          )}
          {!instrumentsQuery.isLoading && filtered.length === 0 && (
            <div className="py-8 text-center text-xs text-muted">无匹配结果</div>
          )}
          {filtered.map(item => {
            const checked = selected.has(item.symbol)
            return (
              <label
                key={item.symbol}
                className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 hover:bg-elevated/50"
              >
                <span
                  className={`flex h-4 w-4 items-center justify-center rounded border ${checked ? 'border-accent bg-accent' : 'border-border'}`}
                >
                  {checked && <Check className="h-3 w-3 text-white" />}
                </span>
                <input type="checkbox" checked={checked} onChange={() => toggle(item.symbol)} className="sr-only" />
                <span className="font-mono text-xs text-secondary">{item.symbol}</span>
                <span className="text-xs text-foreground">{item.name}</span>
              </label>
            )
          })}
        </div>

        {/* 底栏 */}
        <div className="flex items-center justify-between border-t border-border px-4 py-2.5">
          <span className="text-xs text-muted">已选 {selected.size} 只</span>
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
