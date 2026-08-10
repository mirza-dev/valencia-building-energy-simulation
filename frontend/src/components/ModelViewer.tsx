import { useCallback, useEffect, useMemo, useRef, useState, type ComponentRef } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { Edges, OrbitControls, TransformControls } from '@react-three/drei'
import * as THREE from 'three'
import { Building2, Camera, Eye, EyeOff, Focus, Layers3, Navigation, RotateCcw, ScanLine, SlidersHorizontal, SquareChevronUp } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useFeedback } from './FeedbackProvider'
import { formatModelDisplayText } from '../lib/modelGraph'
import { deriveContextRoofs } from '../lib/sceneMassing'
import type { SceneItem, SceneModel } from '../lib/types'

const boundaryColors: Record<string, string> = {
  Outdoors: '#3a8f9d',
  Adiabatic: '#d24b60',
  Surface: '#6f9d67',
  Ground: '#956f4f',
  window: '#36c2d0',
  door: '#2865a8',
  context: '#99a39f',
  overhang: '#3f4946',
}

const constructionPalette = ['#2e7c86', '#d15a42', '#557a52', '#8b6aa5', '#b88438', '#476f9f', '#9b5b72']
type VisibilityCategory = 'exterior' | 'party' | 'roof' | 'floor' | 'window' | 'door' | 'overhang' | 'context'
type ViewMode = 'overview' | 'boundary' | 'construction'

function visibilityCategory(item: SceneItem): VisibilityCategory {
  if (item.category !== 'surface') return item.category
  if (item.surface_type === 'RoofCeiling') return 'roof'
  if (item.surface_type === 'Floor') return 'floor'
  if (item.surface_type === 'Wall' && item.boundary_condition === 'Adiabatic') return 'party'
  return 'exterior'
}

function hashColor(value: string) {
  let hash = 0
  for (const char of value) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0
  return constructionPalette[Math.abs(hash) % constructionPalette.length]
}

function overviewColor(item: SceneItem) {
  if (item.category === 'context') return item.context_role === 'roof' ? '#385d70' : '#a6b3b9'
  if (item.category === 'window') return '#76a7bb'
  if (item.category === 'door') return '#4f7f96'
  if (item.category === 'overhang') return '#8d733d'
  if (item.surface_type === 'RoofCeiling') return '#7f3f3d'
  if (item.surface_type === 'Floor') return '#8f7844'
  return '#c2a24f'
}

function polygonGeometry(vertices: number[][]): THREE.BufferGeometry {
  const geometry = new THREE.BufferGeometry()
  const vectors = vertices.map(([x, y, z]) => new THREE.Vector3(x, z, -y))
  if (vectors.length < 3) return geometry
  const normal = new THREE.Vector3().crossVectors(
    vectors[1].clone().sub(vectors[0]), vectors[2].clone().sub(vectors[0]),
  ).normalize()
  const axis = ['x', 'y', 'z'].reduce((best, key) =>
    Math.abs(normal[key as 'x' | 'y' | 'z']) > Math.abs(normal[best as 'x' | 'y' | 'z']) ? key : best, 'x')
  const points2d = vectors.map((point) => {
    if (axis === 'x') return new THREE.Vector2(point.y, point.z)
    if (axis === 'y') return new THREE.Vector2(point.x, point.z)
    return new THREE.Vector2(point.x, point.y)
  })
  const triangles = THREE.ShapeUtils.triangulateShape(points2d, [])
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(vectors.flatMap((point) => [point.x, point.y, point.z]), 3))
  geometry.setIndex(triangles.flatMap((triangle) => triangle))
  geometry.computeVertexNormals()
  return geometry
}

function SceneMesh({ item, center, mode, selected, clippingPlane, onSelect }: {
  item: SceneItem
  center: THREE.Vector3
  mode: ViewMode
  selected: boolean
  clippingPlane: THREE.Plane | null
  onSelect: (item: SceneItem) => void
}) {
  const geometry = useMemo(() => polygonGeometry(item.vertices), [item.vertices])
  const key = item.category === 'surface' ? item.boundary_condition ?? 'surface' : item.category
  const color = mode === 'overview'
    ? overviewColor(item)
    : mode === 'construction' && item.construction
      ? hashColor(item.construction)
      : boundaryColors[key] ?? '#74807b'
  const opacity = mode === 'overview'
    ? (item.category === 'context' ? (item.context_role === 'roof' ? 0.94 : 0.88) : 0.96)
    : item.category === 'context' ? 0.22
      : item.category === 'surface' && item.boundary_condition === 'Surface' ? 0.25 : 0.84
  const edgeColor = selected ? '#d49a20' : item.category === 'context' ? '#465861' : '#493f2d'
  return (
    <mesh
      geometry={geometry}
      position={[-center.x, -center.y, -center.z]}
      onClick={(event) => { event.stopPropagation(); onSelect(item) }}
      renderOrder={item.category === 'window' || item.category === 'door' ? 3 : 1}
      castShadow={item.category !== 'context'}
      receiveShadow
    >
      <meshStandardMaterial
        color={selected ? '#ffd45c' : color}
        transparent={opacity < 1}
        opacity={selected ? 1 : opacity}
        side={THREE.DoubleSide}
        roughness={0.74}
        metalness={0.02}
        polygonOffset
        polygonOffsetFactor={item.category === 'window' || item.category === 'door' ? -2 : 0}
        clippingPlanes={clippingPlane ? [clippingPlane] : []}
      />
      <Edges threshold={12} color={edgeColor} />
    </mesh>
  )
}

function SyncedControls({ syncKey, onTargetChange, resetSignal, topSignal, focusTarget, span, focusSpan }: {
  syncKey?: string
  onTargetChange?: (target: number[]) => void
  resetSignal: number
  topSignal: number
  focusTarget: number[] | null
  span: number
  focusSpan: number
}) {
  const { camera } = useThree()
  const controls = useRef<ComponentRef<typeof OrbitControls>>(null)
  const source = useRef(crypto.randomUUID())
  const receiving = useRef(false)
  useEffect(() => {
    if (!syncKey) return
    const eventName = `workbench-camera-${syncKey}`
    const listener = (raw: Event) => {
      const detail = (raw as CustomEvent<{ source: string; position: number[]; target: number[] }>).detail
      if (detail.source === source.current || !controls.current) return
      receiving.current = true
      camera.position.fromArray(detail.position)
      controls.current.target.fromArray(detail.target)
      controls.current.update()
      onTargetChange?.(detail.target)
      receiving.current = false
    }
    window.addEventListener(eventName, listener)
    return () => window.removeEventListener(eventName, listener)
  }, [camera, onTargetChange, syncKey])
  useEffect(() => {
    if (!controls.current) return
    camera.position.set(span * 0.92, span * 0.66, span * 0.92)
    controls.current.target.set(0, 0, 0)
    controls.current.update()
    onTargetChange?.([0, 0, 0])
  }, [camera, onTargetChange, resetSignal, span])
  useEffect(() => {
    if (!controls.current || topSignal === 0) return
    camera.position.set(0, span * 2.1, 0.01)
    controls.current.target.set(0, 0, 0)
    controls.current.update()
    onTargetChange?.([0, 0, 0])
  }, [camera, onTargetChange, span, topSignal])
  useEffect(() => {
    if (!controls.current || !focusTarget) return
    controls.current.target.fromArray(focusTarget)
    camera.position.set(focusTarget[0] + focusSpan * 0.9, focusTarget[1] + focusSpan * 0.62, focusTarget[2] + focusSpan * 0.9)
    controls.current.update()
    onTargetChange?.(focusTarget)
  }, [camera, focusSpan, focusTarget, onTargetChange])
  return <OrbitControls ref={controls} makeDefault target={[0, 0, 0]} maxPolarAngle={Math.PI / 2.02}
    minDistance={Math.max(3, focusSpan * 0.35)} maxDistance={span * 4}
    onChange={() => {
      if (!controls.current) return
      onTargetChange?.(controls.current.target.toArray())
      if (!syncKey || receiving.current) return
      window.dispatchEvent(new CustomEvent(`workbench-camera-${syncKey}`, { detail: {
        source: source.current, position: camera.position.toArray(), target: controls.current.target.toArray(),
      } }))
    }} />
}

function RenderReady({ onReady }: { onReady: () => void }) {
  const frames = useRef(0)
  useFrame(() => {
    frames.current += 1
    if (frames.current === 4) onReady()
  })
  return null
}

function TranslationHandle({ item, center, snap, onTranslate }: {
  item: SceneItem
  center: THREE.Vector3
  snap: number
  onTranslate: (item: SceneItem, delta: { dx: number; dy: number; dz: number }) => void
}) {
  const handle = useRef<THREE.Mesh>(null)
  const origin = useMemo(() => {
    const count = item.vertices.length
    const x = item.vertices.reduce((sum, vertex) => sum + vertex[0], 0) / count - center.x
    const y = item.vertices.reduce((sum, vertex) => sum + vertex[2], 0) / count - center.y
    const z = -(item.vertices.reduce((sum, vertex) => sum + vertex[1], 0) / count) - center.z
    return new THREE.Vector3(x, y, z)
  }, [center, item])
  useEffect(() => { handle.current?.position.copy(origin) }, [origin])
  const commit = () => {
    if (!handle.current) return
    const delta = handle.current.position.clone().sub(origin)
    handle.current.position.copy(origin)
    if (delta.lengthSq() < 1e-10) return
    onTranslate(item, { dx: delta.x, dy: -delta.z, dz: delta.y })
  }
  return <TransformControls mode="translate" translationSnap={snap} onMouseUp={commit}>
    <mesh ref={handle} position={origin} renderOrder={10}>
      <sphereGeometry args={[0.22, 16, 16]} />
      <meshStandardMaterial color="#f0b94f" depthTest={false} />
    </mesh>
  </TransformControls>
}

export default function ModelViewer({ scene, compact = false, syncKey, onCapture, selectedConstructionId, selectedItemId, editMode = false, translationSnap = 0.25, onTranslate, onItemSelect }: {
  scene: SceneModel
  compact?: boolean
  syncKey?: string
  onCapture?: (viewState: Record<string, unknown>, pngDataUrl: string) => void
  selectedConstructionId?: string | null
  selectedItemId?: string | null
  editMode?: boolean
  translationSnap?: number
  onTranslate?: (item: SceneItem, delta: { dx: number; dy: number; dz: number }) => void
  onItemSelect?: (item: SceneItem) => void
}) {
  const { t, i18n } = useTranslation()
  const { notify } = useFeedback()
  const [mode, setMode] = useState<ViewMode>('overview')
  const [selected, setSelected] = useState<SceneItem | null>(null)
  const [categories, setCategories] = useState<Record<VisibilityCategory, boolean>>({
    exterior: true, party: true, roof: true, floor: true,
    window: true, door: true, context: true, overhang: true,
  })
  const [storyVisibility, setStoryVisibility] = useState<Record<string, boolean>>({})
  const [cutHeight, setCutHeight] = useState<number | null>(null)
  const [renderReady, setRenderReady] = useState(false)
  const [resetSignal, setResetSignal] = useState(0)
  const [topSignal, setTopSignal] = useState(0)
  const [focusTarget, setFocusTarget] = useState<number[] | null>(null)
  const renderer = useRef<THREE.WebGLRenderer | null>(null)
  const camera = useRef<THREE.Camera | null>(null)
  const orbitTarget = useRef<number[]>([0, 0, 0])
  const handleTargetChange = useCallback((target: number[]) => { orbitTarget.current = target }, [])
  const selectItem = useCallback((item: SceneItem) => {
    setSelected(item)
    onItemSelect?.(item)
  }, [onItemSelect])

  const contextRoofs = useMemo(() => deriveContextRoofs(scene.shading), [scene.shading])
  const all = useMemo(() => [...scene.surfaces, ...scene.subsurfaces, ...scene.shading, ...contextRoofs], [contextRoofs, scene])
  const activeSelected = selectedItemId !== undefined ? all.find((item) => item.id === selectedItemId) ?? null : selected
  useEffect(() => {
    if (selectedItemId === undefined && selected) setSelected(all.find((item) => item.id === selected.id) ?? null)
  }, [all, selected, selectedItemId])
  useEffect(() => setRenderReady(false), [scene])
  const buildingPoints = useMemo(() => scene.surfaces.flatMap((item) => item.vertices), [scene])
  const sitePoints = useMemo(() => [
    ...buildingPoints,
    ...scene.shading.filter((item) => item.category === 'context').flatMap((item) => item.vertices),
  ], [buildingPoints, scene.shading])
  const dimensions = useMemo(() => {
    const xs = buildingPoints.map((p) => p[0])
    const ys = buildingPoints.map((p) => p[1])
    const zs = buildingPoints.map((p) => p[2])
    const maxZ = Math.max(...zs)
    const centerX = (Math.min(...xs) + Math.max(...xs)) / 2
    const centerY = (Math.min(...ys) + Math.max(...ys)) / 2
    const targetSpan = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), maxZ)
    const siteRadius = Math.max(...sitePoints.flatMap((point) => [Math.abs(point[0] - centerX), Math.abs(point[1] - centerY)]))
    return {
      center: new THREE.Vector3(centerX, maxZ / 2, -centerY),
      maxZ,
      targetSpan,
      span: Math.max(targetSpan, siteRadius * 2) * 1.06,
    }
  }, [buildingPoints, sitePoints])
  const stories = useMemo(() => Array.from(new Set(
    [...scene.surfaces, ...scene.subsurfaces].map((item) => item.story).filter((item): item is string => Boolean(item)),
  )).sort(), [scene])
  useEffect(() => {
    setStoryVisibility(Object.fromEntries(stories.map((story) => [story, true])))
  }, [stories])
  const clippingPlane = useMemo(() => cutHeight == null ? null : new THREE.Plane(
    new THREE.Vector3(0, 1, 0), -(cutHeight - dimensions.maxZ / 2),
  ), [cutHeight, dimensions.maxZ])
  const visible = all.filter((item) => {
    if (!categories[visibilityCategory(item)]) return false
    if (item.story && storyVisibility[item.story] === false) return false
    return true
  })
  const selectedFacade = activeSelected?.azimuth_deg == null || !scene.facade_qa.length ? null : scene.facade_qa.reduce((best, facade) => (
    Math.abs(facade.azimut - activeSelected.azimuth_deg!) < Math.abs(best.azimut - activeSelected.azimuth_deg!) ? facade : best
  ), scene.facade_qa[0])
  const focusSelected = () => {
    if (!activeSelected?.vertices.length) return
    const count = activeSelected.vertices.length
    const x = activeSelected.vertices.reduce((sum, vertex) => sum + vertex[0], 0) / count - dimensions.center.x
    const y = activeSelected.vertices.reduce((sum, vertex) => sum + vertex[2], 0) / count - dimensions.center.y
    const z = -(activeSelected.vertices.reduce((sum, vertex) => sum + vertex[1], 0) / count) - dimensions.center.z
    setFocusTarget([x, y, z])
  }

  return (
    <div className={`model-viewer ${compact ? 'compact' : ''}`} data-sync-key={syncKey} data-render-ready={renderReady}
      data-view-mode={mode} data-context-roofs={contextRoofs.length}
      data-selected-construction={selectedConstructionId ?? ''} data-selected-item={activeSelected?.id ?? ''} data-edit-mode={editMode}>
      <Canvas gl={{ preserveDrawingBuffer: true }} camera={{ position: [dimensions.span * 0.92, dimensions.span * 0.66, dimensions.span * 0.92], fov: 42, near: 0.1, far: 1000 }}
        onCreated={(state) => { state.gl.localClippingEnabled = true; renderer.current = state.gl; camera.current = state.camera }}>
        <color attach="background" args={['#fbfcfb']} />
        <hemisphereLight args={['#ffffff', '#c8d0cb', 1.55]} />
        <ambientLight intensity={1.2} />
        <directionalLight position={[dimensions.span * 0.6, dimensions.span, dimensions.span * 0.45]} intensity={1.85} />
        <group rotation={[0, 0, 0]}>
          {visible.map((item) => (
            <SceneMesh key={item.id} item={item} center={dimensions.center} mode={mode}
              selected={activeSelected?.id === item.id || Boolean(selectedConstructionId && item.construction_id === selectedConstructionId)}
              clippingPlane={clippingPlane} onSelect={selectItem} />
          ))}
        </group>
        {editMode && activeSelected?.category === 'surface' && onTranslate
          ? <TranslationHandle item={activeSelected} center={dimensions.center} snap={translationSnap} onTranslate={onTranslate} /> : null}
        <SyncedControls syncKey={syncKey} onTargetChange={handleTargetChange}
          resetSignal={resetSignal} topSignal={topSignal} focusTarget={focusTarget} span={dimensions.span} focusSpan={dimensions.targetSpan} />
        <RenderReady onReady={() => setRenderReady(true)} />
      </Canvas>

      {!renderReady ? <div className="viewer-render-loading"><span className="spinner" />{t('viewer.rendering')}</div> : null}

      <div className="viewer-toolbar">
        <div className="segmented-control">
          <button className={mode === 'overview' ? 'active' : ''} onClick={() => setMode('overview')} title={t('viewer.overview')} aria-pressed={mode === 'overview'}>
            <Building2 size={15} /> {t('viewer.overview')}
          </button>
          <button className={mode === 'boundary' ? 'active' : ''} onClick={() => setMode('boundary')} title={t('viewer.boundary')} aria-pressed={mode === 'boundary'}>
            <ScanLine size={15} /> {t('viewer.boundary')}
          </button>
          <button className={mode === 'construction' ? 'active' : ''} onClick={() => setMode('construction')} title={t('viewer.construction')} aria-pressed={mode === 'construction'}>
            <Layers3 size={15} /> {t('viewer.construction')}
          </button>
        </div>
        <button className="viewer-toggle" title={t('viewer.reset')} aria-label={t('viewer.reset')} onClick={() => { setFocusTarget(null); setResetSignal((value) => value + 1) }}><RotateCcw size={15} />{!compact ? t('viewer.reset') : null}</button>
        <button className="viewer-toggle" title={t('viewer.top')} aria-label={t('viewer.top')} onClick={() => { setFocusTarget(null); setTopSignal((value) => value + 1) }}><SquareChevronUp size={15} />{!compact ? t('viewer.top') : null}</button>
        {!compact && onCapture ? <button className="viewer-toggle active" title={t('viewer.store')}
          onClick={() => {
            if (!renderer.current || !camera.current) return
            onCapture({ mode, cutHeight, categories, storyVisibility,
              camera: { position: camera.current.position.toArray(), rotation: camera.current.rotation.toArray(), target: orbitTarget.current } },
            renderer.current.domElement.toDataURL('image/png'))
            notify(t('viewer.viewStored'), 'success')
          }}><Camera size={15} /> {t('viewer.store')}</button> : null}
      </div>

      <div className="viewer-layer-control" aria-label={t('viewer.categories')}>
        {(['exterior', 'party', 'roof', 'floor', 'window', 'door', 'overhang', 'context'] as const).map((category) => (
          <button key={category} className={categories[category] ? 'active' : ''}
            onClick={() => setCategories((current) => ({ ...current, [category]: !current[category] }))} title={t(`viewer.${category}`)} aria-pressed={categories[category]}>
            {categories[category] ? <Eye size={14} /> : <EyeOff size={14} />} {t(`viewer.${category}`)}
          </button>
        ))}
      </div>

      {!compact && stories.length ? <div className="viewer-story-control">
        <span><SlidersHorizontal size={13} /> {t('viewer.stories')}</span>
        {stories.map((story) => <label key={story} title={formatModelDisplayText(story, i18n.language)}><input type="checkbox" checked={storyVisibility[story] ?? true}
          onChange={(event) => setStoryVisibility((current) => ({ ...current, [story]: event.target.checked }))} />{formatModelDisplayText(story, i18n.language)}</label>)}
      </div> : null}

      {!compact ? (
        <div className="viewer-cut-control">
          <label htmlFor="cut-height">{t('viewer.section')} <code>{cutHeight == null ? t('viewer.all') : `${cutHeight.toFixed(1)} m`}</code></label>
          <input id="cut-height" type="range" min="0" max={dimensions.maxZ + 0.1} step="3"
            value={cutHeight ?? dimensions.maxZ + 0.1}
            onChange={(event) => {
              const value = Number(event.target.value)
              setCutHeight(value > dimensions.maxZ ? null : value)
            }} />
        </div>
      ) : null}

      {activeSelected && !compact ? (
        <aside className="surface-inspector">
          {selectedItemId === undefined ? <button className="surface-close" onClick={() => setSelected(null)} aria-label={t('viewer.closeInspector')}>×</button> : null}
          <span className="eyebrow">{t('viewer.selected')}</span>
          <strong title={activeSelected.name}>{formatModelDisplayText(activeSelected.name, i18n.language)}</strong>
          <button className="focus-surface-button" onClick={focusSelected}><Focus size={14} />{t('viewer.focus')}</button>
          <dl>
            <div><dt>{t('viewer.type')}</dt><dd>{activeSelected.surface_type ?? activeSelected.subsurface_type ?? activeSelected.category}</dd></div>
            <div><dt>{t('viewer.boundaryLabel')}</dt><dd>{activeSelected.boundary_condition ?? '—'}</dd></div>
            <div><dt>{t('viewer.area')}</dt><dd>{activeSelected.area_m2.toFixed(2)} m²</dd></div>
            <div><dt>{t('viewer.azimuth')}</dt><dd>{activeSelected.azimuth_deg == null ? '—' : `${activeSelected.azimuth_deg.toFixed(1)}°`}</dd></div>
            <div><dt>{t('viewer.construction')}</dt><dd>{activeSelected.construction ?? '—'}</dd></div>
            <div><dt>{t('viewer.zone')}</dt><dd>{activeSelected.zone ?? '—'}</dd></div>
            <div><dt>{t('viewer.storySpace')}</dt><dd title={`${activeSelected.story ?? '—'} / ${activeSelected.space ?? '—'}`}>{formatModelDisplayText(activeSelected.story ?? '—', i18n.language)} / {formatModelDisplayText(activeSelected.space ?? '—', i18n.language)}</dd></div>
            <div><dt>{t('viewer.facadeWwr')}</dt><dd>{selectedFacade ? `${selectedFacade.wwr_real} (target ${selectedFacade.wwr_target})` : '—'}</dd></div>
          </dl>
        </aside>
      ) : null}
      <div className="north-indicator" title={t('viewer.northAxis', { value: scene.north_axis_deg })}><Navigation size={17} style={{ transform: `rotate(${scene.north_axis_deg}deg)` }} /><strong>{t('viewer.north')}</strong></div>
      <div className="viewer-scale">{t('viewer.scale', { value: Math.max(1, Math.round(dimensions.span)) })}</div>
    </div>
  )
}
