import type { FeatureCollection } from 'geojson'
import type { NeighborhoodMapDescriptor } from './types'

export type NeighborhoodMapState = 'loading' | 'source-loaded' | 'delayed' | 'ready' | 'empty' | 'error'

export const NEIGHBORHOOD_MAP_TIMEOUT_MS = 25_000

function validCoordinates(value: unknown): boolean {
  if (!Array.isArray(value) || !value.length) return false
  if (typeof value[0] === 'number') {
    return value.length >= 2 && Number.isFinite(value[0]) && Number.isFinite(value[1])
      && value[0] >= -180 && value[0] <= 180 && value[1] >= -90 && value[1] <= 90
  }
  return value.every(validCoordinates)
}

export function validateNeighborhoodMapPayload(
  data: FeatureCollection,
  descriptor: NeighborhoodMapDescriptor,
): string | null {
  if (!/^[0-9a-f]{64}$/.test(descriptor.fingerprint) || descriptor.sha256 !== descriptor.fingerprint) {
    return 'invalid_fingerprint'
  }
  if (descriptor.crs !== 'EPSG:4326' || descriptor.bounds.length !== 4
    || descriptor.bounds.some((value) => !Number.isFinite(value))
    || descriptor.bounds[0] >= descriptor.bounds[2] || descriptor.bounds[1] >= descriptor.bounds[3]
    || descriptor.bounds[0] < -180 || descriptor.bounds[2] > 180
    || descriptor.bounds[1] < -90 || descriptor.bounds[3] > 90) {
    return 'invalid_spatial_contract'
  }
  if (!Number.isInteger(descriptor.feature_count) || descriptor.feature_count < 0
    || !Number.isInteger(descriptor.size_bytes) || descriptor.size_bytes <= 0
    || !descriptor.url.startsWith('/api/neighborhood/')) return 'invalid_descriptor'
  if (data.type !== 'FeatureCollection' || !Array.isArray(data.features)) return 'invalid_feature_collection'
  if (data.features.length !== descriptor.feature_count) return 'feature_count_mismatch'
  if (!data.features.length) return 'empty_feature_collection'
  const featureIds = new Set<string>()
  for (const feature of data.features) {
    const geometry = feature.geometry
    if (!geometry || (geometry.type !== 'Polygon' && geometry.type !== 'MultiPolygon')
      || !validCoordinates(geometry.coordinates)) return 'invalid_geometry'
    const reference = String(feature.properties?.refparcela ?? '').trim()
    if (!reference) return 'invalid_refparcela'
    const featureId = String(feature.id ?? reference).trim()
    if (!featureId || featureIds.has(featureId)) return 'invalid_feature_id'
    featureIds.add(featureId)
  }
  return null
}

export function resolveNeighborhoodMapState({
  featureCount,
  sourceLoaded,
  mapIdle,
  timedOut,
  error,
}: {
  featureCount: number
  sourceLoaded: boolean
  mapIdle: boolean
  timedOut: boolean
  error: boolean
}): NeighborhoodMapState {
  if (error) return 'error'
  if (featureCount <= 0) return 'empty'
  if (sourceLoaded && mapIdle) return 'ready'
  if (timedOut) return 'delayed'
  if (sourceLoaded) return 'source-loaded'
  return 'loading'
}
