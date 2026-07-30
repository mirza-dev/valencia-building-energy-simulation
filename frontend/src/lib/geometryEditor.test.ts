import { describe, expect, it } from 'vitest'
import { suggestOpeningRect } from './geometryEditor'
import type { SceneItem } from './types'

const wall: SceneItem = {
  id: 'wall',
  name: 'Residential wall',
  category: 'surface',
  surface_type: 'Wall',
  boundary_condition: 'Outdoors',
  area_m2: 30,
  vertices: [[0, 0, 0], [10, 0, 0], [10, 0, 3], [0, 0, 3]],
}

describe('suggestOpeningRect', () => {
  it('uses the conventional centered rectangle on an empty wall', () => {
    expect(suggestOpeningRect(wall, [])).toEqual({ u_min: 0.3, u_max: 0.7, v_min: 0.25, v_max: 0.75 })
  })

  it('finds a clear top slot without replacing an automatic opening', () => {
    const opening: SceneItem = {
      id: 'window',
      parent_id: wall.id,
      name: 'Automatic window',
      category: 'window',
      subsurface_type: 'FixedWindow',
      area_m2: 9,
      vertices: [[2, 0, 0.6], [8, 0, 0.6], [8, 0, 2.1], [2, 0, 2.1]],
    }
    const suggestion = suggestOpeningRect(wall, [opening])
    expect(suggestion.v_min).toBeGreaterThan(0.7)
    expect(suggestion.u_min).toBeGreaterThanOrEqual(0)
    expect(suggestion.u_max).toBeLessThanOrEqual(1)
  })
})
