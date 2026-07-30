import { describe, expect, it } from 'vitest'
import {
  limitPrimaryMetrics, recentRunItems, resolveFocusedStatus, resolveInitialPanel,
} from './focusedWorkspace'

describe('focused workspace state', () => {
  it('prioritizes blockers and warnings over quiet success', () => {
    expect(resolveFocusedStatus({ blocked: true, warning: true, verified: true })).toBe('blocked')
    expect(resolveFocusedStatus({ warning: true, verified: true })).toBe('warning')
    expect(resolveFocusedStatus({ verified: true })).toBe('verified')
    expect(resolveFocusedStatus({})).toBe('pending')
  })

  it('opens setup without a result and evidence for a blocker', () => {
    expect(resolveInitialPanel({ hasResult: false })).toBe('setup')
    expect(resolveInitialPanel({ hasResult: true })).toBeNull()
    expect(resolveInitialPanel({ hasResult: true, blocked: true })).toBe('evidence')
  })

  it('limits the primary summary to three metrics', () => {
    expect(limitPrimaryMetrics([1, 2, 3, 4])).toEqual([1, 2, 3])
  })

  it('keeps a selected older run inside the five-item picker', () => {
    const runs = Array.from({ length: 8 }, (_, index) => ({ id: `run-${index}` }))
    expect(recentRunItems(runs, 'run-7').map((item) => item.id)).toEqual([
      'run-7', 'run-0', 'run-1', 'run-2', 'run-3',
    ])
  })
})
