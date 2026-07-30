import { describe, expect, it } from 'vitest'
import { initialStockRunDraft, stockRunIssues, stockRunPayload } from './stockRunValidation'

describe('stock run validation', () => {
  it('keeps a baseline free of scientific overrides', () => {
    expect(stockRunIssues(initialStockRunDraft, true)).toEqual([])
    expect(stockRunPayload(initialStockRunDraft, true)).toMatchObject({
      mode: 'baseline', scope_mode: 'boundary', heat_delta_c: 0, cool_delta_c: 0,
    })
  })

  it('requires a real scenario change and provenance', () => {
    const issues = stockRunIssues({ ...initialStockRunDraft, mode: 'scenario' }, false)
    expect(issues).toEqual(expect.arrayContaining(['name_required', 'reason_required', 'scenario_noop']))
  })

  it('serializes a district climate and comfort scenario', () => {
    const draft = {
      ...initialStockRunDraft,
      mode: 'scenario' as const,
      scopeMode: 'district' as const,
      district: 'CAMPANAR',
      name: 'Comfort study',
      heatDelta: 1,
      coolDelta: -1,
      weatherDatasetId: 'future-epw',
      reason: 'Supervisor scenario',
      sourceType: 'supervisor' as const,
    }
    expect(stockRunIssues(draft, true)).toEqual([])
    expect(stockRunPayload(draft, true)).toMatchObject({
      scope_mode: 'district', district: 'CAMPANAR', heat_delta_c: 1,
      cool_delta_c: -1, weather_dataset_id: 'future-epw', source_type: 'supervisor',
    })
  })

  it('requires and serializes a cadastral reference for single-building runs', () => {
    const missing = { ...initialStockRunDraft, scopeMode: 'building' as const }
    expect(stockRunIssues(missing, true)).toContain('building_required')
    const draft = { ...missing, buildingRef: '4252702YJ2745A' }
    expect(stockRunIssues(draft, true)).toEqual([])
    expect(stockRunPayload(draft, true)).toMatchObject({
      scope_mode: 'building', building_ref: '4252702YJ2745A', district: null,
    })
  })
})
