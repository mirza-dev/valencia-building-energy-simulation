import { useCallback, useEffect, useMemo, useRef, useState, type ComponentRef } from 'react'
import Map, { Layer, NavigationControl, ScaleControl, Source, type LayerProps, type MapLayerMouseEvent, type MapSourceDataEvent } from 'react-map-gl/maplibre'
import type { FilterSpecification } from 'maplibre-gl'
import type { FeatureCollection } from 'geojson'
import { useTranslation } from 'react-i18next'
import type { NeighborhoodMapDescriptor } from '../lib/types'
import { NEIGHBORHOOD_MAP_TIMEOUT_MS, resolveNeighborhoodMapState, validateNeighborhoodMapPayload } from '../lib/neighborhoodMap'

const blankStyle = {
  version: 8 as const,
  sources: {},
  layers: [{ id: 'background', type: 'background' as const, paint: { 'background-color': '#edf0eb' } }],
}

const TRANSIENT_SOURCE_ERROR = "There is no tile manager with ID 'neighborhood-stock'"

const lineLayer: LayerProps = {
  id: 'neighborhood-outline', type: 'line',
  paint: { 'line-color': '#33463f', 'line-width': 0.65, 'line-opacity': 0.72 },
}

const representativeLayer: LayerProps = {
  id: 'representative-outline', type: 'line',
  filter: ['==', ['get', 'is_representative'], true],
  paint: { 'line-color': '#e04f39', 'line-width': 2.4, 'line-opacity': 1 },
}

type EnergyMode = 'heating' | 'cooling' | 'hvac' | 'site' | 'cluster'

function fillLayer(mode: EnergyMode): LayerProps {
  const metric = mode === 'heating' ? 'heating_kwh_m2'
    : mode === 'cooling' ? 'cooling_kwh_m2'
      : mode === 'hvac' ? 'cons_hc_kwh_m2' : 'total_site_kwh_m2'
  return {
    id: 'neighborhood-fill', type: 'fill',
    paint: mode === 'cluster' ? {
      'fill-color': [
        'match', ['get', 'family'],
        'BlocPluri', '#3f756d', 'EdiPluri', '#d99b35', 'VivUni', '#a85850', '#8b9690',
      ],
      'fill-opacity': 0.72,
    } : {
      'fill-color': [
        'interpolate', ['linear'], ['coalesce', ['get', metric], 0],
        0, '#e7eee5', 10, '#8cb8a4', 18, '#efc15b', 28, '#c95743', 40, '#7c2634',
      ],
      'fill-opacity': 0.78,
    },
  }
}

export default function NeighborhoodMap({ data, descriptor, mode, selectedCluster, retryToken, onSelectCluster, onRetry }: {
  data: FeatureCollection | string
  descriptor: NeighborhoodMapDescriptor
  mode: EnergyMode
  selectedCluster: string
  retryToken: number
  onSelectCluster: (cluster: string) => void
  onRetry: () => void
}) {
  const { t } = useTranslation()
  const mapRef = useRef<ComponentRef<typeof Map>>(null)
  const timeoutRef = useRef<number | null>(null)
  const [sourceLoaded, setSourceLoaded] = useState(false)
  const [mapIdle, setMapIdle] = useState(false)
  const [renderedFeatureCount, setRenderedFeatureCount] = useState(0)
  const [timedOut, setTimedOut] = useState(false)
  const [mapError, setMapError] = useState('')
  const featureData = typeof data === 'string' ? null : data
  const validationError = useMemo(
    () => featureData ? validateNeighborhoodMapPayload(featureData, descriptor) : 'invalid_feature_collection',
    [descriptor, featureData],
  )
  const emptyPayload = validationError === 'empty_feature_collection' || descriptor.feature_count === 0
  const state = resolveNeighborhoodMapState({
    featureCount: emptyPayload ? 0 : descriptor.feature_count,
    sourceLoaded,
    mapIdle,
    timedOut,
    error: Boolean((validationError && !emptyPayload) || mapError),
  })
  const selectedLayer = useMemo<LayerProps>(() => ({
    id: 'selected-cluster', type: 'line',
    filter: ['==', ['to-string', ['get', 'cluster']], selectedCluster] as FilterSpecification,
    paint: { 'line-color': '#111d19', 'line-width': 2.2, 'line-opacity': 1 },
  }), [selectedCluster])

  useEffect(() => {
    setSourceLoaded(false)
    setMapIdle(false)
    setRenderedFeatureCount(0)
    setTimedOut(false)
    setMapError('')
    if (timeoutRef.current != null) window.clearTimeout(timeoutRef.current)
    timeoutRef.current = window.setTimeout(() => setTimedOut(true), NEIGHBORHOOD_MAP_TIMEOUT_MS)
    return () => {
      if (timeoutRef.current != null) window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
  }, [descriptor.fingerprint, retryToken])

  useEffect(() => {
    if (sourceLoaded && mapIdle && timeoutRef.current != null) {
      window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
      setTimedOut(false)
    }
  }, [mapIdle, sourceLoaded])

  const inspectRenderedFeatures = useCallback(() => {
    const map = mapRef.current?.getMap()
    if (!map || validationError) return
    try {
      if (!map.isStyleLoaded() || !map.getSource('neighborhood-stock')) return
      if (!map.isSourceLoaded('neighborhood-stock')) return
      setSourceLoaded(true)
      setMapIdle(true)
      try {
        const features = map.queryRenderedFeatures({ layers: ['neighborhood-fill'] })
        setRenderedFeatureCount((current) => Math.max(current, features.length))
      } catch {
        // Feature inspection is diagnostic only. MapLibre's loaded-source + idle
        // contract determines readiness even when this best-effort query is empty.
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (!message.includes(TRANSIENT_SOURCE_ERROR)) setMapError(message)
    }
  }, [validationError])

  const sourceData = (event: MapSourceDataEvent) => {
    if (event.sourceId === 'neighborhood-stock' && event.isSourceLoaded) {
      setSourceLoaded(true)
    }
  }

  const click = (event: MapLayerMouseEvent) => {
    const cluster = event.features?.[0]?.properties?.cluster
    if (cluster) onSelectCluster(String(cluster))
  }

  const statusText = state === 'error'
    ? t('neighborhood.mapError')
    : state === 'empty' ? t('neighborhood.mapEmpty')
      : state === 'delayed' ? t('neighborhood.mapDelayed')
      : state === 'source-loaded' ? t('neighborhood.mapRendering') : t('neighborhood.mapLoading')

  return <div className="neighborhood-map"
    data-map-ready={state === 'ready' ? 'true' : 'false'} data-map-state={state}
    data-feature-count={descriptor.feature_count} data-rendered-feature-count={renderedFeatureCount}
    data-map-fingerprint={descriptor.fingerprint} data-map-error={mapError || (emptyPayload ? '' : validationError) || ''}>
    {!validationError && featureData ? <Map ref={mapRef} initialViewState={{
      bounds: [[descriptor.bounds[0], descriptor.bounds[1]], [descriptor.bounds[2], descriptor.bounds[3]]],
      fitBoundsOptions: { padding: 34, maxZoom: 16 },
    }}
      mapStyle={blankStyle} attributionControl={false} interactiveLayerIds={['neighborhood-fill']}
      onSourceData={sourceData} onIdle={inspectRenderedFeatures}
      onError={(event) => {
        const message = event.error?.message ?? 'MapLibre source error'
        if (!message.includes(TRANSIENT_SOURCE_ERROR)) setMapError(message)
      }}
      onClick={click} cursor="crosshair">
      <NavigationControl position="top-right" showCompass={false} />
      <ScaleControl position="bottom-right" unit="metric" />
      <Source id="neighborhood-stock" type="geojson" data={featureData} tolerance={0.75} buffer={64} maxzoom={16}>
        <Layer {...fillLayer(mode)} />
        <Layer {...lineLayer} />
        <Layer {...representativeLayer} />
        <Layer {...selectedLayer} />
      </Source>
    </Map> : null}
    <div className="neighborhood-map-legend" aria-label={t('neighborhood.mapLegend')}>
      {mode === 'cluster' ? <>
        <span><i style={{ background: '#3f756d' }} />BlocPluri</span>
        <span><i style={{ background: '#d99b35' }} />EdiPluri</span>
        <span><i style={{ background: '#a85850' }} />VivUni</span>
      </> : <><span>0</span><div className="energy-ramp" /><span>40 kWh/m²</span></>}
      <span className="representative-key"><i />{t('neighborhood.representative')}</span>
    </div>
    {state !== 'ready' ? <div className={`neighborhood-map-status ${state}`} role={state === 'error' || state === 'empty' ? 'alert' : 'status'}>
      {state === 'loading' || state === 'source-loaded' || state === 'delayed' ? <span className="spinner" /> : null}
      <strong>{statusText}</strong>
      {mapError || (validationError && !emptyPayload) ? <code>{mapError || validationError}</code> : null}
      {state === 'error' || state === 'empty' ? <button className="secondary-button" onClick={onRetry}>{t('neighborhood.retryMap')}</button> : null}
    </div> : null}
  </div>
}
