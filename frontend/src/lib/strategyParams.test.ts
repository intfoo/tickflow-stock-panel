import { describe, expect, it } from 'vitest'
import type { StrategyParamDef } from '@/lib/api'
import { groupParams, isParamVisible, visibleParams } from '@/lib/strategyParams'

const p = (id: string, extra: Partial<StrategyParamDef> = {}): StrategyParamDef => ({
  id,
  label: id,
  type: 'float',
  default: 0,
  ...extra,
})

describe('isParamVisible', () => {
  it('无 visible_if 恒显示', () => {
    expect(isParamVisible(p('a'), {})).toBe(true)
  })

  it('单条件: values 命中/未命中/缺省回落 default', () => {
    const def = p('use_x', { visible_if: { param: 'pool', in: ['custom'] } })
    const pool = p('pool', { type: 'select', default: 'classic4' })
    expect(isParamVisible(def, { pool: 'custom' }, [pool, def])).toBe(true)
    expect(isParamVisible(def, { pool: 'classic4' }, [pool, def])).toBe(false)
    // values 缺省 → 按 pool 的 default('classic4') 判定 → 隐藏
    expect(isParamVisible(def, {}, [pool, def])).toBe(false)
  })

  it('递归: all / any 组合（标的池联动场景）', () => {
    // 显示条件 = pool ∈ presets OR (pool=custom AND use_gold)
    const def = p('tp_gold', {
      visible_if: {
        any: [
          { param: 'pool', in: ['classic4', 'no_hs300'] },
          { all: [{ param: 'pool', in: ['custom'] }, { param: 'use_gold', in: [true] }] },
        ],
      },
    })
    const useGold = p('use_gold', { type: 'bool', default: true })
    const all = [def, useGold]
    // 预设模式: 不看勾选状态
    expect(isParamVisible(def, { pool: 'classic4', use_gold: false }, all)).toBe(true)
    // custom 勾选 → 显示; 取消勾选 → 隐藏
    expect(isParamVisible(def, { pool: 'custom', use_gold: true }, all)).toBe(true)
    expect(isParamVisible(def, { pool: 'custom', use_gold: false }, all)).toBe(false)
  })
})

describe('visibleParams / groupParams', () => {
  it('过滤隐藏参数并按组归并（保序）', () => {
    const params = [
      p('pool', { type: 'select', default: 'classic4', group: '标的池' }),
      p('use_x', { type: 'bool', default: true, group: '标的池', visible_if: { param: 'pool', in: ['custom'] } }),
      p('m_days', { group: '动量' }),
      p('tp_x', { group: '止盈' }),
      p('lock_x', { group: '止盈' }),
    ]
    const visible = visibleParams(params, { pool: 'classic4' })
    expect(visible.map(x => x.id)).toEqual(['pool', 'm_days', 'tp_x', 'lock_x'])
    const groups = groupParams(visible)
    expect(groups.map(g => [g.name, g.items.length])).toEqual([
      ['标的池', 1],
      ['动量', 1],
      ['止盈', 2],
    ])
  })
})
