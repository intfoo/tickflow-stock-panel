import { useSidebarState, type SidebarState } from '@/lib/useSidebarState'
import { SidebarContent } from './SidebarContent'
import { IconRailContent } from './IconRailContent'
import { PanelLeftClose, PanelLeft, ChevronsLeft } from 'lucide-react'

const WIDTH_CLASS: Record<SidebarState, string> = {
  expanded: 'w-56', // 14rem
  collapsed: 'w-14', // 3.5rem
  hidden: 'w-0',
}

const ICON: Record<SidebarState, typeof PanelLeft> = {
  expanded: PanelLeftClose, // 展开态显示"收起"图标
  collapsed: ChevronsLeft, // 图标条态显示"进一步隐藏"图标
  hidden: PanelLeft, // 隐藏态显示"展开"图标
}

const TITLE: Record<SidebarState, string> = {
  expanded: '折叠为图标条',
  collapsed: '完全隐藏',
  hidden: '展开侧边栏',
}

export function DesktopSidebar() {
  const [state, cycle] = useSidebarState()
  const ToggleIcon = ICON[state]

  // 切换按钮（必须在 return 之前声明，避免 TDZ）
  const toggleBtn = (
    <button
      onClick={cycle}
      className={
        state === 'hidden'
          ? 'fixed left-0 top-4 z-30 flex h-8 w-6 items-center justify-center rounded-r bg-surface border border-l-0 border-border text-foreground/80 hover:bg-elevated hover:text-foreground cursor-pointer'
          : 'flex h-8 w-8 items-center justify-center rounded-btn text-foreground/80 hover:bg-elevated hover:text-foreground cursor-pointer'
      }
      title={TITLE[state]}
    >
      <ToggleIcon className="h-4 w-4" />
    </button>
  )

  return (
    <aside
      className={`${WIDTH_CLASS[state]} shrink-0 ${
        state === 'hidden' ? 'border-r-0' : 'border-r border-border'
      } bg-surface flex flex-col h-full min-h-0 overflow-hidden transition-[width] duration-200 ease-smooth`}
    >
      {state === 'expanded' && <SidebarContent toggleButton={toggleBtn} />}
      {state === 'collapsed' && <IconRailContent toggleButton={toggleBtn} />}
      {state === 'hidden' && toggleBtn}
    </aside>
  )
}
