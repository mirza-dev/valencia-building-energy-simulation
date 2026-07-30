import { useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Box, BrickWall, CalendarClock, Edit3, Fan, Gauge, Layers3, Move3D, Play, Search, ShieldCheck, SlidersHorizontal, Trash2, UsersRound } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import PageHeader from './PageHeader'
import ModelViewer from './ModelViewer'
import ModelEditWorkspace from './ModelEditWorkspace'
import { api } from '../lib/api'
import { modelEditStorageKey, parseStoredModelEditSession } from '../lib/modelEditor'
import { compactValue, formatModelDisplayText, humanizeModelKey, matchesGraphSearch } from '../lib/modelGraph'
import type { ModelConstruction, ModelEditPatch, ModelEditSessionResponse, ModelGraph, ModelGraphObject, ModelMaterial, ModelPreflight, ModelProjectParameter, ModelSchedule, RunRecord, SceneItem } from '../lib/types'

type InspectorTab = 'projectParameters' | 'geometry' | 'constructions' | 'materials' | 'schedules' | 'spaceTypes' | 'zones' | 'hvac' | 'simulation'

const tabs: Array<{ id: InspectorTab; icon: typeof BrickWall }> = [
  { id: 'projectParameters', icon: SlidersHorizontal },
  { id: 'geometry', icon: Move3D },
  { id: 'constructions', icon: BrickWall },
  { id: 'materials', icon: Layers3 },
  { id: 'schedules', icon: CalendarClock },
  { id: 'spaceTypes', icon: UsersRound },
  { id: 'zones', icon: Box },
  { id: 'hvac', icon: Fan },
  { id: 'simulation', icon: Gauge },
]

function EvidenceRows({ values }: { values: Record<string, unknown> }) {
  const { t, i18n } = useTranslation()
  return <dl className="model-evidence-rows">{Object.entries(values).map(([key, value]) => (
    <div key={key}><dt>{t(`modelInspector.evidence.${key}`, { defaultValue: humanizeModelKey(key) })}</dt><dd title={compactValue(value)}>{Array.isArray(value)
      ? t('modelInspector.itemCount', { count: value.length })
      : formatModelDisplayText(compactValue(value), i18n.language)}</dd></div>
  ))}</dl>
}

function ObjectCard({ item, active, onClick, children }: {
  item: ModelGraphObject
  active?: boolean
  onClick?: () => void
  children?: ReactNode
}) {
  const { i18n } = useTranslation()
  const rawName = item.name || item.type
  const content = <><header><span><strong title={rawName}>{formatModelDisplayText(rawName, i18n.language)}</strong><small title={item.type}>{item.type}</small></span><code>{item.id.slice(1, 9)}</code></header>{children}</>
  return onClick
    ? <button className={`model-object-card ${active ? 'active' : ''}`} onClick={onClick}>{content}</button>
    : <article className="model-object-card">{content}</article>
}

function ConstructionList({ items, selected, onSelect }: {
  items: ModelConstruction[]
  selected: string | null
  onSelect: (id: string) => void
}) {
  const { t } = useTranslation()
  return <div className="model-object-list">{items.map((item) => <ObjectCard key={item.id} item={item} active={selected === item.id} onClick={() => onSelect(item.id)}>
    <div className="construction-metrics"><span><b>{item.u_factor_w_m2k == null ? '—' : item.u_factor_w_m2k.toFixed(3)}</b>W/m²K</span><span><b>{item.layers.length}</b>{t('modelInspector.layers')}</span><span><b>{item.surface_usage.surface_count}</b>{t('modelInspector.usedSurfaces')}</span><span><b>{item.surface_usage.area_m2.toFixed(1)}</b>m²</span></div>
    <div className="layer-strip">{item.layers.map((layer, index) => <span key={`${layer.id}-${index}`} title={layer.name}>{index + 1}<em>{layer.name}</em></span>)}</div>
  </ObjectCard>)}</div>
}

function MaterialList({ items }: { items: ModelMaterial[] }) {
  return <div className="model-object-list">{items.map((item) => <ObjectCard key={item.id} item={item}>
    <EvidenceRows values={item.properties} />
  </ObjectCard>)}</div>
}

function ScheduleList({ items }: { items: ModelSchedule[] }) {
  const { t } = useTranslation()
  return <div className="model-object-list">{items.map((item) => <ObjectCard key={item.id} item={item}>
    <div className="schedule-summary"><span>{t('modelInspector.ruleCount', { count: item.rules.length })}</span><span>{t('modelInspector.profileCount', { count: item.profiles.length })}</span><span>{item.type_limits?.unit_type ?? t('modelInspector.unitless')}</span></div>
    {item.profiles[0]?.points.length ? <div className="schedule-points">{item.profiles[0].points.slice(0, 8).map((point) => <span key={`${point.hour}-${point.value}`}><i style={{ height: `${Math.max(4, Math.min(30, Math.abs(point.value)))}px` }} /><code>{point.hour}h</code><b>{point.value}</b></span>)}</div> : null}
  </ObjectCard>)}</div>
}

function GenericList({ items }: { items: ModelGraphObject[] }) {
  return <div className="model-object-list">{items.map((item) => <ObjectCard key={item.id} item={item}>
    <EvidenceRows values={Object.fromEntries(Object.entries(item).filter(([key]) => !['id', 'name', 'type'].includes(key)))} />
  </ObjectCard>)}</div>
}

function ProjectParameterList({ items, context }: {
  items: ModelProjectParameter[]
  context: ModelGraph['project_parameter_context']
}) {
  const { t } = useTranslation()
  return <div className="project-parameter-view">
    <div className="project-reference-strip">
      <span><small>{t('modelInspector.template')}</small><code>{context.base_template.split('/').at(-1)}</code></span>
      <span><small>{t('modelInspector.baseline')}</small><b>{context.reference_baseline_kwh_m2.heating} / {context.reference_baseline_kwh_m2.cooling}</b><em>kWh/m²</em></span>
      <span><small>{t('modelInspector.cadastre')}</small><b>{context.cadastre_reference_kwh_m2.demanda_ca} / {context.cadastre_reference_kwh_m2.demanda__1}</b><em>kWh/m²</em></span>
    </div>
    <p className="project-reference-note">{t('modelInspector.cadastrePending')}</p>
    <div className="model-object-list">{items.map((item) => <article className="project-parameter-card" data-parameter-key={item.key} key={item.id}>
      <header><span><strong title={item.key}>{t(`modelInspector.parameter.${item.key}`, { defaultValue: humanizeModelKey(item.key) })}</strong><small title={item.binding.object_type ?? undefined}>{item.binding.status === 'run_setting' ? t('modelInspector.postProcessing') : item.binding.object_type}</small></span><b>{t('modelInspector.readOnly')}</b></header>
      <div className="project-parameter-value"><span>{t('modelInspector.currentValue')}</span><strong>{compactValue(item.current_value)}</strong>{item.unit ? <em>{item.unit}</em> : null}</div>
      <div className="project-parameter-band"><span>{t('modelInspector.lhsWarnBand')}</span><code>{item.warn_bounds ? `${item.warn_bounds.minimum} – ${item.warn_bounds.maximum} ${item.unit ?? ''}` : 'ON / OFF'}</code></div>
      <div className="project-parameter-binding"><span>{t('modelInspector.binding')}</span><strong>{item.binding.object_name ?? t('modelInspector.postProcessing')}</strong><code>{item.binding.property_path}</code>{item.binding.object_id ? <small>{item.binding.object_id}</small> : null}</div>
      {Object.keys(item.evidence).length ? <EvidenceRows values={item.evidence} /> : null}
    </article>)}</div>
  </div>
}

export default function ModelInspectorPage() {
  const { modelId = '' } = useParams()
  const { t } = useTranslation()
  const [tab, setTab] = useState<InspectorTab>('projectParameters')
  const [search, setSearch] = useState('')
  const [selectedConstruction, setSelectedConstruction] = useState<string | null>(null)
  const [selectedObject, setSelectedObject] = useState<string | null>(null)
  const [session, setSession] = useState<ModelEditSessionResponse | null>(null)
  const [credentials, setCredentials] = useState<{ sessionId: string; token: string } | null>(null)
  const [editorMode, setEditorMode] = useState<'advanced' | 'guided'>('advanced')
  const [editorBusy, setEditorBusy] = useState(false)
  const [editorError, setEditorError] = useState<string | null>(null)
  const [preflight, setPreflight] = useState<ModelPreflight | null>(null)
  const [scenarioName, setScenarioName] = useState(() => t('modelInspector.defaultScenario'))
  const [committedRun, setCommittedRun] = useState<RunRecord | null>(null)
  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities, staleTime: 5_000 })
  const response = useQuery({ queryKey: ['model-graph', modelId], queryFn: () => api.modelGraph(modelId), enabled: Boolean(modelId) })
  const scene = useQuery({ queryKey: ['model-scene', modelId], queryFn: () => api.modelScene(modelId), enabled: Boolean(modelId) })
  const editorOptions = useQuery({ queryKey: ['model-editor-options'], queryFn: api.modelEditorOptions, enabled: capabilities.data?.capabilities.model_editor?.runtime_ready === true, staleTime: 60_000 })
  const graph = session?.graph ?? response.data?.graph
  const activeScene = session?.scene ?? scene.data

  useEffect(() => {
    if (!modelId || credentials || session) return
    const stored = parseStoredModelEditSession(sessionStorage.getItem(modelEditStorageKey(modelId)))
    if (!stored) return
    setEditorBusy(true)
    api.recoverModelEditSession(stored.sessionId, stored.token).then((recovered) => {
      setCredentials(stored)
      setSession(recovered)
    }).catch(() => sessionStorage.removeItem(modelEditStorageKey(modelId))).finally(() => setEditorBusy(false))
  }, [credentials, modelId, session])

  const startEditor = async () => {
    setEditorBusy(true); setEditorError(null); setCommittedRun(null)
    try {
      const created = await api.createModelEditSession(modelId)
      if (!created.token) throw new Error(t('modelInspector.sessionTokenMissing'))
      const next = { sessionId: created.session.id, token: created.token }
      sessionStorage.setItem(modelEditStorageKey(modelId), JSON.stringify(next))
      setCredentials(next); setSession(created); setPreflight(created.preflight); setEditorMode('advanced')
    } catch (error) { setEditorError(error instanceof Error ? error.message : String(error)) } finally { setEditorBusy(false) }
  }

  const applyEdits = async (patches: ModelEditPatch[]) => {
    if (!credentials) return
    setEditorBusy(true); setEditorError(null); setPreflight(null)
    try { setSession(await api.applyModelEdits(credentials.sessionId, credentials.token, patches)) }
    catch (error) { setEditorError(error instanceof Error ? error.message : String(error)) } finally { setEditorBusy(false) }
  }

  const uploadMeasure = async (file: File, trusted: boolean) => {
    if (!credentials) return
    setEditorBusy(true); setEditorError(null)
    try { setSession(await api.uploadModelMeasure(credentials.sessionId, credentials.token, file, trusted)) }
    catch (error) { setEditorError(error instanceof Error ? error.message : String(error)) } finally { setEditorBusy(false) }
  }

  const runPreflight = async () => {
    if (!credentials) return
    setEditorBusy(true); setEditorError(null)
    try { setPreflight(await api.preflightModelEdit(credentials.sessionId, credentials.token)) }
    catch (error) { setEditorError(error instanceof Error ? error.message : String(error)) } finally { setEditorBusy(false) }
  }

  const discardEditor = async () => {
    if (!credentials) return
    setEditorBusy(true); setEditorError(null)
    try {
      await api.discardModelEdit(credentials.sessionId, credentials.token)
      sessionStorage.removeItem(modelEditStorageKey(modelId)); setCredentials(null); setSession(null); setPreflight(null)
    } catch (error) { setEditorError(error instanceof Error ? error.message : String(error)) } finally { setEditorBusy(false) }
  }

  const commitEditor = async () => {
    if (!credentials) return
    setEditorBusy(true); setEditorError(null)
    try {
      const run = await api.commitModelEdit(credentials.sessionId, credentials.token, scenarioName)
      sessionStorage.removeItem(modelEditStorageKey(modelId)); setCredentials(null); setSession(null); setPreflight(null); setCommittedRun(run)
    } catch (error) { setEditorError(error instanceof Error ? error.message : String(error)) } finally { setEditorBusy(false) }
  }

  const collections = useMemo(() => {
    if (!graph) return { projectParameters: [], geometry: [], constructions: [], materials: [], schedules: [], spaceTypes: [], zones: [], hvac: [], simulation: [] } as Record<InspectorTab, ModelGraphObject[]>
    const geometry = activeScene ? [...activeScene.surfaces, ...activeScene.subsurfaces, ...activeScene.shading].map((item) => ({
      ...item,
      type: item.surface_type ? `OS:Surface · ${item.surface_type}` : item.subsurface_type ? `OS:SubSurface · ${item.subsurface_type}` : `OS:ShadingSurface · ${item.category}`,
    })) : []
    return {
      projectParameters: graph.project_parameters,
      geometry,
      constructions: graph.constructions,
      materials: graph.materials,
      schedules: graph.schedules,
      spaceTypes: graph.space_types,
      zones: [...graph.zones, ...graph.spaces],
      hvac: [...graph.hvac.zone_equipment, ...graph.hvac.air_loops, ...graph.hvac.plant_loops],
      simulation: [
        {
          id: 'simulation-settings', name: t('modelInspector.settings'), type: 'Simulation Settings',
          timesteps_per_hour: graph.simulation.timesteps_per_hour,
          simulation_control: graph.simulation.simulation_control,
          sizing: graph.simulation.sizing,
        },
        graph.simulation.run_period,
        ...graph.simulation.design_days,
        ...graph.simulation.output_variables,
      ],
    }
  }, [activeScene, graph, t])
  const filtered = useMemo(() => collections[tab].filter((item) => matchesGraphSearch(item, search)), [collections, search, tab])
  const selectTab = (next: InspectorTab) => {
    setTab(next)
    setSelectedObject(null)
  }
  const moveTabFocus = (event: KeyboardEvent<HTMLButtonElement>, current: InspectorTab) => {
    const currentIndex = tabs.findIndex((candidate) => candidate.id === current)
    let nextIndex: number | null = null
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length
    if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = tabs.length - 1
    if (nextIndex == null) return
    event.preventDefault()
    const next = tabs[nextIndex].id
    selectTab(next)
    event.currentTarget.parentElement?.querySelector<HTMLButtonElement>(`[data-model-tab="${next}"]`)?.focus()
  }
  const onSurfaceSelect = (item: SceneItem) => {
    if (tab === 'geometry') {
      setSelectedObject(item.id)
      return
    }
    if (!item.construction_id) return
    setSelectedConstruction(item.construction_id)
    setSelectedObject(item.construction_id)
    setTab('constructions')
  }

  if (capabilities.data && !capabilities.data.capabilities.model_editor?.runtime_ready) {
    return <div className="page"><div className="diagnostic-panel"><span className="eyebrow">E1 / CAPABILITY</span><h2>{t('modelInspector.blocked')}</h2><p>{capabilities.data.capabilities.model_editor?.diagnostic.reason}</p></div></div>
  }

  return <div className="page model-inspector-page">
    <PageHeader eyebrow={t('modelInspector.eyebrow')} title={t('modelInspector.title')} subtitle={t('modelInspector.subtitle')}
      actions={<><Link className="secondary-button" to={`/runs?run=${encodeURIComponent(response.data?.model.requested_id ?? modelId)}`}><ArrowLeft size={15} />{t('modelInspector.back')}</Link>{!session ? <button data-testid="start-model-editor" className="primary-button" disabled={editorBusy || !response.data} onClick={() => void startEditor()}><Edit3 size={15} />{editorBusy ? t('modelInspector.startingOverride') : t('modelInspector.startOverride')}</button> : null}</>} />
    <div className="model-inspector-meta">
      <span><ShieldCheck size={14} />{response.data?.model.verification_status ?? t('common.checking')}</span>
      <span>{response.data?.model.immutable ? t('modelInspector.immutable') : t('modelInspector.preview')}</span>
      <code>{response.data?.model.refparcela ?? '—'}</code>
      <code>OS {graph?.openstudio_version ?? '—'}</code>
      <code>GRAPH {graph?.graph_sha256.slice(0, 12) ?? '—'}</code>
      {session ? <code className="authored-badge">SESSION · {session.session.patch_count} PATCH</code> : null}
    </div>
    {editorError ? <div className="model-editor-error error-message" role="alert">{editorError}</div> : null}
    {committedRun ? <div className="model-editor-success"><ShieldCheck size={15} /><span><b>{t('modelInspector.committed')}</b><small>{committedRun.id} · {t('modelInspector.authoredFrom')} {committedRun.authored_from}</small></span><Link to={`/model/${committedRun.id}`}>{t('modelInspector.openAuthored')}</Link><Link to={`/simulation?model=${committedRun.id}`}><Play size={13} />{t('modelInspector.runPartB')}</Link></div> : null}
    <div className="model-inspector-layout">
      <section className="model-inspector-stage">
        {session && graph && activeScene && editorOptions.data ? <ModelEditWorkspace graph={graph} scene={activeScene} options={editorOptions.data} measures={session.measures} activeTab={tab} selectedId={selectedObject} mode={editorMode} busy={editorBusy} reports={session.reports} runSettings={session.run_settings} onModeChange={setEditorMode} onSelect={(id) => { setSelectedObject(id); if (tab === 'constructions') setSelectedConstruction(id) }} onApply={applyEdits} onUploadMeasure={uploadMeasure} />
          : activeScene ? <ModelViewer scene={activeScene} selectedConstructionId={selectedConstruction} selectedItemId={tab === 'geometry' ? selectedObject : undefined} onItemSelect={onSurfaceSelect} />
          : <div className="model-empty">{scene.isLoading ? <><span className="spinner" />{t('common.loading')}</> : scene.error instanceof Error ? scene.error.message : t('modelInspector.noScene')}</div>}
      </section>
      <aside className="model-graph-panel">
        <div className="model-graph-toolbar">
          <label className="search-field"><Search size={14} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t('modelInspector.search')} /></label>
          <span><b>{filtered.length}</b> / {collections[tab].length}</span>
        </div>
        <div className="model-graph-tabs" role="tablist" aria-label={t('modelInspector.tabs')}>
          {tabs.map(({ id, icon: Icon }) => <button key={id} id={`model-graph-tab-${id}`} data-model-tab={id} role="tab" aria-selected={tab === id} aria-controls="model-graph-tabpanel" tabIndex={tab === id ? 0 : -1} className={tab === id ? 'active' : ''} onClick={() => selectTab(id)} onKeyDown={(event) => moveTabFocus(event, id)}><Icon size={14} /><span>{t(`modelInspector.tab.${id}`)}</span><b>{collections[id].length}</b></button>)}
        </div>
        <div className="model-graph-scroll" id="model-graph-tabpanel" role="tabpanel" aria-labelledby={`model-graph-tab-${tab}`} tabIndex={0}>
          {response.isLoading ? <div className="page-loading"><span className="spinner" />{t('common.loading')}</div> : null}
          {response.error instanceof Error ? <div className="error-message" role="alert">{response.error.message}</div> : null}
          {!response.isLoading && !response.error && !filtered.length ? <div className="empty-state"><Search size={20} /><p>{t('modelInspector.noResults')}</p></div> : null}
          {tab === 'constructions' ? <ConstructionList items={filtered as ModelConstruction[]} selected={selectedConstruction} onSelect={(id) => { setSelectedConstruction(id); setSelectedObject(id) }} /> : null}
          {tab === 'materials' ? <MaterialList items={filtered as ModelMaterial[]} /> : null}
          {tab === 'schedules' ? <ScheduleList items={filtered as ModelSchedule[]} /> : null}
          {tab === 'projectParameters' && graph ? <ProjectParameterList items={filtered as ModelProjectParameter[]} context={graph.project_parameter_context} /> : null}
          {!['projectParameters', 'constructions', 'materials', 'schedules'].includes(tab) ? <GenericList items={filtered} /> : null}
        </div>
      </aside>
    </div>
    {session ? <div className="model-edit-commit-bar"><div><span className="eyebrow">{t('modelInspector.uncommitted')}</span><b>{t('modelInspector.overrideCount', { count: session.session.patch_count })}</b><small>{t('modelInspector.expires')} {new Date(session.session.expires_at).toLocaleString()}</small></div><label><span>{t('modelInspector.authoredScenario')}</span><input value={scenarioName} onChange={(event) => setScenarioName(event.target.value)} /></label>{preflight ? <span className={preflight.ready ? 'preflight-ready' : 'preflight-blocked'}>{preflight.ready ? t('modelInspector.preflightReady') : t('modelInspector.blockerCount', { count: preflight.errors.length })} · {t('modelInspector.warningCount', { count: preflight.warnings.length })}</span> : null}<button disabled={editorBusy} onClick={() => void runPreflight()}>{t('modelInspector.reviewPreflight')}</button><button className="danger-button" disabled={editorBusy} onClick={() => void discardEditor()}><Trash2 size={13} />{t('modelInspector.discardSession')}</button><button data-testid="commit-authored-model" className="primary-button" disabled={editorBusy || session.session.patch_count < 1 || preflight?.ready !== true} onClick={() => void commitEditor()}><ShieldCheck size={13} />{t('modelInspector.commitVariant')}</button></div> : null}
  </div>
}
