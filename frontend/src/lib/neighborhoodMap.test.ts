import { describe, expect, it } from 'vitest'
import type { FeatureCollection } from 'geojson'
import { NEIGHBORHOOD_MAP_TIMEOUT_MS, resolveNeighborhoodMapState, validateNeighborhoodMapPayload } from './neighborhoodMap'
import type { NeighborhoodMapDescriptor } from './types'

const fingerprint = 'a'.repeat(64)
const descriptor: NeighborhoodMapDescriptor = {
  fingerprint,
  sha256: fingerprint,
  feature_count: 1,
  bounds: [-0.41, 39.48, -0.37, 39.51],
  crs: 'EPSG:4326',
  size_bytes: 512,
  url: `/api/neighborhood/maps/${fingerprint}.geojson`,
}
const map: FeatureCollection = {
  type: 'FeatureCollection',
  features: [{
    type: 'Feature',
    properties: { refparcela: 'A', cluster: 'BlocPluriP04' },
    geometry: { type: 'Polygon', coordinates: [[[-0.40, 39.49], [-0.39, 39.49], [-0.39, 39.50], [-0.40, 39.49]]] },
  }],
}

describe('neighborhood map contract', () => {
  it('accepts a matching WGS84 content-addressed resource', () => {
    expect(validateNeighborhoodMapPayload(map, descriptor)).toBeNull()
  })

  it('rejects descriptor, count, geometry, coordinate, and identity drift', () => {
    expect(validateNeighborhoodMapPayload(map, { ...descriptor, fingerprint: 'bad' })).toBe('invalid_fingerprint')
    expect(validateNeighborhoodMapPayload(map, { ...descriptor, crs: 'EPSG:4326', bounds: [1, 1, 0, 2] })).toBe('invalid_spatial_contract')
    expect(validateNeighborhoodMapPayload(map, { ...descriptor, size_bytes: 0 })).toBe('invalid_descriptor')
    expect(validateNeighborhoodMapPayload(map, { ...descriptor, feature_count: 2 })).toBe('feature_count_mismatch')
    expect(validateNeighborhoodMapPayload({ ...map, features: [{ ...map.features[0], geometry: { type: 'Point', coordinates: [0, 0] } }] }, descriptor)).toBe('invalid_geometry')
    expect(validateNeighborhoodMapPayload({ ...map, features: [{ ...map.features[0], geometry: { type: 'Polygon', coordinates: [[[Number.NaN, 39.49], [-0.39, 39.49], [-0.40, 39.49]]] } }] }, descriptor)).toBe('invalid_geometry')
    expect(validateNeighborhoodMapPayload({ ...map, features: [map.features[0], map.features[0]] }, { ...descriptor, feature_count: 2 })).toBe('invalid_feature_id')
  })

  it('uses loaded-source + idle as readiness without depending on rendered feature inspection', () => {
    const state = (overrides: Partial<Parameters<typeof resolveNeighborhoodMapState>[0]>) => resolveNeighborhoodMapState({
      featureCount: 959, sourceLoaded: false, mapIdle: false, timedOut: false, error: false, ...overrides,
    })
    expect(state({})).toBe('loading')
    expect(state({ sourceLoaded: true })).toBe('source-loaded')
    expect(state({ mapIdle: true })).toBe('loading')
    expect(state({ sourceLoaded: true, mapIdle: true })).toBe('ready')
    expect(state({ featureCount: 0 })).toBe('empty')
    expect(state({ timedOut: true })).toBe('delayed')
    expect(state({ sourceLoaded: true, timedOut: true })).toBe('delayed')
    expect(state({ sourceLoaded: true, mapIdle: true, timedOut: true })).toBe('ready')
    expect(state({ error: true, sourceLoaded: true, mapIdle: true })).toBe('error')
    expect(NEIGHBORHOOD_MAP_TIMEOUT_MS).toBe(25_000)
  })
})
