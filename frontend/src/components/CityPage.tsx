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
import type { CityDistrict, NeighborhoodCluster, StockRunRequest } from '../lib/types'

const CityMap = lazy(() => import('./CityMap'))
const number = (value: number | null | undefined, digits = 1) =>
  value == null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value)

export default function CityPage() {
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
  const ready = capabilities.data?.capabilities.city?.runtime_ready === true
  const history = useQuery({ queryKey: ['city-run-summaries'], queryFn: api.cityRunSummaries, enabled: ready, staleTime: 30_000 })
  const activeJob = useQuery({ queryKey: ['city-active-job'], queryFn: api.activeCityJob, enabled: ready && !jobId, refetchInterval: 2500 })
  const job = useQuery({
    queryKey: ['job', jobId], queryFn: () => api.cityJob(jobId), enabled: Boolean(jobId),
    refetchInterval: (query) => ['completed', 'failed', 'canceled'].includes(query.state.data?.status ?? '') ? false : 800,
    retry: (count, error) => !(error instanceof ApiError && [404, 422].includes(error.status)) && count < 3,
  })
  const [selectedRunId, setSelectedRunId] = useState('')
  const [selectedCluster, setSelectedCluster] = useState('')
  const [selectedDistrict, setSelectedDistrict] = useState('')
  const [mapMode, setMapMode] = useState<'cluster' | 'hvac' | 'site' | 'heating' | 'cooling' | 'district'>('cluster')
  const [ledgerMode, setLedgerMode] = useState<'clusters' | 'districts'>('clusters')
  const [panel, setPanel] = useState<FocusedPanel | null>(null)
  const handledJob = useRef('')
  const selectedRunQuery = useQuery({
    queryKey: ['city-run', selectedRunId], queryFn: () => api.cityRun(selectedRunId),
    enabled: Boolean(selectedRunId), staleTime: 30_000,
  })
  const selectedRun = selectedRunQuery.data
  const needsSetupData = panel === 'setup' || (history.isSuccess && !history.data.length)
  const options = useQuery({ queryKey: ['city-options'], queryFn: api.cityOptions, enabled: ready && needsSetupData, staleTime: 30_000 })
  const preflight = useQuery({ queryKey: ['city-preflight'], queryFn: api.cityPreflight, enabled: ready && needsSetupData, staleTime: 300_000 })
  const mapMetrics = useQuery({
    queryKey: ['city-map-metrics', selectedRunId], queryFn: () => api.cityMapMetrics(selectedRunId),
    enabled: Boolean(selectedRunId && selectedRun?.result?.map_available),
  })
  const result = selectedRun?.result
  const clusters = result?.clusters.length ? result.clusters : preflight.data?.representatives ?? []
  const districts = mapMetrics.data?.districts.length ? mapMetrics.data.districts
    : result?.districts.length ? result.districts : preflight.data?.districts ?? []
  const selectedClusterRow = clusters.find((item) => item.cluster === selectedCluster) ?? clusters[0]
  const selectedClusterResult = selectedClusterRow && 'qa_all_pass' in selectedClusterRow
    ? selectedClusterRow as NeighborhoodCluster : null
  const selectedDistrictRow = districts.find((item) => item.nombre === selectedDistrict) ?? districts[0]

  useEffect(() => {
    if (!jobId && activeJob.data?.job) {
      client.setQueryData(['job', activeJob.data.job.id], activeJob.data.job)
      setSelectedRunId('')
      setJobRoute(activeJob.data.job.id)
      notify(t('city.job.restored'), 'success')
    }
  }, [activeJob.data, client, jobId, notify, setJobRoute, t])
  useEffect(() => {
    if (!jobId && !activeJob.isPending && !activeJob.data?.job && !selectedRunId && history.data?.[0]) setSelectedRunId(history.data[0].id)
  }, [activeJob.data, activeJob.isPending, history.data, jobId, selectedRunId])
  useEffect(() => {
    if (job.data?.status === 'completed' && job.data.run_id && handledJob.current !== job.data.id) {
      handledJob.current = job.data.id
      setSelectedRunId(job.data.run_id)
      void client.invalidateQueries({ queryKey: ['city-run-summaries'] })
      void client.invalidateQueries({ queryKey: ['city-run', job.data.run_id] })
      void client.invalidateQueries({ queryKey: ['runs'] })
      notify(t('city.completed'), 'success')
    }
  }, [client, job.data, notify, t])
  useEffect(() => {
    if (clusters[0] && !clusters.some((item) => item.cluster === selectedCluster)) setSelectedCluster(clusters[0].cluster)
  }, [clusters, selectedCluster])
  useEffect(() => {
    if (districts[0] && !districts.some((item) => item.nombre === selectedDistrict)) setSelectedDistrict(districts[0].nombre)
  }, [districts, selectedDistrict])
  useEffect(() => {
    if (!result?.summary.totals && ['hvac', 'site', 'heating', 'cooling'].includes(mapMode)) setMapMode('cluster')
  }, [mapMode, result?.summary.totals])
  useEffect(() => {
    if (result?.qa.scientific_status === 'INVALID') setPanel('evidence')
    else if (selectedRunId) setPanel((current) => current === 'setup' ? null : current)
    else if (!jobId && history.isSuccess && !history.data.length) setPanel('setup')
  }, [history.data, history.isSuccess, jobId, result?.qa.scientific_status, selectedRunId])

  const start = useMutation({
    mutationFn: (payload: StockRunRequest) => api.createCityRun(payload),
    onSuccess: (created) => {
      handledJob.current = ''
      setSelectedRunId('')
      setPanel(null)
      client.setQueryData(['job', created.id], created)
      setJobRoute(created.id)
      notify(t('city.queued'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const cancel = useMutation({ mutationFn: () => api.cancelJob(jobId), onSuccess: (updated) => client.setQueryData(['job', jobId], updated) })
  const retry = useMutation({ mutationFn: () => api.retryJob(jobId), onSuccess: (created) => { client.setQueryData(['job', created.id], created); setJobRoute(created.id) } })
  const running = Boolean(jobId && ['queued', 'running'].includes(job.data?.status ?? 'queued'))
  const totals = result?.summary.totals
  const invalid = result?.qa.scientific_status === 'INVALID'
  const verifiedScientificRun = Boolean(selectedRun?.verification_status === 'VERIFIED' && selectedRun.verification?.ok && result?.qa.scientific_status === 'VALIDATED')
  const statusLevel = resolveFocusedStatus({ blocked: invalid, verified: verifiedScientificRun })
  const source = capabilities.data?.capabilities.city
  const bounds = mapMetrics.data?.bounds ?? preflight.data?.bounds
  const focusBounds = mapMetrics.data?.focus_bounds ?? bounds
  const tileUrl = api.cityTileUrl(result?.energy_map_available ? selectedRun?.id : undefined)
  const methodRows = useMemo(() => [
    [t('city.scope'), 'Valencia'],
    [t('city.stock'), `${preflight.data?.summary.buildings ?? '—'} ${t('city.buildings')}`],
    [t('city.districts'), `${preflight.data?.summary.districts ?? '—'} ${t('city.district')}`],
    [t('city.method'), t('city.typologyMethod')],
    [t('city.execution'), t('city.sequential')],
  ], [preflight.data, t])

  if (capabilities.isPending) return <div className="page-loading"><span className="spinner" />{t('common.loading')}</div>
  if (!ready) return <Navigate to="/health" replace />
  return <div className="page neighborhood-page city-page">
    <PageHeader eyebrow={t('city.eyebrow')} title={t('city.title')} subtitle={t('city.subtitle')} />
    <FocusedWorkspace
      className="focused-city"
      panel={panel}
      onPanelChange={setPanel}
      toolbarLead={<div className="segmented-control city-map-modes" aria-label={t('city.mapMode')}><button className={mapMode === 'cluster' ? 'active' : ''} onClick={() => { setMapMode('cluster'); setLedgerMode('clusters') }}>{t('city.cluster')}</button><button className={mapMode === 'hvac' ? 'active' : ''} onClick={() => { setMapMode('hvac'); setLedgerMode('clusters') }} disabled={!totals}>{t('city.hvacShort')}</button><button className={mapMode === 'site' ? 'active' : ''} onClick={() => { setMapMode('site'); setLedgerMode('clusters') }} disabled={!totals}>{t('city.siteShort')}</button><button className={mapMode === 'heating' ? 'active' : ''} onClick={() => { setMapMode('heating'); setLedgerMode('clusters') }} disabled={!totals}>{t('city.heatDemandShort')}</button><button className={mapMode === 'cooling' ? 'active' : ''} onClick={() => { setMapMode('cooling'); setLedgerMode('clusters') }} disabled={!totals}>{t('city.coolDemandShort')}</button><button className={mapMode === 'district' ? 'active' : ''} onClick={() => { setMapMode('district'); setLedgerMode('districts') }}>{t('city.district')}</button></div>}
      status={{ level: statusLevel, label: t(`focused.${statusLevel === 'verified' ? 'verified' : statusLevel === 'blocked' ? 'blocked' : 'pending'}`), detail: selectedRun ? `${selectedRun.verification_status} · ${result?.qa.scientific_status ?? '—'}` : t('city.preflight') }}
      runs={(history.data ?? []).map((item) => ({ id: item.id, label: item.scenario_name || item.scientific_status || item.id.slice(0, 8), meta: formatDate(item.created_at, i18n.language) }))}
      selectedRunId={selectedRunId}
      onSelectRun={setSelectedRunId}
      tabs={[
        { id: 'setup', label: selectedRun ? t('focused.newRun') : t('focused.setup'), content: <>
          <section className="neighborhood-contract"><div className="inspector-subhead"><ShieldCheck size={14} />{t('city.contract')}</div><div className="contract-pass"><CheckCircle2 size={17} /><span><strong>{t('city.contractStatus')}</strong><code>{source?.contract?.checks ? Object.values(source.contract.checks).filter(Boolean).length : 0} / {source?.contract?.checks ? Object.keys(source.contract.checks).length : 0}</code></span></div></section>
          <section className="neighborhood-method"><div className="inspector-subhead"><LockKeyhole size={14} />{t('city.verifiedProtocol')}</div>{methodRows.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}<div className="preflight-strip"><span>{t('city.resArea')}</span><strong>{number(preflight.data?.summary.residential_area_m2, 0)} m²</strong><small>{preflight.data?.summary.imputed_floor_buildings ?? '—'} {t('city.floorImputed')} · {preflight.data?.summary.proxy_area_buildings ?? '—'} {t('city.areaProxy')} · {preflight.data?.summary.duplicate_parcel_rows ?? '—'} {t('city.duplicateParcels')}</small></div></section>
          <StockRunSetup workflow="city" options={options.data} disabled={running || !preflight.data} pending={start.isPending} onStart={(payload) => start.mutate(payload as StockRunRequest)} />
        </> },
        { id: 'selection', label: t('focused.selection'), hidden: !clusters.length && !districts.length, content: <>
          <div className="city-ledger-tabs segmented-control" aria-label={t('city.ledger')}><button className={ledgerMode === 'clusters' ? 'active' : ''} onClick={() => setLedgerMode('clusters')}>{t('city.clusters')}</button><button className={ledgerMode === 'districts' ? 'active' : ''} onClick={() => setLedgerMode('districts')}>{t('city.districts')}</button></div>
          {ledgerMode === 'clusters' ? <>{selectedClusterRow ? <section className="cluster-inspector"><span className="eyebrow">{t('city.selectedCluster')}</span><h2>{selectedClusterRow.cluster}</h2><code>{selectedClusterRow.refparcela}</code><dl className="metadata-list"><div><dt>{t('city.familyPeriod')}</dt><dd>{selectedClusterRow.family} · {selectedClusterRow.period}</dd></div><div><dt>{t('city.buildings')}</dt><dd>{selectedClusterRow.n_buildings}</dd></div><div><dt>{t('city.repFootprint')}</dt><dd>{number(selectedClusterRow.rep_area_m2)} m²</dd></div><div><dt>{t('city.repFloors')}</dt><dd>{selectedClusterRow.rep_floors}</dd></div>{selectedClusterResult ? <><div className="primary-metric"><dt>{t('city.hvacConsumption')}</dt><dd>{number(selectedClusterResult.cons_hc_kwh_m2, 2)} kWh/m²</dd></div><div className="primary-metric"><dt>{t('city.totalSite')}</dt><dd>{number(selectedClusterResult.total_site_kwh_m2, 2)} kWh/m²</dd></div><div><dt>{t('city.demand')}</dt><dd>{number(selectedClusterResult.heating_kwh_m2, 2)} H · {number(selectedClusterResult.cooling_kwh_m2, 2)} C</dd></div><div><dt>{t('city.carbon')}</dt><dd>{number(selectedClusterResult.hvac_co2_kg_m2, 2)} HVAC · {number(selectedClusterResult.total_site_co2_kg_m2, 2)} total</dd></div><div><dt>{t('city.energyPlusQa')}</dt><dd>{selectedClusterResult.qa_all_pass ? 'PASS' : 'FAIL'} · {selectedClusterResult.eplus_warnings}</dd></div></> : null}</dl></section> : null}<section className="cluster-ledger-list"><div className="table-head"><span>{t('city.clusterLedger')}</span><code>{clusters.length}</code></div>{clusters.map((item) => <button key={item.cluster} className={selectedCluster === item.cluster ? 'active' : ''} onClick={() => setSelectedCluster(item.cluster)}><span><strong>{item.cluster}</strong><small>{item.n_buildings} {t('city.buildings')} · {item.refparcela}</small></span>{'qa_all_pass' in item ? <i className={item.qa_all_pass ? 'pass' : 'fail'}>{item.qa_all_pass ? '✓' : '!'}</i> : <i>{item.period}</i>}</button>)}</section></> : <>{selectedDistrictRow ? <DistrictInspector district={selectedDistrictRow} /> : null}<section className="cluster-ledger-list district-ledger-list"><div className="table-head"><span>{t('city.districtLedger')}</span><code>{districts.length}</code></div>{districts.map((item) => <button key={item.nombre} className={selectedDistrict === item.nombre ? 'active' : ''} onClick={() => setSelectedDistrict(item.nombre)}><span><strong>{item.nombre}</strong><small>{item.n_buildings} {t('city.buildings')} · {number(item.res_area_m2, 0)} m²</small></span><i>{item.coddistrit ?? '—'}</i></button>)}</section></>}
        </> },
        { id: 'evidence', label: t('focused.evidence'), alert: invalid || Boolean(result?.qa.failures.length), content: <>
          {selectedRun ? <div className="focused-drawer-actions"><Link className="secondary-button" to={`/runs?run=${selectedRun.id}`}><Archive size={16} />{t('city.openRun')}</Link><a className="secondary-button" href={api.exportUrl(selectedRun.id)}><Download size={16} />{t('common.export')}</a></div> : null}
          <section className="neighborhood-contract"><div className="inspector-subhead"><ShieldCheck size={14} />{t('focused.evidence')}</div><div className="contract-pass"><CheckCircle2 size={17} /><span><strong>{selectedRun?.verification_status ?? t('focused.pending')}</strong><code>{result?.qa.scientific_status ?? 'PREFLIGHT'}</code></span></div></section>
          {result?.qa.failures.length ? <section className="cluster-failures"><div className="inspector-subhead"><CircleAlert size={14} />{t('city.failures')}</div>{result.qa.failures.map((failure) => <button key={failure.cluster} onClick={() => { setLedgerMode('clusters'); setSelectedCluster(failure.cluster); setPanel('selection') }}><strong>{failure.cluster}</strong><small>{failure.error}</small></button>)}</section> : null}
        </> },
        { id: 'metrics', label: t('focused.metrics'), hidden: !totals, content: totals ? <section className="simulation-section"><div className="table-head"><span>{t('focused.metrics')}</span><code>IDEAL LOADS + HVAC</code></div><div className="metric-pairs"><div><span>{t('simulation.heating')}</span><strong>{number(totals.heating_gwh_yr, 2)} GWh/yr</strong></div><div><span>{t('simulation.cooling')}</span><strong>{number(totals.cooling_gwh_yr, 2)} GWh/yr</strong></div><div><span>{t('city.hvacCarbon')}</span><strong>{number(totals.hvac_co2_t_yr, 0)} tCO₂/yr</strong></div><div><span>S1 / S2 CO₂</span><strong>{number(totals.s1_co2_t_yr, 0)} / {number(totals.s2_co2_t_yr, 0)} t/yr</strong></div><div><span>{t('city.resArea')}</span><strong>{number(totals.residential_area_m2, 0)} m²</strong></div></div></section> : null },
      ]}
    >
      <div className="neighborhood-workspace">
        {jobId ? <NeighborhoodJobControl jobId={jobId} job={job.data} translationRoot="city" onCancel={() => cancel.mutate()} onRetry={() => retry.mutate()} onDismiss={() => setJobRoute('')} /> : null}
        {invalid ? <div className="neighborhood-invalid city-invalid"><CircleAlert size={18} /><span><strong>{t('city.invalidTitle')}</strong><small>{t('city.invalidText')}</small></span></div> : null}
        {totals ? <PrimaryMetricStrip className="neighborhood-result-strip city-consumption-strip" metrics={[
          { label: t('city.hvacConsumption'), value: number(totals.hvac_consumption_gwh_yr, 2), unit: 'GWh/yr' },
          { label: t('city.totalSite'), value: number(totals.total_site_gwh_yr, 2), unit: 'GWh/yr' },
          { label: t('city.totalCarbon'), value: number(totals.total_site_co2_t_yr, 0), unit: 'tCO₂/yr' },
        ]} /> : null}
        <div className="neighborhood-map-stage">{bounds && focusBounds && clusters.length ? <Suspense fallback={<div className="page-loading"><span className="spinner" />{t('common.loading')}</div>}><CityMap tileUrl={tileUrl} bounds={bounds} focusBounds={focusBounds} clusters={clusters} districts={districts} mode={mapMode} selectedCluster={selectedCluster} selectedDistrict={selectedDistrict} onSelectCluster={(cluster) => { setSelectedCluster(cluster); setLedgerMode('clusters'); setPanel('selection') }} onSelectDistrict={(district) => { setSelectedDistrict(district); setLedgerMode('districts'); setPanel('selection') }} /></Suspense> : <div className="model-empty"><span className="spinner" />{t('city.loadingStock')}</div>}</div>
      </div>
    </FocusedWorkspace>
  </div>
}

function DistrictInspector({ district }: { district: CityDistrict }) {
  const { t } = useTranslation()
  return <section className="cluster-inspector district-inspector"><span className="eyebrow">{t('city.selectedDistrict')}</span><h2>{district.nombre}</h2><code>{t('city.districtCode')} · {district.coddistrit ?? '—'}</code><dl className="metadata-list"><div><dt>{t('city.buildings')}</dt><dd>{district.n_buildings}</dd></div><div><dt>{t('city.resArea')}</dt><dd>{number(district.res_area_m2, 0)} m²</dd></div>{district.cons_hc_gwh != null ? <><div className="primary-metric"><dt>{t('city.hvacConsumption')}</dt><dd>{number(district.cons_hc_gwh, 2)} GWh</dd></div><div className="primary-metric"><dt>{t('city.totalSite')}</dt><dd>{number(district.total_site_gwh, 2)} GWh</dd></div><div><dt>{t('city.carbon')}</dt><dd>{number(district.hvac_co2_t, 0)} HVAC · {number(district.total_site_co2_t, 0)} total t</dd></div></> : null}{district.heating_gwh != null ? <><div><dt>{t('city.demand')}</dt><dd>{number(district.heating_gwh, 2)} H · {number(district.cooling_gwh, 2)} C GWh</dd></div><div><dt>S1 / S2 CO₂</dt><dd>{number(district.s1_co2_t, 0)} / {number(district.s2_co2_t, 0)} t</dd></div></> : null}</dl></section>
}
