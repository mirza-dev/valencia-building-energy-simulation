import { useEffect, useMemo, useState, type KeyboardEvent } from 'react'
import { AlertTriangle, ArrowDown, ArrowUp, Braces, Cable, Check, Network, Plus, Trash2, Unplug, Upload, WandSparkles, Wrench } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import GeometryEditor from './GeometryEditor'
import { automaticSpaceTypeDraft, formatSchedulePoints, inferCuratedHvacSystem, parseSchedulePoints, parseTypedPatch } from '../lib/modelEditor'
import type {
  ModelEditPatch, ModelEditReport, ModelEditorOptions, ModelGraph,
  ModelGraphObject, ModelHvacComponent, ModelHvacLoop, ModelMeasure, ModelProjectParameter, SceneModel,
} from '../lib/types'

type InspectorTab = 'projectParameters' | 'geometry' | 'constructions' | 'materials' | 'schedules' | 'spaceTypes' | 'zones' | 'hvac' | 'simulation'
type HvacPanel = 'topology' | 'measures'

const hvacPanels: HvacPanel[] = ['topology', 'measures']

interface Props {
  graph: ModelGraph
  scene: SceneModel
  options: ModelEditorOptions
  activeTab: InspectorTab
  selectedId: string | null
  mode: 'advanced' | 'guided'
  busy: boolean
  reports: ModelEditReport[]
  runSettings: Record<string, number | null>
  measures: ModelMeasure[]
  onModeChange: (mode: 'advanced' | 'guided') => void
  onSelect: (id: string) => void
  onApply: (patches: ModelEditPatch[]) => Promise<void>
  onUploadMeasure: (file: File, trusted: boolean) => Promise<void>
}

function asNumber(value: string): number {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) throw new Error('A finite numeric value is required.')
  return parsed
}

function ObjectPicker({ items, value, onChange }: { items: ModelGraphObject[]; value: string | null; onChange: (id: string) => void }) {
  const { t } = useTranslation()
  return <label className="editor-field"><span>{t('modelEditor.targetObject')}</span><select value={value ?? ''} onChange={(event) => onChange(event.target.value)}>
    <option value="" disabled>{t('modelEditor.selectObject')}</option>
    {items.map((item) => <option value={item.id} key={item.id}>{item.name} · {item.type}</option>)}
  </select></label>
}

function ParameterControl({ item, value, busy, onApply }: {
  item: ModelProjectParameter
  value: string | number | boolean | null
  busy: boolean
  onApply: (patches: ModelEditPatch[]) => Promise<void>
}) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState(String(value ?? ''))
  useEffect(() => setDraft(String(value ?? '')), [item.key, value])
  const boolean = item.value_kind === 'boolean'
  const checked = value === true || draft === 'true'
  const outside = item.warn_bounds && draft !== '' && Number.isFinite(Number(draft))
    && (Number(draft) < item.warn_bounds.minimum || Number(draft) > item.warn_bounds.maximum)
  const submit = () => onApply([{ op: 'project_parameter.update', payload: { key: item.key, value: boolean ? checked : asNumber(draft) } }])
  return <article className="editor-parameter" data-parameter-key={item.key}>
    <header><span><strong>{item.key}</strong><small>{item.binding.object_name ?? t('modelEditor.runSetting')}</small></span><code>{item.unit ?? 'bool'}</code></header>
    <div className="editor-inline-control">
      {boolean
        ? <label className="toggle-field"><input type="checkbox" checked={checked} onChange={(event) => setDraft(String(event.target.checked))} /><span>{checked ? 'ON' : 'OFF'}</span></label>
        : <input data-testid={`parameter-${item.key}-input`} type="number" step="any" value={draft} onChange={(event) => setDraft(event.target.value)} />}
      <button data-testid={`parameter-${item.key}-apply`} disabled={busy} onClick={() => void submit()}><Check size={13} />{t('modelEditor.applyOverride')}</button>
    </div>
    <footer className={outside ? 'warn' : ''}>{item.warn_bounds ? t('modelEditor.referenceRange', { minimum: item.warn_bounds.minimum, maximum: item.warn_bounds.maximum }) : t('modelEditor.structuralSwitch')}{outside ? ` · ${t('modelEditor.warningOnly')}` : ''}</footer>
  </article>
}

function ProjectParametersEditor({ graph, runSettings, busy, onApply }: Pick<Props, 'graph' | 'runSettings' | 'busy' | 'onApply'>) {
  return <div className="editor-parameter-grid">{graph.project_parameters.map((item) => <ParameterControl key={item.key} item={item}
    value={item.binding.status === 'run_setting' ? runSettings[item.key] ?? item.current_value : item.current_value}
    busy={busy} onApply={onApply} />)}</div>
}

function ConstructionEditor({ graph, selectedId, onSelect, busy, onApply }: Pick<Props, 'graph' | 'selectedId' | 'onSelect' | 'busy' | 'onApply'>) {
  const { t } = useTranslation()
  const selected = graph.constructions.find((item) => item.id === selectedId) ?? graph.constructions[0]
  const [layers, setLayers] = useState<string[]>([])
  useEffect(() => setLayers(selected?.layers.map((item) => item.id) ?? []), [selected])
  const move = (index: number, delta: number) => setLayers((current) => {
    const next = [...current]
    const target = index + delta
    if (target < 0 || target >= next.length) return current
    ;[next[index], next[target]] = [next[target], next[index]]
    return next
  })
  const add = (id: string) => id && setLayers((current) => [...current, id])
  return <div className="editor-form-stack">
    <ObjectPicker items={graph.constructions} value={selected?.id ?? null} onChange={onSelect} />
    <div className="editor-layer-stack">{layers.map((id, index) => {
      const material = graph.materials.find((item) => item.id === id)
      return <div key={`${id}-${index}`}><b>{index + 1}</b><span>{material?.name ?? id}</span>
        <button aria-label={t('modelEditor.moveLayerUp')} onClick={() => move(index, -1)}><ArrowUp size={12} /></button>
        <button aria-label={t('modelEditor.moveLayerDown')} onClick={() => move(index, 1)}><ArrowDown size={12} /></button>
        <button aria-label={t('modelEditor.removeLayer')} onClick={() => setLayers((current) => current.filter((_, item) => item !== index))}><Trash2 size={12} /></button>
      </div>
    })}</div>
    <label className="editor-field"><span>{t('modelEditor.addMaterialLayer')}</span><select defaultValue="" onChange={(event) => { add(event.target.value); event.target.value = '' }}><option value="">{t('modelEditor.chooseMaterial')}</option>{graph.materials.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
    <button className="primary-button compact" disabled={busy || !selected || !layers.length} onClick={() => selected && void onApply([{ op: 'construction.set_layers', target_id: selected.id, payload: { layer_ids: layers } }])}>{t('modelEditor.applyLayerOrder')}</button>
  </div>
}

function MaterialEditor({ graph, selectedId, onSelect, busy, onApply }: Pick<Props, 'graph' | 'selectedId' | 'onSelect' | 'busy' | 'onApply'>) {
  const { t } = useTranslation()
  const selected = graph.materials.find((item) => item.id === selectedId) ?? graph.materials[0]
  const [properties, setProperties] = useState<Record<string, string>>({})
  const [name, setName] = useState(() => t('modelEditor.authoredMaterial'))
  const [kind, setKind] = useState('StandardOpaqueMaterial')
  useEffect(() => setProperties(Object.fromEntries(Object.entries(selected?.properties ?? {}).map(([key, value]) => [key, String(value ?? '')]))), [selected])
  const update = () => {
    if (!selected) return Promise.resolve()
    const values = Object.fromEntries(Object.entries(properties).filter(([, value]) => value !== '').map(([key, value]) => [key, key === 'roughness' ? value : asNumber(value)]))
    return onApply([{ op: 'material.update', target_id: selected.id, payload: { properties: values } }])
  }
  return <div className="editor-form-stack">
    <ObjectPicker items={graph.materials} value={selected?.id ?? null} onChange={onSelect} />
    <div className="editor-property-grid">{Object.entries(properties).map(([key, value]) => <label key={key}><span>{key}</span><input value={value} onChange={(event) => setProperties((current) => ({ ...current, [key]: event.target.value }))} /></label>)}</div>
    <div className="editor-action-row"><button className="primary-button compact" disabled={busy || !Object.keys(properties).length} onClick={() => void update()}>{t('modelEditor.updateMaterial')}</button><button className="danger-button compact" disabled={busy || !selected} onClick={() => selected && void onApply([{ op: 'material.delete', target_id: selected.id, payload: {} }])}><Trash2 size={13} />{t('modelEditor.deleteUnused')}</button></div>
    <div className="editor-subsection"><h4><Plus size={13} />{t('modelEditor.createMaterial')}</h4><div className="editor-property-grid"><label><span>{t('modelEditor.materialType')}</span><select value={kind} onChange={(event) => setKind(event.target.value)}><option>StandardOpaqueMaterial</option><option>MasslessOpaqueMaterial</option><option>AirGap</option><option>SimpleGlazing</option></select></label><label><span>{t('modelEditor.materialName')}</span><input value={name} onChange={(event) => setName(event.target.value)} /></label></div><button disabled={busy} onClick={() => void onApply([{ op: 'material.create', payload: { kind, name } }])}>{t('modelEditor.create')}</button></div>
  </div>
}

function ScheduleEditor({ graph, selectedId, onSelect, busy, onApply }: Pick<Props, 'graph' | 'selectedId' | 'onSelect' | 'busy' | 'onApply'>) {
  const { t } = useTranslation()
  const schedules = graph.schedules.filter((item) => item.profiles.length)
  const selected = schedules.find((item) => item.id === selectedId) ?? schedules[0]
  const [points, setPoints] = useState('24:20')
  const [ruleName, setRuleName] = useState(() => t('modelEditor.authoredRule'))
  useEffect(() => setPoints(formatSchedulePoints(selected?.profiles[0]?.points ?? [{ hour: 24, value: 20 }])), [selected])
  return <div className="editor-form-stack">
    <ObjectPicker items={schedules} value={selected?.id ?? null} onChange={onSelect} />
    <label className="editor-field"><span>{t('modelEditor.dayProfile')}</span><textarea rows={8} value={points} onChange={(event) => setPoints(event.target.value)} /></label>
    <button className="primary-button compact" disabled={busy || !selected} onClick={() => selected && void onApply([{ op: 'schedule.update_day', target_id: selected.id, payload: { role: selected.profiles[0]?.role ?? 'default', points: parseSchedulePoints(points) } }])}>{t('modelEditor.updateSelectedProfile')}</button>
    <div className="editor-subsection"><h4><Plus size={13} />{t('modelEditor.addCalendarRule')}</h4><label className="editor-field"><span>{t('modelEditor.ruleName')}</span><input value={ruleName} onChange={(event) => setRuleName(event.target.value)} /></label><button disabled={busy || !selected} onClick={() => selected && void onApply([{ op: 'schedule.add_rule', target_id: selected.id, payload: { name: ruleName, start: { month: 1, day: 1 }, end: { month: 12, day: 31 }, days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], points: parseSchedulePoints(points) } }])}>{t('modelEditor.addFullYearRule')}</button></div>
    {selected?.rules.length ? <div className="editor-rule-list">{selected.rules.map((rule) => <div key={rule.id}><span>{rule.name}</span><code>{rule.days.join(' ')}</code><button disabled={busy} onClick={() => void onApply([{ op: 'schedule.delete_rule', target_id: rule.id, payload: {} }])}><Trash2 size={12} /></button></div>)}</div> : null}
  </div>
}

function SpaceTypeEditor({ graph, selectedId, onSelect, busy, onApply }: Pick<Props, 'graph' | 'selectedId' | 'onSelect' | 'busy' | 'onApply'>) {
  const { t } = useTranslation()
  const selected = graph.space_types.find((item) => item.id === selectedId) ?? graph.space_types[0]
  const [loads, setLoads] = useState({ people_per_floor_area: '', lighting_power_per_floor_area: '', electric_equipment_power_per_floor_area: '', gas_equipment_power_per_floor_area: '' })
  const [infiltration, setInfiltration] = useState({ id: null as string | null, method: 'air_changes_per_hour', value: '' })
  const [outdoorAir, setOutdoorAir] = useState({ method: 'flow_per_person_m3_s', value: '' })
  useEffect(() => {
    const draft = automaticSpaceTypeDraft(selected)
    setLoads(draft.loads)
    setInfiltration(draft.infiltration)
    setOutdoorAir(draft.outdoorAir)
  }, [selected])
  const numericPayload = (values: Record<string, string>) => Object.fromEntries(Object.entries(values).filter(([, value]) => value !== '').map(([key, value]) => [key, asNumber(value)]))
  return <div className="editor-form-stack"><ObjectPicker items={graph.space_types} value={selected?.id ?? null} onChange={onSelect} />
    <p className="editor-help auto-derived-note"><Check size={13} />{t('modelEditor.autoFilledSelected')}</p>
    <h4>{t('modelEditor.internalLoads')}</h4><div className="editor-property-grid">{Object.entries(loads).map(([key, value]) => <label key={key}><span>{key}</span><input data-testid={`space-load-${key}`} type="number" step="any" value={value} onChange={(event) => setLoads((current) => ({ ...current, [key]: event.target.value }))} /></label>)}</div>
    <button disabled={busy || !selected} onClick={() => selected && void onApply([{ op: 'space_type.set_loads', target_id: selected.id, payload: numericPayload(loads) }])}>{t('modelEditor.applyLoads')}</button>
    <div className="editor-method-row"><label className="editor-field"><span>{t('modelEditor.infiltrationCalculation')}</span><select value={infiltration.method} onChange={(event) => setInfiltration((current) => ({ ...current, method: event.target.value }))}><option value="air_changes_per_hour">{t('modelEditor.ach')}</option><option value="design_flow_rate_m3_s">{t('modelEditor.designFlow')}</option><option value="flow_per_floor_area_m3_s_m2">{t('modelEditor.flowFloor')}</option><option value="flow_per_exterior_area_m3_s_m2">{t('modelEditor.flowExterior')}</option><option value="flow_per_exterior_wall_area_m3_s_m2">{t('modelEditor.flowExteriorWall')}</option></select></label><label className="editor-field"><span>{t('modelEditor.currentAutomaticValue')}</span><input data-testid="space-infiltration-value" type="number" step="any" value={infiltration.value} onChange={(event) => setInfiltration((current) => ({ ...current, value: event.target.value }))} /></label><button disabled={busy || !selected || !infiltration.value} onClick={() => selected && void onApply([{ op: 'space_type.set_infiltration', target_id: selected.id, payload: { ...(infiltration.id ? { infiltration_id: infiltration.id } : {}), [infiltration.method]: asNumber(infiltration.value) } }])}>{t('modelEditor.applyOverride')}</button></div>
    <div className="editor-method-row"><label className="editor-field"><span>{t('modelEditor.outdoorAirCalculation')}</span><select value={outdoorAir.method} onChange={(event) => setOutdoorAir((current) => ({ ...current, method: event.target.value }))}><option value="flow_per_person_m3_s">{t('modelEditor.flowPerson')}</option><option value="flow_per_floor_area_m3_s_m2">{t('modelEditor.flowFloor')}</option><option value="flow_rate_m3_s">{t('modelEditor.absoluteFlow')}</option><option value="air_changes_per_hour">{t('modelEditor.ach')}</option></select></label><label className="editor-field"><span>{t('modelEditor.currentAutomaticValue')}</span><input data-testid="space-outdoor-air-value" type="number" step="any" value={outdoorAir.value} onChange={(event) => setOutdoorAir((current) => ({ ...current, value: event.target.value }))} /></label><button disabled={busy || !selected || !outdoorAir.value} onClick={() => selected && void onApply([{ op: 'space_type.set_dsoa', target_id: selected.id, payload: { [outdoorAir.method]: asNumber(outdoorAir.value) } }])}>{t('modelEditor.applyOverride')}</button></div>
  </div>
}

function ThermostatEditor({ graph, selectedId, onSelect, busy, onApply }: Pick<Props, 'graph' | 'selectedId' | 'onSelect' | 'busy' | 'onApply'>) {
  const { t } = useTranslation()
  const zones = graph.zones.filter((item) => item.thermostat)
  const selected = zones.find((item) => item.id === selectedId) ?? zones[0]
  const [heating, setHeating] = useState('0')
  const [cooling, setCooling] = useState('0')
  const thermostat = selected?.thermostat as { heating_schedule?: { name?: string }; cooling_schedule?: { name?: string } } | undefined
  return <div className="editor-form-stack"><ObjectPicker items={zones} value={selected?.id ?? null} onChange={onSelect} />
    <div className="editor-auto-source"><span><b>{t('modelEditor.autoHeatingSchedule')}</b><code>{thermostat?.heating_schedule?.name ?? '—'}</code></span><span><b>{t('modelEditor.autoCoolingSchedule')}</b><code>{thermostat?.cooling_schedule?.name ?? '—'}</code></span></div>
    <div className="editor-property-grid"><label><span>{t('modelEditor.heatingDelta')}</span><input data-testid="thermostat-heating-delta" type="number" step="0.1" value={heating} onChange={(event) => setHeating(event.target.value)} /></label><label><span>{t('modelEditor.coolingDelta')}</span><input data-testid="thermostat-cooling-delta" type="number" step="0.1" value={cooling} onChange={(event) => setCooling(event.target.value)} /></label></div>
    <p className="editor-help">{t('modelEditor.sentinelPreserved')}</p>
    <button data-testid="thermostat-apply" className="primary-button compact" disabled={busy || !selected} onClick={() => selected && void onApply([{ op: 'thermostat.set_setpoints', target_id: selected.id, payload: { heating_delta_c: asNumber(heating), cooling_delta_c: asNumber(cooling) } }])}>{t('modelEditor.shiftThermostat')}</button>
  </div>
}

function HvacLoopCard({ loop, busy, onApply, onSelectComponent }: {
  loop: ModelHvacLoop
  busy: boolean
  onApply: Props['onApply']
  onSelectComponent: (component: ModelHvacComponent) => void
}) {
  const { t } = useTranslation()
  return <article className="hvac-loop-card" data-testid={`hvac-loop-${loop.kind}`}>
    <header><span><Network size={14} /><strong>{loop.name}</strong><small>{loop.type} · {loop.kind.toUpperCase()}</small></span><button className="danger-button compact" disabled={busy} onClick={() => void onApply([{ op: 'hvac.loop.delete', target_id: loop.id, payload: {} }])}><Trash2 size={12} />{t('modelEditor.deleteLoop')}</button></header>
    <div className="hvac-node-chain" aria-label={t('modelEditor.supplyPath', { name: loop.name })}>{loop.supply_components.map((component) => component.node
      ? <i className="hvac-node" title={component.name} key={component.id} />
      : <button className={component.editable ? 'editable' : ''} key={component.id} onClick={() => component.editable && onSelectComponent(component)}><span>{component.name}</span><code>{component.type}</code></button>)}</div>
    {loop.kind === 'air' ? <div className="hvac-zone-branches"><span>{t('modelEditor.zoneBranches')}</span>{loop.zones.map((zone) => <button key={zone.id} disabled={busy} title={t('modelEditor.disconnectZone')} onClick={() => void onApply([{ op: 'hvac.zone.disconnect', target_id: loop.id, payload: { zone_id: zone.id } }])}><Cable size={11} />{zone.name}<Unplug size={10} /></button>)}</div> : <dl className="hvac-loop-sizing">{Object.entries(loop.sizing).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl>}
  </article>
}

function measureValue(value: string, type: string): string | number | boolean {
  if (type === 'Double' || type === 'Integer') return asNumber(value)
  if (type === 'Boolean') return value === 'true'
  return value
}

function HvacEditor({ graph, options, measures, busy, onApply, onUploadMeasure }: Pick<Props, 'graph' | 'options' | 'measures' | 'busy' | 'onApply' | 'onUploadMeasure'>) {
  const { t } = useTranslation()
  const automaticSystem = inferCuratedHvacSystem(graph)
  const [system, setSystem] = useState(automaticSystem)
  const [panel, setPanel] = useState<HvacPanel>('topology')
  const [template, setTemplate] = useState(options.hvac_air_loop_templates[0]?.id ?? '')
  const [loopName, setLoopName] = useState(() => t('modelEditor.authoredHvacLoop'))
  const [targetLoopId, setTargetLoopId] = useState('')
  const [targetZoneId, setTargetZoneId] = useState('')
  const [equipmentType, setEquipmentType] = useState(options.hvac_equipment_library[0]?.id ?? '')
  const [equipmentName, setEquipmentName] = useState(() => t('modelEditor.authoredEquipment'))
  const [plantLoopId, setPlantLoopId] = useState('')
  const [selectedComponentId, setSelectedComponentId] = useState('')
  const [componentCapacity, setComponentCapacity] = useState('')
  const [componentEfficiency, setComponentEfficiency] = useState('')
  const [componentAutosize, setComponentAutosize] = useState(true)
  const [measureId, setMeasureId] = useState(measures[0]?.id ?? '')
  const [measureArguments, setMeasureArguments] = useState<Record<string, string>>({})
  const [measureFile, setMeasureFile] = useState<File | null>(null)
  const [trustMeasure, setTrustMeasure] = useState(false)
  useEffect(() => setSystem(automaticSystem), [automaticSystem, graph.graph_sha256])
  const loops = useMemo(() => [...graph.hvac.air_loops, ...graph.hvac.plant_loops], [graph.hvac.air_loops, graph.hvac.plant_loops])
  const components = useMemo(() => loops.flatMap((loop) => [...loop.supply_components, ...loop.demand_components]).filter((item) => item.editable), [loops])
  const selectedComponent = components.find((item) => item.id === selectedComponentId)
  const selectedMeasure = measures.find((item) => item.id === measureId)
  const targetLoop = loops.find((item) => item.id === targetLoopId)
  const compatibleEquipment = options.hvac_equipment_library.filter((item) => !targetLoop || item.loop_kinds.includes(targetLoop.kind))
  const selectedEquipment = options.hvac_equipment_library.find((item) => item.id === equipmentType)
  const heatingOnlySystem = system === 'gas_furnace' || system === 'electric_furnace'
  const hvacSystemLabel = (id: string, fallback: string) => t(`modelEditor.hvacSystems.${id}`, { defaultValue: fallback })
  useEffect(() => {
    if (!targetLoopId && loops[0]) setTargetLoopId(loops[0].id)
    if (!targetZoneId && graph.zones[0]) setTargetZoneId(graph.zones[0].id)
  }, [graph.zones, loops, targetLoopId, targetZoneId])
  useEffect(() => {
    if (!selectedComponent) return
    const capacity = selectedComponent.properties.capacity
    const efficiency = selectedComponent.properties.efficiency
    setComponentCapacity(capacity?.value == null ? '' : String(capacity.value))
    setComponentEfficiency(efficiency?.value == null ? '' : String(efficiency.value))
    setComponentAutosize(capacity?.autosized ?? true)
  }, [selectedComponent])
  useEffect(() => {
    if (!measures.some((item) => item.id === measureId)) setMeasureId(measures[0]?.id ?? '')
  }, [measureId, measures])
  useEffect(() => setMeasureArguments(Object.fromEntries((selectedMeasure?.arguments ?? []).map((item) => [item.name, item.default_value ?? '']))), [selectedMeasure])
  const selectComponent = (component: ModelHvacComponent) => setSelectedComponentId(component.id)
  const applyComponent = () => {
    if (!selectedComponent) return Promise.resolve()
    const payload: Record<string, unknown> = { autosize: componentAutosize }
    if (!componentAutosize && componentCapacity !== '') payload.capacity = asNumber(componentCapacity)
    if (componentEfficiency !== '') payload.efficiency = asNumber(componentEfficiency)
    return onApply([{ op: 'hvac.component.update', target_id: selectedComponent.id, payload }])
  }
  const applyMeasure = () => {
    if (!selectedMeasure) return Promise.resolve()
    const argumentsPayload = Object.fromEntries(selectedMeasure.arguments.map((item) => [item.name, measureValue(measureArguments[item.name] ?? '', item.type)]))
    return onApply([{ op: 'measure.apply', payload: { measure_id: selectedMeasure.id, arguments: argumentsPayload } }])
  }
  const movePanelFocus = (event: KeyboardEvent<HTMLButtonElement>, current: HvacPanel) => {
    const currentIndex = hvacPanels.indexOf(current)
    let nextIndex: number | null = null
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % hvacPanels.length
    if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + hvacPanels.length) % hvacPanels.length
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = hvacPanels.length - 1
    if (nextIndex == null) return
    event.preventDefault()
    const next = hvacPanels[nextIndex]
    setPanel(next)
    event.currentTarget.parentElement?.querySelector<HTMLButtonElement>(`[data-hvac-tab="${next}"]`)?.focus()
  }
  return <div className="editor-form-stack hvac-authoring-panel">
    <div className="hvac-panel-switch" role="tablist" aria-label={t('modelEditor.hvacAndMeasures')}><button id="hvac-tab-topology" data-hvac-tab="topology" role="tab" aria-selected={panel === 'topology'} aria-controls="hvac-tabpanel-topology" tabIndex={panel === 'topology' ? 0 : -1} className={panel === 'topology' ? 'active' : ''} onClick={() => setPanel('topology')} onKeyDown={(event) => movePanelFocus(event, 'topology')}><Network size={13} />{t('modelEditor.loopAuthoring')}</button><button id="hvac-tab-measures" data-hvac-tab="measures" data-testid="measure-panel-tab" role="tab" aria-selected={panel === 'measures'} aria-controls="hvac-tabpanel-measures" tabIndex={panel === 'measures' ? 0 : -1} className={panel === 'measures' ? 'active' : ''} onClick={() => setPanel('measures')} onKeyDown={(event) => movePanelFocus(event, 'measures')}><Wrench size={13} />{t('modelEditor.measures')}</button></div>
    {panel === 'topology' ? <div className="hvac-tab-panel" id="hvac-tabpanel-topology" role="tabpanel" aria-labelledby="hvac-tab-topology" tabIndex={0}>
      <p className="editor-help auto-derived-note"><Check size={13} />{t('modelEditor.autoFilledHvac')}</p>
      {loops.length ? <div className="hvac-loop-grid">{loops.map((loop) => <HvacLoopCard key={loop.id} loop={loop} busy={busy} onApply={onApply} onSelectComponent={selectComponent} />)}</div> : <div className="editor-auto-source">{graph.hvac.zone_equipment.map((item) => <span key={item.id}><b>{item.name}</b><code>{item.type}</code></span>)}</div>}
      <div className="editor-subsection"><h4><Plus size={13} />{t('modelEditor.createTopology')}</h4><div className="editor-property-grid"><label><span>{t('modelEditor.systemTemplate')}</span><select data-testid="hvac-loop-template" value={template} onChange={(event) => setTemplate(event.target.value)}>{options.hvac_air_loop_templates.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><label><span>{t('modelEditor.loopName')}</span><input value={loopName} onChange={(event) => setLoopName(event.target.value)} /></label></div><button data-testid="hvac-create-loop" className="primary-button compact" disabled={busy || !template} onClick={() => void onApply([{ op: 'hvac.air_loop.create', payload: { template, name: loopName } }])}><Network size={13} />{t('modelEditor.createConnectZones')}</button></div>
      {graph.hvac.air_loops.length ? <div className="editor-subsection"><h4><Cable size={13} />{t('modelEditor.connectZone')}</h4><div className="editor-method-row"><label className="editor-field"><span>{t('modelEditor.airLoop')}</span><select value={targetLoopId} onChange={(event) => setTargetLoopId(event.target.value)}>{graph.hvac.air_loops.map((loop) => <option key={loop.id} value={loop.id}>{loop.name}</option>)}</select></label><label className="editor-field"><span>{t('modelEditor.thermalZone')}</span><select value={targetZoneId} onChange={(event) => setTargetZoneId(event.target.value)}>{graph.zones.map((zone) => <option key={zone.id} value={zone.id}>{zone.name}</option>)}</select></label><button disabled={busy || !targetLoopId || !targetZoneId} onClick={() => void onApply([{ op: 'hvac.zone.connect', target_id: targetLoopId, payload: { zone_id: targetZoneId } }])}>{t('modelEditor.connect')}</button></div></div> : null}
      {loops.length ? <div className="editor-subsection"><h4><Plus size={13} />{t('modelEditor.equipmentLibrary')}</h4><div className="editor-property-grid"><label><span>{t('modelEditor.targetLoop')}</span><select value={targetLoopId} onChange={(event) => { setTargetLoopId(event.target.value); const loop = loops.find((item) => item.id === event.target.value); const first = options.hvac_equipment_library.find((item) => !loop || item.loop_kinds.includes(loop.kind)); if (first) setEquipmentType(first.id) }}>{loops.map((loop) => <option key={loop.id} value={loop.id}>{loop.name} · {loop.kind}</option>)}</select></label><label><span>{t('modelEditor.equipmentType')}</span><select value={equipmentType} onChange={(event) => setEquipmentType(event.target.value)}>{compatibleEquipment.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><label><span>{t('modelEditor.equipmentName')}</span><input value={equipmentName} onChange={(event) => setEquipmentName(event.target.value)} /></label>{selectedEquipment?.requires_plant ? <label><span>{t('modelEditor.plantLoopRequirement', { kind: selectedEquipment.requires_plant })}</span><select value={plantLoopId} onChange={(event) => setPlantLoopId(event.target.value)}><option value="">{t('modelEditor.selectPlantLoop')}</option>{graph.hvac.plant_loops.map((loop) => <option key={loop.id} value={loop.id}>{loop.name}</option>)}</select></label> : null}</div><button disabled={busy || !targetLoopId || !equipmentType || Boolean(selectedEquipment?.requires_plant && !plantLoopId)} onClick={() => void onApply([{ op: 'hvac.component.add', target_id: targetLoopId, payload: { equipment_type: equipmentType, name: equipmentName, ...(plantLoopId ? { plant_loop_id: plantLoopId } : {}), autosize: true } }])}>{t('modelEditor.addAutosizedEquipment')}</button></div> : null}
      {selectedComponent ? <div className="editor-subsection hvac-sizing-editor"><h4><Wrench size={13} />{t('modelEditor.sizeSelectedComponent', { name: selectedComponent.name })}</h4><div className="editor-property-grid"><label><span>{t('modelEditor.capacityMaxFlow')}</span><input type="number" step="any" value={componentCapacity} disabled={componentAutosize} onChange={(event) => setComponentCapacity(event.target.value)} /></label><label><span>{t('modelEditor.efficiencyCop')}</span><input type="number" step="any" value={componentEfficiency} onChange={(event) => setComponentEfficiency(event.target.value)} /></label></div><label className="toggle-field"><input type="checkbox" checked={componentAutosize} onChange={(event) => setComponentAutosize(event.target.checked)} /><span>{t('modelEditor.openStudioAutosize')}</span></label><div className="editor-action-row"><button className="primary-button compact" disabled={busy} onClick={() => void applyComponent()}>{t('modelEditor.applySizing')}</button><button className="danger-button compact" disabled={busy} onClick={() => void onApply([{ op: 'hvac.component.remove', target_id: selectedComponent.id, payload: {} }])}><Trash2 size={12} />{t('modelEditor.removeComponent')}</button></div></div> : null}
      <details className="hvac-legacy-system"><summary>{t('modelEditor.wholeSystem')}</summary><label className="editor-field"><span>{t('modelEditor.curatedTopology')}</span><select data-testid="hvac-system-select" value={system} onChange={(event) => setSystem(event.target.value)}><option value="">{t('modelEditor.currentTopology')}</option>{options.hvac_systems.map((item) => <option key={item.id} value={item.id}>{hvacSystemLabel(item.id, item.label)}</option>)}</select></label><p className="editor-help warn"><AlertTriangle size={13} />{t('modelEditor.replacesTopology')}</p>{heatingOnlySystem ? <p className="editor-help warn" data-testid="heating-only-warning"><AlertTriangle size={13} />{t('modelEditor.heatingOnlyWarning')}</p> : null}<button disabled={busy || !system} onClick={() => void onApply([{ op: 'hvac.set_system', payload: { system } }])}>{t('modelEditor.applyWholeSystem')}</button></details>
    </div> : <div className="hvac-tab-panel" id="hvac-tabpanel-measures" role="tabpanel" aria-labelledby="hvac-tab-measures" tabIndex={0}>
      <p className="editor-help warn"><AlertTriangle size={13} />{t('modelEditor.measureTrustWarning')}</p>
      <div className="measure-upload"><input data-testid="measure-file" type="file" accept=".zip,application/zip" aria-label={t('modelEditor.measureZip')} onChange={(event) => setMeasureFile(event.target.files?.[0] ?? null)} /><label className="toggle-field"><input type="checkbox" checked={trustMeasure} onChange={(event) => setTrustMeasure(event.target.checked)} /><span>{t('modelEditor.trustMeasure')}</span></label><button data-testid="measure-upload" disabled={busy || !measureFile || !trustMeasure} onClick={() => measureFile && void onUploadMeasure(measureFile, trustMeasure)}><Upload size={13} />{t('modelEditor.uploadZip')}</button></div>
      <label className="editor-field"><span>{t('modelEditor.measureCatalog')}</span><select data-testid="measure-select" value={measureId} onChange={(event) => setMeasureId(event.target.value)}>{measures.map((item) => <option key={item.id} value={item.id}>{item.display_name} · {item.source} · {item.language}</option>)}</select></label>
      {selectedMeasure ? <div className="measure-detail"><header><span><strong>{selectedMeasure.display_name}</strong><small>{selectedMeasure.description}</small></span><code>{selectedMeasure.sha256.slice(0, 12)}</code></header>{selectedMeasure.arguments.map((argument) => <label className="editor-field" key={argument.name}><span>{argument.display_name} {argument.units ? `· ${argument.units}` : ''}</span>{argument.type === 'Boolean' ? <select value={measureArguments[argument.name] ?? 'false'} onChange={(event) => setMeasureArguments((current) => ({ ...current, [argument.name]: event.target.value }))}><option value="true">true</option><option value="false">false</option></select> : argument.choices.length ? <select value={measureArguments[argument.name] ?? ''} onChange={(event) => setMeasureArguments((current) => ({ ...current, [argument.name]: event.target.value }))}>{argument.choices.map((choice) => <option key={choice.value} value={choice.value}>{choice.display_name}</option>)}</select> : <input data-testid={`measure-argument-${argument.name}`} type={argument.type === 'Double' || argument.type === 'Integer' ? 'number' : 'text'} step="any" value={measureArguments[argument.name] ?? ''} onChange={(event) => setMeasureArguments((current) => ({ ...current, [argument.name]: event.target.value }))} />}<small>{argument.description}</small></label>)}<button data-testid="measure-apply" className="primary-button compact" disabled={busy} onClick={() => void applyMeasure()}><Wrench size={13} />{t('modelEditor.applyJournaledPatch')}</button></div> : null}
    </div>}
  </div>
}

function SimulationEditor({ graph, busy, onApply }: Pick<Props, 'graph' | 'busy' | 'onApply'>) {
  const { t } = useTranslation()
  const period = graph.simulation.run_period
  const [beginMonth, setBeginMonth] = useState(String(period.begin.month))
  const [beginDay, setBeginDay] = useState(String(period.begin.day))
  const [endMonth, setEndMonth] = useState(String(period.end.month))
  const [endDay, setEndDay] = useState(String(period.end.day))
  const [variables, setVariables] = useState(JSON.stringify(graph.simulation.output_variables.map((item) => ({ name: item.variable, key: item.key, frequency: item.frequency })), null, 2))
  return <div className="editor-form-stack"><h4>{t('modelEditor.runPeriod')}</h4><div className="editor-property-grid"><label><span>{t('modelEditor.beginMonth')}</span><input type="number" min="1" max="12" value={beginMonth} onChange={(event) => setBeginMonth(event.target.value)} /></label><label><span>{t('modelEditor.beginDay')}</span><input type="number" min="1" max="31" value={beginDay} onChange={(event) => setBeginDay(event.target.value)} /></label><label><span>{t('modelEditor.endMonth')}</span><input type="number" min="1" max="12" value={endMonth} onChange={(event) => setEndMonth(event.target.value)} /></label><label><span>{t('modelEditor.endDay')}</span><input type="number" min="1" max="31" value={endDay} onChange={(event) => setEndDay(event.target.value)} /></label></div><button disabled={busy} onClick={() => void onApply([{ op: 'sim.set_run_period', payload: { begin: { month: asNumber(beginMonth), day: asNumber(beginDay) }, end: { month: asNumber(endMonth), day: asNumber(endDay) } } }])}>{t('modelEditor.applyRunPeriod')}</button>
    <h4>{t('modelEditor.outputVariables')}</h4><label className="editor-field"><span>{t('modelEditor.typedVariableArray')}</span><textarea rows={9} value={variables} onChange={(event) => setVariables(event.target.value)} /></label><button disabled={busy} onClick={() => void onApply([{ op: 'sim.set_output_variables', payload: { variables: JSON.parse(variables) as unknown[] } }])}>{t('modelEditor.replaceOutputVariables')}</button></div>
}

function RawPatchEditor({ busy, onApply }: Pick<Props, 'busy' | 'onApply'>) {
  const { t } = useTranslation()
  const [raw, setRaw] = useState('{\n  "op": "project_parameter.update",\n  "payload": { "key": "wall_u", "value": 1.5 }\n}')
  const [error, setError] = useState<string | null>(null)
  return <details className="typed-patch-console"><summary><Braces size={13} />{t('modelEditor.typedPatchConsole')}</summary><textarea rows={9} value={raw} aria-label={t('modelEditor.typedPatchInput')} onChange={(event) => setRaw(event.target.value)} />{error ? <p className="error-message" role="alert">{error}</p> : null}<button disabled={busy} onClick={() => { try { setError(null); void onApply([parseTypedPatch(raw)]) } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) } }}>{t('modelEditor.applyTypedPatch')}</button></details>
}

function GuidedEditor({ options, busy, onApply }: Pick<Props, 'options' | 'busy' | 'onApply'>) {
  const { t } = useTranslation()
  return <div className="guided-preset-grid">{options.guided_presets.map((preset) => <button key={preset.id} disabled={busy} onClick={() => void onApply(preset.patches)}><WandSparkles size={16} /><span><strong>{preset.name}</strong><small>{t('modelEditor.typedPatchCount', { count: preset.patches.length })}</small></span></button>)}<p><AlertTriangle size={14} />{t('modelEditor.guidedGuardrail')}</p></div>
}

export default function ModelEditWorkspace(props: Props) {
  const { t } = useTranslation()
  const body = useMemo(() => {
    const common = { graph: props.graph, selectedId: props.selectedId, onSelect: props.onSelect, busy: props.busy, onApply: props.onApply }
    if (props.activeTab === 'projectParameters') return <ProjectParametersEditor graph={props.graph} runSettings={props.runSettings} busy={props.busy} onApply={props.onApply} />
    if (props.activeTab === 'geometry') return <GeometryEditor scene={props.scene} mode={props.mode} busy={props.busy} selectedId={props.selectedId} onSelect={props.onSelect} onApply={props.onApply} />
    if (props.activeTab === 'constructions') return <ConstructionEditor {...common} />
    if (props.activeTab === 'materials') return <MaterialEditor {...common} />
    if (props.activeTab === 'schedules') return <ScheduleEditor {...common} />
    if (props.activeTab === 'spaceTypes') return <SpaceTypeEditor {...common} />
    if (props.activeTab === 'zones') return <ThermostatEditor {...common} />
    if (props.activeTab === 'hvac') return <HvacEditor graph={props.graph} options={props.options} measures={props.measures} busy={props.busy} onApply={props.onApply} onUploadMeasure={props.onUploadMeasure} />
    return <SimulationEditor graph={props.graph} busy={props.busy} onApply={props.onApply} />
  }, [props])
  const geometry = props.activeTab === 'geometry'
  const phaseLabel = geometry ? t('modelEditor.phaseGeometry') : props.activeTab === 'hvac' ? t('modelEditor.phaseHvac') : t('modelEditor.phaseOverride')
  return <section className="model-edit-workspace">
    <header className="editor-mode-header"><div><span className="eyebrow">{phaseLabel}</span><h3>{t('modelEditor.title')}</h3><p>{t('modelEditor.coreModel')}</p></div><div role="group" aria-label={t('modelEditor.mode')}><button data-testid="editor-mode-advanced" aria-pressed={props.mode === 'advanced'} className={props.mode === 'advanced' ? 'active' : ''} onClick={() => props.onModeChange('advanced')}>{t('modelEditor.advanced')}</button><button data-testid="editor-mode-guided" aria-pressed={props.mode === 'guided'} className={props.mode === 'guided' ? 'active' : ''} onClick={() => props.onModeChange('guided')}>{t('modelEditor.guided')}</button></div></header>
    <div className={`editor-workspace-scroll ${geometry ? 'geometry-workspace-scroll' : ''}`}>{geometry ? body : props.mode === 'guided' ? <GuidedEditor options={props.options} busy={props.busy} onApply={props.onApply} /> : body}{props.mode === 'advanced' ? <RawPatchEditor busy={props.busy} onApply={props.onApply} /> : null}
      {props.reports.length ? <div className="editor-report-list">{props.reports.map((report, index) => <div className={report.severity} key={`${report.op}-${index}`}><b>{report.status}</b><span>{report.message}</span>{report.warnings.map((warning) => <small key={warning.code}>{warning.message}</small>)}</div>)}</div> : null}
    </div>
  </section>
}
