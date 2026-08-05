export function shortHash(value?: string | null, size = 10): string {
  if (!value) return '—'
  return value.length <= size ? value : `${value.slice(0, size)}…`
}

export function formatProductBytes(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let amount = Math.max(0, value)
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  return `${amount >= 10 || unit === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`
}

export function formatDuration(minutes?: number | null): string {
  if (minutes == null || !Number.isFinite(minutes)) return '—'
  if (minutes < 60) return `${Math.round(minutes)} min`
  const hours = minutes / 60
  if (hours < 48) return `${hours.toFixed(hours >= 10 ? 0 : 1)} h`
  return `${(hours / 24).toFixed(1)} d`
}

export function safeRunName(value: string): string {
  return value.trim().replace(/[^a-zA-Z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80)
}

export function countDone(counts?: Record<string, number>): number {
  if (!counts) return 0
  return (counts.ok ?? 0) + (counts.failed ?? 0) + (counts.excluded ?? 0)
}
