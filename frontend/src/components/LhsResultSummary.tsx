import { CheckCircle2, CircleAlert, FlaskConical } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { LhsResult } from '../lib/types'

const number = (value: number | null | undefined, digits = 2) =>
  value == null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value)

const outputs = ['heating_kwh_m2', 'cooling_kwh_m2', 'co2_kg_m2'] as const

export default function LhsResultSummary({ result, blocked = false }: { result?: LhsResult | null; blocked?: boolean }) {
  const { t } = useTranslation()
  if (!result) return <div className="simulation-empty"><FlaskConical size={24} /><p>{t(blocked ? 'lhs.integrityBlocked' : 'lhs.noHistory')}</p></div>
  const statistics = result.summary.statistics
  const validated = result.qa.scientific_status === 'VALIDATED'
  if (!validated) return <div className="simulation-result-scroll lhs-summary">
    <div className="scientific-banner invalid"><CircleAlert size={18} /><span><strong>{result.qa.scientific_status}</strong><small>{t('lhs.unverified')}</small></span></div>
    <section className="lhs-qa-workspace"><section>{result.qa.checks.map((check) => <div className={check.passed ? 'pass' : 'fail'} key={check.name}>{check.passed ? <CheckCircle2 size={15} /> : <CircleAlert size={15} />}<span><strong>{t(`lhs.checks.${check.name}`)}</strong><small>{t('lhs.expected')} {String(check.expected)} · {t('lhs.actual')} {String(check.actual)}</small></span></div>)}</section></section>
  </div>
  return <div className="simulation-result-scroll lhs-summary">
    <div className={`scientific-banner ${validated ? 'validated' : 'unverified'}`}>
      {validated ? <CheckCircle2 size={18} /> : <CircleAlert size={18} />}
      <span><strong>{result.qa.scientific_status}</strong><small>{t(validated ? 'lhs.validated' : 'lhs.unverified')}</small></span>
    </div>
    <section className="energy-hero-band lhs-energy-hero">
      <div><span>{t('lhs.heatingMean')}</span><strong>{number(statistics.heating_kwh_m2?.mean)}</strong><small>kWh/m²·yr</small></div>
      <div><span>{t('lhs.coolingMean')}</span><strong>{number(statistics.cooling_kwh_m2?.mean)}</strong><small>kWh/m²·yr</small></div>
      <div><span>{t('lhs.carbonMean')}</span><strong>{number(statistics.co2_kg_m2?.mean)}</strong><small>kgCO₂/m²·yr</small></div>
      <div><span>{t('lhs.samples')}</span><strong>{result.summary.samples_completed}</strong><small>N={result.summary.samples_expected}</small></div>
    </section>
    <section className="simulation-section"><header><span className="eyebrow">{t('lhs.uncertaintyBand')}</span></header>
      <table className="simulation-table"><thead><tr><th>{t('lhs.output')}</th><th>{t('lhs.mean')}</th><th>{t('lhs.median')}</th><th>P5</th><th>P95</th></tr></thead><tbody>
        {outputs.map((output) => <tr key={output}><td>{t(`lhs.outputs.${output}`)}</td><td>{number(statistics[output]?.mean)}</td><td>{number(statistics[output]?.median)}</td><td>{number(statistics[output]?.p5)}</td><td>{number(statistics[output]?.p95)}</td></tr>)}
      </tbody></table>
    </section>
    <section className="simulation-section"><header><span className="eyebrow">{t('lhs.strongestDrivers')}</span></header>
      <table className="simulation-table"><thead><tr><th>{t('lhs.output')}</th><th>{t('lhs.variable')}</th><th>ρ</th></tr></thead><tbody>
        {outputs.map((output) => { const driver = result.sensitivity[output]?.[0]; return <tr key={output}><td>{t(`lhs.outputs.${output}`)}</td><td>{driver ? t(`lhs.variables.${driver.variable}`) : '—'}</td><td className={driver && driver.rho < 0 ? 'negative-number' : 'positive-number'}>{number(driver?.rho, 2)}</td></tr> })}
      </tbody></table>
    </section>
  </div>
}
