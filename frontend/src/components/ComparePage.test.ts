import { describe, expect, it } from 'vitest'
import { summarizeDiffValue } from './ComparePage'

describe('summarizeDiffValue', () => {
  it('turns an authored patch journal into an operation summary', () => {
    expect(summarizeDiffValue('editor.patch_journal', [
      { patch: { op: 'material.update', payload: { conductivity: 0.3 } } },
      { patch: { op: 'material.update', payload: { density: 900 } } },
      { op: 'thermostat.set_setpoints', payload: { heating: 21 } },
    ])).toBe('3 authored edits · material.update × 2 · thermostat.set_setpoints × 1')
  })

  it('does not dump arbitrary structured objects into the comparison table', () => {
    expect(summarizeDiffValue('editor.provenance', {
      source: 'expert', rationale: 'measured', revision: 3,
    })).toBe('3 fields · source · rationale · revision')
  })
})
