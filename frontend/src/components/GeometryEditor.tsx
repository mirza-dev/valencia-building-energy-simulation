import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CopyPlus, Grid3X3, Move3D, Plus, RectangleHorizontal, Ruler, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import ModelViewer from './ModelViewer'
import { suggestOpeningRect } from '../lib/geometryEditor'
import type { ModelEditPatch, SceneItem, SceneModel } from '../lib/types'

interface Props {
  scene: SceneModel
  mode: 'advanced' | 'guided'
  busy: boolean
  selectedId: string | null
  onSelect: (id: string) => void
  onApply: (patches: ModelEditPatch[]) => Promise<void>
}

type RectDraft = { u_min: string; u_max: string; v_min: string; v_max: string }
type MoveDraft = { dx: string; dy: string; dz: string }

function numberValue(value: string, fallback = 0): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function sceneType(item: SceneItem): string {
  return item.surface_type ?? item.subsurface_type ?? item.category
}

export default function GeometryEditor({ scene, mode, busy, selectedId, onSelect, onApply }: Props) {
  const { t } = useTranslation()
  const openings = scene.subsurfaces
  const exteriorWalls = useMemo(() => {
    const openingParents = new Set(openings.map((item) => item.parent_id))
    return scene.surfaces.filter((item) => item.surface_type === 'Wall' && item.boundary_condition === 'Outdoors')
      .sort((left, right) => {
        const residential = Number(!String(left.space_type ?? '').includes('Vivienda'))
          - Number(!String(right.space_type ?? '').includes('Vivienda'))
        return residential || Number(openingParents.has(left.id)) - Number(openingParents.has(right.id))
      })
  }, [openings, scene.surfaces])
  const allItems = useMemo(() => [...scene.surfaces, ...scene.subsurfaces, ...scene.shading], [scene])
  const selected = allItems.find((item) => item.id === selectedId) ?? exteriorWalls[0] ?? allItems[0] ?? null
  const selectedWall = selected?.category === 'surface' && selected.surface_type === 'Wall' ? selected : exteriorWalls[0] ?? null
  const selectedOpening = selected && (selected.category === 'window' || selected.category === 'door') ? selected : openings[0] ?? null
  const [rect, setRect] = useState<RectDraft>({ u_min: '0.30', u_max: '0.70', v_min: '0.25', v_max: '0.75' })
  const [openingType, setOpeningType] = useState('FixedWindow')
  const [wwr, setWwr] = useState('0.30')
  const [sill, setSill] = useState('0.90')
  const [overhangDepth, setOverhangDepth] = useState('0.60')
  const [overhangOffset, setOverhangOffset] = useState('0.10')
  const [snap, setSnap] = useState('0.25')
  const [move, setMove] = useState<MoveDraft>({ dx: '0', dy: '0', dz: '0' })
  const [storyHeight, setStoryHeight] = useState('3.0')

  useEffect(() => {
    if (!selectedId && selected) onSelect(selected.id)
  }, [onSelect, selected, selectedId])

  useEffect(() => {
    if (!selectedWall) return
    const suggested = suggestOpeningRect(selectedWall, openings)
    setRect(Object.fromEntries(
      Object.entries(suggested).map(([key, value]) => [key, value.toFixed(3)]),
    ) as RectDraft)
  }, [openings, selectedWall])

  const storyCandidates = useMemo(() => {
    const byId = new Map<string, { id: string; name: string; top: number }>()
    for (const surface of scene.surfaces) {
      if (!surface.story_id) continue
      const top = Math.max(...surface.vertices.map((vertex) => vertex[2]))
      const current = byId.get(surface.story_id)
      if (!current || top > current.top) byId.set(surface.story_id, { id: surface.story_id, name: surface.story ?? t('modelEditor.story'), top })
    }
    return [...byId.values()].sort((left, right) => left.top - right.top)
  }, [scene.surfaces, t])
  const topStory = storyCandidates.at(-1) ?? null

  const facadeWalls = useMemo(() => {
    if (!selectedWall || selectedWall.azimuth_deg == null) return selectedWall ? [selectedWall] : []
    return exteriorWalls.filter((wall) => wall.azimuth_deg != null && Math.abs(wall.azimuth_deg - selectedWall.azimuth_deg!) < 0.6)
  }, [exteriorWalls, selectedWall])

  const applyMove = (wall: SceneItem, delta: { dx: number; dy: number; dz: number }) => onApply([{
    op: 'surface.move', target_id: wall.id, payload: delta,
  }])

  return <div className="geometry-editor" data-testid="geometry-editor">
    <section className="geometry-editor-viewer">
      <ModelViewer scene={scene} selectedItemId={selected?.id ?? null} editMode={mode === 'advanced'}
        translationSnap={Math.max(0.01, numberValue(snap, 0.25))}
        onItemSelect={(item) => onSelect(item.id)}
        onTranslate={(item, delta) => { if (!busy) void applyMove(item, delta) }} />
    </section>

    <div className="geometry-ledger">
      <span><b>{scene.surfaces.length}</b> {t('modelEditor.surfaces')}</span>
      <span><b data-testid="geometry-opening-count">{openings.length}</b> {t('modelEditor.openings')}</span>
      <span><b>{storyCandidates.length}</b> {t('modelEditor.stories')}</span>
      <span><b>{scene.shading.filter((item) => item.category === 'overhang').length}</b> {t('modelEditor.overhangs')}</span>
    </div>

    <section className="geometry-control-grid">
      <div className="editor-form-stack geometry-card">
        <h4><RectangleHorizontal size={14} />{t('modelEditor.openingOverride')}</h4>
        <label className="editor-field"><span>{t('modelEditor.exteriorParentWall')}</span><select data-testid="geometry-wall-select" value={selectedWall?.id ?? ''} onChange={(event) => onSelect(event.target.value)}>
          {exteriorWalls.map((wall) => <option key={wall.id} value={wall.id}>{wall.name} · {wall.azimuth_deg?.toFixed(0) ?? '—'}°</option>)}
        </select></label>
        <div className="geometry-number-grid">
          {Object.entries(rect).map(([key, value]) => <label key={key}><span>{key.replace('_', ' ')}</span><input type="number" min="0.01" max="0.99" step="0.05" value={value} onChange={(event) => setRect((current) => ({ ...current, [key]: event.target.value }))} /></label>)}
        </div>
        <div className="editor-action-row"><label className="editor-field"><span>{t('modelEditor.openingType')}</span><select value={openingType} onChange={(event) => setOpeningType(event.target.value)}><option>FixedWindow</option><option>OperableWindow</option><option>GlassDoor</option><option>Door</option></select></label><button data-testid="geometry-create-opening" disabled={busy || !selectedWall} onClick={() => selectedWall && void onApply([{ op: 'subsurface.create', target_id: selectedWall.id, payload: { subsurface_type: openingType, normalized_rect: Object.fromEntries(Object.entries(rect).map(([key, value]) => [key, numberValue(value)])) } }])}><Plus size={13} />{t('modelEditor.drawOpening')}</button></div>
        <p className="editor-help">{t('modelEditor.openingNote')}</p>
      </div>

      <div className="editor-form-stack geometry-card">
        <h4><Ruler size={14} />{t('modelEditor.facadeWwrShade')}</h4>
        <div className="geometry-number-grid"><label><span>WWR</span><input type="number" min="0.01" max="0.94" step="0.05" value={wwr} onChange={(event) => setWwr(event.target.value)} /></label><label><span>{t('modelEditor.sill')} m</span><input type="number" min="0" step="0.1" value={sill} onChange={(event) => setSill(event.target.value)} /></label></div>
        <button data-testid="geometry-apply-wwr" disabled={busy || !facadeWalls.length} onClick={() => void onApply(facadeWalls.map((wall) => ({ op: 'surface.set_wwr', target_id: wall.id, payload: { ratio: numberValue(wwr), sill_m: numberValue(sill) } })))}><Grid3X3 size={13} />{t('modelEditor.applyFacadeWwr', { count: facadeWalls.length, azimuth: selectedWall?.azimuth_deg?.toFixed(0) ?? '—' })}</button>
        <label className="editor-field"><span>{t('modelEditor.openingForOverhang')}</span><select value={selectedOpening?.id ?? ''} onChange={(event) => onSelect(event.target.value)}>{openings.map((opening) => <option key={opening.id} value={opening.id}>{opening.name} · {sceneType(opening)}</option>)}</select></label>
        <div className="geometry-number-grid"><label><span>{t('modelEditor.depth')} m</span><input type="number" min="0.01" step="0.1" value={overhangDepth} onChange={(event) => setOverhangDepth(event.target.value)} /></label><label><span>{t('modelEditor.offset')} m</span><input type="number" step="0.1" value={overhangOffset} onChange={(event) => setOverhangOffset(event.target.value)} /></label></div>
        <div className="editor-action-row"><button disabled={busy || !selectedOpening} onClick={() => selectedOpening && void onApply([{ op: 'subsurface.add_overhang', target_id: selectedOpening.id, payload: { depth_m: numberValue(overhangDepth), offset_m: numberValue(overhangOffset) } }])}>{t('modelEditor.addOverhang')}</button><button className="danger-button compact" disabled={busy || !selectedOpening} onClick={() => selectedOpening && void onApply([{ op: 'subsurface.delete', target_id: selectedOpening.id, payload: {} }])}><Trash2 size={13} />{t('modelEditor.deleteOpening')}</button></div>
      </div>

      <div className="editor-form-stack geometry-card">
        <h4><CopyPlus size={14} />{t('modelEditor.addFloor')}</h4>
        <p className="editor-help auto-derived-note">{t('modelEditor.addFloorNote')}</p>
        <label className="editor-field"><span>{t('modelEditor.automaticSourceStory')}</span><input readOnly value={topStory ? `${topStory.name} · z ${topStory.top.toFixed(2)} m` : t('modelEditor.noOccupiedStory')} /></label>
        <label className="editor-field"><span>{t('modelEditor.floorHeight')} m</span><input type="number" min="0.5" step="0.1" value={storyHeight} onChange={(event) => setStoryHeight(event.target.value)} /></label>
        <button data-testid="geometry-add-floor" disabled={busy || !topStory} onClick={() => topStory && void onApply([{ op: 'space.duplicate_story', target_id: topStory.id, payload: { height_m: numberValue(storyHeight, 3), name: `${topStory.name} · authored +1` } }])}><CopyPlus size={13} />{t('modelEditor.duplicateTopStory')}</button>
      </div>

      {mode === 'advanced' ? <div className="editor-form-stack geometry-card geometry-advanced-card">
        <h4><Move3D size={14} />{t('modelEditor.advancedTopology')}</h4>
        <label className="editor-field"><span>{t('modelEditor.selectedSurface')}</span><select value={selected?.category === 'surface' ? selected.id : selectedWall?.id ?? ''} onChange={(event) => onSelect(event.target.value)}>{scene.surfaces.map((surface) => <option key={surface.id} value={surface.id}>{surface.name} · {surface.surface_type}</option>)}</select></label>
        <label className="editor-field"><span>{t('modelEditor.translationSnap')} m</span><input type="number" min="0.01" step="0.05" value={snap} onChange={(event) => setSnap(event.target.value)} /></label>
        <div className="geometry-number-grid">{Object.entries(move).map(([key, value]) => <label key={key}><span>{key} m</span><input type="number" step={snap} value={value} onChange={(event) => setMove((current) => ({ ...current, [key]: event.target.value }))} /></label>)}</div>
        <div className="editor-action-row"><button disabled={busy || !selectedWall} onClick={() => selectedWall && void applyMove(selectedWall, { dx: numberValue(move.dx), dy: numberValue(move.dy), dz: numberValue(move.dz) })}><Move3D size={13} />{t('modelEditor.translate')}</button><button className="danger-button compact" disabled={busy || !selectedWall} onClick={() => selectedWall && void onApply([{ op: 'surface.delete', target_id: selectedWall.id, payload: {} }])}><Trash2 size={13} />{t('modelEditor.deleteSurface')}</button></div>
        <p className="editor-help warn"><AlertTriangle size={13} />{t('modelEditor.topologyWarning')}</p>
      </div> : null}
    </section>
  </div>
}
