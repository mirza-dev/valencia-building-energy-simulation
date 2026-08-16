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

export interface InputReadiness {
  state: 'checking' | 'ready' | 'blocked'
  /** What is blocking, named.  Empty while checking or when ready. */
  reason: string
}

/**
 * Whether a run could start, or that we cannot say yet.
 *
 * Two states were previously collapsed into one boolean, and both read wrong.
 * A page that has not received the profile yet is not a page that found a
 * problem, so an unanswered query is `checking`, not `blocked`.  And when the
 * blocker is an engine entry point rather than a missing file, `missing_inputs`
 * is empty - so joining it produced an empty string and the surface printed its
 * default all-clear sentence underneath a red "INPUTS REQUIRED" heading, for as
 * long as the entry point stayed missing.  The failing entry points are named
 * here instead.
 *
 * A failed profile query blocks rather than reporting `checking` forever: we
 * cannot confirm the inputs, and this project answers "unknown" by closing the
 * gate, never by assuming the good case.
 */
export function inputReadiness(
  profile?: { missing_inputs?: string[]; entrypoints?: Record<string, boolean> } | null,
  failed = false,
): InputReadiness {
  if (!profile) {
    return failed
      ? { state: 'blocked', reason: 'Could not read the input profile.' }
      : { state: 'checking', reason: '' }
  }
  const missing = profile.missing_inputs ?? []
  const broken = Object.entries(profile.entrypoints ?? {})
    .filter(([, present]) => !present)
    .map(([name]) => name)
  if (missing.length === 0 && broken.length === 0) return { state: 'ready', reason: '' }
  const parts = [...missing]
  if (broken.length) parts.push(`engine entry point${broken.length === 1 ? '' : 's'} unavailable: ${broken.join(', ')}`)
  return { state: 'blocked', reason: parts.join(' · ') }
}

export interface RunProgress {
  done: number
  total: number | null
  pct: number | null
}

/**
 * How far along a run is, or that we cannot say.
 *
 * The denominator is the whole point here.  `summary.coverage.buildings_in_scope`
 * is derived by `aggregate()` from the rows written so far, which mid-run is the
 * same set `countDone` counts - so using it reports 100% from the very first
 * row, beside a live RUNNING chip.  The scope the runner recorded at start is
 * the only figure that means "in scope" while rows are still arriving.
 *
 * The test on the summary is `summary_is_partial`, not `running`: a run that
 * was stopped part-way is no longer running, yet its recomputed total covers
 * only the rows it reached, so its coverage count is no more a scope than a
 * live one's.  Only a written aggregate settles that question.
 *
 * When nothing on record can supply a denominator this returns `null` rather
 * than a number: an unknown length is honest, a full bar is not.
 */
export function runProgress(
  detail?: {
    running?: boolean
    scope?: { total: number } | null
    summary?: { coverage: { buildings_in_scope: number } } | null
    summary_is_partial?: boolean
    progress?: { counts?: Record<string, number> }
  } | null,
  preflight?: { buildings_in_scope?: number } | null,
): RunProgress {
  const done = countDone(detail?.progress?.counts)
  const settled = detail ? !detail.running && detail.summary_is_partial !== true : false
  const total = detail?.scope?.total
    ?? preflight?.buildings_in_scope
    ?? (settled ? detail?.summary?.coverage.buildings_in_scope : undefined)
  if (total == null || !Number.isFinite(total) || total <= 0) {
    return { done, total: null, pct: null }
  }
  return { done, total, pct: Math.min(100, (done / total) * 100) }
}
