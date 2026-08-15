import { describe, expect, it } from 'vitest'
import { countDone, formatDuration, formatProductBytes, runProgress, safeRunName, shortHash } from './productStock'

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

describe('run progress denominator', () => {
  // The shape a live run actually has: `aggregate()` derives its scope count
  // from the rows written so far, so `buildings_in_scope` equals the finished
  // count exactly.  Reading progress off it reports a complete run.
  const live = {
    running: true,
    progress: { counts: { ok: 200, failed: 1, excluded: 162 } },
    summary: { coverage: { buildings_in_scope: 363 } },
  }

  it('uses the recorded scope, not the ledger-derived one, while running', () => {
    const progress = runProgress({ ...live, scope: { total: 1035 } })
    expect(progress.done).toBe(363)
    expect(progress.total).toBe(1035)
    expect(progress.pct).toBeCloseTo(35.07, 2)
  })

  it('refuses to report progress when no scope was ever recorded', () => {
    // The old code fell back to the completed count itself and printed 100%.
    const progress = runProgress(live)
    expect(progress.done).toBe(363)
    expect(progress.total).toBeNull()
    expect(progress.pct).toBeNull()
  })

  it('prefers a preflight scope when the run has not recorded one', () => {
    expect(runProgress(live, { buildings_in_scope: 1035 }).pct).toBeCloseTo(35.07, 2)
  })

  it('trusts the summary only once a written aggregate settles it', () => {
    const finished = runProgress({ ...live, running: false, summary_is_partial: false })
    expect(finished.total).toBe(363)
    expect(finished.pct).toBe(100)
  })

  it('does not treat a stopped run’s partial total as a scope', () => {
    // Stopped part-way: no longer running, but its recomputed coverage still
    // counts only the rows it reached, so it is not a denominator.
    const stopped = runProgress({ ...live, running: false, summary_is_partial: true })
    expect(stopped.total).toBeNull()
    expect(stopped.pct).toBeNull()
  })

  it('treats an empty or absent scope as unknown rather than dividing', () => {
    expect(runProgress({ ...live, scope: { total: 0 } }).pct).toBeNull()
    expect(runProgress(undefined).total).toBeNull()
    expect(runProgress(null).done).toBe(0)
  })

  it('never exceeds 100% when more rows land than the scope predicted', () => {
    const overrun = runProgress({ ...live, scope: { total: 100 } })
    expect(overrun.pct).toBe(100)
  })
})
