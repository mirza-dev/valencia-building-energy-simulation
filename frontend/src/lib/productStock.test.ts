import { describe, expect, it } from 'vitest'
import { countDone, formatDuration, formatProductBytes, inputReadiness, lhsBelongsToRun, pickUncertaintyStudy, runProgress, safeRunName, shortHash } from './productStock'

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

describe('input readiness', () => {
  const entrypoints = { run_stock: true, aggregate: true, screen_geometry: true }

  it('reports checking, not blocked, before the profile arrives', () => {
    // The old boolean read an unanswered query as a failed one and printed
    // CHECK INPUTS / INPUTS REQUIRED on every cold load.
    expect(inputReadiness(undefined)).toEqual({ state: 'checking', reason: '' })
  })

  it('blocks when the profile could not be read at all', () => {
    // Unknown closes the gate here; it never assumes the good case.
    const readiness = inputReadiness(undefined, true)
    expect(readiness.state).toBe('blocked')
    expect(readiness.reason).toMatch(/could not read/i)
  })

  it('is ready when nothing is missing and every entry point resolves', () => {
    expect(inputReadiness({ missing_inputs: [], entrypoints })).toEqual({ state: 'ready', reason: '' })
  })

  it('names the failing entry point when no input is missing', () => {
    // This is the case that used to print the all-clear sentence under a red
    // heading: `missing_inputs` is empty, so joining it produced ''.
    const readiness = inputReadiness({ missing_inputs: [], entrypoints: { ...entrypoints, aggregate: false } })
    expect(readiness.state).toBe('blocked')
    expect(readiness.reason).toContain('aggregate')
    expect(readiness.reason).not.toBe('')
  })

  it('lists missing inputs and broken entry points together', () => {
    const readiness = inputReadiness({ missing_inputs: ['stock', 'climate'], entrypoints: { ...entrypoints, run_stock: false } })
    expect(readiness.reason).toContain('stock')
    expect(readiness.reason).toContain('climate')
    expect(readiness.reason).toContain('run_stock')
  })

  it('treats an empty profile as ready rather than crashing on absent fields', () => {
    expect(inputReadiness({}).state).toBe('ready')
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

describe('lhsBelongsToRun', () => {
  const pilot = '4252702YJ2745A'

  it('accepts a study whose building the run actually simulated', () => {
    expect(lhsBelongsToRun([{ refparcela: pilot }], pilot)).toBe(true)
  })

  it('rejects a study from another city', () => {
    // The real case: the only two LHS runs in the system are on a Valencia
    // pilot, and they were rendering under Lecco's heading.
    expect(lhsBelongsToRun([{ refparcela: '1274367890' }], pilot)).toBe(false)
  })

  it('is not fooled by the ledger search being a substring match', () => {
    // `q=<ref>` matches refparcela, cluster and error text, so a non-empty
    // page is not evidence.  Only an exact reference counts.
    expect(lhsBelongsToRun([{ refparcela: `${pilot}B` }], pilot)).toBe(false)
    expect(lhsBelongsToRun([{ refparcela: 'X' }], pilot)).toBe(false)
  })

  it('treats an unknown building or an unloaded page as not belonging', () => {
    expect(lhsBelongsToRun(undefined, pilot)).toBe(false)
    expect(lhsBelongsToRun([], pilot)).toBe(false)
    expect(lhsBelongsToRun([{ refparcela: pilot }], undefined)).toBe(false)
    expect(lhsBelongsToRun([{ refparcela: pilot }], '')).toBe(false)
  })
})

describe('runProgress on a resumed run', () => {
  // The real ALL-VALENC-A shape: a resume rewrote run_config.json with the
  // work it had left, so the recorded scope is smaller than the ledger.
  const resumed = {
    running: false,
    summary_is_partial: false,
    scope: { total: 7602 },
    summary: { coverage: { buildings_in_scope: 26445 } },
    progress: { counts: { ok: 24983, failed: 111, excluded: 1351 } },
  }

  it('uses the finished aggregate rather than the narrowed scope', () => {
    const progress = runProgress(resumed)
    expect(progress.done).toBe(26445)
    expect(progress.total).toBe(26445)
    expect(progress.pct).toBe(100)
  })

  it('would otherwise have reported a clamped 268%', () => {
    // What the old ordering produced: honest arithmetic on a dishonest
    // denominator, hidden by the clamp as a permanent full bar.
    expect((26445 / 7602) * 100).toBeGreaterThan(268)
  })

  it('still prefers the recorded scope while the run is live', () => {
    const live = { ...resumed, running: true, summary_is_partial: true }
    expect(runProgress(live).total).toBe(7602)
  })
})

describe('pickUncertaintyStudy', () => {
  const base = {
    id: 'a', verification: { ok: true }, verification_status: 'VERIFIED',
    current_compatibility: { current: true }, config: { stock_run: 'LECCO_REAL' },
  }

  it('falls back to the annual study when no event study belongs to this run', () => {
    expect(pickUncertaintyStudy([], 'LECCO_REAL')).toEqual({ kind: 'annual' })
    expect(pickUncertaintyStudy(undefined, 'LECCO_REAL')).toEqual({ kind: 'annual' })
  })

  it('never shows a study committed against a different stock run', () => {
    // The failure this replaces: the newest study in the system rendered on
    // every Outputs page, so one run's band appeared under another's heading.
    expect(pickUncertaintyStudy([{ ...base, config: { stock_run: 'LECCO_1' } }], 'LECCO_REAL'))
      .toEqual({ kind: 'annual' })
  })

  it('prefers a verified, current study over an outdated one', () => {
    const stale = { ...base, id: 'stale', current_compatibility: { current: false } }
    const good = { ...base, id: 'good' }
    expect(pickUncertaintyStudy([stale, good], 'LECCO_REAL')).toEqual({ kind: 'event', run: good })
  })

  it('still surfaces an unusable study rather than hiding it', () => {
    // An outdated or unverified study says something true about provenance; the
    // section withholds its numbers instead of pretending it is not there.
    const stale = { ...base, id: 'stale', current_compatibility: { current: false } }
    expect(pickUncertaintyStudy([stale], 'LECCO_REAL')).toEqual({ kind: 'event', run: stale })
  })

  it('does not accept a study whose artifacts failed to verify as the current one', () => {
    const tampered = { ...base, id: 'bad', verification: { ok: false }, verification_status: 'TAMPERED' }
    const good = { ...base, id: 'good' }
    expect(pickUncertaintyStudy([tampered, good], 'LECCO_REAL')).toEqual({ kind: 'event', run: good })
  })
})
