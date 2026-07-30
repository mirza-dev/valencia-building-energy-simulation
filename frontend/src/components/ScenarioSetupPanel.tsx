import { useMemo, useState } from 'react'
import { ChevronRight, CloudSun, FileText, GitBranch, Play, Snowflake, Thermometer } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ScenarioOptions, ScenarioParent, ScenarioRequest, SourceType } from '../lib/types'
import { scenarioValid, validateScenario } from '../lib/scenarioValidation'

const sourceKeys: Record<SourceType, string> = {
  human_judgement: 'config.human',
  dataset: 'config.dataset',
  publication: 'config.publication',
  supervisor: 'config.supervisor',
  other: 'config.other',
}

export default function ScenarioSetupPanel({
  options, parent, disabled, pending, onStart,
}: {
  options: ScenarioOptions
  parent?: ScenarioParent
  disabled: boolean
  pending: boolean
  onStart: (request: ScenarioRequest) => void
}) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  const [heat, setHeat] = useState(0)
  const [cool, setCool] = useState(0)
  const [weatherId, setWeatherId] = useState('')
  const [reason, setReason] = useState('')
  const [sourceType, setSourceType] = useState<SourceType>('human_judgement')
  const [sourceRef, setSourceRef] = useState('')
  const request = useMemo<ScenarioRequest>(() => ({
    parent_run_id: parent?.id ?? '',
    name,
    heat_delta_c: heat,
    cool_delta_c: cool,
    weather_dataset_id: weatherId || null,
    reason,
    source_type: sourceType,
    source_ref: sourceRef.trim() || null,
  }), [cool, heat, name, parent?.id, reason, sourceRef, sourceType, weatherId])
  const errors = useMemo(
    () => validateScenario(request, parent?.weather_snapshot_hash, options.weather_datasets),
    [options.weather_datasets, parent?.weather_snapshot_hash, request],
  )
  const weather = options.weather_datasets.find((item) => item.id === weatherId)

  return <div className="scenario-form">
    <div className="scenario-field-grid">
      <label className={errors.name ? 'has-error' : ''}>
        <span><GitBranch size={13} />{t('scenario.name')}</span>
        <input value={name} maxLength={80} onChange={(event) => setName(event.target.value)} disabled={disabled} placeholder={t('scenario.namePlaceholder')} />
      </label>
      <label className={errors.weather ? 'has-error' : ''}>
        <span><CloudSun size={13} />{t('scenario.weather')}</span>
        <select value={weatherId} onChange={(event) => setWeatherId(event.target.value)} disabled={disabled}>
          <option value="">{t('scenario.parentWeather')} · {parent?.weather_snapshot_hash.slice(0, 10) ?? '—'}</option>
          {options.weather_datasets.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.snapshot_hash.slice(0, 10)}</option>)}
        </select>
      </label>
      <label className={errors.heat ? 'has-error' : ''} title={t('scenario.heatHint')}>
        <span><Thermometer size={13} />{t('scenario.heatOffset')}</span>
        <div className="scenario-number"><input type="number" min={-3} max={3} step={0.1} value={heat} onChange={(event) => setHeat(event.target.valueAsNumber)} disabled={disabled} /><em>K</em></div>
      </label>
      <label className={errors.cool ? 'has-error' : ''} title={t('scenario.coolHint')}>
        <span><Snowflake size={13} />{t('scenario.coolOffset')}</span>
        <div className="scenario-number"><input type="number" min={-3} max={3} step={0.1} value={cool} onChange={(event) => setCool(event.target.valueAsNumber)} disabled={disabled} /><em>K</em></div>
      </label>
    </div>
    <label className={`scenario-rationale ${errors.reason ? 'has-error' : ''}`}>
      <span><FileText size={13} />{t('scenario.reason')}</span>
      <textarea value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} disabled={disabled} rows={3} placeholder={t('scenario.reasonPlaceholder')} />
    </label>
    <div className="scenario-source-grid">
      <label><span>{t('scenario.sourceType')}</span><select value={sourceType} onChange={(event) => setSourceType(event.target.value as SourceType)} disabled={disabled}>{options.source_types.map((item) => <option key={item} value={item}>{t(sourceKeys[item])}</option>)}</select></label>
      <label><span>{t('scenario.sourceRef')}</span><input value={sourceRef} maxLength={500} onChange={(event) => setSourceRef(event.target.value)} disabled={disabled} placeholder={t('scenario.optional')} /></label>
    </div>
    {Object.keys(errors).length ? <div className="scenario-form-error" role="alert">{errors.weather === 'noop' ? t('scenario.noop') : t('scenario.invalid')}</div> : null}
    <div className="scenario-chain" aria-label={t('scenario.chain')}>
      <span><small>1</small>{t('scenario.chainParent')}</span><ChevronRight size={14} /><span><small>2</small>{t('scenario.chainVariant')}</span><ChevronRight size={14} /><span><small>3</small>{t('scenario.chainSimulation')}</span>
    </div>
    <div className="scenario-selection-evidence">
      <span>{t('scenario.selectedWeather')}</span>
      <code>{weather?.source_name ?? parent?.weather_source_name ?? '—'}</code>
    </div>
    <button className="primary-button simulation-start" onClick={() => onStart(request)} disabled={disabled || pending || !scenarioValid(errors)}><Play size={16} />{pending ? t('common.loading') : t('scenario.start')}</button>
  </div>
}
