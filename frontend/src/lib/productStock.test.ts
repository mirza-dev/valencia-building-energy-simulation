import { describe, expect, it } from 'vitest'
import { countDone, formatDuration, formatProductBytes, safeRunName, shortHash } from './productStock'

describe('product stock presentation helpers', () => {
  it('formats evidence without changing values', () => {
    expect(shortHash('0123456789abcdef')).toBe('0123456789…')
    expect(formatProductBytes(5.8 * 1024 ** 3)).toBe('5.8 GB')
    expect(formatDuration(90)).toBe('1.5 h')
  })

  it('makes a path-safe run label and totals terminal ledger states', () => {
    expect(safeRunName(' Valencia / full run ')).toBe('Valencia-full-run')
    expect(countDone({ ok: 10, failed: 2, excluded: 3, queued: 8 })).toBe(15)
  })
})
