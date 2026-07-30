export type FocusedPanel = 'setup' | 'selection' | 'evidence' | 'metrics'
export type FocusedStatusLevel = 'verified' | 'warning' | 'blocked' | 'pending'

export function resolveFocusedStatus({
  blocked = false,
  warning = false,
  verified = false,
}: {
  blocked?: boolean
  warning?: boolean
  verified?: boolean
}): FocusedStatusLevel {
  if (blocked) return 'blocked'
  if (warning) return 'warning'
  if (verified) return 'verified'
  return 'pending'
}

export function resolveInitialPanel({
  hasResult,
  blocked = false,
}: {
  hasResult: boolean
  blocked?: boolean
}): FocusedPanel | null {
  if (blocked) return 'evidence'
  return hasResult ? null : 'setup'
}

export function limitPrimaryMetrics<T>(metrics: readonly T[]): T[] {
  return metrics.slice(0, 3)
}

export function recentRunItems<T extends { id: string }>(items: readonly T[], selectedId: string, limit = 5): T[] {
  const recent = items.slice(0, limit)
  if (!selectedId || recent.some((item) => item.id === selectedId)) return recent
  const selected = items.find((item) => item.id === selectedId)
  return selected ? [selected, ...recent.slice(0, Math.max(0, limit - 1))] : recent
}
