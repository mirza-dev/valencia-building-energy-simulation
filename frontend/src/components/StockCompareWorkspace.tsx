import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { FeatureCollection } from 'geojson'
import { AlertTriangle, Check, CircleSlash2, Database, FlaskConical, MapPinned, ShieldCheck } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { availableRowMetrics } from '../lib/stockCompare'
import type { StockCompareEvidence, StockCompareKind, StockComparison } from '../lib/types'
import StockCompareMap from './StockCompareMap'

const metricUnits: Record<string, string> = {
  heating_kwh_m2: 'kWh/m²·yr', cooling_kwh_m2: 'kWh/m²·yr',
  cons_hc_kwh_m2: 'kWh/m²·yr', total_site_kwh_m2: 'kWh/m²·yr',
  s1_co2_kg_m2: 'kgCO₂/m²·yr', s2_co2_kg_m2: 'kgCO₂/m²·yr',
  hvac_co2_kg_m2: 'kgCO₂/m²·yr', total_site_co2_kg_m2: 'kgCO₂/m²·yr',
  heating_gwh: 'GWh/yr', cooling_gwh: 'GWh/yr', cons_hc_gwh: 'GWh/yr', total_site_gwh: 'GWh/yr',
  s1_co2_t: 'tCO₂/yr', s2_co2_t: 'tCO₂/yr', hvac_co2_t: 'tCO₂/yr', total_site_co2_t: 'tCO₂/yr',
}

function value(value: number | null, digits = 2) {
  return value == null ? '—' : value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function ScenarioEvidence({ side, evidence }: { side: 'A' | 'B'; evidence: StockCompareEvidence }) {
  const { t } = useTranslation()
  const scenario = evidence.scenario
  return <section className="stock-evidence-column">
    <header><span>{side}</span><div><strong>{evidence.scenario_name}</strong><small>{evidence.scope} · {t(`stockCompare.runMode.${evidence.run_mode}`)}</small></div></header>
    <dl>
      <div><dt>{t('stockCompare.integrity')}</dt><dd><ShieldCheck size={13} />{evidence.verification_status}</dd></div>
      <div><dt>{t('stockCompare.scientific')}</dt><dd><FlaskConical size={13} />{evidence.scientific_status}</dd></div>
      <div><dt>EPW SHA</dt><dd><code>{evidence.weather_snapshot_hash?.slice(0, 12) ?? '—'}</code></dd></div>
      <div><dt>{t('stockCompare.offsets')}</dt><dd>{scenario ? `${scenario.heat_delta_c >= 0 ? '+' : ''}${scenario.heat_delta_c.toFixed(1)} / ${scenario.cool_delta_c >= 0 ? '+' : ''}${scenario.cool_delta_c.toFixed(1)} K` : '0.0 / 0.0 K'}</dd></div>
      <div><dt>{t('stockCompare.provenance')}</dt><dd>{scenario ? `${scenario.source_type}${scenario.source_ref ? ` · ${scenario.source_ref}` : ''}` : t('stockCompare.lockedBaseline')}</dd></div>
    </dl>
    {scenario?.reason ? <p>{scenario.reason}</p> : null}
  </section>
}

export default function StockCompareWorkspace({ kind, comparison, loading }: {
  kind: StockCompareKind
  comparison?: StockComparison
  loading: boolean
}) {
  const { t } = useTranslation()
  const [group, setGroup] = useState<'clusters' | 'districts'>('clusters')
  const rows = comparison ? (group === 'districts' ? comparison.districts : comparison.clusters) : []
  const metrics = useMemo(() => availableRowMetrics(rows), [rows])
  const [metric, setMetric] = useState('heating_kwh_m2')
  const [selected, setSelected] = useState('')
  useEffect(() => {
    if (!metrics.includes(metric)) setMetric(metrics[0] ?? '')
  }, [metric, metrics])
  useEffect(() => {
    if (!rows.some((row) => row.key === selected)) setSelected(rows[0]?.key ?? '')
  }, [rows, selected])
  useEffect(() => {
    setGroup('clusters')
    setMetric('heating_kwh_m2')
    setSelected('')
  }, [comparison?.left.id, comparison?.right.id, kind])

  const neighborhoodMap = useQuery<FeatureCollection>({
    queryKey: ['stock-compare-neighborhood-map', comparison?.left.id],
    queryFn: () => api.neighborhoodMap(comparison!.left.id),
    enabled: kind === 'neighborhood' && Boolean(comparison?.compatibility.interpretation_enabled && comparison.left.map_descriptor),
  })
  const cityMap = useQuery({
    queryKey: ['stock-compare-city-map', comparison?.left.id],
    queryFn: () => api.cityMapMetrics(comparison!.left.id),
    enabled: kind === 'city' && Boolean(comparison?.compatibility.interpretation_enabled),
  })
  if (loading) return <div className="stock-compare-empty"><span className="spinner" />{t('common.loading')}</div>
  if (!comparison) return <div className="stock-compare-empty"><Database size={22} /><strong>{t('stockCompare.selectTwo')}</strong><p>{t('stockCompare.selectTwoText')}</p></div>

  const compatibility = comparison.compatibility
  const bounds = kind === 'neighborhood' ? comparison.left.map_descriptor?.bounds : cityMap.data?.bounds
  const mapReady = Boolean(bounds && metric && rows.length && (kind === 'city' ? cityMap.data : neighborhoodMap.data))
  return <div className="stock-compare-workspace" data-compare-kind={kind} data-percent-enabled={compatibility.percent_enabled}>
    <section className={`stock-compatibility ${!compatibility.interpretation_enabled ? 'blocked' : compatibility.percent_enabled ? 'pass' : 'warning'}`}>
      <div className="stock-compatibility-title">
        {!compatibility.interpretation_enabled ? <CircleSlash2 size={18} /> : compatibility.percent_enabled ? <Check size={18} /> : <AlertTriangle size={18} />}
        <div><strong>{t(!compatibility.interpretation_enabled ? 'stockCompare.blocked' : compatibility.percent_enabled ? 'stockCompare.comparable' : 'stockCompare.absoluteOnly')}</strong><small>{t(!compatibility.interpretation_enabled ? 'stockCompare.blockedText' : compatibility.percent_enabled ? 'stockCompare.comparableText' : 'stockCompare.absoluteOnlyText')}</small></div>
      </div>
      <div className="stock-check-grid">{compatibility.checks.map((check) => <span key={check.id} className={check.passed ? 'pass' : 'fail'}>{check.passed ? <Check size={12} /> : <AlertTriangle size={12} />}{t(`stockCompare.checks.${check.id}`)}</span>)}</div>
    </section>

    <div className="stock-evidence-grid"><ScenarioEvidence side="A" evidence={comparison.left} /><ScenarioEvidence side="B" evidence={comparison.right} /></div>

    {compatibility.interpretation_enabled ? <>
      <section className="stock-total-ledger">
        <div className="table-head"><span>{t('stockCompare.totalDelta')}</span><code>{compatibility.percent_enabled ? t('stockCompare.absoluteAndPercent') : t('stockCompare.absolute')}</code></div>
        <table><thead><tr><th>{t('compare.field')}</th><th>A</th><th>B</th><th>Δ</th><th>Δ%</th></tr></thead><tbody>{comparison.metrics.map((item) => <tr key={item.key}><td><strong>{t(`stockCompare.metrics.${item.key}`)}</strong><small>{item.unit}</small></td><td>{value(item.left)}</td><td>{value(item.right)}</td><td className={(item.delta ?? 0) > 0 ? 'increase' : (item.delta ?? 0) < 0 ? 'decrease' : ''}>{value(item.delta)}</td><td>{item.percent == null ? '—' : `${item.percent > 0 ? '+' : ''}${value(item.percent, 1)}%`}</td></tr>)}</tbody></table>
      </section>

      <section className="stock-map-ledger">
        <div className="stock-map-toolbar"><div><MapPinned size={15} /><strong>{t('stockCompare.spatialDelta')}</strong></div><div className="segmented-control"><button className={group === 'clusters' ? 'active' : ''} onClick={() => setGroup('clusters')}>{t('stockCompare.clusters')}</button>{kind === 'city' ? <button className={group === 'districts' ? 'active' : ''} onClick={() => setGroup('districts')}>{t('stockCompare.districts')}</button> : null}</div><label><span>{t('stockCompare.mapMetric')}</span><select value={metric} onChange={(event) => setMetric(event.target.value)}>{metrics.map((key) => <option key={key} value={key}>{t(`stockCompare.metrics.${key}`)}</option>)}</select></label></div>
        <div className="stock-spatial-layout">
          <div className="stock-map-stage">{mapReady && bounds ? <StockCompareMap kind={kind} rows={rows} metric={metric} unit={metricUnits[metric] ?? ''} group={group} selected={selected} bounds={bounds} geojson={neighborhoodMap.data} tileUrl={kind === 'city' ? api.cityTileUrl(comparison.left.id) : undefined} onSelect={setSelected} /> : <div className="stock-map-loading">{neighborhoodMap.isError || cityMap.isError ? <><AlertTriangle size={18} />{t('stockCompare.mapUnavailable')}</> : <><span className="spinner" />{t('stockCompare.mapLoading')}</>}</div>}</div>
          <div className="stock-row-ledger" role="list" aria-label={t(group === 'districts' ? 'stockCompare.districts' : 'stockCompare.clusters')}>{rows.map((row) => {
            const item = row.metrics[metric]
            return <button key={row.key} className={selected === row.key ? 'active' : ''} onClick={() => setSelected(row.key)} role="listitem"><span><strong>{row.label}</strong><small>{value(row.left_buildings, 0)} / {value(row.right_buildings, 0)} {t('stockCompare.buildings')}</small></span><span><em>A {value(item?.left)}</em><em>B {value(item?.right)}</em></span><b className={(item?.delta ?? 0) > 0 ? 'increase' : (item?.delta ?? 0) < 0 ? 'decrease' : ''}>{item?.delta == null ? '—' : `${item.delta > 0 ? '+' : ''}${value(item.delta)}`}<small>{item?.percent == null ? t('stockCompare.absoluteShort') : `${item.percent > 0 ? '+' : ''}${value(item.percent, 1)}%`}</small></b></button>
          })}</div>
        </div>
      </section>
    </> : <section className="stock-interpretation-blocked"><CircleSlash2 size={24} /><strong>{t('stockCompare.energyHidden')}</strong><p>{t('stockCompare.energyHiddenText')}</p></section>}
  </div>
}
