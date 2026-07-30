export function localeCode(language: string) {
  return language === 'en' ? 'en-GB' : 'tr-TR'
}

export function formatDate(value: string, language: string) {
  return new Intl.DateTimeFormat(localeCode(language), { day: '2-digit', month: 'short', year: 'numeric' }).format(new Date(value))
}

export function formatDuration(start: unknown, end: unknown) {
  if (typeof start !== 'string' || typeof end !== 'string') return '—'
  const seconds = Math.max(0, Math.round((Date.parse(end) - Date.parse(start)) / 1000))
  if (!Number.isFinite(seconds)) return '—'
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}
