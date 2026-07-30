import { describe, expect, it } from 'vitest'
import { availableRowMetrics, maximumAbsoluteDelta, stockDeltaExpression } from './stockCompare'
import type { StockCompareRow } from './types'

const rows: StockCompareRow[] = [
  { key: 'A', label: 'A', left_present: true, right_present: true, left_buildings: 2, right_buildings: 2, metrics: { heating: { left: 10, right: 12, delta: 2, percent: 20 } } },
  { key: 'B', label: 'B', left_present: true, right_present: true, left_buildings: 3, right_buildings: 3, metrics: { heating: { left: 20, right: 15, delta: -5, percent: -25 }, cooling: { left: 4, right: 4, delta: 0, percent: 0 } } },
]

describe('stock comparison map helpers', () => {
  it('uses a symmetric delta domain and deterministic match expression', () => {
    expect(maximumAbsoluteDelta(rows, 'heating')).toBe(5)
    const expression = stockDeltaExpression(rows, 'heating', 'cluster')
    expect(expression).toContain('#27658f')
    expect(expression).toContain('#b8423a')
    expect(JSON.stringify(expression)).toContain('cluster')
  })

  it('collects every metric exposed by the ledger', () => {
    expect(availableRowMetrics(rows)).toEqual(['heating', 'cooling'])
    expect(maximumAbsoluteDelta([], 'heating')).toBe(0.1)
  })
})
