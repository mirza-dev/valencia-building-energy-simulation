import { describe, expect, it } from 'vitest'
import { deriveContextRoofs } from './sceneMassing'
import type { SceneItem } from './types'

function wall(id: string, a: [number, number], b: [number, number], height = 12): SceneItem {
  return {
    id,
    name: id,
    category: 'context',
    vertices: [[a[0], a[1], height], [a[0], a[1], 0], [b[0], b[1], 0], [b[0], b[1], height]],
    area_m2: Math.hypot(b[0] - a[0], b[1] - a[1]) * height,
  }
}

describe('deriveContextRoofs', () => {
  it('closes unordered wall edges into an exact visual roof cap', () => {
    const items = [
      wall('south', [10, 0], [0, 0]),
      wall('east', [10, 6], [10, 0]),
      wall('north', [0, 6], [10, 6]),
      wall('west', [0, 0], [0, 6]),
    ]
    const roofs = deriveContextRoofs(items)
    expect(roofs).toHaveLength(1)
    expect(roofs[0].context_role).toBe('roof')
    expect(roofs[0].vertices).toHaveLength(4)
    expect(roofs[0].area_m2).toBe(60)
  })

  it('ignores incomplete context contours and non-context shading', () => {
    const overhang = { ...wall('overhang', [0, 0], [4, 0]), category: 'overhang' as const }
    expect(deriveContextRoofs([wall('open', [0, 0], [4, 0]), overhang])).toEqual([])
  })
})
