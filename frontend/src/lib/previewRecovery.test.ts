import { describe, expect, it } from 'vitest'
import { compactRecoveryItems, restorePreviewDraft, isAttachedPreviewTerminal } from './previewRecovery'
import type { BuildConfig, PreviewDetail, PreviewRecoveryItem, Profile } from './types'

const config: BuildConfig = {
  data: { building_path: 'a', neighbor_path: 'b', template_path: 'c', epw_path: 'd', output_root: 'e' },
  geometry: { floor_height_m: 3, ground_unconditioned: true, neighbor_assume_ground: true, simplify_tolerance_m: 0.3, max_area_delta_fraction: 0.01, footprint_min_m2: 50, footprint_max_m2: 5000, party_wall_tolerance_m: 0.3, min_shared_edge_m: 1, party_overlap_ratio: 0.5, context_radius_m: 50 },
  envelope: { wall_u: null, roof_u: null, window_u: 5.7, window_g: 0.82, thermal_bridge_du: 0.1, massless: false },
  openings: { wwr_north: 0.12, wwr_east: 0.18, wwr_south: 0.25, wwr_west: 0.18, window_width_m: 1.2, window_height_m: 1.2, window_sill_m: 0.9, door_width_m: 1.2, door_height_m: 2.1, door_sill_m: 0.01, balcony_doors_per_facade_floor: 2, balcony_depth_m: 1 },
  shading: { context_enabled: true, blind_name: 'blind', setpoint_w_m2: 250, summer_start_month: 6, summer_end_month: 9, max_shadow_figures: 200000 },
  operation: { infiltration_ach: null, output_variables: { heating: 'h', cooling: 'c' } },
  qa: { facade_wwr_warning_pct: 15 },
  provenance: { baseline_profile: 'pilot', scenario_name: 'Recovered variant', locale: 'tr', overrides: [{ field: 'openings.wwr_south', reason: 'Survey', source_type: 'dataset', source_ref: 'survey.gpkg' }] },
}

const profile: Profile = { id: 'pilot', label: 'Pilot', source: 'fixture', config: { ...config, openings: { ...config.openings, wwr_south: 0.2 }, provenance: { ...config.provenance, overrides: [] } } }
const preview = {
  job: { id: 'preview-a', kind: 'preview', status: 'ready', stage: 'Preview ready', refparcela: '4252702YJ2745A', attempt_count: 1, max_attempts: 2, cancel_requested: 0, timeout_seconds: 180, created_at: '2026-07-15T00:00:00Z', updated_at: '2026-07-15T00:01:00Z' },
  config, geometry_actions: [{ action: 'simplify', approved: true }], scene: null, stats: null, qa: null, renderer: null,
  request_fingerprint: 'a'.repeat(64), artifact_state: { status: 'AVAILABLE', recoverable: true, issues: [] },
  queue_context: { position: null, queued_total: 0, worker_busy: false, active_job: null },
} satisfies PreviewDetail

describe('builder preview recovery', () => {
  it('restores the exact request while retaining its locked baseline and provenance', () => {
    const restored = restorePreviewDraft(preview, [profile])
    expect(restored.selectedRef).toBe('4252702YJ2745A')
    expect(restored.config.openings.wwr_south).toBe(0.25)
    expect(restored.baseline.openings.wwr_south).toBe(0.2)
    expect(restored.geometryActions[0]).toMatchObject({ action: 'simplify', approved: true })
    expect(restored).toMatchObject({ rationale: 'Survey', sourceType: 'dataset', sourceRef: 'survey.gpkg' })
  })

  it('treats ready previews as stream-terminal but still commit-recoverable', () => {
    expect(isAttachedPreviewTerminal('running')).toBe(false)
    expect(isAttachedPreviewTerminal('ready')).toBe(true)
    expect(isAttachedPreviewTerminal('failed')).toBe(true)
  })

  it('keeps the newest semantic preview plus active and explicitly attached records', () => {
    const item = (id: string, status: PreviewRecoveryItem['status'], scenario = 'Pilot') => ({
      id, status, stage: status, refparcela: '4252702YJ2745A', scenario_name: scenario,
      baseline_profile: 'pilot', created_at: '2026-08-10T00:00:00Z', updated_at: '2026-08-10T00:00:00Z',
      artifact_state: { status: 'AVAILABLE', recoverable: true, issues: [] },
    }) satisfies PreviewRecoveryItem
    const items = [item('newest', 'ready'), item('older', 'ready'), item('active', 'running'), item('variant', 'ready', 'Variant')]
    expect(compactRecoveryItems(items, null).map((entry) => entry.id)).toEqual(['newest', 'active', 'variant'])
    expect(compactRecoveryItems(items, 'older').map((entry) => entry.id)).toEqual(['newest', 'older', 'active', 'variant'])
  })
})
