import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Settings2, Database } from 'lucide-react'
import { etfFundApi } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { FundFlowChart } from '@/components/etf/FundFlowChart'
import { ZoneChart } from '@/components/etf/ZoneChart'
import { EtfLeaderboard } from '@/components/etf/EtfLeaderboard'
import { BroadEtfDialog } from '@/components/etf/BroadEtfDialog'
import { EtfSyncCard } from '@/components/etf/EtfSyncCard'

export function Etf() {
  const qc = useQueryClient()
  const [broadDialogOpen, setBroadDialogOpen] = useState(false)
  const [syncCardOpen, setSyncCardOpen] = useState(false)
  const [overlayIndex, setOverlayIndex] = useState('000001.SH')
  const [statDays, setStatDays] = useState<5 | 20 | 60>(5)

  // 配置查询 — 初始化 overlayIndex
  const configQuery = useQuery({
    queryKey: QK.etfFundConfig,
    queryFn: etfFundApi.getConfig,
    staleTime: 60_000,
  })

  const configured = !!configQuery.data?.data_source

  // 当配置返回时初始化 overlayIndex
  const configOverlay = configQuery.data?.overlay_index
  useEffect(() => {
    if (configOverlay && configOverlay !== overlayIndex) {
      setOverlayIndex(configOverlay)
    }
  }, [configOverlay]) // eslint-disable-line react-hooks/exhaustive-deps

  // 同步状态监听 — 手动同步 (增量/回填) 结束后自动刷新页面数据。
  // 放页面级而非 EtfSyncCard: 卡片折叠即卸载, 轮询与回调都会中断。
  const statusQuery = useQuery({
    queryKey: QK.etfFundStatus,
    queryFn: etfFundApi.getStatus,
    refetchInterval: (query) => query.state.data?.running ? 2000 : false,
  })
  const syncRunning = statusQuery.data?.running ?? false
  const prevRunningRef = useRef<boolean | null>(null)
  useEffect(() => {
    const prev = prevRunningRef.current
    prevRunningRef.current = syncRunning
    if (prev === true && !syncRunning) {
      // 前缀失效: 排行榜/资金流/风险区/状态等全部 etf-fund 查询一起刷新
      qc.invalidateQueries({ queryKey: ['etf-fund'] })
      const err = statusQuery.data?.incremental?.error ?? statusQuery.data?.backfill?.error
      if (err) toast(`同步失败: ${err}`, 'error')
      else toast('同步完成，数据已刷新', 'success')
    }
  }, [syncRunning]) // eslint-disable-line react-hooks/exhaustive-deps

  // 资金流数据 — 全量加载 (API 上限 750 交易日 ≈ 3 年), dataZoom 默认聚焦最近 120 天
  const flowQuery = useQuery({
    queryKey: QK.etfFundFlow(750),
    queryFn: () => etfFundApi.getFlow(750),
    enabled: configured,
  })

  // 持久化 overlayIndex 到后端
  const handleOverlayChange = (symbol: string) => {
    setOverlayIndex(symbol)
    etfFundApi.putConfig({ data_source: configQuery.data?.data_source ?? '', overlay_index: symbol }).then(() => {
      qc.invalidateQueries({ queryKey: QK.etfFundConfig })
    }).catch(() => {})
  }

  return (
    <div className="h-full overflow-auto bg-base p-4 space-y-4">
      {/* 标题行 */}
      <div className="flex items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">ETF行情</h1>
          <p className="mt-1 text-xs text-muted">
            宽基ETF资金净流入与排行榜
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setBroadDialogOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-btn bg-elevated px-3 py-1.5 text-xs text-secondary hover:text-foreground"
          >
            <Settings2 className="h-3.5 w-3.5" />
            宽基配置
          </button>
          <button
            onClick={() => setSyncCardOpen(o => !o)}
            className={`inline-flex items-center gap-1.5 rounded-btn px-3 py-1.5 text-xs ${syncCardOpen ? 'bg-accent text-white' : 'bg-elevated text-secondary hover:text-foreground'}`}
          >
            <Database className="h-3.5 w-3.5" />
            数据同步
          </button>
        </div>
      </div>

      {/* 同步卡片（可折叠） */}
      {syncCardOpen && (
        <EtfSyncCard />
      )}

      {/* 未配置提示 */}
      {!configured && !syncCardOpen && (
        <div className="rounded-card border border-warning/30 bg-warning/5 p-4 text-center">
          <p className="text-sm text-warning">尚未配置数据源</p>
          <p className="mt-1 text-xs text-muted">
            点击右上角「数据同步」按钮配置数据源并同步数据。
          </p>
        </div>
      )}

      {/* 资金流图卡片 */}
      <div className="rounded-card border border-border bg-surface p-3">
        {configured ? (
          flowQuery.isLoading ? (
            <div className="py-10 text-center text-sm text-muted">资金流加载中…</div>
          ) : flowQuery.isError ? (
            <div className="py-4 text-center text-sm text-danger">资金流加载失败</div>
          ) : flowQuery.data ? (
            <FundFlowChart
              flow={flowQuery.data}
              overlayIndex={overlayIndex}
              onOverlayChange={handleOverlayChange}
              statDays={statDays}
              onStatDaysChange={setStatDays}
            />
          ) : null
        ) : (
          <div className="py-10 text-center text-sm text-muted">配置数据源后展示资金流图</div>
        )}
      </div>

      {/* 指数风险/机会区卡片 */}
      {configured && (
        <div className="rounded-card border border-border bg-surface p-3">
          <ZoneChart overlayIndex={overlayIndex} />
        </div>
      )}

      {/* 排行榜卡片 */}
      <div className="rounded-card border border-border bg-surface p-3">
        <EtfLeaderboard />
      </div>

      {/* 宽基配置弹窗 */}
      <BroadEtfDialog open={broadDialogOpen} onClose={() => setBroadDialogOpen(false)} />
    </div>
  )
}
