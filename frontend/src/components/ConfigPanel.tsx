import { useEffect, useState } from 'react'
import { ChevronDown, FlaskConical, Info, LockKeyhole, SlidersHorizontal } from 'lucide-react'
import { useForm, type FieldPath } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import type { BuildConfig, Profile, SourceType } from '../lib/types'
import type { BuildConfigErrors } from '../lib/buildConfigValidation'

type Register = ReturnType<typeof useForm<BuildConfig>>['register']

function NumberField({ label, name, register, errors, touched, onTouched, unit, step = 0.01, nullable = false, hint }: {
  label: string
  name: FieldPath<BuildConfig>
  register: Register
  errors: BuildConfigErrors
  touched: Set<string>
  onTouched: (name: string) => void
  unit?: string
  step?: number
  nullable?: boolean
  hint?: string
}) {
  const { t } = useTranslation()
  const registration = register(name, { setValueAs: (value) => nullable && (value === '' || value == null) ? null : Number(value) })
  const error = touched.has(name) ? errors[name] : undefined
  const errorId = `${name.replaceAll('.', '-')}-error`
  return (
    <label className={`field-row ${error ? 'has-error' : ''}`}>
      <span>{label}{hint ? <span className="hint-icon" title={hint}><Info size={13} /></span> : null}</span>
      <span className="input-with-unit">
        <input type="number" step={step} {...registration}
          onBlur={(event) => { void registration.onBlur(event); onTouched(name) }}
          aria-invalid={Boolean(error)} aria-describedby={error ? errorId : undefined} />
        {unit ? <em>{unit}</em> : null}
      </span>
      {error ? <small className="field-error" id={errorId}>{t(error, { defaultValue: t('validation.invalid') })}</small> : null}
    </label>
  )
}

function ToggleField({ label, name, register, hint }: { label: string; name: FieldPath<BuildConfig>; register: Register; hint?: string }) {
  return (
    <label className="toggle-row">
      <span>{label}{hint ? <span className="hint-icon" title={hint}><Info size={13} /></span> : null}</span>
      <span className="switch"><input type="checkbox" {...register(name)} /><i /></span>
    </label>
  )
}

export default function ConfigPanel({ config, profiles, changedCount, validationErrors, onChange, onProfileRequest,
  rationale, onRationale, sourceType, onSourceType, sourceRef, onSourceRef }: {
  config: BuildConfig
  profiles: Profile[]
  changedCount: number
  validationErrors: BuildConfigErrors
  onChange: (config: BuildConfig) => void
  onProfileRequest: (profile: Profile) => void
  rationale: string
  onRationale: (value: string) => void
  sourceType: SourceType
  onSourceType: (value: SourceType) => void
  sourceRef: string
  onSourceRef: (value: string) => void
}) {
  const { t } = useTranslation()
  const [expert, setExpert] = useState(false)
  const [touched, setTouched] = useState<Set<string>>(new Set())
  const form = useForm<BuildConfig>({ defaultValues: config, mode: 'onChange' })
  const { register, reset, watch } = form
  const touch = (name: string) => setTouched((current) => new Set(current).add(name))

  useEffect(() => { reset(config); setTouched(new Set()) }, [config.provenance.baseline_profile, reset])
  useEffect(() => {
    const subscription = watch((value) => onChange(value as BuildConfig))
    return () => subscription.unsubscribe()
  }, [watch, onChange])
  const numberProps = { register, errors: validationErrors, touched, onTouched: touch }
  const scenarioError = touched.has('provenance.scenario_name') ? validationErrors['provenance.scenario_name'] : undefined

  return (
    <div className="config-panel">
      <div className="panel-section profile-section">
        <div className="section-heading"><LockKeyhole size={15} /><span>{t('builder.baseline')}</span></div>
        <select value={config.provenance.baseline_profile} aria-label={t('builder.baseline')} onChange={(event) => {
          const selected = profiles.find((profile) => profile.id === event.target.value)
          if (selected) onProfileRequest(selected)
        }}>
          {profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.label}</option>)}
        </select>
        <p className="source-note">{profiles.find((profile) => profile.id === config.provenance.baseline_profile)?.source}</p>
        <label className={`stacked-field ${scenarioError ? 'has-error' : ''}`}><span>{t('builder.scenario')}</span>
          <input {...register('provenance.scenario_name')} onBlur={() => touch('provenance.scenario_name')}
            aria-invalid={Boolean(scenarioError)} aria-describedby={scenarioError ? 'scenario-name-error' : undefined} />
          {scenarioError ? <small className="field-error" id="scenario-name-error">{t(scenarioError)}</small> : null}
        </label>
      </div>

      <div className="panel-section">
        <div className="section-heading"><FlaskConical size={15} /><span>{t('config.envelope')}</span></div>
        <NumberField label={t('config.wallU')} name="envelope.wall_u" {...numberProps} unit="W/m²K" nullable hint="None resolves to pilot IVE U=1.33." />
        <NumberField label={t('config.roofU')} name="envelope.roof_u" {...numberProps} unit="W/m²K" nullable hint="None uses Cubierta plana no aislada from the template." />
        <NumberField label={t('config.windowU')} name="envelope.window_u" {...numberProps} unit="W/m²K" />
        <NumberField label={t('config.windowG')} name="envelope.window_g" {...numberProps} step={0.01} />
        <NumberField label={t('config.bridge')} name="envelope.thermal_bridge_du" {...numberProps} unit="W/m²K" />
        <NumberField label={t('config.infiltration')} name="operation.infiltration_ach" {...numberProps} unit="ACH" nullable />
        <NumberField label={t('config.blindSetpoint')} name="shading.setpoint_w_m2" {...numberProps} unit="W/m²" step={10} />
        <ToggleField label={t('config.groundBuffer')} name="geometry.ground_unconditioned" register={register} />
        <ToggleField label={t('config.contextShading')} name="shading.context_enabled" register={register} />
        <ToggleField label={t('config.massless')} name="envelope.massless" register={register} hint="Intended only for isolated LHS U-value studies." />
      </div>

      <button className={`expert-trigger ${expert ? 'active' : ''}`} type="button" onClick={() => setExpert(!expert)} aria-expanded={expert}>
        <SlidersHorizontal size={15} /> {t('builder.expert')} <ChevronDown size={15} />
      </button>

      {expert ? <div className="expert-sections">
        <details open><summary>{t('config.geometry')}</summary><div className="details-body">
          <NumberField label={t('config.floorHeight')} name="geometry.floor_height_m" {...numberProps} unit="m" />
          <NumberField label={t('config.simplify')} name="geometry.simplify_tolerance_m" {...numberProps} unit="m" />
          <NumberField label={t('config.maxArea')} name="geometry.max_area_delta_fraction" {...numberProps} step={0.001} />
          <NumberField label={t('config.footprintMin')} name="geometry.footprint_min_m2" {...numberProps} unit="m²" step={10} />
          <NumberField label={t('config.footprintMax')} name="geometry.footprint_max_m2" {...numberProps} unit="m²" step={100} />
          <NumberField label={t('config.partyTolerance')} name="geometry.party_wall_tolerance_m" {...numberProps} unit="m" />
          <NumberField label={t('config.sharedEdge')} name="geometry.min_shared_edge_m" {...numberProps} unit="m" />
          <NumberField label={t('config.partyRatio')} name="geometry.party_overlap_ratio" {...numberProps} step={0.05} />
          <NumberField label={t('config.contextRadius')} name="geometry.context_radius_m" {...numberProps} unit="m" step={5} />
          <ToggleField label={t('config.neighborGround')} name="geometry.neighbor_assume_ground" register={register} />
        </div></details>
        <details><summary>{t('config.openings')}</summary><div className="details-body">
          <NumberField label={t('config.wwrNorth')} name="openings.wwr_north" {...numberProps} />
          <NumberField label={t('config.wwrEast')} name="openings.wwr_east" {...numberProps} />
          <NumberField label={t('config.wwrSouth')} name="openings.wwr_south" {...numberProps} />
          <NumberField label={t('config.wwrWest')} name="openings.wwr_west" {...numberProps} />
          <NumberField label={t('config.windowWidth')} name="openings.window_width_m" {...numberProps} unit="m" />
          <NumberField label={t('config.windowHeight')} name="openings.window_height_m" {...numberProps} unit="m" />
          <NumberField label={t('config.windowSill')} name="openings.window_sill_m" {...numberProps} unit="m" />
          <NumberField label={t('config.doorWidth')} name="openings.door_width_m" {...numberProps} unit="m" />
          <NumberField label={t('config.doorHeight')} name="openings.door_height_m" {...numberProps} unit="m" />
          <NumberField label={t('config.doorSill')} name="openings.door_sill_m" {...numberProps} unit="m" />
          <NumberField label={t('config.balconyCount')} name="openings.balcony_doors_per_facade_floor" {...numberProps} step={1} />
          <NumberField label={t('config.balconyDepth')} name="openings.balcony_depth_m" {...numberProps} unit="m" />
        </div></details>
        <details><summary>{t('config.system')}</summary><div className="details-body">
          <NumberField label={t('config.summerStart')} name="shading.summer_start_month" {...numberProps} step={1} />
          <NumberField label={t('config.summerEnd')} name="shading.summer_end_month" {...numberProps} step={1} />
          <NumberField label={t('config.maxShadows')} name="shading.max_shadow_figures" {...numberProps} step={5000} />
          <NumberField label={t('config.wwrWarning')} name="qa.facade_wwr_warning_pct" {...numberProps} unit="%" step={1} />
          <label className="stacked-field read-only"><span>{t('config.blindReadonly')}</span><input value={config.shading.blind_name} readOnly /></label>
          {Object.entries(config.operation.output_variables).map(([key, value]) => <label className="stacked-field read-only" key={key}><span>{t('config.output')} · {key}</span><input value={value} readOnly /></label>)}
        </div></details>
      </div> : null}

      {changedCount > 0 ? <div className="override-block">
        <div className="override-head"><span>{t('config.override', { count: changedCount })}</span><strong>{t('config.provenance')}</strong></div>
        <textarea rows={3} value={rationale} onChange={(event) => onRationale(event.target.value)} placeholder={t('builder.rationale')} />
        <div className="override-grid"><select value={sourceType} onChange={(event) => onSourceType(event.target.value as SourceType)}>
          <option value="human_judgement">{t('config.human')}</option><option value="dataset">{t('config.dataset')}</option>
          <option value="publication">{t('config.publication')}</option><option value="supervisor">{t('config.supervisor')}</option><option value="other">{t('config.other')}</option>
        </select><input value={sourceRef} onChange={(event) => onSourceRef(event.target.value)} placeholder={t('builder.source')} /></div>
      </div> : null}
    </div>
  )
}
