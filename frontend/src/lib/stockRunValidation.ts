import type { NeighborhoodRunRequest, StockRunRequest } from './types'

export interface StockRunDraft {
  mode: 'baseline' | 'scenario'
  scopeMode: 'boundary' | 'district' | 'building'
  district: string
  buildingRef: string
  name: string
  heatDelta: number
  coolDelta: number
  weatherDatasetId: string
  reason: string
  sourceType: StockRunRequest['source_type']
  sourceRef: string
}

export const initialStockRunDraft: StockRunDraft = {
  mode: 'baseline',
  scopeMode: 'boundary',
  district: '',
  buildingRef: '',
  name: '',
  heatDelta: 0,
  coolDelta: 0,
  weatherDatasetId: '',
  reason: '',
  sourceType: 'human_judgement',
  sourceRef: '',
}

export function stockRunIssues(draft: StockRunDraft, includeScope: boolean): string[] {
  const issues: string[] = []
  if (includeScope && draft.scopeMode === 'district' && !draft.district.trim()) issues.push('district_required')
  if (includeScope && draft.scopeMode === 'building' && draft.buildingRef.trim().length < 3) issues.push('building_required')
  if (![draft.heatDelta, draft.coolDelta].every(Number.isFinite)) issues.push('finite_offsets')
  if (draft.heatDelta < -3 || draft.heatDelta > 3 || draft.coolDelta < -3 || draft.coolDelta > 3) issues.push('offset_bounds')
  if (draft.mode === 'baseline') {
    if (draft.heatDelta !== 0 || draft.coolDelta !== 0 || draft.weatherDatasetId) issues.push('baseline_override')
    return issues
  }
  if (draft.name.trim().length < 2) issues.push('name_required')
  if (draft.reason.trim().length < 3) issues.push('reason_required')
  if (!draft.sourceType) issues.push('source_required')
  if (draft.heatDelta === 0 && draft.coolDelta === 0 && !draft.weatherDatasetId) issues.push('scenario_noop')
  return issues
}

export function stockRunPayload(draft: StockRunDraft, includeScope: false): StockRunRequest
export function stockRunPayload(draft: StockRunDraft, includeScope: true): NeighborhoodRunRequest
export function stockRunPayload(draft: StockRunDraft, includeScope: boolean): StockRunRequest | NeighborhoodRunRequest {
  const common: StockRunRequest = {
    mode: draft.mode,
    heat_delta_c: draft.mode === 'scenario' ? draft.heatDelta : 0,
    cool_delta_c: draft.mode === 'scenario' ? draft.coolDelta : 0,
    weather_dataset_id: draft.mode === 'scenario' ? draft.weatherDatasetId || null : null,
    name: draft.mode === 'scenario' ? draft.name.trim() : null,
    reason: draft.mode === 'scenario' ? draft.reason.trim() : null,
    source_type: draft.mode === 'scenario' ? draft.sourceType : null,
    source_ref: draft.mode === 'scenario' ? draft.sourceRef.trim() || null : null,
  }
  return includeScope ? {
    ...common,
    scope_mode: draft.scopeMode,
    district: draft.scopeMode === 'district' ? draft.district : null,
    building_ref: draft.scopeMode === 'building' ? draft.buildingRef.trim() : null,
  } : common
}
