import { useCallback, useMemo, useState } from 'react'
import { CircleAlert, CloudSun, Play, SlidersHorizontal } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { NeighborhoodRunRequest, StockInputPolicy, StockRunOptions, StockRunRequest } from '../lib/types'
import {
  initialStockRunDraft, stockRunIssues, stockRunPayload, type StockRunDraft,
} from '../lib/stockRunValidation'
import StockInputPolicyPanel from './StockInputPolicyPanel'

interface Props {
  options?: StockRunOptions
  workflow: 'neighborhood' | 'city'
  includeScope?: boolean
  disabled?: boolean
  pending?: boolean
  onScopeChange?: (district: string | null) => void
  onStart: (payload: StockRunRequest | NeighborhoodRunRequest) => void
}

export default function StockRunSetup({
  options, workflow, includeScope = false, disabled = false, pending = false, onScopeChange, onStart,
}: Props) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState<StockRunDraft>(initialStockRunDraft)
  const [policyOverride, setPolicyOverride] = useState<Partial<StockInputPolicy> | null>(null)
  const [policyReady, setPolicyReady] = useState(true)
  const issues = useMemo(() => stockRunIssues(draft, includeScope), [draft, includeScope])
  const set = <K extends keyof StockRunDraft>(key: K, value: StockRunDraft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
  }
  const chooseMode = (mode: StockRunDraft['mode']) => {
    setDraft((current) => mode === 'baseline'
      ? { ...initialStockRunDraft, scopeMode: current.scopeMode, district: current.district, buildingRef: current.buildingRef }
      : { ...current, mode })
  }
  const chooseScope = (scopeMode: StockRunDraft['scopeMode']) => {
    setDraft((current) => ({ ...current, scopeMode, district: scopeMode === 'district' ? current.district : '', buildingRef: scopeMode === 'building' ? current.buildingRef : '' }))
    if (scopeMode !== 'district') onScopeChange?.(null)
  }
  const chooseDistrict = (district: string) => {
    set('district', district)
    onScopeChange?.(district || null)
  }
  const submit = () => {
    if (issues.length || !policyReady) return
    onStart({
      ...stockRunPayload(draft, includeScope as true),
      input_policy_override: policyOverride,
    } as NeighborhoodRunRequest | StockRunRequest)
  }
  const policyChanged = useCallback((policy: Partial<StockInputPolicy> | null, ready: boolean) => {
    setPolicyOverride(policy)
    setPolicyReady(ready)
  }, [])

  return <section className="stock-run-setup" data-testid="stock-run-setup">
    <div className="inspector-subhead"><SlidersHorizontal size={14} />{t('stockRun.configuration')}</div>
    <div className="segmented-control stock-run-mode" aria-label={t('stockRun.mode')}>
      <button className={draft.mode === 'baseline' ? 'active' : ''} onClick={() => chooseMode('baseline')}>{t('stockRun.baseline')}</button>
      <button className={draft.mode === 'scenario' ? 'active' : ''} onClick={() => chooseMode('scenario')} disabled={!options?.features.comfort_scenario && !options?.features.weather_scenario}>{t('stockRun.scenario')}</button>
    </div>
    {includeScope && options?.features.district_scope ? <div className="stock-scope-controls">
      <label><span>{t('stockRun.scope')}</span><select value={draft.scopeMode} onChange={(event) => chooseScope(event.target.value as StockRunDraft['scopeMode'])}><option value="boundary">Benicalap</option><option value="district">{t('stockRun.municipalDistrict')}</option>{options.features.single_building ? <option value="building">{t('stockRun.singleBuilding')}</option> : null}</select></label>
      {draft.scopeMode === 'district' ? <label><span>{t('stockRun.district')}</span><select value={draft.district} onChange={(event) => chooseDistrict(event.target.value)}><option value="">{t('common.notSelected')}</option>{options.districts?.map((district) => <option key={district} value={district}>{district}</option>)}</select></label> : null}
      {draft.scopeMode === 'building' ? <label><span>{t('stockRun.buildingRef')}</span><input value={draft.buildingRef} onChange={(event) => set('buildingRef', event.target.value)} placeholder="4252702YJ2745A" autoCapitalize="characters" /></label> : null}
    </div> : null}
    {draft.mode === 'scenario' ? <div className="stock-scenario-fields">
      <label className="field-wide"><span>{t('stockRun.name')}</span><input value={draft.name} onChange={(event) => set('name', event.target.value)} placeholder={t('stockRun.namePlaceholder')} /></label>
      <label><span>{t('stockRun.heatDelta')}</span><div className="input-with-unit"><input type="number" min={-3} max={3} step={0.1} value={draft.heatDelta} onChange={(event) => set('heatDelta', Number(event.target.value))} /><b>K</b></div></label>
      <label><span>{t('stockRun.coolDelta')}</span><div className="input-with-unit"><input type="number" min={-3} max={3} step={0.1} value={draft.coolDelta} onChange={(event) => set('coolDelta', Number(event.target.value))} /><b>K</b></div></label>
      {options?.features.weather_scenario ? <label className="field-wide"><span><CloudSun size={13} />{t('stockRun.weather')}</span><select value={draft.weatherDatasetId} onChange={(event) => set('weatherDatasetId', event.target.value)}><option value="">{t('stockRun.projectWeather')}</option>{options.weather_datasets.map((weather) => <option key={weather.id} value={weather.id}>{weather.name} · {weather.snapshot_hash.slice(0, 10)}</option>)}</select></label> : null}
      <label className="field-wide"><span>{t('stockRun.reason')}</span><textarea value={draft.reason} onChange={(event) => set('reason', event.target.value)} placeholder={t('stockRun.reasonPlaceholder')} rows={2} /></label>
      <label><span>{t('stockRun.sourceType')}</span><select value={draft.sourceType ?? ''} onChange={(event) => set('sourceType', event.target.value as StockRunDraft['sourceType'])}>{options?.source_types.map((source) => <option key={source} value={source}>{t(`config.${source === 'human_judgement' ? 'human' : source}`)}</option>)}</select></label>
      <label><span>{t('stockRun.sourceRef')}</span><input value={draft.sourceRef} onChange={(event) => set('sourceRef', event.target.value)} /></label>
    </div> : <div className="stock-baseline-lock"><span>{t('stockRun.baselineLocked')}</span><code>0.0 K · PROJECT EPW</code></div>}
    <StockInputPolicyPanel
      workflow={workflow}
      mode="run"
      district={workflow === 'neighborhood' && draft.scopeMode === 'district' ? draft.district : null}
      buildingRef={workflow === 'neighborhood' && draft.scopeMode === 'building' ? draft.buildingRef : null}
      onOverrideChange={policyChanged}
    />
    {issues.length ? <div className="stock-run-issues" role="status"><CircleAlert size={14} /><span>{issues.map((issue) => t(`stockRun.issues.${issue}`)).join(' · ')}</span></div> : null}
    <button className="primary-button neighborhood-start" onClick={submit} disabled={disabled || pending || issues.length > 0 || !policyReady}><Play size={16} />{draft.mode === 'scenario' ? t('stockRun.startScenario') : t('stockRun.startBaseline')}</button>
  </section>
}
