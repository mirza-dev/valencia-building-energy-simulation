import { describe, expect, it } from 'vitest'
import { cleanupCategoryKeys, formatBytes, readinessClass } from './storage'
import type { StorageCleanupPlan } from './types'

describe('storage presentation helpers', () => {
  it('formats capacity without hiding byte scale', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(3 * 1024 ** 3)).toBe('3.0 GB')
    expect(formatBytes(1536 * 1024 ** 2, 2)).toBe('1.50 GB')
  })

  it('keeps warning distinct from ready and blocked', () => {
    expect(readinessClass('READY')).toBe('is-ok')
    expect(readinessClass('WARNING')).toBe('is-warn')
    expect(readinessClass('BLOCKED')).toBe('is-bad')
  })

  it('selects only cleanup groups that actually contain candidates', () => {
    const plan = {
      categories: {
        export_cache: { count: 2, size_bytes: 10 },
        map_cache: { count: 0, size_bytes: 0 },
        abandoned_scratch: { count: 1, size_bytes: 2 },
      },
    } as StorageCleanupPlan
    expect(cleanupCategoryKeys(plan)).toEqual(['export_cache', 'abandoned_scratch'])
  })
})
