import { useEffect, useMemo, useRef, useState, type ComponentRef } from 'react'
import Map, { Layer, NavigationControl, ScaleControl, Source, type LayerProps, type MapLayerMouseEvent } from 'react-map-gl/maplibre'
import type { Feature, FeatureCollection } from 'geojson'
import { useTranslation } from 'react-i18next'
import type { BuildingFeature, GeometryResult } from '../lib/types'
import { absoluteApiUrl } from '../lib/api'
import MapErrorNotice from './MapErrorNotice'

const blankStyle = {
  version: 8 as const,
  sources: {},
  layers: [{ id: 'background', type: 'background' as const, paint: { 'background-color': '#e8ece6' } }],
}

const buildingFill: LayerProps = {
  id: 'building-fill',
  type: 'fill',
  'source-layer': 'buildings',
  paint: { 'fill-color': '#58786d', 'fill-opacity': 0.22 },
}
const buildingLine: LayerProps = {
  id: 'building-line',
  type: 'line',
  'source-layer': 'buildings',
  paint: { 'line-color': '#405c53', 'line-width': 0.8, 'line-opacity': 0.66 },
}
const selectedFill: LayerProps = {
  id: 'selected-fill', type: 'fill',
  paint: { 'fill-color': '#e05a43', 'fill-opacity': 0.42 },
}
const selectedLine: LayerProps = {
  id: 'selected-line', type: 'line',
  paint: { 'line-color': '#ac352d', 'line-width': 2.6 },
}
const neighborFill: LayerProps = {
  id: 'neighbors-fill', type: 'fill',
  paint: { 'fill-color': '#70869a', 'fill-opacity': 0.22 },
}
const neighborLine: LayerProps = {
  id: 'neighbors-line', type: 'line',
  paint: { 'line-color': '#4f667a', 'line-width': 1.15 },
}
const partyLine: LayerProps = {
  id: 'party-line', type: 'line',
  paint: { 'line-color': '#d13252', 'line-width': 4.2, 'line-opacity': 0.95 },
}
const simplifiedLine: LayerProps = {
  id: 'simplified-line', type: 'line',
  paint: { 'line-color': '#f2b134', 'line-width': 2, 'line-dasharray': [2, 1.5] },
}

function featureCollection(features: Feature[]): FeatureCollection {
  return { type: 'FeatureCollection', features }
}

export default function MapPanel({ selected, geometry, onSelect }: {
  selected?: BuildingFeature
  geometry?: GeometryResult | null
  onSelect: (ref: string) => void
}) {
  const { t } = useTranslation()
  const mapRef = useRef<ComponentRef<typeof Map>>(null)
  const [mapRevision, setMapRevision] = useState(0)
  const [mapLoaded, setMapLoaded] = useState(false)
  const [mapError, setMapError] = useState('')
  const selectedCollection = useMemo(() => featureCollection(selected ? [selected] : []), [selected])
  const simplifiedCollection = useMemo(() => featureCollection(geometry ? [{
    type: 'Feature', properties: {}, geometry: geometry.simplified,
  }] : []), [geometry])
  const partyCollection = useMemo(() => featureCollection(geometry?.party_geometry ? [{
    type: 'Feature', properties: {}, geometry: geometry.party_geometry,
  }] : []), [geometry])

  const click = (event: MapLayerMouseEvent) => {
    const feature = event.features?.[0]
    const ref = feature?.properties?.refparcela
    if (ref) onSelect(String(ref))
  }
  useEffect(() => {
    if (!selected?.geometry || selected.geometry.type !== 'Polygon') return
    const coordinates = selected.geometry.coordinates.flat()
    const xs = coordinates.map((point) => point[0])
    const ys = coordinates.map((point) => point[1])
    mapRef.current?.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]], {
      padding: 90, maxZoom: 17, duration: 650,
    })
  }, [selected])

  const retryMap = () => {
    setMapError('')
    setMapLoaded(false)
    setMapRevision((current) => current + 1)
  }

  return (
    <div className="map-panel" data-map-ready={mapLoaded && !mapError ? 'true' : 'false'} data-map-error={mapError}>
      <Map
        key={mapRevision}
        ref={mapRef}
        initialViewState={{ longitude: -0.395, latitude: 39.495, zoom: 14.2, bearing: 0, pitch: 0 }}
        mapStyle={blankStyle}
        interactiveLayerIds={['building-fill']}
        onClick={click}
        onLoad={() => setMapLoaded(true)}
        onError={(event) => setMapError(event.error?.message ?? 'MapLibre source error')}
        cursor="crosshair"
        attributionControl={false}
      >
        <NavigationControl position="top-right" showCompass={false} />
        <ScaleControl position="bottom-right" unit="metric" />
        <Source id="buildings" type="vector" tiles={[absoluteApiUrl('/api/map/tiles/{z}/{x}/{y}.mvt')]} minzoom={8} maxzoom={18}>
          <Layer {...buildingFill} />
          <Layer {...buildingLine} />
        </Source>
        {geometry ? (
          <Source id="neighbors" type="geojson" data={geometry.neighbors}>
            <Layer {...neighborFill} />
            <Layer {...neighborLine} />
          </Source>
        ) : null}
        <Source id="selected" type="geojson" data={selectedCollection}>
          <Layer {...selectedFill} />
          <Layer {...selectedLine} />
        </Source>
        <Source id="simplified" type="geojson" data={simplifiedCollection}>
          <Layer {...simplifiedLine} />
        </Source>
        <Source id="party" type="geojson" data={partyCollection}>
          <Layer {...partyLine} />
        </Source>
      </Map>
      <div className="map-legend" aria-label={t('map.legend')}>
        <span><i className="legend-swatch target" />{t('map.target')}</span>
        <span><i className="legend-swatch neighbor" />{t('map.context')}</span>
        <span><i className="legend-line party" />{t('map.party')}</span>
        <span><i className="legend-line simplified" />{t('map.simplified')}</span>
      </div>
      {mapError ? <MapErrorNotice message={mapError} onRetry={retryMap} /> : null}
    </div>
  )
}
