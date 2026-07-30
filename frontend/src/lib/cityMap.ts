import type { CityDistrict, NeighborhoodCluster, NeighborhoodRepresentative } from './types'

const FAMILY_COLORS: Record<string, string> = {
  BlocPluri: '#3f756d',
  EdiPluri: '#d99b35',
  VivUni: '#a85850',
}

export function clusterColorExpression(rows: NeighborhoodRepresentative[]): unknown[] {
  const matches = rows.flatMap((row) => [row.cluster, FAMILY_COLORS[row.family] ?? '#8b9690'])
  return ['match', ['get', 'cluster'], ...matches, '#a5aea9']
}

export function metricMatchExpression<T extends Record<string, unknown>>(
  rows: T[], key: keyof T, metric: keyof T,
): unknown[] {
  const matches = rows.flatMap((row) => [String(row[key] ?? ''), Number(row[metric] ?? 0)])
  return ['match', ['to-string', ['get', String(key)]], ...matches, 0]
}

export function energyColorExpression(
  rows: NeighborhoodCluster[], metric: 'heating_kwh_m2' | 'cooling_kwh_m2' | 'cons_hc_kwh_m2' | 'total_site_kwh_m2',
): unknown[] {
  const value = metricMatchExpression(rows as unknown as Record<string, unknown>[], 'cluster', metric)
  const maximum = Math.max(1, ...rows.map((row) => Number(row[metric] ?? 0)))
  return [
    'interpolate', ['linear'], value,
    0, '#e7eee5', maximum * 0.34, '#8cb8a4', maximum * 0.62, '#efc15b',
    maximum * 0.82, '#c95743', maximum, '#7c2634',
  ]
}

export function districtColorExpression(rows: CityDistrict[]): unknown[] {
  const metric = rows.some((row) => row.cons_hc_gwh != null) ? 'cons_hc_gwh'
    : rows.some((row) => row.heating_gwh != null) ? 'heating_gwh' : 'res_area_m2'
  const value = metricMatchExpression(rows as unknown as Record<string, unknown>[], 'nombre', metric)
  const maximum = Math.max(1, ...rows.map((row) => Number(row[metric] ?? 0)))
  return [
    'interpolate', ['linear'], value,
    0, '#edf0eb', maximum * 0.3, '#b8d2c5', maximum * 0.6, '#67a08d', maximum, '#245f58',
  ]
}
