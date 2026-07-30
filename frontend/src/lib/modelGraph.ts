import type { ModelGraphObject } from './types'

export function matchesGraphSearch(item: ModelGraphObject, query: string): boolean {
  const normalized = query.trim().toLocaleLowerCase()
  if (!normalized) return true
  return JSON.stringify(item).toLocaleLowerCase().includes(normalized)
}

export function compactValue(value: unknown): string {
  if (value == null) return '—'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
  if (typeof value === 'boolean') return value ? 'YES' : 'NO'
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return `${value.length} item(s)`
  return Object.entries(value as Record<string, unknown>)
    .map(([key, item]) => `${key}: ${compactValue(item)}`).join(' · ')
}

function englishOrdinal(value: number): string {
  const remainder100 = value % 100
  if (remainder100 >= 11 && remainder100 <= 13) return `${value}th`
  const suffix = value % 10 === 1 ? 'st' : value % 10 === 2 ? 'nd' : value % 10 === 3 ? 'rd' : 'th'
  return `${value}${suffix}`
}

/**
 * Corrects legacy display names without mutating the source OSM graph. PlantillaOS
 * object IDs and raw names remain untouched in artifacts and provenance.
 */
export function formatModelDisplayText(value: string, language: string): string {
  const turkish = language.toLowerCase().startsWith('tr')
  const prefix = (kind: string) => turkish
    ? ({ Story: 'Kat', Zone: 'Zon', Space: 'Mekân' }[kind] ?? kind)
    : kind
  return value
    .replace(/\b(Story|Zone|Space) (\d+) - Ground \(commercial or buffer zone\)/g, (_match, kind: string, index: string) => (
      turkish
        ? `${prefix(kind)} ${index} · Zemin (ticari veya tampon bölge)`
        : `${kind} ${index} · Ground floor (commercial or buffer)`
    ))
    .replace(/\b(Story|Zone|Space) (\d+) - (\d+)(?:st|nd|rd|th) floor\b/g, (_match, kind: string, index: string, floor: string) => (
      turkish
        ? `${prefix(kind)} ${index} · ${Number(floor)}. kat`
        : `${kind} ${index} · ${englishOrdinal(Number(floor))} floor`
    ))
}

export function humanizeModelKey(value: string): string {
  const spaced = value.replaceAll('_', ' ').replace(/([a-z\d])([A-Z])/g, '$1 $2')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}
