import { Activity, AlertTriangle, CheckCircle2, CloudSun, Gauge, GitBranch, Thermometer, Wind } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { SimulationResult, SimulationRun } from '../lib/types'
import { PrimaryMetricStrip } from './FocusedWorkspace'
import RunArtifactLinks from './RunArtifactLinks'

const number = (value: number | null | undefined, digits = 2) =>
  value == null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value)

export function SimulationFocusedResult({ result }: { result: SimulationResult | null | undefined }) {
  const { t, i18n } = useTranslation()
  if (!result) return <div className="simulation-focused-empty"><Activity size={25} /><p>{t('simulation.noResult')}</p></div>
  const invalid = result.qa.scientific_status === 'INVALID'
  const detailedHvac = result.settings.energy_basis === 'detailed_hvac_consumption'
  const energy = result.normalized_energy
  const carbon = result.carbon
  if (invalid) return <section className="diagnostic-panel"><span className="eyebrow">{t('simulation.diagnostic')}</span><h2>{t('simulation.invalidTitle')}</h2><p>{result.qa.error_summary ?? t('simulation.invalidEvidence')}</p><dl className="metadata-list"><div><dt>Warning</dt><dd>{result.warnings.warnings}</dd></div><div><dt>Severe</dt><dd>{result.warnings.severes}</dd></div><div><dt>Fatal</dt><dd>{result.warnings.fatals}</dd></div></dl></section>
  return <div className="simulation-focused-result">
    {result.settings.scenario ? <section className="part-g-result-strip">
      <span><GitBranch size={14} /><small>PART G</small><strong>{result.settings.scenario.name}</strong></span>
      <span><Thermometer size={14} /><small>HEAT</small><strong>{result.settings.scenario.heat_delta_c > 0 ? '+' : ''}{result.settings.scenario.heat_delta_c} K</strong></span>
      <span><Wind size={14} /><small>COOL</small><strong>{result.settings.scenario.cool_delta_c > 0 ? '+' : ''}{result.settings.scenario.cool_delta_c} K</strong></span>
      <span><CloudSun size={14} /><small>EPW</small><code>{result.settings.scenario.weather_snapshot_hash.slice(0, 10)}</code></span>
    </section> : null}
    <PrimaryMetricStrip metrics={detailedHvac ? [
      { label: t('simulation.detailedHvac'), value: number(energy?.hvac_consumption_kwh_m2), unit: 'kWh/m²·yr' },
      { label: t('simulation.totalSite'), value: number(energy?.total_site_kwh_m2), unit: 'kWh/m²·yr' },
      { label: t('simulation.totalSiteCarbon'), value: number(carbon?.total_site_co2_kg_m2 as number), unit: 'kgCO₂/m²·yr' },
    ] : [
      { label: t('simulation.heating'), value: number(energy?.heating_kwh_m2), unit: 'kWh/m²·yr' },
      { label: t('simulation.cooling'), value: number(energy?.cooling_kwh_m2), unit: 'kWh/m²·yr' },
      { label: 'S2 CO₂', value: number(carbon?.s2_co2_kg_m2 as number), unit: 'kgCO₂/m²·yr' },
    ]} />
    <section className={`energy-basis-strip ${detailedHvac ? 'detailed-hvac-basis' : 'ideal-loads-basis'}`}>
      <span><NetworkBasisIcon /><small>{t('simulation.energyBasis').toLocaleUpperCase(i18n.language)}</small><strong title={t(detailedHvac ? 'simulation.detailedHvacBasis' : 'simulation.idealLoadsBasis')}>{t(detailedHvac ? 'simulation.detailedHvacBasis' : 'simulation.idealLoadsBasis')}</strong></span>
    </section>
    <div className="simulation-focused-context"><p>{t(detailedHvac ? 'simulation.detailedHvacBasisNote' : 'simulation.idealLoadsBasisNote')}</p><code>{number(result.settings.conditioned_residential_area_m2, 1)} m²</code></div>
  </div>
}

export function SimulationResultPanel({ result }: { result: SimulationResult | null | undefined }) {
  const { t, i18n } = useTranslation()
  if (!result) return <div className="simulation-empty"><Activity size={24} /><p>{t('simulation.noResult')}</p></div>
  const invalid = result.qa.scientific_status === 'INVALID'
  const energy = result.normalized_energy
  const raw = result.raw_energy
  const carbon = result.carbon
  const cadastre = result.cadastre_heating
  const detailedHvac = result.settings.energy_basis === 'detailed_hvac_consumption'

  return <div className="simulation-result-scroll">
    <div className={`scientific-banner ${result.qa.scientific_status.toLowerCase()}`}>
      {invalid ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}
      <span><strong>{result.qa.scientific_status}</strong><small>{invalid ? t('simulation.invalidEvidence') : t('simulation.validatedEvidence')}</small></span>
    </div>
    {result.settings.scenario ? <section className="part-g-result-strip">
      <span><GitBranch size={14} /><small>PART G</small><strong>{result.settings.scenario.name}</strong></span>
      <span><Thermometer size={14} /><small>HEAT</small><strong>{result.settings.scenario.heat_delta_c > 0 ? '+' : ''}{result.settings.scenario.heat_delta_c} K</strong></span>
      <span><Wind size={14} /><small>COOL</small><strong>{result.settings.scenario.cool_delta_c > 0 ? '+' : ''}{result.settings.scenario.cool_delta_c} K</strong></span>
      <span><CloudSun size={14} /><small>EPW</small><code>{result.settings.scenario.weather_snapshot_hash.slice(0, 10)}</code></span>
    </section> : null}
    <section className={`energy-basis-strip ${detailedHvac ? 'detailed-hvac-basis' : 'ideal-loads-basis'}`}>
      <span><NetworkBasisIcon /><small>{t('simulation.energyBasis').toLocaleUpperCase(i18n.language)}</small><strong title={t(detailedHvac ? 'simulation.detailedHvacBasis' : 'simulation.idealLoadsBasis')}>{t(detailedHvac ? 'simulation.detailedHvacBasis' : 'simulation.idealLoadsBasis')}</strong></span>
      {detailedHvac ? <><span><Gauge size={14} /><small>HVAC + FANS</small><strong>{number(energy?.hvac_consumption_kwh_m2)} kWh/m²</strong></span><span><Activity size={14} /><small>{t('simulation.totalSite').toLocaleUpperCase(i18n.language)}</small><strong>{number(energy?.total_site_kwh_m2)} kWh/m²</strong></span></> : null}
    </section>
    <p className="energy-basis-explainer">{t(detailedHvac ? 'simulation.detailedHvacBasisNote' : 'simulation.idealLoadsBasisNote')}</p>
    {invalid ? <section className="diagnostic-panel"><span className="eyebrow">{t('simulation.diagnostic')}</span><h2>{t('simulation.invalidTitle')}</h2><p>{result.qa.error_summary ?? t('simulation.invalidEvidence')}</p><dl className="metadata-list"><div><dt>Warning</dt><dd>{result.warnings.warnings}</dd></div><div><dt>Severe</dt><dd>{result.warnings.severes}</dd></div><div><dt>Fatal</dt><dd>{result.warnings.fatals}</dd></div></dl></section> : <>
      <section className="energy-hero-band">
        <div><span><Thermometer size={15} />{t('simulation.heating')}</span><strong>{number(energy?.heating_kwh_m2)}</strong><small>kWh/m²·yr</small></div>
        <div><span><Wind size={15} />{t('simulation.cooling')}</span><strong>{number(energy?.cooling_kwh_m2)}</strong><small>kWh/m²·yr</small></div>
        <div><span><Gauge size={15} />{t('simulation.areaBasis')}</span><strong>{number(result.settings.conditioned_residential_area_m2, 1)}</strong><small>m²</small></div>
      </section>
      <section className="simulation-section">
        <header><span className="eyebrow">{t('simulation.rawEnergy')}</span><code>{detailedHvac ? 'SQL · END USES' : 'SQL · RUN PERIOD'}</code></header>
        <table className="simulation-table"><thead><tr><th>{t('simulation.metric')}</th><th>J</th><th>kWh</th><th>kWh/m²</th></tr></thead><tbody>
          <tr><td>{t('simulation.heating')}</td><td>{number(raw?.heating.joule, 0)}</td><td>{number(raw?.heating.kwh, 2)}</td><td>{number(raw?.heating.kwh_m2, 4)}</td></tr>
          <tr><td>{t('simulation.cooling')}</td><td>{number(raw?.cooling.joule, 0)}</td><td>{number(raw?.cooling.kwh, 2)}</td><td>{number(raw?.cooling.kwh_m2, 4)}</td></tr>
        </tbody></table>
      </section>
      <section className="simulation-section">
        <header><span className="eyebrow">{t('simulation.carbon')}</span><code>{t('simulation.operationalOnly')}</code></header>
        <table className="simulation-table"><thead><tr><th>{t('simulation.scenario')}</th><th>kWh/m²</th><th>kgCO₂/m²</th><th>tCO₂/yr</th></tr></thead><tbody>{detailedHvac ? <>
          <tr><td>{t('simulation.detailedHvac')}</td><td>{number(energy?.hvac_consumption_kwh_m2)}</td><td>{number(carbon?.hvac_co2_kg_m2 as number)}</td><td>{number(carbon?.hvac_co2_t_yr as number, 1)}</td></tr>
          <tr><td>{t('simulation.totalSite')}</td><td>{number(energy?.total_site_kwh_m2)}</td><td>{number(carbon?.total_site_co2_kg_m2 as number)}</td><td>{number(carbon?.total_site_co2_t_yr as number, 1)}</td></tr>
        </> : [1, 2].map((index) => <tr key={index}><td>{String(carbon?.[`s${index}_scenario`] ?? `S${index}`)}</td><td>{number(carbon?.[`s${index}_consumption_kwh_m2`] as number)}</td><td>{number(carbon?.[`s${index}_co2_kg_m2`] as number)}</td><td>{number(carbon?.[`s${index}_co2_t_yr`] as number, 1)}</td></tr>)}</tbody></table>
      </section>
      <section className="simulation-section cadastre-section">
        <header><span className="eyebrow">{t('simulation.cadastre')}</span><code>{t('simulation.heatingOnly')}</code></header>
        <div className="cadastre-ledger"><div><span>{t('simulation.baselineHeating')}</span><strong>{number(cadastre.baseline_heating_kwh_m2)}</strong><small>kWh/m²·yr</small></div><div><span>{t('simulation.interventionHeating')}</span><strong>{number(cadastre.post_intervention_heating_kwh_m2)}</strong><small>kWh/m²·yr</small></div></div>
        <p>{t('simulation.noCoolingReference')}</p>
      </section>
    </>}
  </div>
}

function NetworkBasisIcon() {
  return <span aria-hidden="true" className="network-basis-icon">↔</span>
}

export function SimulationEvidencePanel({ run }: { run: SimulationRun | null | undefined }) {
  const { t } = useTranslation()
  if (!run) return <div className="simulation-empty compact"><p>{t('simulation.evidencePending')}</p></div>
  const result = run.result
  return <div className="simulation-evidence-scroll">
    {result ? <>
      <section className="evidence-block"><span className="eyebrow">{t('simulation.qaLedger')}</span><div className="qa-count-strip"><span>{t('common.warn')}<strong>{result.warnings.warnings}</strong></span><span>{t('common.severe')}<strong>{result.warnings.severes}</strong></span><span>{t('common.fatal')}<strong>{result.warnings.fatals}</strong></span></div>
        <div className="simulation-checks">{result.qa.checks.map((check) => 'check' in check ? <div key={check.check} className={check.passed ? 'pass' : 'fail'}><i>{check.passed ? '✓' : '!'}</i><span><strong title={check.check}>{check.check}</strong><small>{String(check.model)} ↔ {String(check.eplus)} · tol {String(check.tolerance)}</small></span></div> : null)}</div>
      </section>
      <section className="evidence-block"><span className="eyebrow">{t('simulation.warningCategories')}</span><div className="warning-ledger">{result.warnings.categories.map((item) => <div key={item.category}><span>{item.category}</span><strong>{item.count}</strong></div>)}{!result.warnings.categories.length ? <p>{t('common.none')}</p> : null}</div></section>
    </> : <section className="evidence-block"><p>{t('simulation.evidencePending')}</p></section>}
    <section className="evidence-block artifact-viewer-block"><RunArtifactLinks run={run} /></section>
    <section className="evidence-block"><span className="eyebrow">{t('runs.artifacts')}</span><div className="artifact-list">{run.artifacts?.map((artifact) => <div key={artifact.name}><span>{artifact.name}</span><code>{artifact.sha256.slice(0, 10)}</code></div>)}</div></section>
  </div>
}
