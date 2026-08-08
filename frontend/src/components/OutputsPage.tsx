import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, BarChart3, Building2, CheckCircle2, Download, ExternalLink, FileSearch, PackageCheck, Search } from 'lucide-react'
import { api } from '../lib/api'
import { formatProductBytes } from '../lib/productStock'
import type { ProductLedgerRow } from '../lib/types'

const PAGE_SIZE = 100
// Must stay a subset of `stock_runner.KEPT_ARTIFACTS`, which is what a stock run
// actually preserves.  `qa_report.txt` used to be listed here and always 404'd:
// it belongs to the single-building Part B path, not to the stock runner, whose
// QA record lives inside `deep_layers.json`.
const ARTIFACTS = [
  ['eplustbl.htm', 'EnergyPlus table'], ['deep_layers.json', 'Model layers and QA'],
  ['model_python.osm', 'OpenStudio model'], ['eplusout.err', 'EnergyPlus errors'],
  ['verified_profile.json', 'Verified profile'],
] as const

function number(value?: number, digits = 2) {
  return value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('en-GB', { maximumFractionDigits: digits })
}

export default function OutputsPage() {
  const [selected, setSelected] = useState('')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)
  const [building, setBuilding] = useState<ProductLedgerRow | null>(null)
  const [exportPlan, setExportPlan] = useState<Awaited<ReturnType<typeof api.stockExportPlan>> | null>(null)
  const [exportConfirmed, setExportConfirmed] = useState(false)

  const runs = useQuery({ queryKey: ['stock-runs'], queryFn: api.stockRuns, refetchInterval: 10_000 })
  useEffect(() => {
    if (!selected && runs.data?.runs[0]) setSelected(runs.data.runs[0].run)
  }, [runs.data, selected])
  const detail = useQuery({
    queryKey: ['stock-run', selected], queryFn: () => api.stockRun(selected), enabled: Boolean(selected),
    refetchInterval: (state) => state.state.data?.running ? 3_000 : false,
  })
  const ledger = useQuery({
    queryKey: ['stock-ledger', selected, query, status, offset],
    queryFn: () => api.stockLedger(selected, query, status, offset, PAGE_SIZE),
    enabled: Boolean(selected),
  })
  const summary = detail.data?.summary
  const totals = summary?.totals
  const exportPlanMutation = useMutation({
    mutationFn: () => api.stockExportPlan(selected),
    onSuccess: (value) => { setExportPlan(value); setExportConfirmed(false) },
  })

  return <div className="product-page outputs-page">
    <header className="product-page-header outputs-header">
      <div><span>03 / EVIDENCE</span><h1>Outputs</h1><p>Read aggregate results, audit every building and open the files preserved by the runner.</p></div>
      <div className="output-run-picker"><label><span>RUN</span><select value={selected} onChange={(event) => { setSelected(event.target.value); setOffset(0); setBuilding(null); setExportPlan(null) }}>
        {(runs.data?.runs ?? []).map((run) => <option key={run.run} value={run.run}>{run.run}{run.running ? ' · RUNNING' : ''}</option>)}
      </select></label>{selected && <><a className="secondary-button" href={api.stockLedgerCsvUrl(selected)}><Download size={14} /> Building CSV</a><button className="secondary-button" disabled={detail.data?.running || exportPlanMutation.isPending} onClick={() => exportPlanMutation.mutate()}><PackageCheck size={14} /> {exportPlanMutation.isPending ? 'Sizing…' : 'Full signed ZIP'}</button></>}</div>
    </header>

    <div className="product-scroll outputs-scroll">
      {exportPlan && <section className="export-plan-banner">
        <PackageCheck size={20} /><div><strong>Full signed package</strong><p>{exportPlan.files.toLocaleString()} files · {formatProductBytes(exportPlan.uncompressed_bytes)} before ZIP compression · Ed25519 manifest included.</p></div>
        <label><input type="checkbox" checked={exportConfirmed} onChange={(event) => setExportConfirmed(event.target.checked)} /> I reviewed the package size.</label>
        <a className={`primary-button ${exportConfirmed ? '' : 'disabled-link'}`} aria-disabled={!exportConfirmed} href={exportConfirmed ? api.stockExportUrl(selected) : undefined}><Download size={14} /> Create & download</a>
      </section>}
      {summary ? <>
        <section className="output-kpi-grid">
          <article><span>TOTAL SITE ENERGY</span><strong>{number(totals?.total_site_gwh)}</strong><small>GWh / year · modelled subset</small></article>
          <article><span>GEOMETRIC EUI</span><strong>{number(totals?.area_weighted_total_site_kwh_m2, 1)}</strong><small>kWh/m² · conditioned geometry</small></article>
          <article className="accent"><span>CADASTRAL EUI</span><strong>{number(totals?.cadastral_total_site_kwh_m2, 1)}</strong><small>kWh/m² · Tipo15 residential area</small></article>
          <article><span>CARBON</span><strong>{number(totals?.carbon_total_site_t_yr, 0)}</strong><small>tCO₂ / year · total site</small></article>
          <article><span>COVERAGE</span><strong>{number(summary.coverage.building_coverage_pct, 1)}%</strong><small>{summary.buildings_ok.toLocaleString()} OK · {summary.buildings_excluded} excluded</small></article>
          <article className={summary.qa_failed || summary.unexplained_severes ? 'danger' : 'pass'}><span>QA STATUS</span><strong>{summary.qa_failed || summary.unexplained_severes ? 'REVIEW' : 'PASS'}</strong><small>{summary.qa_failed} QA failed · {summary.unexplained_severes} unexplained severe</small></article>
        </section>

        <section className="output-section">
          <header><div><BarChart3 size={17} /><span><strong>Cluster totals</strong><small>Both geometric and cadastral denominators remain visible.</small></span></div></header>
          <div className="product-table-scroll"><table className="product-table cluster-table"><thead><tr><th>Cluster</th><th>Buildings</th><th>Site energy</th><th>Geometric EUI</th><th>Cadastral EUI</th><th>Rai reference</th><th>Δ vs Rai</th></tr></thead><tbody>
            {summary.by_cluster.map((row) => <tr key={row.cluster}><td><code>{row.cluster}</code></td><td>{row.buildings.toLocaleString()}</td><td>{number(row.total_site_gwh)} GWh</td><td>{number(row.area_weighted_kwh_m2, 1)}</td><td>{number(row.cadastral_kwh_m2, 1)}</td><td>{number(row.rai_consume_kwh_m2, 1)}</td><td className={(row.vs_rai_pct ?? 0) > 50 ? 'warn-value' : ''}>{number(row.vs_rai_pct, 1)}%</td></tr>)}
          </tbody></table></div>
        </section>
      </> : <section className="output-pending"><AlertTriangle size={24} /><strong>{detail.data?.running ? 'Aggregate pending while the run continues' : 'No aggregate is available for this run'}</strong><p>The building ledger remains inspectable below.</p></section>}

      {selected && <section className="output-section ledger-section">
        <header><div><FileSearch size={17} /><span><strong>Building ledger</strong><small>{ledger.data?.total.toLocaleString() ?? '—'} matching terminal records</small></span></div>
          <div className="ledger-tools"><label className="ledger-search"><Search size={14} /><input value={query} onChange={(event) => { setQuery(event.target.value); setOffset(0) }} placeholder="Reference, cluster, error…" /></label><select value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0) }} aria-label="Filter ledger by status"><option value="">All statuses</option><option value="ok">OK</option><option value="failed">Failed</option><option value="excluded">Excluded</option></select></div>
        </header>
        <div className="product-table-scroll"><table className="product-table ledger-table"><thead><tr><th>Status</th><th>refparcela</th><th>Cluster</th><th>Site EUI</th><th>Energy</th><th>CO₂</th><th>Occupancy</th><th>QA</th></tr></thead><tbody>
          {(ledger.data?.items ?? []).map((row) => <tr key={`${row.refparcela}-${row.status}`} tabIndex={0} onClick={() => setBuilding(row)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') setBuilding(row) }} className={building?.refparcela === row.refparcela ? 'selected' : ''}>
            <td><span className={`ledger-status ${row.status}`}>{row.status === 'ok' ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}{row.status}</span></td><td><code>{row.refparcela}</code></td><td>{String(row.cluster ?? '—')}</td><td>{number(row.total_site_kwh_m2 as number, 1)}</td><td>{number((row.total_site_kwh as number) / 1000, 1)} MWh</td><td>{number(row.total_site_co2_t_yr as number, 1)} t</td><td>{String(row.occupancy_plausibility ?? '—')}</td><td>{row.qa_all_passed === true ? 'PASS' : row.qa_all_passed === false ? 'FAIL' : '—'}</td>
          </tr>)}
        </tbody></table></div>
        <footer className="ledger-pagination"><span>{offset + 1}–{Math.min(offset + PAGE_SIZE, ledger.data?.total ?? 0)} of {ledger.data?.total ?? 0}</span><div><button className="secondary-button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button><button className="secondary-button" disabled={offset + PAGE_SIZE >= (ledger.data?.total ?? 0)} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button></div></footer>
      </section>}
    </div>

    {building && <aside className="building-evidence-drawer" aria-label={`Evidence for ${building.refparcela}`}>
      <header><div><Building2 size={17} /><span><small>BUILDING EVIDENCE</small><strong>{building.refparcela}</strong></span></div><button className="icon-button" onClick={() => setBuilding(null)} aria-label="Close evidence">×</button></header>
      <dl><div><dt>Status</dt><dd>{building.status}</dd></div><div><dt>Cluster</dt><dd>{String(building.cluster ?? '—')}</dd></div><div><dt>Total site EUI</dt><dd>{number(building.total_site_kwh_m2, 2)} kWh/m²</dd></div><div><dt>QA</dt><dd>{building.qa_all_passed === true ? 'PASS' : building.qa_all_passed === false ? 'FAIL' : '—'}</dd></div>{building.error && <div><dt>Error</dt><dd>{building.error}</dd></div>}</dl>
      {building.status === 'ok' && <nav><span>PRESERVED FILES</span>{ARTIFACTS.map(([file, label]) => <a key={file} href={api.stockArtifactUrl(selected, building.refparcela, file)} target="_blank" rel="noreferrer"><ExternalLink size={14} /><span><strong>{label}</strong><code>{file}</code></span></a>)}</nav>}
      <a className="building-package-link" href={api.stockExportUrl(selected, [building.refparcela])}><PackageCheck size={14} /><span><strong>Signed building package</strong><small>Model, preserved outputs, ledger evidence and Ed25519 manifest</small></span></a>
    </aside>}
  </div>
}
