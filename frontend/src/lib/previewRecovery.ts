import { deepClone } from './config'
import type { PreviewDetail, PreviewRecoveryItem, Profile, SourceType } from './types'

const SOURCE_TYPES = new Set<SourceType>([
  'human_judgement', 'dataset', 'publication', 'supervisor', 'other',
])

export function restorePreviewDraft(preview: PreviewDetail, profiles: Profile[]) {
  const config = deepClone(preview.config)
  const profile = profiles.find((item) => item.id === config.provenance.baseline_profile)
  const baseline = deepClone(profile?.config ?? config)
  const firstOverride = config.provenance.overrides[0]
  const sourceType = firstOverride && SOURCE_TYPES.has(firstOverride.source_type)
    ? firstOverride.source_type
    : 'human_judgement'
  return {
    selectedRef: preview.job.refparcela,
    config,
    baseline,
    geometryActions: deepClone(preview.geometry_actions ?? []),
    rationale: firstOverride?.reason ?? '',
    sourceType,
    sourceRef: firstOverride?.source_ref ?? '',
  }
}

export function isAttachedPreviewTerminal(status: string | undefined) {
  return Boolean(status && ['ready', 'completed', 'failed', 'canceled'].includes(status))
}

export function compactRecoveryItems(items: PreviewRecoveryItem[], attachedId: string | null) {
  const seen = new Set<string>()
  return items.filter((item) => {
    if (item.id === attachedId || item.status === 'running' || item.status === 'queued') return true
    const key = `${item.refparcela}\u0000${item.scenario_name}\u0000${item.baseline_profile ?? ''}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}
