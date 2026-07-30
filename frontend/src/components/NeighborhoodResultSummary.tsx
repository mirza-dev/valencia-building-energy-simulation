import { AlertTriangle, CheckCircle2, MapPinned } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { NeighborhoodResult } from '../lib/types'

const number = (value: number | null | undefined, digits = 2) =>
  value == null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value)

export default function NeighborhoodResultSummary({ result }: { result: NeighborhoodResult | null | undefined }) {
  const { t } = useTranslation()
  if (!result) return <div className="simulation-empty"><MapPinned size={24} /><p>{t('neighborhood.noHistory')}</p></div>
  const invalid = result.qa.scientific_status === 'INVALID'
  const totals = result.summary.totals
  return <div className="simulation-result-scroll neighborhood-summary">
    <div className={`scientific-banner ${result.qa.scientific_status.toLowerCase()}`}>{invalid ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}<span><strong>{result.qa.scientific_status}</strong><small>{result.summary.clusters_completed}/{result.summary.clusters_expected} clusters · {result.summary.qa_passed_clusters} QA PASS</small></span></div>
    {result.single_building ? <section className="single-building-evidence"><header><span className="eyebrow">{t('stockRun.singleBuildingEvidence')}</span><code>{result.single_building.refparcela}</code></header><pre>{result.single_building.report_text || t('stockRun.reportUnavailable')}</pre></section> : totals ? <section className="energy-hero-band"><div><span>{t('simulation.heating')}</span><strong>{number(totals.heating_gwh_yr)}</strong><small>GWh/yr</small></div><div><span>{t('simulation.cooling')}</span><strong>{number(totals.cooling_gwh_yr)}</strong><small>GWh/yr</small></div><div><span>{t('neighborhood.resArea')}</span><strong>{number(totals.residential_area_m2, 0)}</strong><small>m²</small></div></section> : <section className="diagnostic-panel"><h2>{t('neighborhood.invalidTitle')}</h2><p>{t('neighborhood.invalidText')}</p></section>}
    {!result.single_building ? <section className="simulation-section"><header><span className="eyebrow">{t('neighborhood.clusterLedger')}</span><code>{result.clusters.length}</code></header><table className="simulation-table"><thead><tr><th>Cluster</th><th>{t('neighborhood.buildings')}</th><th>{t('simulation.heating')}</th><th>{t('simulation.cooling')}</th></tr></thead><tbody>{result.clusters.map((item) => <tr key={item.cluster}><td>{item.cluster}</td><td>{item.n_buildings}</td><td>{number(item.heating_kwh_m2)}</td><td>{number(item.cooling_kwh_m2)}</td></tr>)}</tbody></table></section> : null}
  </div>
}
