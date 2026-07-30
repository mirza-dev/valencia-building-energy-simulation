import { AlertTriangle, Building2, CheckCircle2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { CityResult } from '../lib/types'

const number = (value: number | null | undefined, digits = 2) =>
  value == null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value)

export default function CityResultSummary({ result }: { result: CityResult | null | undefined }) {
  const { t } = useTranslation()
  if (!result) return <div className="simulation-empty"><Building2 size={24} /><p>{t('city.noHistory')}</p></div>
  const invalid = result.qa.scientific_status === 'INVALID'
  const totals = result.summary.totals
  return <div className="simulation-result-scroll neighborhood-summary city-summary">
    <div className={`scientific-banner ${result.qa.scientific_status.toLowerCase()}`}>{invalid ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}<span><strong>{result.qa.scientific_status}</strong><small>{result.summary.clusters_completed}/{result.summary.clusters_expected} clusters · {result.summary.districts}/19 districts · {result.summary.qa_passed_clusters} QA PASS</small></span></div>
    {totals ? <><section className="energy-hero-band city-energy-hero"><div><span>{t('city.hvacConsumption')}</span><strong>{number(totals.hvac_consumption_gwh_yr)}</strong><small>GWh/yr</small></div><div><span>{t('city.totalSite')}</span><strong>{number(totals.total_site_gwh_yr)}</strong><small>GWh/yr</small></div><div><span>{t('city.totalCarbon')}</span><strong>{number(totals.total_site_co2_t_yr, 0)}</strong><small>tCO₂/yr</small></div></section><section className="simulation-section city-demand-summary"><header><span className="eyebrow">{t('city.demandSecondary')}</span><code>IDEAL LOADS</code></header><div className="metric-pairs"><div><span>{t('simulation.heating')}</span><strong>{number(totals.heating_gwh_yr)} GWh/yr</strong></div><div><span>{t('simulation.cooling')}</span><strong>{number(totals.cooling_gwh_yr)} GWh/yr</strong></div><div><span>S1 / S2 CO₂</span><strong>{number(totals.s1_co2_t_yr, 0)} / {number(totals.s2_co2_t_yr, 0)} t/yr</strong></div><div><span>{t('city.resArea')}</span><strong>{number(totals.residential_area_m2, 0)} m²</strong></div></div></section></> : <section className="diagnostic-panel"><h2>{t('city.invalidTitle')}</h2><p>{t('city.invalidText')}</p></section>}
    <section className="simulation-section"><header><span className="eyebrow">{t('city.districtLedger')}</span><code>{result.districts.length}</code></header><table className="simulation-table"><thead><tr><th>{t('city.district')}</th><th>{t('city.buildings')}</th><th>{t('city.hvacConsumption')}</th><th>{t('city.totalSite')}</th><th>{t('city.demand')}</th></tr></thead><tbody>{result.districts.map((item) => <tr key={item.nombre}><td>{item.nombre}</td><td>{item.n_buildings}</td><td>{number(item.cons_hc_gwh)}</td><td>{number(item.total_site_gwh)}</td><td>{number(item.heating_gwh)} H · {number(item.cooling_gwh)} C</td></tr>)}</tbody></table></section>
    {result.validation ? <section className="simulation-section city-validation"><header><span className="eyebrow">{t('city.validation')}</span></header><pre>{result.validation}</pre></section> : null}
  </div>
}
