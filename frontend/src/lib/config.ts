import type { BuildConfig, OverrideRecord, SourceType } from './types'

export function deepClone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

export function flatten(value: unknown, prefix = '', output: Record<string, unknown> = {}): Record<string, unknown> {
  if (Array.isArray(value)) return output
  if (value && typeof value === 'object') {
    Object.entries(value).forEach(([key, child]) => {
      const path = prefix ? `${prefix}.${key}` : key
      flatten(child, path, output)
    })
  } else {
    output[prefix] = value
  }
  return output
}

export function changedFields(config: BuildConfig, baseline: BuildConfig): string[] {
  const current = flatten(config)
  const base = flatten(baseline)
  const ignored = new Set([
    'provenance.scenario_name',
    'provenance.locale',
    'provenance.baseline_profile',
  ])
  return Object.keys(current).filter((key) => !ignored.has(key) && current[key] !== base[key])
}

export function withOverrideRecords(
  config: BuildConfig,
  baseline: BuildConfig,
  reason: string,
  sourceType: SourceType,
  sourceRef: string,
): BuildConfig {
  const next = deepClone(config)
  const previous = new Map(next.provenance.overrides.map((entry) => [entry.field, entry]))
  const records: OverrideRecord[] = changedFields(next, baseline).map((field) => previous.get(field) ?? ({
    field,
    reason,
    source_type: sourceType,
    source_ref: sourceRef || null,
  }))
  next.provenance.overrides = records
  return next
}
