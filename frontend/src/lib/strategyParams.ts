import type { ParamVisibleIf, StrategyParamDef } from '@/lib/api'

/**
 * 策略参数级联显隐与分组（META 里的 visible_if/group 为 UI 元数据，后端原样透传）。
 *
 * - visible_if 支持递归条件: { param, in } 单条件 / { all: [...] } 与 / { any: [...] } 或；
 *   values 缺省时回落该参数的 default（未触碰过的控件按默认值判定）。
 * - group: 相同组名的相邻声明归并为一节；无 group 的参数归入无名节（不显示节标题）。
 */
function evalVisibleIf(
  cond: ParamVisibleIf,
  values: Record<string, any>,
  allParams?: StrategyParamDef[],
): boolean {
  if (cond.all?.length) return cond.all.every(c => evalVisibleIf(c, values, allParams))
  if (cond.any?.length) return cond.any.some(c => evalVisibleIf(c, values, allParams))
  if (cond.param) {
    const actual = values[cond.param] ?? allParams?.find(x => x.id === cond.param)?.default
    return (cond.in ?? []).includes(actual)
  }
  return true
}

export function isParamVisible(
  p: StrategyParamDef,
  values: Record<string, any>,
  allParams?: StrategyParamDef[],
): boolean {
  if (!p.visible_if) return true
  return evalVisibleIf(p.visible_if, values, allParams)
}

export function visibleParams(
  params: StrategyParamDef[],
  values: Record<string, any>,
): StrategyParamDef[] {
  return params.filter(p => isParamVisible(p, values, params))
}

export interface ParamGroup {
  name: string | null
  items: StrategyParamDef[]
}

export function groupParams(params: StrategyParamDef[]): ParamGroup[] {
  const groups: ParamGroup[] = []
  for (const p of params) {
    const name = p.group ?? null
    const last = groups[groups.length - 1]
    if (last && last.name === name) last.items.push(p)
    else groups.push({ name, items: [p] })
  }
  return groups
}
