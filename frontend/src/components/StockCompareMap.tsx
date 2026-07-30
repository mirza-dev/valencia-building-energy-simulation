import { useCallback, useEffect, useMemo, useRef, useState, type ComponentRef } from 'react'
import Map, { Layer, NavigationControl, ScaleControl, Source, type LayerProps, type MapLayerMouseEvent, type MapSourceDataEvent } from 'react-map-gl/maplibre'
import type { FeatureCollection } from 'geojson'
import type { FillLayerSpecification, FilterSpecification } from 'maplibre-gl'
import { useTranslation } from 'react-i18next'
import type { StockCompareKind, StockCompareRow } from '../lib/types'
import { maximumAbsoluteDelta, stockDeltaExpression } from '../lib/stockCompare'
import MapErrorNotice from './MapErrorNotice'

const blankStyle = {
  version: 8 as const,
  sources: {},
  layers: [{ id: 'background', type: 'background' as const, paint: { 'background-color': '#edf0eb' } }],
}

export default function StockCompareMap({ kind, rows, metric, unit, group, selected, bounds, geojson, tileUrl, onSelect }: {
  kind: StockCompareKind
  rows: StockCompareRow[]
  metric: string
  unit: string
  group: 'clusters' | 'districts'
  selected: string
  bounds: [number, number, number, number]
  geojson?: FeatureCollection
  tileUrl?: string
  onSelect: (key: string) => void
}) {
  const { t } = useTranslation()
  const mapRef = useRef<ComponentRef<typeof Map>>(null)
  const [revision, setRevision] = useState(0)
  const [loaded, setLoaded] = useState(false)
  const [sourceLoaded, setSourceLoaded] = useState(false)
  const [rendered, setRendered] = useState(0)
  const [error, setError] = useState('')
  const property = group === 'districts' ? 'nombre' : 'cluster'
  const maximum = maximumAbsoluteDelta(rows, metric)
  const fill = useMemo<LayerProps>(() => ({
    id: 'stock-compare-fill',
    type: 'fill',
    ...(kind === 'city' ? { 'source-layer': 'buildings' } : {}),
    paint: {
      'fill-color': stockDeltaExpression(rows, metric, property),
      'fill-opacity': 0.8,
    } as FillLayerSpecification['paint'],
  }), [kind, metric, property, rows])
  const selection = useMemo<LayerProps>(() => ({
    id: 'stock-compare-selection',
    type: 'line',
    ...(kind === 'city' ? { 'source-layer': 'buildings' } : {}),
    filter: ['==', ['to-string', ['get', property]], selected] as FilterSpecification,
    paint: { 'line-color': '#111b17', 'line-width': 2.5, 'line-opacity': 1 },
  }), [kind, property, selected])

  useEffect(() => {
    if (!loaded) return
    mapRef.current?.fitBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]], { padding: 26, duration: 0 })
  }, [bounds, loaded, revision])

  const inspect = useCallback(() => {
    const map = mapRef.current?.getMap()
    if (!map || !map.isStyleLoaded() || !map.getSource('stock-compare-source')) return
    try {
      if (!map.isSourceLoaded('stock-compare-source')) return
      setSourceLoaded(true)
      setRendered((current) => Math.max(current, map.queryRenderedFeatures({ layers: ['stock-compare-fill'] }).length))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }, [])

  const sourceData = (event: MapSourceDataEvent) => {
    if (event.sourceId === 'stock-compare-source' && event.isSourceLoaded) setSourceLoaded(true)
  }
  const click = (event: MapLayerMouseEvent) => {
    const key = event.features?.[0]?.properties?.[property]
    if (key != null) onSelect(String(key))
  }
  const retry = () => {
    setError('')
    setLoaded(false)
    setSourceLoaded(false)
    setRendered(0)
    setRevision((current) => current + 1)
  }
  const ready = loaded && sourceLoaded && rendered > 0 && !error

  return <div className="stock-compare-map" data-map-ready={ready ? 'true' : 'false'} data-rendered-feature-count={rendered}>
    <Map key={revision} ref={mapRef} initialViewState={{ longitude: -0.376, latitude: 39.47, zoom: kind === 'city' ? 10.5 : 13.8 }}
      mapStyle={blankStyle} attributionControl={false} interactiveLayerIds={['stock-compare-fill']}
      onLoad={() => setLoaded(true)} onSourceData={sourceData} onIdle={inspect} onClick={click}
      onError={(event) => setError(event.error?.message ?? 'MapLibre comparison error')} cursor="crosshair">
      <NavigationControl position="top-right" showCompass={false} />
      <ScaleControl position="bottom-right" unit="metric" />
      {kind === 'city' && tileUrl ? <Source id="stock-compare-source" type="vector" tiles={[tileUrl]} minzoom={8} maxzoom={17}>
        <Layer {...fill} />
        <Layer id="stock-compare-outline" type="line" source-layer="buildings" paint={{ 'line-color': '#3d4b45', 'line-width': 0.4, 'line-opacity': 0.62 }} />
        <Layer {...selection} />
      </Source> : geojson ? <Source id="stock-compare-source" type="geojson" data={geojson}>
        <Layer {...fill} />
        <Layer id="stock-compare-outline" type="line" paint={{ 'line-color': '#3d4b45', 'line-width': 0.55, 'line-opacity': 0.68 }} />
        <Layer {...selection} />
      </Source> : null}
    </Map>
    <div className="stock-delta-legend" aria-label={t('stockCompare.deltaLegend')}>
      <span className="cooler">−{maximum.toFixed(2)}</span><div className="stock-delta-ramp" /><span>0</span><div className="stock-delta-ramp warm" /><span className="warmer">+{maximum.toFixed(2)} {unit}</span>
    </div>
    {!ready && !error ? <div className="stock-map-loading"><span className="spinner" />{t('stockCompare.mapLoading')}</div> : null}
    {error ? <MapErrorNotice message={error} onRetry={retry} /> : null}
  </div>
}
