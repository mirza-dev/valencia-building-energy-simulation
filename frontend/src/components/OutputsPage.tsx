import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, BarChart3, Box, Building2, CheckCircle2, Download, ExternalLink, FileSearch, Image, Map, PackageCheck, Search } from 'lucide-react'
import { api } from '../lib/api'
import { formatProductBytes } from '../lib/productStock'
import type { ProductLedgerRow } from '../lib/types'
import { useFeedback } from './FeedbackProvider'
import GeometryCheckDrawer from './GeometryCheckDrawer'

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

// The three outputs the sampling study reports, with the label each one needs
// so a reader cannot mistake a pilot-building figure for a stock figure.
const LHS_OUTPUTS = [
  ['heating_kwh_m2', 'Space heating', 'kWh/m²'],
  ['cooling_kwh_m2', 'Space cooling', 'kWh/m²'],
  ['co2_kg_m2', 'Carbon', 'kgCO₂/m²'],
] as const

/**
 * Uncertainty evidence, kept deliberately apart from the stock totals above.
 *
 * Two things this must never do. It must not imply that the interval belongs
 * to the district total: the study samples one pilot building on the demand
 * chain, while the totals on this page come from the whole-stock chain, where
 * roughly nine tenths of the energy is a per-area norm the study does not vary.
 * And it must not print statistics from a run whose engine sources have since
 * changed - a stale band beside current numbers is exactly the failure this
 * page keeps having to retract, so an outdated run shows its provenance and
 * withholds its numbers rather than dressing them as today's.
 */
function UncertaintySection() {
  const runs = useQuery({ queryKey: ['lhs-runs'], queryFn: api.lhsRuns })
  const newest = (runs.data ?? [])[0]
  const detail = useQuery({
    queryKey: ['lhs-run', newest?.id], queryFn: () => api.lhsRun(newest!.id), enabled: Boolean(newest?.id),
  })
  if (runs.isLoading || !newest) return null

  const run = detail.data ?? newest
  const verified = run.verification?.ok !== false && run.verification_status === 'VERIFIED'
  const current = run.current_compatibility?.current !== false
  const changed = run.current_compatibility?.changed_roles ?? []
  const statistics = run.result?.summary.statistics ?? {}
  const settings = run.result?.settings
  const usable = verified && current && run.result?.qa.scientific_status === 'VALIDATED'

  return <section className="output-section">
    <header><div><BarChart3 size={17} /><span><strong>Uncertainty study (Latin hypercube)</strong>
      <small>{settings ? `${settings.n} samples · seed ${settings.seed} · ${run.refparcela || 'pilot building'}` : 'Sampling study'} — a band on the pilot building&apos;s demand, not on the totals above.</small>
    </span></div>
      {usable && <nav className="lhs-downloads">
        <a className="secondary-button" href={api.lhsArtifactUrl(run.id, 'runs.csv')}><Download size={14} /> Sample ledger (.csv)</a>
        <a className="secondary-button" href={api.lhsArtifactUrl(run.id, 'histograms.png')} target="_blank" rel="noreferrer"><Image size={14} /> Distributions (.png)</a>
        <a className="secondary-button" href={api.lhsArtifactUrl(run.id, 'tornado.png')} target="_blank" rel="noreferrer"><Image size={14} /> Sensitivity (.png)</a>
        <a className="secondary-button" href={api.lhsExportUrl(run.id)}><PackageCheck size={14} /> Signed ZIP</a>
      </nav>}
    </header>

    {!verified ? <div className="output-interpretation-note"><AlertTriangle size={17} /><div>
      <strong>This study&apos;s artifacts did not verify</strong>
      <p>Its statistics are withheld: {run.verification?.status ?? run.verification_status}.</p>
    </div></div> : !current ? <div className="output-interpretation-note"><AlertTriangle size={17} /><div>
      <strong>This study describes an earlier configuration of the engine</strong>
      <p>Its evidence is intact and still traceable, but {changed.length} engine source{changed.length === 1 ? ' has' : 's have'} changed since it ran, so its interval is not a confidence interval for anything on this page. Re-run the study to restore one.</p>
      <small><code>{changed.join(' · ')}</code></small>
    </div></div> : <>
      <div className="product-table-scroll"><table className="product-table"><thead><tr>
        <th>Output</th><th>P5</th><th>Median</th><th>P95</th><th>Mean</th>
      </tr></thead><tbody>
        {LHS_OUTPUTS.map(([key, label, unit]) => {
          const value = statistics[key]
          return <tr key={key}><td>{label} <small>{unit}</small></td>
            <td>{number(value?.p5, 2)}</td><td>{number(value?.median, 2)}</td>
            <td>{number(value?.p95, 2)}</td><td>{number(value?.mean, 2)}</td></tr>
        })}
      </tbody></table></div>
      <figure className="lhs-figures">
        <img src={api.lhsFigureUrl(run.id, 'histograms.png')} alt="Sampled output distributions with the deterministic baselines drawn on them" loading="lazy" />
        <img src={api.lhsFigureUrl(run.id, 'tornado.png')} alt="Rank correlation of each sampled variable with each output" loading="lazy" />
      </figure>
    </>}
  </section>
}

export default function OutputsPage() {
  const { notify } = useFeedback()
  const [selected, setSelected] = useState('')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)
  const [building, setBuilding] = useState<ProductLedgerRow | null>(null)
  const [geometry, setGeometry] = useState(false)
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
  // A microclimate event run fills the same fields as an annual one, so these
  // labels are read off the run rather than assumed.  A run that predates the
  // period being recorded was annual.
  const period = summary?.energy_period
  const isEvent = period?.period === 'microclimate_event'
  const perPeriod = isEvent
    ? (period?.event_days ? `${period.event_days}-day event` : 'event window')
    : 'year'
  const rejected = summary
    ? summary.buildings_failed + summary.buildings_failed_qa
    : 0
  // The published per-cluster reference exists for Valencia only.  Showing the
  // column for another city would print an empty comparison next to real
  // numbers, which reads as "we measured zero difference" rather than "there is
  // nothing here to compare against".
  const hasReference = (summary?.by_cluster ?? []).some(
    (row) => row.rai_consume_kwh_m2 != null)
  // The same reasoning one level up.  Both cadastral figures come from the
  // Spanish Tipo15 record, so `aggregate()` omits them entirely for a stock
  // without one - absent, not zero.  Rendering the card anyway printed a
  // permanently empty accent-styled "CADASTRAL EUI" on every non-Valencia run,
  // which reads as a missing measurement rather than as a basis that does not
  // exist for this city.
  const hasCadastral = totals?.cadastral_total_site_kwh_m2 != null
  const hasClusterCadastral = (summary?.by_cluster ?? []).some(
    (row) => row.cadastral_kwh_m2 != null)
  const exportPlanMutation = useMutation({
    mutationFn: () => api.stockExportPlan(selected),
    onSuccess: (value) => { setExportPlan(value); setExportConfirmed(false) },
    // Without this the spinner just reverts and the failure is invisible: this
    // is the only mutation on the page, so nothing else would report it.
    onError: (error) => notify(error instanceof Error ? error.message : 'Export plan failed.', 'error'),
  })

  const geometryOpen = geometry && building?.status === 'ok'
  return <div className={`product-page outputs-page ${geometryOpen ? 'geometry-open' : ''}`}>
    <header className="product-page-header outputs-header">
      <div><span>03 / EVIDENCE</span><h1>Outputs</h1><p>Read aggregate results, audit every building and open the files preserved by the runner.</p></div>
      <div className="output-run-picker"><label><span>RUN</span><select value={selected} onChange={(event) => { setSelected(event.target.value); setOffset(0); setBuilding(null); setGeometry(false); setExportPlan(null) }}>
        {(runs.data?.runs ?? []).map((run) => <option key={run.run} value={run.run}>{run.run}{run.running ? ' · RUNNING' : ''}</option>)}
      </select></label>{selected && <><a className="secondary-button" href={api.stockLedgerCsvUrl(selected)}><Download size={14} /> Building CSV</a>{summary?.results_layer?.written && <a className="secondary-button" href={api.stockResultsLayerUrl(selected)} title={`One feature per building in ${summary.results_layer.crs ?? 'the run projection'}, plus ${Object.keys(summary.results_layer.zone_layers ?? {}).join(' and ') || 'no'} roll-up layers, styled on opening — drag into QGIS`}><Map size={14} /> GIS layer (.gpkg)</a>}{summary?.results_layer?.heatmap?.written && <a className="secondary-button" href={api.stockHeatmapUrl(selected)} title={`${(summary.results_layer.heatmap.panels ?? []).length} panels; buildings with no result drawn grey — a picture for a report, no GIS needed`}><Image size={14} /> Heat map (.png)</a>}<button className="secondary-button" disabled={detail.data?.running || exportPlanMutation.isPending} onClick={() => exportPlanMutation.mutate()}><PackageCheck size={14} /> {exportPlanMutation.isPending ? 'Sizing…' : 'Full signed ZIP'}</button></>}</div>
    </header>

    <div className="product-scroll outputs-scroll">
      {exportPlan && <section className="export-plan-banner">
        <PackageCheck size={20} /><div><strong>Full signed package</strong><p>{exportPlan.files.toLocaleString()} files · {formatProductBytes(exportPlan.uncompressed_bytes)} before ZIP compression · Ed25519 manifest included.</p></div>
        <label><input type="checkbox" checked={exportConfirmed} onChange={(event) => setExportConfirmed(event.target.checked)} /> I reviewed the package size.</label>
        <a className={`primary-button ${exportConfirmed ? '' : 'disabled-link'}`} aria-disabled={!exportConfirmed} href={exportConfirmed ? api.stockExportUrl(selected) : undefined}><Download size={14} /> Create & download</a>
      </section>}
      {summary ? <>
        {/* A run that has not written its aggregate is totalled from the rows
            it has finished so far.  Those totals carry the same field names as
            a final run's, so without this banner the page headlines a fraction
            of the stock under a label that claims all of it.  The figures stay
            visible - what was missing is the label, not the data. */}
        {detail.data?.summary_is_partial && <section className="export-plan-banner" role="note">
          <AlertTriangle size={20} /><div><strong>This run has not finished — every figure below is a running total</strong>
          <p>These numbers cover only the {summary.buildings_ok.toLocaleString()} buildings completed so far{detail.data?.scope?.total ? ` of ${detail.data.scope.total.toLocaleString()} in scope` : ''}, and will keep rising until the run ends. &ldquo;Modelled subset&rdquo; below refers to excluded buildings, not to run progress.</p></div>
        </section>}
        {isEvent && <section className="export-plan-banner" role="note">
          <Image size={20} /><div><strong>Microclimate event run — these are not annual figures</strong>
          <p>Every energy figure on this page, in the building table and in the downloads covers {period?.unit ?? 'the event window'}{period?.event_window ? ` (${period.event_window})` : ''}. Fields named per-year in the exported files hold a figure for this window only.</p></div>
        </section>}
        {period?.period === 'mixed' && <section className="export-plan-banner" role="note">
          <Image size={20} /><div><strong>This ledger holds more than one run mode</strong>
          <p>Its rows are not all per the same period, so no total on this page can be read as one number. Aggregate each run into its own directory.</p></div>
        </section>}
        <section className="output-kpi-grid">
          <article><span>TOTAL SITE ENERGY</span><strong>{number(totals?.total_site_gwh)}</strong><small>GWh / {perPeriod} · modelled subset</small></article>
          <article title={totals?.area_basis_note}><span>RESIDENTIAL-AREA EUI</span><strong>{number(totals?.area_weighted_total_site_kwh_m2, 1)}</strong><small>kWh/m² · geometric residential storeys</small></article>
          {hasCadastral && <article className="accent"><span>CADASTRAL EUI</span><strong>{number(totals?.cadastral_total_site_kwh_m2, 1)}</strong><small>kWh/m² · Tipo15 residential area</small></article>}
          <article><span>CARBON</span><strong>{number(totals?.carbon_total_site_t_yr, 0)}</strong><small>tCO₂ / {perPeriod} · total site</small></article>
          <article><span>COVERAGE</span><strong>{number(summary.coverage.building_coverage_pct, 1)}%</strong><small>{summary.buildings_ok.toLocaleString()} OK · {summary.buildings_failed} failed · {summary.buildings_failed_qa} QA rejected · {summary.buildings_excluded} excluded</small></article>
          <article className={rejected || summary.qa_failed || summary.unexplained_severes ? 'danger' : 'pass'}><span>RESULT STATUS</span><strong>{rejected || summary.qa_failed || summary.unexplained_severes ? 'REVIEW' : 'PASS'}</strong><small>{summary.buildings_failed} runtime failed · {summary.buildings_failed_qa} QA rejected · accepted rows: {summary.unexplained_severes} unexplained severe</small></article>
        </section>

        {(summary.coverage.note || rejected || summary.implausible_occupancy) ? <section className="output-interpretation-note" aria-label="Result interpretation">
          <AlertTriangle size={17} /><div><strong>Read the subset before interpreting its intensity</strong>
            {summary.coverage.note ? <p>{summary.coverage.note}</p> : null}
            <small>{rejected ? `${rejected} building result${rejected === 1 ? '' : 's'} did not enter the totals. ` : ''}{summary.implausible_occupancy ? `${summary.implausible_occupancy} accepted building${summary.implausible_occupancy === 1 ? '' : 's'} carry an occupancy plausibility flag.` : ''}</small>
          </div>
        </section> : null}

        <section className="output-section">
          <header><div><BarChart3 size={17} /><span><strong>Cluster totals</strong><small>{hasClusterCadastral ? 'Both geometric and cadastral denominators remain visible.' : 'This stock carries no cadastral dwelling area, so every figure is on the geometric basis.'}{hasReference ? '' : ' No published reference exists for it either, so no comparison column is shown.'}</small></span></div></header>
          <div className="product-table-scroll"><table className="product-table cluster-table"><thead><tr><th>Cluster</th><th>Buildings</th><th>Site energy</th><th>Geometric EUI</th>{hasClusterCadastral && <th>Cadastral EUI</th>}{hasReference && <><th>Rai reference</th><th>Δ vs Rai</th></>}</tr></thead><tbody>
            {summary.by_cluster.map((row) => <tr key={row.cluster}><td><code>{row.cluster}</code></td><td>{row.buildings.toLocaleString()}</td><td>{number(row.total_site_gwh)} GWh</td><td>{number(row.area_weighted_kwh_m2, 1)}</td>{hasClusterCadastral && <td>{number(row.cadastral_kwh_m2, 1)}</td>}{hasReference && <><td>{number(row.rai_consume_kwh_m2, 1)}</td><td className={(row.vs_rai_pct ?? 0) > 50 ? 'warn-value' : ''}>{number(row.vs_rai_pct, 1)}%</td></>}</tr>)}
          </tbody></table></div>
        </section>
      </> : <section className="output-pending"><AlertTriangle size={24} /><strong>{detail.data?.running ? 'Aggregate pending while the run continues' : 'No aggregate is available for this run'}</strong><p>The building ledger remains inspectable below.</p></section>}

      <UncertaintySection />


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
      <header><div><Building2 size={17} /><span><small>BUILDING EVIDENCE</small><strong>{building.refparcela}</strong></span></div><button className="icon-button" onClick={() => { setBuilding(null); setGeometry(false) }} aria-label="Close evidence">×</button></header>
      <dl><div><dt>Status</dt><dd>{building.status}</dd></div><div><dt>Cluster</dt><dd>{String(building.cluster ?? '—')}</dd></div><div><dt>Total site EUI</dt><dd>{number(building.total_site_kwh_m2, 2)} kWh/m²</dd></div><div><dt>QA</dt><dd>{building.qa_all_passed === true ? 'PASS' : building.qa_all_passed === false ? 'FAIL' : '—'}</dd></div>{(building.error || building.message || building.reason) && <div><dt>Failure</dt><dd><strong>{building.reason ?? 'Error'}</strong>{building.message || building.error ? <span>{String(building.message ?? building.error)}</span> : null}</dd></div>}</dl>
      {building.status === 'ok' && <button className="geometry-check-button" onClick={() => setGeometry(true)}><Box size={14} /><span><strong>Geometry check</strong><small>Open the model this building was simulated from</small></span></button>}
      {building.status === 'ok' && <nav><span>PRESERVED FILES</span>{ARTIFACTS.map(([file, label]) => <a key={file} href={api.stockArtifactUrl(selected, building.refparcela, file)} target="_blank" rel="noreferrer"><ExternalLink size={14} /><span><strong>{label}</strong><code>{file}</code></span></a>)}</nav>}
      <a className="building-package-link" href={api.stockExportUrl(selected, [building.refparcela])}><PackageCheck size={14} /><span><strong>Signed building package</strong><small>Model, preserved outputs, ledger evidence and Ed25519 manifest</small></span></a>
    </aside>}

    {geometryOpen && building && <GeometryCheckDrawer
      run={selected} reference={building.refparcela} onClose={() => setGeometry(false)} />}
  </div>
}
