import { useCallback, useEffect, useMemo, useRef, useState, type ComponentRef } from 'react'
import Map, { Layer, Marker, NavigationControl, ScaleControl, Source, type LayerProps, type MapLayerMouseEvent, type MapSourceDataEvent } from 'react-map-gl/maplibre'
import type { CircleLayerSpecification, FillLayerSpecification, FilterSpecification } from 'maplibre-gl'
import { useTranslation } from 'react-i18next'
import { clusterColorExpression, districtColorExpression, energyColorExpression } from '../lib/cityMap'
import type { CityDistrict, NeighborhoodCluster, NeighborhoodRepresentative } from '../lib/types'
import MapErrorNotice from './MapErrorNotice'

const blankStyle = {
  version: 8 as const,
  sources: {},
  layers: [{ id: 'background', type: 'background' as const, paint: { 'background-color': '#edf0eb' } }],
}

const TRANSIENT_SOURCE_ERROR = "There is no tile manager with ID 'city-stock'"

type CityMapMode = 'cluster' | 'hvac' | 'site' | 'heating' | 'cooling' | 'district'

function footprintPrefetchUrls(tileUrl: string, bounds: [number, number, number, number]) {
  const zoom = 11
  const scale = 2 ** zoom
  const tileX = (longitude: number) => Math.floor((longitude + 180) / 360 * scale)
  const tileY = (latitude: number) => Math.floor(
    (1 - Math.asinh(Math.tan(latitude * Math.PI / 180)) / Math.PI) / 2 * scale,
  )
  const minX = Math.max(0, tileX(bounds[0]) - 1)
  const maxX = Math.min(scale - 1, tileX(bounds[2]) + 1)
  const minY = Math.max(0, tileY(bounds[3]))
  const maxY = Math.min(scale - 1, tileY(bounds[1]))
  const urls: string[] = []
  for (let x = minX; x <= maxX; x += 1) {
    for (let y = minY; y <= maxY; y += 1) {
      urls.push(tileUrl.replace('{z}', String(zoom)).replace('{x}', String(x)).replace('{y}', String(y)))
    }
  }
  return urls.slice(0, 8)
}

function fillLayer(mode: CityMapMode, clusters: Array<NeighborhoodCluster | NeighborhoodRepresentative>, districts: CityDistrict[]): LayerProps {
  let color: unknown[]
  if (mode === 'cluster') color = clusterColorExpression(clusters)
  else if (mode === 'district') color = districtColorExpression(districts)
  else {
    const metric = mode === 'hvac' ? 'cons_hc_kwh_m2'
      : mode === 'site' ? 'total_site_kwh_m2'
        : mode === 'heating' ? 'heating_kwh_m2' : 'cooling_kwh_m2'
    color = energyColorExpression(clusters as NeighborhoodCluster[], metric)
  }
  return {
    id: 'city-fill', type: 'fill', 'source-layer': 'buildings', minzoom: 11,
    paint: { 'fill-color': color, 'fill-opacity': mode === 'district' ? 0.76 : 0.72 } as FillLayerSpecification['paint'],
  }
}

function overviewLayer(mode: CityMapMode, clusters: Array<NeighborhoodCluster | NeighborhoodRepresentative>, districts: CityDistrict[]): LayerProps {
  const color = mode === 'cluster' ? clusterColorExpression(clusters)
    : mode === 'district' ? districtColorExpression(districts)
      : energyColorExpression(clusters as NeighborhoodCluster[], mode === 'hvac' ? 'cons_hc_kwh_m2'
        : mode === 'site' ? 'total_site_kwh_m2' : mode === 'heating' ? 'heating_kwh_m2' : 'cooling_kwh_m2')
  return {
    id: 'city-overview', type: 'circle', 'source-layer': 'overview', maxzoom: 11,
    paint: {
      'circle-color': color as unknown as NonNullable<CircleLayerSpecification['paint']>['circle-color'],
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 2.1, 11, 2.7, 14, 3.4],
      'circle-opacity': mode === 'district' ? 0.78 : 0.74,
      'circle-stroke-color': 'rgba(255,255,255,0.48)',
      'circle-stroke-width': 0.45,
    },
  }
}

export default function CityMap({ tileUrl, bounds, focusBounds, clusters, districts, mode, selectedCluster, selectedDistrict, onSelectCluster, onSelectDistrict }: {
  tileUrl: string
  bounds: [number, number, number, number]
  focusBounds: [number, number, number, number]
  clusters: Array<NeighborhoodCluster | NeighborhoodRepresentative>
  districts: CityDistrict[]
  mode: CityMapMode
  selectedCluster: string
  selectedDistrict: string
  onSelectCluster: (cluster: string) => void
  onSelectDistrict: (district: string) => void
}) {
  const { t } = useTranslation()
  const mapRef = useRef<ComponentRef<typeof Map>>(null)
  const [mapRevision, setMapRevision] = useState(0)
  const [mapLoaded, setMapLoaded] = useState(false)
  const [sourceLoaded, setSourceLoaded] = useState(false)
  const [mapIdle, setMapIdle] = useState(false)
  const [renderedFeatureCount, setRenderedFeatureCount] = useState(0)
  const [cameraZoom, setCameraZoom] = useState<number | null>(null)
  const [mapError, setMapError] = useState('')
  const representativeRefs = useMemo(() => clusters.map((item) => item.refparcela), [clusters])
  const selectedFilter: FilterSpecification = (mode === 'district'
    ? ['==', ['to-string', ['get', 'nombre']], selectedDistrict]
    : ['==', ['to-string', ['get', 'cluster']], selectedCluster]) as FilterSpecification

  const inspectRenderedFeatures = useCallback(() => {
    const map = mapRef.current?.getMap()
    if (!map) return
    try {
      if (!map.isStyleLoaded() || !map.getSource('city-stock')) return
      if (!map.isSourceLoaded('city-stock')) return
      setSourceLoaded(true)
      setMapIdle(true)
      try {
        const count = map.queryRenderedFeatures({ layers: ['city-fill', 'city-overview'] }).length
        setRenderedFeatureCount((current) => Math.max(current, count))
      } catch {
        // Source-loaded + idle is the render contract. Feature inspection is
        // diagnostic only and may be empty while the camera is changing.
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (!message.includes(TRANSIENT_SOURCE_ERROR)) setMapError(message)
    }
  }, [])

  const sourceData = (event: MapSourceDataEvent) => {
    if (event.sourceId === 'city-stock' && event.isSourceLoaded) setSourceLoaded(true)
  }

  const retryMap = () => {
    setMapError('')
    setMapLoaded(false)
    setSourceLoaded(false)
    setMapIdle(false)
    setRenderedFeatureCount(0)
    setMapRevision((current) => current + 1)
  }

  const click = (event: MapLayerMouseEvent) => {
    const properties = event.features?.[0]?.properties
    if (mode === 'district' && properties?.nombre) onSelectDistrict(String(properties.nombre))
    else if (properties?.cluster) onSelectCluster(String(properties.cluster))
  }

  const ready = mapLoaded && sourceLoaded && mapIdle && !mapError
  const mapLod = cameraZoom == null ? '' : cameraZoom >= 14 ? 'buildings' : cameraZoom >= 11 ? 'building-footprints' : 'overview'
  const fitExtent = useCallback((target: [number, number, number, number], duration = 260) => {
    mapRef.current?.fitBounds(
      [[target[0], target[1]], [target[2], target[3]]],
      { padding: 28, maxZoom: 12, duration },
    )
  }, [])

  // Map metrics arrive independently from the run summary. If the map mounts
  // while only the full-stock bounds are available, initialViewState is not
  // re-applied when the tighter urban focus arrives. Refit once the map and
  // the resolved focus are both ready.
  useEffect(() => {
    if (mapLoaded) fitExtent(focusBounds, 0)
  }, [fitExtent, focusBounds, mapLoaded])

  // Prepare the first real-footprint zoom while the lightweight city overview
  // is already usable. Browser and server caches then make the first zoom-in
  // reveal buildings as one coherent surface instead of tile-by-tile patches.
  useEffect(() => {
    if (!mapLoaded || !tileUrl.includes('/api/city/runs/')) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      void Promise.allSettled(
        footprintPrefetchUrls(tileUrl, focusBounds).map(async (url) => {
          const response = await fetch(url, { cache: 'force-cache', signal: controller.signal })
          if (response.ok) await response.arrayBuffer()
        }),
      )
    }, 250)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [focusBounds, mapLoaded, tileUrl])

  return <div className="neighborhood-map city-map" data-map-ready={ready ? 'true' : 'false'} data-map-error={mapError}
    data-rendered-feature-count={renderedFeatureCount} data-map-zoom={cameraZoom?.toFixed(2) ?? ''}
    data-map-lod={mapLod}
    data-stock-bounds={bounds.join(',')} data-focus-bounds={focusBounds.join(',')}>
    <Map key={mapRevision} ref={mapRef} initialViewState={{
      bounds: [[focusBounds[0], focusBounds[1]], [focusBounds[2], focusBounds[3]]],
      fitBoundsOptions: { padding: 28, maxZoom: 12 },
    }}
      mapStyle={blankStyle} attributionControl={false} interactiveLayerIds={['city-fill', 'city-overview']}
      onLoad={() => setMapLoaded(true)} onSourceData={sourceData} onIdle={inspectRenderedFeatures}
      onMoveEnd={(event) => setCameraZoom(event.viewState.zoom)}
      onClick={click} onError={(event) => {
        const message = event.error?.message ?? 'MapLibre source error'
        if (!message.includes(TRANSIENT_SOURCE_ERROR)) setMapError(message)
      }} cursor="crosshair">
      <NavigationControl position="top-right" showCompass={false} />
      <ScaleControl position="bottom-right" unit="metric" />
      <Source id="city-stock" type="vector" tiles={[tileUrl]} minzoom={8} maxzoom={17}>
        <Layer {...overviewLayer(mode, clusters, districts)} />
        <Layer {...fillLayer(mode, clusters, districts)} />
        <Layer id="city-outline" type="line" source-layer="buildings" minzoom={11}
          paint={{ 'line-color': '#405049', 'line-width': ['interpolate', ['linear'], ['zoom'], 11, 0.18, 14, 0.36], 'line-opacity': ['interpolate', ['linear'], ['zoom'], 11, 0.38, 14, 0.62] }} />
        <Layer id="city-representatives" type="line" source-layer="buildings"
          minzoom={14}
          filter={['in', ['to-string', ['get', 'refparcela']], ['literal', representativeRefs]]}
          paint={{ 'line-color': '#e04f39', 'line-width': 2.2, 'line-opacity': 1 }} />
        <Layer id="city-overview-selected" type="circle" source-layer="overview" maxzoom={11}
          filter={selectedFilter} paint={{ 'circle-color': 'transparent', 'circle-radius': 2.1, 'circle-stroke-color': '#101b17', 'circle-stroke-width': 0.4, 'circle-stroke-opacity': 0.55 }} />
        <Layer id="city-selected" type="line" source-layer="buildings"
          minzoom={11} filter={selectedFilter}
          paint={{ 'line-color': '#101b17', 'line-width': ['interpolate', ['linear'], ['zoom'], 11, 0.28, 14, 2.4], 'line-opacity': ['interpolate', ['linear'], ['zoom'], 11, 0.52, 14, 1] }} />
      </Source>
      {mode === 'district' ? districts.filter((item) => item.longitude != null && item.latitude != null).map((item) =>
        <Marker key={item.nombre} longitude={item.longitude!} latitude={item.latitude!} anchor="center">
          <button className={selectedDistrict === item.nombre ? 'city-district-marker active' : 'city-district-marker'}
            onClick={(event) => { event.stopPropagation(); onSelectDistrict(item.nombre) }} aria-label={item.nombre}>
            <span>{item.nombre}</span><small>{item.cons_hc_gwh != null ? `${item.cons_hc_gwh.toFixed(1)} GWh HVAC` : item.heating_gwh != null ? `${item.heating_gwh.toFixed(1)} GWh` : item.n_buildings}</small>
          </button>
        </Marker>,
      ) : null}
    </Map>
    <div className="city-map-extent-controls" aria-label={t('city.mapExtent')}>
      <button type="button" onClick={() => fitExtent(focusBounds)}>{t('city.focusExtent')}</button>
      <button type="button" onClick={() => fitExtent(bounds)}>{t('city.fullExtent')}</button>
    </div>
    <div className="neighborhood-map-legend" aria-label={t('city.mapLegend')}>
      {mode === 'cluster' ? <><span><i style={{ background: '#3f756d' }} />BlocPluri</span><span><i style={{ background: '#d99b35' }} />EdiPluri</span><span><i style={{ background: '#a85850' }} />VivUni</span></>
        : mode === 'district' ? <><span>{t('city.lower')}</span><div className="energy-ramp district" /><span>{t('city.higher')}</span></>
          : <><span>0</span><div className="energy-ramp" /><span>kWh/m²</span></>}
      <span className="representative-key"><i />{t('city.representative')}</span>
    </div>
    {mapError ? <MapErrorNotice message={mapError} onRetry={retryMap} /> : null}
  </div>
}
