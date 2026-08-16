import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, RefreshCw, AlertTriangle } from 'lucide-react'
import { etfFundApi } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { DatePicker } from '@/components/DatePicker'

const pad = (n: number) => String(n).padStart(2, '0')
const toISO = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`

function todayStr(): string {
  return toISO(new Date())
}

/** 往前推 N 个月, 日期溢出时收敛到当月最后一天 (如 08-31 → 02-28) */
function minusMonths(iso: string, months: number): string {
  const d = new Date(`${iso}T00:00:00`)
  const day = d.getDate()
  d.setMonth(d.getMonth() - months)
  if (d.getDate() !== day) d.setDate(0)
  return toISO(d)
}

export function EtfSyncCard() {
  const qc = useQueryClient()
  const [backfillStart, setBackfillStart] = useState('')
  const [backfillEnd, setBackfillEnd] = useState('')

  // 配置查询
  const configQuery = useQuery({
    queryKey: QK.etfFundConfig,
    queryFn: etfFundApi.getConfig,
    staleTime: 30_000,
  })

  // 状态轮询
  const statusQuery = useQuery({
    queryKey: QK.etfFundStatus,
    queryFn: etfFundApi.getStatus,
    refetchInterval: (query) => query.state.data?.backfill?.running ? 2000 : false,
  })

  const config = configQuery.data
  const status = statusQuery.data
  const configured = !!config?.data_source

  // 默认回填区间: 结束 = 本地数据最早日 (无数据则今天), 开始 = 结束前 6 个月。
  // 等 status 首次就绪后填一次; 用户已填写时不覆盖。
  useEffect(() => {
    if (!status || backfillStart || backfillEnd) return
    const end = status.data_range?.min ?? todayStr()
    setBackfillEnd(end)
    setBackfillStart(minusMonths(end, 6))
  }, [status, backfillStart, backfillEnd])

  const saveConfigMutation = useMutation({
    mutationFn: (name: string) => etfFundApi.putConfig({ data_source: name }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.etfFundConfig })
      qc.invalidateQueries({ queryKey: QK.etfFundStatus })
      toast('数据源已保存', 'success')
    },
  })

  const syncMutation = useMutation({
    mutationFn: (body: { mode: 'incremental' | 'backfill'; start?: string; end?: string }) =>
      etfFundApi.postSync(body),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: QK.etfFundStatus })
      toast(vars.mode === 'incremental' ? '增量同步已启动' : '回填已启动', 'success')
    },
  })

  const handleIncremental = () => {
    syncMutation.mutate({ mode: 'incremental' })
  }

  const handleBackfill = () => {
    const fmt = /^\d{4}-\d{2}-\d{2}$/
    if (!fmt.test(backfillStart) || !fmt.test(backfillEnd)) {
      toast('请输入 YYYY-MM-DD 格式的起止日期', 'error')
      return
    }
    if (backfillStart > backfillEnd) {
      toast('开始日期不能晚于结束日期', 'error')
      return
    }
    syncMutation.mutate({ mode: 'backfill', start: backfillStart, end: backfillEnd })
  }

  const backfillRunning = status?.backfill?.running ?? false
  const backfillProgress = status?.backfill?.total
    ? Math.round((status.backfill.done / status.backfill.total) * 100)
    : 0

  // 未配置引导态
  if (!configured) {
    return (
      <div className="rounded-card border border-warning/30 bg-warning/5 p-4">
        <div className="flex items-center gap-2 text-warning">
          <AlertTriangle className="h-4 w-4" />
          <span className="text-sm font-medium">未配置数据源</span>
        </div>
        <p className="mt-2 text-xs text-muted leading-relaxed">
          ETF 份额/净值数据需要配置自定义数据源（指向 amazingDataHttp 的 /etf/share 和 /etf/nav 端点）。
          请在下方选择一个已配置的数据源并保存。
        </p>
        <div className="mt-3 flex items-center gap-2">
          <select
            value={config?.data_source ?? ''}
            onChange={e => saveConfigMutation.mutate(e.target.value)}
            disabled={saveConfigMutation.isPending}
            className="flex-1 rounded border border-border bg-base px-2 py-1 text-xs text-secondary outline-none focus:border-accent"
          >
            <option value="">选择数据源…</option>
            {config?.sources.map(s => (
              <option key={s.name} value={s.name}>{s.display_name}</option>
            ))}
          </select>
        </div>
        {config?.sources.length === 0 && (
          <p className="mt-2 text-[10px] text-muted">
            尚无自定义数据源，请先在「设置 → 数据源」中添加指向 amazingDataHttp 的数据源。
          </p>
        )}
      </div>
    )
  }

  // 已配置 — 正常卡片
  return (
    <div className="rounded-card border border-border bg-surface p-4 space-y-3">
      {/* 数据源选择 */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted whitespace-nowrap">数据源:</span>
        <select
          value={config?.data_source ?? ''}
          onChange={e => saveConfigMutation.mutate(e.target.value)}
          disabled={saveConfigMutation.isPending}
          className="flex-1 rounded border border-border bg-base px-2 py-1 text-xs text-secondary outline-none focus:border-accent"
        >
          {config?.sources.map(s => (
            <option key={s.name} value={s.name}>{s.display_name}</option>
          ))}
        </select>
      </div>
      {config?.base_url && (
        <div className="text-[10px] text-muted/60">将调用 {config.base_url}/etf/share · /etf/nav</div>
      )}

      {config?.warning && (
        <div className="flex items-center gap-1.5 rounded bg-warning/10 px-2 py-1 text-[10px] text-warning">
          <AlertTriangle className="h-3 w-3 shrink-0" />
          <span>{config.warning}</span>
        </div>
      )}

      {/* 增量同步 */}
      <div className="flex items-center gap-2">
        <button
          onClick={handleIncremental}
          disabled={syncMutation.isPending || backfillRunning}
          className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50"
        >
          {syncMutation.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          同步增量
        </button>
        {status?.last_sync && (
          <span className="text-[10px] text-muted">最近同步: {status.last_sync}</span>
        )}
      </div>

      {/* 回填 */}
      <div className="border-t border-border/50 pt-2 space-y-2">
        <div className="text-xs text-secondary">历史回填</div>
        <div className="flex items-center gap-2">
          <DatePicker
            value={backfillStart}
            onChange={setBackfillStart}
            max={backfillEnd || undefined}
          />
          <span className="text-xs text-muted">至</span>
          <DatePicker
            value={backfillEnd}
            onChange={setBackfillEnd}
            min={backfillStart || undefined}
          />
          <button
            onClick={handleBackfill}
            disabled={syncMutation.isPending || backfillRunning}
            className="rounded-btn bg-elevated px-3 py-1 text-xs text-secondary hover:text-foreground disabled:opacity-50"
          >
            开始回填
          </button>
        </div>

        {/* 回填进度 */}
        {backfillRunning && (
          <div className="space-y-1">
            <div className="flex items-center justify-between text-[10px] text-muted">
              <span>回填中… {status?.backfill?.current ?? ''}</span>
              <span>{status?.backfill?.done ?? 0}/{status?.backfill?.total ?? 0}</span>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-elevated">
              <div className="h-full bg-accent transition-all" style={{ width: `${backfillProgress}%` }} />
            </div>
          </div>
        )}

        {status?.backfill?.error && (
          <div className="text-[10px] text-danger">{status.backfill.error}</div>
        )}

        {/* 数据范围 */}
        {status?.data_range?.min && (
          <div className="text-[10px] text-muted">
            数据范围: {status.data_range.min} ~ {status.data_range.max ?? '--'}
            {status.completed_months.length > 0 && (
              <span className="ml-2">已回填 {status.completed_months.length} 个月</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
