import { describe, expect, it } from 'vitest'
import { viewableSimulationArtifacts } from './runArtifacts'
import type { RunRecord } from './types'

const run = (overrides: Partial<RunRecord> = {}) => ({
  run_type: 'simulation',
  verification_status: 'VERIFIED',
  artifacts: [
    { name: 'eplusout.err', sha256: 'b'.repeat(64), size_bytes: 40 },
    { name: 'not-allowlisted.sql', sha256: 'c'.repeat(64), size_bytes: 80 },
    { name: 'eplustbl.htm', sha256: 'a'.repeat(64), size_bytes: 1200 },
  ],
  ...overrides,
}) as RunRecord

describe('viewable simulation artifacts', () => {
  it('keeps allowlist order and excludes unlisted manifest entries', () => {
    expect(viewableSimulationArtifacts(run()).map((item) => item.name)).toEqual([
      'eplustbl.htm',
      'eplusout.err',
    ])
  })

  it('returns no links for non-simulation or non-verified runs', () => {
    expect(viewableSimulationArtifacts(run({ verification_status: 'TAMPERED' }))).toEqual([])
    expect(viewableSimulationArtifacts(run({ run_type: 'model' }))).toEqual([])
  })
})
