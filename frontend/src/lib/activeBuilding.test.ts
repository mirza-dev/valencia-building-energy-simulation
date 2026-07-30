import { describe, expect, it } from 'vitest'
import { DEFAULT_ACTIVE_BUILDING, normalizeActiveBuildingRef } from './activeBuilding'

describe('single-building context', () => {
  it('normalizes a selected parcel reference for the whole workflow', () => {
    expect(normalizeActiveBuildingRef(' 4252702yj2745a ')).toBe('4252702YJ2745A')
  })

  it('falls back to the documented pilot only when no selection exists', () => {
    expect(normalizeActiveBuildingRef('')).toBe(DEFAULT_ACTIVE_BUILDING)
    expect(normalizeActiveBuildingRef(null)).toBe(DEFAULT_ACTIVE_BUILDING)
  })
})
