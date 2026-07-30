import { describe, expect, it } from 'vitest'
import { changedFields, deepClone, withOverrideRecords } from './config'
import { validateBuildConfig } from './buildConfigValidation'
import type { BuildConfig } from './types'

function fixture(): BuildConfig {
  return {
    data: { building_path: 'a', neighbor_path: 'b', template_path: 'c', epw_path: 'd', output_root: 'e' },
    geometry: {
      floor_height_m: 3, ground_unconditioned: true, neighbor_assume_ground: true,
      simplify_tolerance_m: 0.3, max_area_delta_fraction: 0.01,
      footprint_min_m2: 50, footprint_max_m2: 5000, party_wall_tolerance_m: 0.3,
      min_shared_edge_m: 1, party_overlap_ratio: 0.5, context_radius_m: 50,
    },
    envelope: { wall_u: null, roof_u: null, window_u: 5.7, window_g: 0.82, thermal_bridge_du: 0.1, massless: false },
    openings: {
      wwr_north: 0.12, wwr_east: 0.18, wwr_south: 0.25, wwr_west: 0.18,
      window_width_m: 1.2, window_height_m: 1.2, window_sill_m: 0.9,
      door_width_m: 1.2, door_height_m: 2.1, door_sill_m: 0.01,
      balcony_doors_per_facade_floor: 2, balcony_depth_m: 1,
    },
    shading: { context_enabled: true, blind_name: 'blind', setpoint_w_m2: 250, summer_start_month: 6, summer_end_month: 9, max_shadow_figures: 200000 },
    operation: { infiltration_ach: null, output_variables: { heating: 'h', cooling: 'c' } },
    qa: { facade_wwr_warning_pct: 15 },
    provenance: { baseline_profile: 'pilot', scenario_name: 'Baseline', locale: 'tr', overrides: [] },
  }
}

describe('configuration provenance', () => {
  it('does not mutate the baseline and ignores display-only provenance fields', () => {
    const baseline = fixture()
    const scenario = deepClone(baseline)
    scenario.provenance.scenario_name = 'Variant'
    scenario.provenance.locale = 'en'
    scenario.envelope.window_g = 0.65
    expect(changedFields(scenario, baseline)).toEqual(['envelope.window_g'])
    expect(baseline.envelope.window_g).toBe(0.82)
  })

  it('creates one source-backed record per scientific override', () => {
    const baseline = fixture()
    const scenario = deepClone(baseline)
    scenario.geometry.context_radius_m = 65
    scenario.openings.wwr_south = 0.3
    const result = withOverrideRecords(scenario, baseline, 'Measured scenario', 'dataset', 'survey.gpkg')
    expect(result.provenance.overrides.map((item) => item.field)).toEqual([
      'geometry.context_radius_m', 'openings.wwr_south',
    ])
    expect(result.provenance.overrides.every((item) => item.source_ref === 'survey.gpkg')).toBe(true)
  })

  it('rejects unsafe scientific values before a preview can be queued', () => {
    const config = fixture()
    config.openings.wwr_south = 1.2
    config.geometry.floor_height_m = 0
    config.geometry.footprint_max_m2 = 20
    const errors = validateBuildConfig(config)
    expect(errors['openings.wwr_south']).toBe('validation.wwr')
    expect(errors['geometry.floor_height_m']).toBe('validation.positive')
    expect(errors['geometry.footprint_max_m2']).toBe('validation.footprintRange')
  })

  it('accepts the complete pilot baseline contract', () => {
    expect(validateBuildConfig(fixture())).toEqual({})
  })
})
