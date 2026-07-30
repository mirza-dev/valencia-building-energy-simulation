import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Activity, Archive, Check, CircleAlert, Download, FileBox, Hash, Search, ShieldCheck } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import PageHeader from './PageHeader'
import ModelViewer from './ModelViewer'
import { SimulationResultPanel } from './SimulationResultPanel'
import NeighborhoodResultSummary from './NeighborhoodResultSummary'
import CityResultSummary from './CityResultSummary'
import LhsResultSummary from './LhsResultSummary'
import RunArtifactLinks from './RunArtifactLinks'
import { useFeedback } from './FeedbackProvider'
import { api } from '../lib/api'
import { useActiveBuilding } from '../lib/activeBuilding'
import { formatDate } from '../lib/locale'

export default function RunsPage() {
  const { t, i18n } = useTranslation()
  const { notify } = useFeedback()
  const { activeBuilding } = useActiveBuilding()
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const runs = useQuery({ queryKey: ['runs'], queryFn: api.runs })
  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities, staleTime: 5_000 })
  const [selected, setSelectedState] = useState<string | null>(params.get('run'))
  const [search, setSearch] = useState('')
  const [stateFilter, setStateFilter] = useState('ALL')
  const [typeFilter, setTypeFilter] = useState('ALL')
  const selectRun = (id: string) => { setSelectedState(id); setParams({ run: id }, { replace: true }) }
  const visibleRuns = useMemo(
    () => (runs.data ?? []).filter(
      (item) => item.refparcela === activeBuilding && ['model', 'simulation'].includes(item.run_type),
    ),
    [activeBuilding, runs.data],
  )
  useEffect(() => {
    if (selected && visibleRuns.some((item) => item.id === selected)) return
    if (visibleRuns[0]) selectRun(visibleRuns[0].id)
  }, [selected, visibleRuns])
  const filteredRuns = useMemo(() => visibleRuns.filter((item) => {
    const matchesText = `${item.scenario_name} ${item.refparcela} ${item.provenance ?? 'pipeline'}`.toLowerCase().includes(search.toLowerCase())
    return matchesText && (stateFilter === 'ALL' || item.verification_status === stateFilter)
      && (typeFilter === 'ALL' || item.run_type === typeFilter)
  }), [search, stateFilter, typeFilter, visibleRuns])
  const run = visibleRuns.find((item) => item.id === selected)
  const scene = useQuery({ queryKey: ['run-scene', selected], queryFn: () => api.scene(selected!), enabled: Boolean(selected && run?.run_type === 'model') })
  const simulation = useQuery({ queryKey: ['simulation', selected], queryFn: () => api.simulation(selected!), enabled: Boolean(selected && run?.run_type === 'simulation') })
  const neighborhood = useQuery({ queryKey: ['neighborhood-run', selected], queryFn: () => api.neighborhoodRun(selected!), enabled: Boolean(selected && run?.run_type === 'neighborhood') })
  const city = useQuery({ queryKey: ['city-run', selected], queryFn: () => api.cityRun(selected!), enabled: Boolean(selected && run?.run_type === 'city') })
  const lhs = useQuery({ queryKey: ['lhs-run', selected], queryFn: () => api.lhsRun(selected!), enabled: Boolean(selected && run?.run_type === 'lhs') })
  const verify = useMutation({
    mutationFn: () => api.verifyRun(selected!),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['runs'] }); notify(t('runs.verified'), 'success') },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })

  return (
    <div className="page">
      <PageHeader eyebrow={t('runs.eyebrow')} title={t('runs.title')} subtitle={t('runs.subtitle')}
        actions={run ? <>{capabilities.data?.capabilities.model_editor?.runtime_ready && ['model', 'simulation'].includes(run.run_type) ? <Link className="secondary-button" to={`/model/${encodeURIComponent(run.id)}`}><FileBox size={16} />{t('modelInspector.open')}</Link> : null}<button className="secondary-button" onClick={() => verify.mutate()} disabled={run.verification_status === 'LEGACY' || verify.isPending} title={run.verification_status === 'LEGACY' ? 'LEGACY / UNVERIFIED' : undefined}><ShieldCheck size={16} />{t('runs.verify')}</button>
          {run.verification_status !== 'TAMPERED' ? <a className="secondary-button" href={api.exportUrl(run.id)}><Download size={16} />{t('common.export')}</a> : null}</> : undefined} />
      <div className="runs-layout">
        <section className="run-list-panel">
          <div className="run-filters"><label className="search-field"><Search size={15} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t('runs.search')} /></label>
            <div className="run-filter-row"><select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} aria-label={t('runs.type')}><option value="ALL">{t('runs.allTypes')}</option><option value="model">MODEL</option><option value="simulation">SIMULATION</option></select><select value={stateFilter} onChange={(event) => setStateFilter(event.target.value)} aria-label={t('common.status')}><option value="ALL">{t('runs.allStates')}</option><option>VERIFIED</option><option>LEGACY</option><option>TAMPERED</option></select></div></div>
          <div className="table-head"><span>{t('runs.count', { count: filteredRuns.length })}</span><code>{t('runs.immutable')}</code></div>
          <div className="run-list">
            {filteredRuns.map((item) => <button key={item.id} className={selected === item.id ? 'active' : ''} onClick={() => selectRun(item.id)} aria-current={selected === item.id} title={item.scenario_name}>
              <i>{item.run_type === 'simulation' ? <Activity size={15} /> : <Archive size={15} />}</i><span><strong>{item.scenario_name}</strong><code>{item.refparcela}</code><small className={`verification-label ${item.verification_status.toLowerCase()}`}>{item.run_type.toUpperCase()} · {item.verification_status}{item.provenance === 'authored' ? ` · AUTHORED ${item.patch_journal?.length ?? 0}` : ''}</small></span>
              <em>{formatDate(item.created_at, i18n.language)}</em>
            </button>)}
            {!runs.isLoading && !filteredRuns.length ? <div className="empty-state"><Archive size={24} /><p>{t('runs.empty')}</p></div> : null}
            {runs.isLoading ? <div className="skeleton-stack" aria-label={t('common.loading')}><i /><i /><i /></div> : null}
            {runs.error instanceof Error ? <div className="error-message" role="alert">{runs.error.message}</div> : null}
          </div>
        </section>
        <section className="run-preview-panel">
          {run?.run_type === 'simulation' ? <SimulationResultPanel result={simulation.data?.result} /> : run?.run_type === 'neighborhood' ? <NeighborhoodResultSummary result={neighborhood.data?.result} /> : run?.run_type === 'city' ? <CityResultSummary result={city.data?.result} /> : run?.run_type === 'lhs' ? <LhsResultSummary result={lhs.data?.result} blocked={lhs.data?.verification.status === 'TAMPERED'} /> : scene.data ? <ModelViewer scene={scene.data} /> : <div className="model-empty">{scene.isLoading ? <><span className="spinner" />{t('common.loading')}</> : t('runs.select')}</div>}
        </section>
        <aside className="run-detail-panel">
          {run ? <><span className="eyebrow">{t('runs.manifest')}</span><h2 title={run.scenario_name}>{run.scenario_name}</h2><code className="run-id">{run.id}</code>
            <div className={`run-pass ${run.verification_status === 'TAMPERED' || run.qa.scientific_status === 'INVALID' ? 'tampered' : ''}`}>{run.verification_status === 'TAMPERED' || run.qa.scientific_status === 'INVALID' ? <CircleAlert size={16} /> : <Check size={16} />}<span><strong>{run.verification_status} · {run.qa.scientific_status ?? `QA ${run.qa.all_pass ? t('common.pass').toLocaleUpperCase(i18n.language) : t('common.fail').toLocaleUpperCase(i18n.language)}`}</strong><small>{t('runs.warnings', { count: run.qa.warning_count ?? Number(run.stats.warning_count ?? 0) })}</small></span></div>
            {run.run_type === 'model' ? (run.renderer?.status !== 'VERSIONED' ? <div className="stale-banner"><CircleAlert size={15} />{t('runs.rendererUnversioned')}</div>
              : run.renderer && !run.renderer.current_match ? <div className="stale-banner"><CircleAlert size={15} />{t('runs.rendererChanged')}</div> : null) : null}
            {run.run_type === 'model' ? <dl className="metadata-list"><div><dt>{t('runs.building')}</dt><dd>{run.refparcela}</dd></div>{run.provenance === 'authored' ? <><div className="primary-metric"><dt>PROVENANCE</dt><dd>AUTHORED · {run.patch_journal?.length ?? 0} PATCH</dd></div><div><dt>AUTHORED FROM</dt><dd>{run.authored_from?.slice(0, 16)}</dd></div></> : null}{run.stats.part_g ? <><div className="primary-metric"><dt>PART G · HEAT / COOL</dt><dd>{String((run.stats.part_g as Record<string, unknown>).heat_delta_c)} / {String((run.stats.part_g as Record<string, unknown>).cool_delta_c)} K</dd></div><div><dt>PART G · EPW</dt><dd>{String((run.stats.part_g as Record<string, unknown>).weather_snapshot_hash).slice(0, 16)}</dd></div><div><dt>{t('simulation.parentModel')}</dt><dd>{run.parent_run_id?.slice(0, 16)}</dd></div></> : null}<div><dt>{t('runs.footprint')}</dt><dd>{String(run.stats.footprint_m2)} m²</dd></div><div><dt>{t('runs.residentialArea')}</dt><dd>{String(run.stats.res_area_m2)} m²</dd></div><div><dt>{t('runs.openings')}</dt><dd>{String(Number(run.stats.n_windows) + Number(run.stats.n_balcony_doors))}</dd></div><div><dt>{t('runs.shading')}</dt><dd>{String(run.stats.n_shading_surfaces)}</dd></div><div><dt>{t('runs.raw')}</dt><dd>{run.raw_model_sha256?.slice(0, 16) ?? t('runs.legacy')}</dd></div><div><dt>{t('runs.canonical')}</dt><dd>{run.canonical_fingerprint?.slice(0, 16) ?? t('runs.legacy')}</dd></div><div><dt>{t('runs.renderer')}</dt><dd>{run.renderer?.source_fingerprint?.slice(0, 16) ?? t('runs.unversioned')}</dd></div><div><dt>{t('runs.visualGeometry')}</dt><dd>{run.renderer?.visual_geometry_fingerprint?.slice(0, 12) ?? '—'}{run.renderer?.status === 'VERSIONED' ? ` · ${run.renderer.context_roof_count ?? 0} ${t('runs.roofs')}` : ''}</dd></div></dl>
              : run.run_type === 'simulation' ? <dl className="metadata-list"><div><dt>{t('runs.building')}</dt><dd>{run.refparcela}</dd></div><div><dt>{t('simulation.parentModel')}</dt><dd>{run.parent_run_id?.slice(0, 16)}</dd></div><div><dt>{t('simulation.period')}</dt><dd>{t('simulation.annual').toLocaleUpperCase(i18n.language)}</dd></div><div><dt>{t('simulation.areaBasis')}</dt><dd>{String(run.stats.conditioned_residential_area_m2)} m²</dd></div><div><dt>{t('common.warn')}</dt><dd>{String(run.stats.warning_count)}</dd></div><div><dt>{t('common.severe')} / {t('common.fatal')}</dt><dd>{String(run.stats.severe_count)} / {String(run.stats.fatal_count)}</dd></div><div><dt>{t('runs.raw')}</dt><dd>{run.raw_model_sha256?.slice(0, 16)}</dd></div><div><dt>{t('runs.canonical')}</dt><dd>{run.canonical_fingerprint?.slice(0, 16)}</dd></div></dl>
                : run.run_type === 'neighborhood' ? <dl className="metadata-list"><div><dt>{t('neighborhood.scope')}</dt><dd>Benicalap</dd></div><div><dt>{t('neighborhood.buildings')}</dt><dd>{String(run.stats.buildings)}</dd></div><div><dt>Clusters</dt><dd>{String(run.stats.clusters_completed)} / {String(run.stats.clusters_expected)}</dd></div><div><dt>QA</dt><dd>{String(run.stats.qa_passed_clusters)} / {String(run.stats.clusters_expected)}</dd></div><div><dt>{t('simulation.heating')}</dt><dd>{String((run.stats.totals as Record<string, unknown> | null)?.heating_gwh_yr ?? '—')} GWh</dd></div><div><dt>{t('simulation.cooling')}</dt><dd>{String((run.stats.totals as Record<string, unknown> | null)?.cooling_gwh_yr ?? '—')} GWh</dd></div></dl>
                  : run.run_type === 'city' ? <dl className="metadata-list"><div><dt>{t('city.scope')}</dt><dd>Valencia</dd></div><div><dt>{t('city.buildings')}</dt><dd>{String(run.stats.buildings)}</dd></div><div><dt>Clusters</dt><dd>{String(run.stats.clusters_completed)} / {String(run.stats.clusters_expected)}</dd></div><div><dt>{t('city.districts')}</dt><dd>{String(run.stats.districts)} / 19</dd></div><div><dt>QA</dt><dd>{String(run.stats.qa_passed_clusters)} / {String(run.stats.clusters_expected)}</dd></div><div className="primary-metric"><dt>{t('city.hvacConsumption')}</dt><dd>{String((run.stats.totals as Record<string, unknown> | null)?.hvac_consumption_gwh_yr ?? '—')} GWh</dd></div><div className="primary-metric"><dt>{t('city.totalSite')}</dt><dd>{String((run.stats.totals as Record<string, unknown> | null)?.total_site_gwh_yr ?? '—')} GWh</dd></div><div><dt>{t('city.demand')}</dt><dd>{String((run.stats.totals as Record<string, unknown> | null)?.heating_gwh_yr ?? '—')} H · {String((run.stats.totals as Record<string, unknown> | null)?.cooling_gwh_yr ?? '—')} C GWh</dd></div></dl>
                    : <dl className="metadata-list"><div><dt>{t('lhs.scope')}</dt><dd>{run.refparcela}</dd></div><div><dt>{t('lhs.sampleCount')}</dt><dd>{String(run.stats.samples_completed)} / {String(run.stats.samples_expected)}</dd></div><div><dt>{t('lhs.seed')}</dt><dd>{String('seed' in run.config ? run.config.seed : 42)}</dd></div>{lhs.data?.verification.ok && lhs.data.result?.qa.scientific_status === 'VALIDATED' ? <><div><dt>{t('lhs.heatingMean')}</dt><dd>{String((run.stats.statistics as Record<string, Record<string, number>>)?.heating_kwh_m2?.mean ?? '—')}</dd></div><div><dt>{t('lhs.coolingMean')}</dt><dd>{String((run.stats.statistics as Record<string, Record<string, number>>)?.cooling_kwh_m2?.mean ?? '—')}</dd></div><div><dt>{t('lhs.carbonMean')}</dt><dd>{String((run.stats.statistics as Record<string, Record<string, number>>)?.co2_kg_m2?.mean ?? '—')}</dd></div></> : null}</dl>}
            {run.run_type === 'simulation' ? <RunArtifactLinks run={run} compact /> : null}
            <div className="artifact-list"><div className="inspector-subhead"><FileBox size={13} />{t('runs.artifacts')}</div>{run.artifacts?.map((artifact) => <div key={artifact.name}><span>{artifact.name}</span><code><Hash size={11} />{artifact.sha256.slice(0, 10)}</code></div>)}</div>
          </> : null}
        </aside>
      </div>
    </div>
  )
}
