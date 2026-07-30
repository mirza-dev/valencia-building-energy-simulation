import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Archive, CheckCircle2, CircleAlert, Download, LockKeyhole, ShieldCheck } from 'lucide-react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import PageHeader from './PageHeader'
import NeighborhoodJobControl from './NeighborhoodJobControl'
import StockRunSetup from './StockRunSetup'
import { FocusedWorkspace, PrimaryMetricStrip } from './FocusedWorkspace'
import { useFeedback } from './FeedbackProvider'
import { api, ApiError } from '../lib/api'
import { formatDate } from '../lib/locale'
import { resolveFocusedStatus, type FocusedPanel } from '../lib/focusedWorkspace'
import type { NeighborhoodCluster, NeighborhoodRunRequest } from '../lib/types'

const NeighborhoodMap = lazy(() => import('./NeighborhoodMap'))

const number = (value: number | null | undefined, digits = 1) =>
  value == null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value)

export default function NeighborhoodPage() {
  const { t, i18n } = useTranslation()
  const { notify } = useFeedback()
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const jobId = params.get('job') ?? ''
  const setJobRoute = useCallback((next: string) => {
    const copy = new URLSearchParams(params)
    if (next) copy.set('job', next)
    else copy.delete('job')
    setParams(copy, { replace: true })
  }, [params, setParams])
  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities })
  const ready = capabilities.data?.capabilities.neighborhood?.runtime_ready === true
  const [preflightDistrict, setPreflightDistrict] = useState<string | null>(null)
  const history = useQuery({ queryKey: ['neighborhood-run-summaries'], queryFn: api.neighborhoodRunSummaries, enabled: ready, staleTime: 30_000 })
  const activeJob = useQuery({ queryKey: ['neighborhood-active-job'], queryFn: api.activeNeighborhoodJob, enabled: ready && !jobId, refetchInterval: 2500 })
  const job = useQuery({
    queryKey: ['job', jobId], queryFn: () => api.neighborhoodJob(jobId), enabled: Boolean(jobId),
    refetchInterval: (query) => ['completed', 'failed', 'canceled'].includes(query.state.data?.status ?? '') ? false : 800,
    retry: (count, error) => !(error instanceof ApiError && [404, 422].includes(error.status)) && count < 3,
  })
  const [selectedRunId, setSelectedRunId] = useState('')
  const [selectedCluster, setSelectedCluster] = useState('')
  const [mapMode, setMapMode] = useState<'heating' | 'cooling' | 'hvac' | 'site' | 'cluster'>('cluster')
  const [mapRevision, setMapRevision] = useState(0)
  const [panel, setPanel] = useState<FocusedPanel | null>(null)
  const handledJob = useRef('')
  const selectedRunQuery = useQuery({
    queryKey: ['neighborhood-run', selectedRunId], queryFn: () => api.neighborhoodRun(selectedRunId),
    enabled: Boolean(selectedRunId), staleTime: 30_000,
  })
  const selectedRun = selectedRunQuery.data
  const needsSetupData = panel === 'setup' || (!history.isLoading && !history.data?.length)
  const options = useQuery({ queryKey: ['neighborhood-options'], queryFn: api.neighborhoodOptions, enabled: ready && needsSetupData, staleTime: 30_000 })
  const preflight = useQuery({ queryKey: ['neighborhood-preflight', preflightDistrict], queryFn: () => api.neighborhoodPreflight(preflightDistrict), enabled: ready && needsSetupData, staleTime: 60_000 })
  const result = selectedRun?.result
  const verifiedScientificRun = Boolean(
    selectedRun?.verification_status === 'VERIFIED'
    && selectedRun.verification?.ok
    && result?.qa.scientific_status === 'VALIDATED'
    && result?.map_available
    && result?.map_descriptor,
  )
  const mapDescriptor = verifiedScientificRun ? result?.map_descriptor : preflight.data?.map_descriptor
  const map = useQuery({
    queryKey: ['neighborhood-map', mapDescriptor?.fingerprint],
    queryFn: () => api.neighborhoodMapResource(mapDescriptor!.url),
    enabled: Boolean(mapDescriptor),
    staleTime: Infinity,
    retry: false,
  })
  const ledger = result?.clusters.length ? result.clusters : preflight.data?.representatives ?? []
  const selectedRow = ledger.find((item) => item.cluster === selectedCluster) ?? ledger[0]
  const selectedClusterResult = selectedRow && 'qa_all_pass' in selectedRow
    ? selectedRow as NeighborhoodCluster
    : null
  const mapData = map.data ?? (!selectedRunId ? preflight.data?.map : null)
  const retryMap = useCallback(() => {
    setMapRevision((current) => current + 1)
    void map.refetch()
  }, [map])

  useEffect(() => {
    if (!jobId && activeJob.data?.job) {
      client.setQueryData(['job', activeJob.data.job.id], activeJob.data.job)
      setSelectedRunId('')
      setJobRoute(activeJob.data.job.id)
      notify(t('neighborhood.job.restored'), 'success')
    }
  }, [activeJob.data, client, jobId, notify, setJobRoute, t])
  useEffect(() => {
    if (!jobId && !activeJob.isPending && !activeJob.data?.job && !selectedRunId && history.data?.[0]) {
      setSelectedRunId(history.data[0].id)
    }
  }, [activeJob.data, activeJob.isPending, history.data, jobId, selectedRunId])
  useEffect(() => {
    if (job.data?.status === 'completed' && job.data.run_id && handledJob.current !== job.data.id) {
      handledJob.current = job.data.id
      setSelectedRunId(job.data.run_id)
      void client.invalidateQueries({ queryKey: ['neighborhood-run-summaries'] })
      void client.invalidateQueries({ queryKey: ['neighborhood-run', job.data.run_id] })
      void client.invalidateQueries({ queryKey: ['runs'] })
      notify(t('neighborhood.completed'), 'success')
    }
  }, [client, job.data, notify, t])
  useEffect(() => {
    if (!selectedCluster && ledger[0]) setSelectedCluster(ledger[0].cluster)
  }, [ledger, selectedCluster])
  useEffect(() => {
    if (!verifiedScientificRun && mapMode !== 'cluster') setMapMode('cluster')
  }, [mapMode, verifiedScientificRun])
  useEffect(() => {
    if (result?.qa.scientific_status === 'INVALID') setPanel('evidence')
    else if (selectedRunId) setPanel((current) => current === 'setup' ? null : current)
    else if (!jobId && !history.isLoading && !history.data?.length) setPanel('setup')
  }, [history.data, history.isLoading, jobId, result?.qa.scientific_status, selectedRunId])

  const start = useMutation({
    mutationFn: (payload: NeighborhoodRunRequest) => api.createNeighborhoodRun(payload),
    onSuccess: (created) => {
      handledJob.current = ''
      setSelectedRunId('')
      setPanel(null)
      client.setQueryData(['job', created.id], created)
      setJobRoute(created.id)
      notify(t('neighborhood.queued'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const cancel = useMutation({ mutationFn: () => api.cancelJob(jobId), onSuccess: (updated) => client.setQueryData(['job', jobId], updated) })
  const retry = useMutation({
    mutationFn: () => api.retryJob(jobId),
    onSuccess: (created) => { client.setQueryData(['job', created.id], created); setJobRoute(created.id) },
  })
  const running = Boolean(jobId && ['queued', 'running'].includes(job.data?.status ?? 'queued'))
  const totals = verifiedScientificRun ? result?.summary.totals : null
  const consumptionAvailable = totals?.hvac_consumption_gwh_yr != null && totals?.total_site_gwh_yr != null
  const invalid = result?.qa.scientific_status === 'INVALID'
  const source = capabilities.data?.capabilities.neighborhood
  const statusLevel = resolveFocusedStatus({ blocked: invalid, verified: verifiedScientificRun })
  const methodRows = useMemo(() => [
    [t('neighborhood.scope'), preflight.data?.scope ?? (preflightDistrict || 'Benicalap')],
    [t('neighborhood.stock'), `${preflight.data?.summary.buildings ?? '—'} ${t('neighborhood.buildings')}`],
    [t('neighborhood.method'), t('neighborhood.typologyMethod')],
    [t('neighborhood.execution'), t('neighborhood.sequential')],
  ], [preflight.data, preflightDistrict, t])

  if (capabilities.isPending) return <div className="page-loading"><span className="spinner" />{t('common.loading')}</div>
  if (!ready) return <Navigate to="/health" replace />
  return <div className="page neighborhood-page">
    <PageHeader eyebrow={t('neighborhood.eyebrow')} title={t('neighborhood.title')} subtitle={t('neighborhood.subtitle')} />
    <FocusedWorkspace
      className="focused-neighborhood"
      panel={panel}
      onPanelChange={setPanel}
      toolbarLead={<div className="segmented-control" aria-label={t('neighborhood.mapMode')}><button className={mapMode === 'cluster' ? 'active' : ''} onClick={() => setMapMode('cluster')}>{t('neighborhood.cluster')}</button><button className={mapMode === 'hvac' ? 'active' : ''} onClick={() => setMapMode('hvac')} disabled={!consumptionAvailable}>{t('city.hvacShort')}</button><button className={mapMode === 'site' ? 'active' : ''} onClick={() => setMapMode('site')} disabled={!consumptionAvailable}>{t('city.siteShort')}</button><button className={mapMode === 'heating' ? 'active' : ''} onClick={() => setMapMode('heating')} disabled={!totals}>{t('simulation.heating')}</button><button className={mapMode === 'cooling' ? 'active' : ''} onClick={() => setMapMode('cooling')} disabled={!totals}>{t('simulation.cooling')}</button></div>}
      status={{ level: statusLevel, label: t(`focused.${statusLevel === 'verified' ? 'verified' : statusLevel === 'blocked' ? 'blocked' : 'pending'}`), detail: selectedRun ? `${selectedRun.verification_status} · ${result?.qa.scientific_status ?? '—'}` : t('neighborhood.preflight') }}
      runs={(history.data ?? []).map((item) => ({ id: item.id, label: item.scenario_name || item.scientific_status || item.id.slice(0, 8), meta: formatDate(item.created_at, i18n.language) }))}
      selectedRunId={selectedRunId}
      onSelectRun={setSelectedRunId}
      tabs={[
        { id: 'setup', label: selectedRun ? t('focused.newRun') : t('focused.setup'), content: <>
          <section className="neighborhood-contract"><div className="inspector-subhead"><ShieldCheck size={14} />{t('neighborhood.contract')}</div><div className="contract-pass"><CheckCircle2 size={17} /><span><strong>{t('neighborhood.contractStatus')}</strong><code>{source?.contract?.checks ? Object.values(source.contract.checks).filter(Boolean).length : 0} / {source?.contract?.checks ? Object.keys(source.contract.checks).length : 0}</code></span></div></section>
          <section className="neighborhood-method"><div className="inspector-subhead"><LockKeyhole size={14} />{t('neighborhood.verifiedProtocol')}</div>{methodRows.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}<div className="preflight-strip"><span>{t('neighborhood.resArea')}</span><strong>{number(preflight.data?.summary.residential_area_m2, 0)} m²</strong><small>{preflight.data?.summary.imputed_floor_buildings ?? '—'} {t('neighborhood.floorImputed')} · {preflight.data?.summary.proxy_area_buildings ?? '—'} {t('neighborhood.areaProxy')}</small></div></section>
          <StockRunSetup workflow="neighborhood" options={options.data} includeScope disabled={running || !preflight.data} pending={start.isPending || preflight.isFetching} onScopeChange={setPreflightDistrict} onStart={(payload) => start.mutate(payload as NeighborhoodRunRequest)} />
        </> },
        { id: 'selection', label: t('focused.selection'), hidden: !ledger.length, content: <>
          {selectedRow ? <section className="cluster-inspector"><span className="eyebrow">{t('neighborhood.selectedCluster')}</span><h2>{selectedRow.cluster}</h2><code>{selectedRow.refparcela}</code><dl className="metadata-list"><div><dt>{t('neighborhood.familyPeriod')}</dt><dd>{selectedRow.family} · {selectedRow.period}</dd></div><div><dt>{t('neighborhood.buildings')}</dt><dd>{selectedRow.n_buildings}</dd></div><div><dt>{t('neighborhood.repFootprint')}</dt><dd>{number(selectedRow.rep_area_m2)} m²</dd></div><div><dt>{t('neighborhood.repFloors')}</dt><dd>{selectedRow.rep_floors}</dd></div>{selectedClusterResult ? <>{selectedClusterResult.cons_hc_kwh_m2 != null ? <><div className="primary-metric"><dt>{t('city.hvacConsumption')}</dt><dd>{number(selectedClusterResult.cons_hc_kwh_m2, 2)} kWh/m²</dd></div><div className="primary-metric"><dt>{t('city.totalSite')}</dt><dd>{number(selectedClusterResult.total_site_kwh_m2, 2)} kWh/m²</dd></div></> : null}<div><dt>{t('simulation.heating')}</dt><dd>{number(selectedClusterResult.heating_kwh_m2, 2)} kWh/m²</dd></div><div><dt>{t('simulation.cooling')}</dt><dd>{number(selectedClusterResult.cooling_kwh_m2, 2)} kWh/m²</dd></div><div><dt>{t('neighborhood.energyPlusQa')}</dt><dd>{selectedClusterResult.qa_all_pass ? 'PASS' : 'FAIL'} · {selectedClusterResult.eplus_warnings}</dd></div></> : null}</dl></section> : null}
          <section className="cluster-ledger-list"><div className="table-head"><span>{t('neighborhood.clusterLedger')}</span><code>{ledger.length}</code></div>{ledger.map((item) => <button key={item.cluster} className={selectedCluster === item.cluster ? 'active' : ''} onClick={() => setSelectedCluster(item.cluster)}><span><strong>{item.cluster}</strong><small>{item.n_buildings} {t('neighborhood.buildings')} · {item.refparcela}</small></span>{'qa_all_pass' in item ? <i className={item.qa_all_pass ? 'pass' : 'fail'}>{item.qa_all_pass ? '✓' : '!'}</i> : <i>{item.period}</i>}</button>)}</section>
        </> },
        { id: 'evidence', label: t('focused.evidence'), alert: invalid || Boolean(result?.qa.failures.length), content: <>
          {selectedRun ? <div className="focused-drawer-actions"><Link className="secondary-button" to={`/runs?run=${selectedRun.id}`}><Archive size={16} />{t('neighborhood.openRun')}</Link><a className="secondary-button" href={api.exportUrl(selectedRun.id)}><Download size={16} />{t('common.export')}</a></div> : null}
          {result?.single_building ? <section className="single-building-evidence"><header><span className="eyebrow">{t('stockRun.singleBuildingEvidence')}</span><code>{result.single_building.refparcela}</code></header><pre>{result.single_building.report_text || t('stockRun.reportUnavailable')}</pre></section> : null}
          <section className="neighborhood-contract"><div className="inspector-subhead"><ShieldCheck size={14} />{t('focused.evidence')}</div><div className="contract-pass"><CheckCircle2 size={17} /><span><strong>{selectedRun?.verification_status ?? t('focused.pending')}</strong><code>{result?.qa.scientific_status ?? 'PREFLIGHT'}</code></span></div></section>
          {result?.qa.failures.length ? <section className="cluster-failures"><div className="inspector-subhead"><CircleAlert size={14} />{t('neighborhood.failures')}</div>{result.qa.failures.map((failure) => <button key={failure.cluster} onClick={() => { setSelectedCluster(failure.cluster); setPanel('selection') }}><strong>{failure.cluster}</strong><small>{failure.error}</small></button>)}</section> : null}
        </> },
        { id: 'metrics', label: t('focused.metrics'), hidden: !totals, content: totals ? <section className="simulation-section"><div className="table-head"><span>{t('focused.metrics')}</span><code>IDEAL LOADS + HVAC</code></div><div className="metric-pairs"><div><span>{t('simulation.heating')}</span><strong>{number(totals.heating_gwh_yr, 2)} GWh/yr</strong></div><div><span>{t('simulation.cooling')}</span><strong>{number(totals.cooling_gwh_yr, 2)} GWh/yr</strong></div><div><span>{t('city.hvacCarbon')}</span><strong>{number(totals.hvac_co2_t_yr, 0)} tCO₂/yr</strong></div><div><span>S1 / S2 CO₂</span><strong>{number(totals.s1_co2_t_yr, 0)} / {number(totals.s2_co2_t_yr, 0)} t/yr</strong></div><div><span>{t('neighborhood.resArea')}</span><strong>{number(totals.residential_area_m2, 0)} m²</strong></div></div></section> : null },
      ]}
    >
      <div className="neighborhood-workspace">
        {jobId ? <NeighborhoodJobControl jobId={jobId} job={job.data} onCancel={() => cancel.mutate()} onRetry={() => retry.mutate()} onDismiss={() => setJobRoute('')} /> : null}
        {invalid ? <div className="neighborhood-invalid"><CircleAlert size={18} /><span><strong>{t('neighborhood.invalidTitle')}</strong><small>{t('neighborhood.invalidText')}</small></span></div> : null}
        {totals ? <PrimaryMetricStrip className="neighborhood-result-strip city-consumption-strip" metrics={consumptionAvailable ? [
          { label: t('city.hvacConsumption'), value: number(totals.hvac_consumption_gwh_yr, 2), unit: 'GWh/yr' },
          { label: t('city.totalSite'), value: number(totals.total_site_gwh_yr, 2), unit: 'GWh/yr' },
          { label: t('city.totalCarbon'), value: number(totals.total_site_co2_t_yr, 0), unit: 'tCO₂/yr' },
        ] : [
          { label: t('simulation.heating'), value: number(totals.heating_gwh_yr, 2), unit: 'GWh/yr' },
          { label: t('simulation.cooling'), value: number(totals.cooling_gwh_yr, 2), unit: 'GWh/yr' },
          { label: 'S2 CO₂', value: number(totals.s2_co2_t_yr, 0), unit: 't/yr' },
        ]} /> : null}
        <div className="neighborhood-map-stage">{mapData && mapDescriptor ? <Suspense fallback={<div className="page-loading"><span className="spinner" />{t('common.loading')}</div>}><NeighborhoodMap key={`${mapDescriptor.fingerprint}:${mapRevision}`} data={mapData} descriptor={mapDescriptor} mode={mapMode} selectedCluster={selectedCluster} retryToken={mapRevision} onSelectCluster={(cluster) => { setSelectedCluster(cluster); setPanel('selection') }} onRetry={retryMap} /></Suspense> : map.isError ? <div className="neighborhood-map-fetch-error" role="alert"><CircleAlert size={20} /><strong>{t('neighborhood.mapFetchError')}</strong><code>{map.error instanceof Error ? map.error.message : t('common.fail')}</code><button className="secondary-button" onClick={retryMap}>{t('neighborhood.retryMap')}</button></div> : <div className="model-empty"><span className="spinner" />{t('neighborhood.loadingStock')}</div>}</div>
      </div>
    </FocusedWorkspace>
  </div>
}
