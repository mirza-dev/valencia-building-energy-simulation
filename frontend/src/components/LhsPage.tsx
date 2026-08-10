import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Archive, CheckCircle2, CircleAlert, Download, FlaskConical, GitCompareArrows, LockKeyhole, Play, ShieldCheck } from 'lucide-react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import PageHeader from './PageHeader'
import NeighborhoodJobControl from './NeighborhoodJobControl'
import { FocusedWorkspace, PrimaryMetricStrip } from './FocusedWorkspace'
import { useFeedback } from './FeedbackProvider'
import { api, ApiError } from '../lib/api'
import { formatDate } from '../lib/locale'
import { resolveFocusedStatus, type FocusedPanel } from '../lib/focusedWorkspace'
import type { LhsDriver, LhsResult, LhsSample, LhsVariable } from '../lib/types'

type WorkspaceTab = 'distributions' | 'sensitivity' | 'samples'
const outputOrder = ['heating_kwh_m2', 'cooling_kwh_m2', 'co2_kg_m2'] as const
const sampleOutputs = ['heating_kwh_m2', 'cooling_kwh_m2', 'consumption_kwh_m2', 'co2_kg_m2', 'co2_t_building'] as const
const number = (value: number | null | undefined, digits = 2) =>
  value == null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value)

export default function LhsPage() {
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
  const ready = capabilities.data?.capabilities.lhs?.runtime_ready === true
  const preflight = useQuery({ queryKey: ['lhs-preflight'], queryFn: api.lhsPreflight, enabled: ready, staleTime: 300_000 })
  const history = useQuery({ queryKey: ['lhs-runs'], queryFn: api.lhsRuns, enabled: ready, refetchInterval: 10_000 })
  const activeJob = useQuery({ queryKey: ['lhs-active-job'], queryFn: api.activeLhsJob, enabled: ready && !jobId, refetchInterval: 2500 })
  const job = useQuery({
    queryKey: ['job', jobId], queryFn: () => api.lhsJob(jobId), enabled: Boolean(jobId),
    refetchInterval: (query) => ['completed', 'failed', 'canceled'].includes(query.state.data?.status ?? '') ? false : 800,
    retry: (count, error) => !(error instanceof ApiError && [404, 422].includes(error.status)) && count < 3,
  })
  const [selectedRunId, setSelectedRunId] = useState('')
  const [compareRunId, setCompareRunId] = useState('')
  const [tab, setTab] = useState<WorkspaceTab>('distributions')
  const [selectedSampleIndex, setSelectedSampleIndex] = useState(0)
  const [selectedVariable, setSelectedVariable] = useState('infiltration_ach')
  const [panel, setPanel] = useState<FocusedPanel | null>('setup')
  const handledJob = useRef('')
  const selectedRun = history.data?.find((item) => item.id === selectedRunId)
  const result = selectedRun?.result
  const artifactTrusted = Boolean(
    selectedRun?.verification_status === 'VERIFIED'
    && selectedRun.verification?.ok
    && result?.qa.scientific_status === 'VALIDATED',
  )
  const outdatedResult = Boolean(artifactTrusted && selectedRun?.current_compatibility?.current === false)
  const trustedResult = Boolean(artifactTrusted && !outdatedResult)
  const selectedSample = result?.samples[selectedSampleIndex]
  const variables = result?.variables ?? preflight.data?.variables ?? []
  const source = capabilities.data?.capabilities.lhs
  const compare = useQuery({
    queryKey: ['lhs-compare', selectedRunId, compareRunId],
    queryFn: () => api.lhsCompare(selectedRunId, compareRunId),
    enabled: Boolean(selectedRunId && compareRunId && selectedRunId !== compareRunId),
  })

  useEffect(() => {
    if (!jobId && activeJob.data?.job) {
      client.setQueryData(['job', activeJob.data.job.id], activeJob.data.job)
      setSelectedRunId('')
      setJobRoute(activeJob.data.job.id)
      notify(t('lhs.job.restored'), 'success')
    }
  }, [activeJob.data, client, jobId, notify, setJobRoute, t])
  useEffect(() => {
    if (!jobId && !activeJob.isPending && !activeJob.data?.job && !selectedRunId && history.data?.[0]) setSelectedRunId(history.data[0].id)
  }, [activeJob.data, activeJob.isPending, history.data, jobId, selectedRunId])
  useEffect(() => {
    if (job.data?.status === 'completed' && job.data.run_id && handledJob.current !== job.data.id) {
      handledJob.current = job.data.id
      setSelectedRunId(job.data.run_id)
      void client.invalidateQueries({ queryKey: ['lhs-runs'] })
      void client.invalidateQueries({ queryKey: ['runs'] })
      notify(t('lhs.completed'), 'success')
    }
  }, [client, job.data, notify, t])
  useEffect(() => {
    setSelectedSampleIndex(0)
    setCompareRunId((current) => current === selectedRunId ? '' : current)
  }, [selectedRunId])
  useEffect(() => {
    if (result && !trustedResult) setPanel('evidence')
    else if (selectedRunId) setPanel((current) => current === 'setup' ? null : current)
    else if (!jobId && !history.isLoading && !history.data?.length) setPanel('setup')
  }, [history.data, history.isLoading, jobId, result, selectedRunId, trustedResult])

  const start = useMutation({
    mutationFn: api.createLhsRun,
    onSuccess: (created) => {
      handledJob.current = ''
      setSelectedRunId('')
      setPanel(null)
      client.setQueryData(['job', created.id], created)
      setJobRoute(created.id)
      notify(t('lhs.queued'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const cancel = useMutation({ mutationFn: () => api.cancelJob(jobId), onSuccess: (updated) => client.setQueryData(['job', jobId], updated) })
  const retry = useMutation({ mutationFn: () => api.retryJob(jobId), onSuccess: (created) => { client.setQueryData(['job', created.id], created); setJobRoute(created.id) } })
  const running = Boolean(jobId && ['queued', 'running'].includes(job.data?.status ?? 'queued'))
  const statusLevel = resolveFocusedStatus({
    blocked: Boolean(result && !artifactTrusted),
    warning: outdatedResult,
    verified: Boolean(result && trustedResult),
  })
  const protocolRows = useMemo(() => [
    [t('lhs.scope'), preflight.data?.scope ?? '4252702YJ2745A'],
    [t('lhs.sampleCount'), `N=${preflight.data?.settings.n ?? 50}`],
    [t('lhs.seed'), String(preflight.data?.settings.seed ?? 42)],
    [t('lhs.distribution'), t('lhs.uniform')],
    [t('lhs.correlation'), 'Spearman ρ'],
    [t('lhs.modelPath'), t('lhs.massless')],
  ], [preflight.data, t])

  if (capabilities.isPending) return <div className="page-loading"><span className="spinner" />{t('common.loading')}</div>
  if (!ready) return <Navigate to="/builder" replace />
  return <div className="page lhs-page">
    <PageHeader eyebrow={t('lhs.eyebrow')} title={t('lhs.title')} subtitle={t('lhs.subtitle')} />
    <FocusedWorkspace
      className="focused-lhs"
      panel={panel}
      onPanelChange={setPanel}
      toolbarLead={<div className="segmented-control" aria-label={t('lhs.workspace')}><button disabled={Boolean(result && !trustedResult)} className={tab === 'distributions' ? 'active' : ''} onClick={() => setTab('distributions')}>{t('lhs.tabs.distributions')}</button><button disabled={Boolean(result && !trustedResult)} className={tab === 'sensitivity' ? 'active' : ''} onClick={() => setTab('sensitivity')}>{t('lhs.tabs.sensitivity')}</button><button disabled={Boolean(result && !trustedResult)} className={tab === 'samples' ? 'active' : ''} onClick={() => setTab('samples')}>{t('lhs.tabs.samples')}</button></div>}
      status={{ level: statusLevel, label: outdatedResult ? t('lhsOutdated.status') : t(`focused.${statusLevel === 'verified' ? 'verified' : statusLevel === 'blocked' ? 'blocked' : 'pending'}`), detail: selectedRun ? `${selectedRun.verification_status} · ${result?.qa.scientific_status ?? '—'}${outdatedResult ? ` · ${selectedRun.current_compatibility?.changed_roles.length ?? 0} ${t('lhsOutdated.changedInputs')}` : ''}` : t('lhs.preflight') }}
      runs={(history.data ?? []).map((item) => ({ id: item.id, label: `${item.result?.qa.scientific_status ?? item.id.slice(0, 8)}${item.current_compatibility?.current === false ? ` · ${t('lhsOutdated.status')}` : ''}`, meta: `N=${item.result?.summary.samples_completed ?? '—'} · ${formatDate(item.created_at, i18n.language)} · ${item.id.slice(0, 8)}` }))}
      selectedRunId={selectedRunId}
      onSelectRun={setSelectedRunId}
      tabs={[
        { id: 'setup', label: selectedRun ? t('focused.newRun') : t('focused.setup'), content: <>
          <section className="lhs-contract"><div className="inspector-subhead"><ShieldCheck size={14} />{t('lhs.contract')}</div><div className="contract-pass"><CheckCircle2 size={17} /><span><strong>{t('lhs.contractStatus')}</strong><code>{source?.contract?.checks ? Object.values(source.contract.checks).filter(Boolean).length : 0} / {source?.contract?.checks ? Object.keys(source.contract.checks).length : 0}</code></span></div></section>
          <section className="lhs-method"><div className="inspector-subhead"><LockKeyhole size={14} />{t('lhs.lockedBaseline')}</div>{protocolRows.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}<div className="lhs-method-note"><strong>{t('lhs.contextFixed')}</strong><small>{t('lhs.contextFixedText')}</small></div><button className="primary-button lhs-start" onClick={() => start.mutate()} disabled={running || start.isPending || !preflight.data}><Play size={16} />{t('lhs.start')}</button></section>
        </> },
        { id: 'selection', label: t('focused.selection'), hidden: !variables.length, content: <>
          <section className="lhs-variable-register"><div className="table-head"><span>{t('lhs.variableRegister')}</span><code>{variables.length}</code></div>{variables.map((variable) => <VariableRow key={variable.name} variable={variable} active={selectedVariable === variable.name} onSelect={() => setSelectedVariable(variable.name)} />)}</section>
          {selectedSample ? <SampleInspector sample={selectedSample} index={selectedSampleIndex} variables={variables} selectedVariable={selectedVariable} /> : null}
        </> },
        { id: 'evidence', label: t('focused.evidence'), alert: Boolean(result && !trustedResult), content: <>
          {outdatedResult ? <section className="lhs-outdated-evidence"><CircleAlert size={17} /><div><strong>{t('lhsOutdated.title')}</strong><p>{t('lhsOutdated.text')}</p><code>{selectedRun?.current_compatibility?.changed_roles.join(' · ')}</code></div></section> : null}
          {selectedRun ? <div className="focused-drawer-actions"><Link className="secondary-button" to={`/runs?run=${selectedRun.id}`}><Archive size={16} />{t('lhs.openRun')}</Link>{selectedRun.verification_status === 'VERIFIED' ? <a className="secondary-button" href={api.exportUrl(selectedRun.id)}><Download size={16} />{t('common.export')}</a> : null}</div> : null}
          {result ? <QaWorkspace result={result} /> : null}
          {selectedRun ? <section className="lhs-compare"><div className="inspector-subhead"><GitCompareArrows size={14} />{t('lhs.compare')}</div><label><span>{t('lhs.compareWith')}</span><select value={compareRunId} onChange={(event) => setCompareRunId(event.target.value)}><option value="">{t('common.notSelected')}</option>{history.data?.filter((item) => item.id !== selectedRunId).map((item) => <option key={item.id} value={item.id}>{formatDate(item.created_at, i18n.language)} · {item.id.slice(0, 8)}</option>)}</select></label>{compare.data ? <div className={`lhs-compare-status ${compare.data.comparable ? 'pass' : 'warn'}`}><strong>{compare.data.comparable ? t('lhs.comparable') : t('lhs.absoluteOnly')}</strong>{compare.data.rows.slice(0, 4).map((row) => <span key={`${row.output}-${row.statistic}`}><code>{t(`lhs.outputs.${row.output}`)} · {row.statistic}</code><b>{row.delta >= 0 ? '+' : ''}{number(row.delta, 3)}{row.percent != null ? ` · ${number(row.percent, 2)}%` : ''}</b></span>)}</div> : null}</section> : null}
          <section className="lhs-provenance"><span className="eyebrow">{t('lhs.provenance')}</span><dl className="metadata-list"><div><dt>Runner SHA</dt><dd>{preflight.data?.capability.runner_sha256.slice(0, 14)}</dd></div><div><dt>Adapter SHA</dt><dd>{preflight.data?.capability.adapter_sha256.slice(0, 14)}</dd></div><div><dt>{t('lhs.registerHash')}</dt><dd>{preflight.data?.variable_fingerprint.slice(0, 14)}</dd></div></dl></section>
        </> },
      ]}
    >
      <div className="lhs-workspace">
        {jobId ? <NeighborhoodJobControl jobId={jobId} job={job.data} translationRoot="lhs" onCancel={() => cancel.mutate()} onRetry={() => retry.mutate()} onDismiss={() => setJobRoute('')} /> : null}
        {trustedResult && result ? <LhsResultStrip result={result} /> : !selectedRun ? <div className="lhs-preflight-banner"><FlaskConical size={18} /><span><strong>{t('lhs.awaitingRun')}</strong><small>{t('lhs.awaitingRunText')}</small></span></div> : null}
        <div className="lhs-stage">
          {!result ? <ProtocolPreview variables={variables} /> : outdatedResult ? <div className="stock-interpretation-blocked lhs-outdated-block"><CircleAlert size={22} /><strong>{t('lhsOutdated.title')}</strong><p>{t('lhsOutdated.text')}</p><AcceptedReference statistics={preflight.data?.accepted_reference?.statistics} /></div> : !trustedResult ? <div className="stock-interpretation-blocked"><CircleAlert size={22} /><strong>{t('lhs.unverified')}</strong><p>{t('lhs.integrityBlocked')}</p></div> : tab === 'distributions' ? <DistributionWorkspace runId={selectedRun!.id} result={result} /> : tab === 'sensitivity' ? <SensitivityWorkspace runId={selectedRun!.id} result={result} /> : <SampleLedger result={result} selected={selectedSampleIndex} onSelect={(index) => { setSelectedSampleIndex(index); setPanel('selection') }} selectedVariable={selectedVariable} />}
        </div>
      </div>
    </FocusedWorkspace>
  </div>
}

function AcceptedReference({ statistics }: { statistics?: Record<string, { mean?: number }> }) {
  const { t } = useTranslation()
  if (!statistics) return null
  return <div className="lhs-accepted-reference"><span>{t('lhsOutdated.currentReference')}</span><strong>{number(statistics.heating_kwh_m2?.mean)} H</strong><strong>{number(statistics.cooling_kwh_m2?.mean)} C</strong><strong>{number(statistics.co2_kg_m2?.mean)} kgCO₂/m²</strong></div>
}

function VariableRow({ variable, active, onSelect }: { variable: LhsVariable; active: boolean; onSelect: () => void }) {
  const { t } = useTranslation()
  return <button className={active ? 'active' : ''} onClick={onSelect}><span><strong>{t(`lhs.variables.${variable.name}`)}</strong><small>{variable.name}</small></span><span><i className={variable.group}>{variable.group === 'simulation' ? 'SIM' : 'POST'}</i><code>{number(variable.minimum, 3)}–{number(variable.maximum, 3)} {variable.unit}</code></span></button>
}

function LhsResultStrip({ result }: { result: LhsResult }) {
  const { t } = useTranslation()
  const stats = result.summary.statistics
  return <PrimaryMetricStrip className="lhs-result-strip" metrics={[
    { label: t('lhs.heatingMean'), value: number(stats.heating_kwh_m2.mean), unit: 'kWh/m²·yr' },
    { label: t('lhs.coolingMean'), value: number(stats.cooling_kwh_m2.mean), unit: 'kWh/m²·yr' },
    { label: t('lhs.carbonMean'), value: number(stats.co2_kg_m2.mean), unit: 'kgCO₂/m²·yr' },
  ]} />
}

function ProtocolPreview({ variables }: { variables: LhsVariable[] }) {
  const { t } = useTranslation()
  return <div className="lhs-protocol-preview"><div><span className="eyebrow">{t('lhs.protocolPreview')}</span><h2>{t('lhs.protocolTitle')}</h2><p>{t('lhs.protocolText')}</p></div><div className="lhs-protocol-flow"><span>10D LHS</span><i /> <span>50 × OSM</span><i /> <span>50 × E+</span><i /> <span>Spearman ρ</span></div><div className="lhs-domain-counts">{['envelope', 'openings', 'operation', 'system', 'carbon'].map((domain) => <span key={domain}><strong>{variables.filter((item) => item.domain === domain).length}</strong>{t(`lhs.domains.${domain}`)}</span>)}</div></div>
}

function DistributionWorkspace({ runId, result }: { runId: string; result: LhsResult }) {
  const { t } = useTranslation()
  return <div className="lhs-analysis"><figure className="lhs-figure"><img src={api.lhsFigureUrl(runId, 'histograms.png')} alt={t('lhs.distributionFigure')} /><figcaption>{t('lhs.distributionCaption')}</figcaption></figure><section className="lhs-statistics-table"><div className="table-head"><span>{t('lhs.uncertaintyBand')}</span><code>P5–P95</code></div><table><thead><tr><th>{t('lhs.output')}</th><th>{t('lhs.mean')}</th><th>{t('lhs.median')}</th><th>P5</th><th>P95</th><th>σ</th></tr></thead><tbody>{outputOrder.map((output) => { const stat = result.summary.statistics[output]; return <tr key={output}><td>{t(`lhs.outputs.${output}`)}</td><td>{number(stat.mean)}</td><td>{number(stat.median)}</td><td>{number(stat.p5)}</td><td>{number(stat.p95)}</td><td>{number(stat.stddev)}</td></tr> })}</tbody></table></section></div>
}

function SensitivityWorkspace({ runId, result }: { runId: string; result: LhsResult }) {
  const { t } = useTranslation()
  return <div className="lhs-analysis lhs-sensitivity"><figure className="lhs-figure"><img src={api.lhsFigureUrl(runId, 'tornado.png')} alt={t('lhs.sensitivityFigure')} /><figcaption>{t('lhs.sensitivityCaption')}</figcaption></figure><div className="lhs-driver-grid">{outputOrder.map((output) => <section key={output}><header><strong>{t(`lhs.outputs.${output}`)}</strong><code>Spearman ρ</code></header>{result.sensitivity[output]?.map((driver) => <DriverBar key={driver.variable} driver={driver} />)}</section>)}</div></div>
}

function DriverBar({ driver }: { driver: LhsDriver }) {
  const { t } = useTranslation()
  return <div className="lhs-driver"><span><b>{driver.rank}</b>{t(`lhs.variables.${driver.variable}`)}</span><div><i className={driver.rho < 0 ? 'negative' : 'positive'} style={{ width: `${Math.max(4, Math.abs(driver.rho) * 100)}%` }} /></div><code>{driver.rho >= 0 ? '+' : ''}{number(driver.rho, 2)}</code></div>
}

function SampleLedger({ result, selected, onSelect, selectedVariable }: { result: LhsResult; selected: number; onSelect: (index: number) => void; selectedVariable: string }) {
  const { t } = useTranslation()
  return <div className="lhs-sample-ledger"><table><thead><tr><th>#</th><th>{t(`lhs.variables.${selectedVariable}`)}</th>{sampleOutputs.map((column) => <th key={column}>{t(`lhs.outputs.${column}`)}</th>)}</tr></thead><tbody>{result.samples.map((sample, index) => <tr key={index} className={selected === index ? 'active' : ''} onClick={() => onSelect(index)}><td>{String(index + 1).padStart(2, '0')}</td><td>{number(sample[selectedVariable as keyof LhsSample] as number, 4)}</td>{sampleOutputs.map((column) => <td key={column}>{number(sample[column], 2)}</td>)}</tr>)}</tbody></table></div>
}

function QaWorkspace({ result }: { result: LhsResult }) {
  const { t } = useTranslation()
  return <div className="lhs-qa-workspace"><section><div className="table-head"><span>{t('lhs.qaLedger')}</span><code>{result.qa.checks.filter((item) => item.passed).length}/{result.qa.checks.length}</code></div>{result.qa.checks.map((check) => <div className={check.passed ? 'pass' : 'fail'} key={check.name}>{check.passed ? <CheckCircle2 size={15} /> : <CircleAlert size={15} />}<span><strong>{t(`lhs.checks.${check.name}`)}</strong><small>{t('lhs.expected')} {String(check.expected)} · {t('lhs.actual')} {String(check.actual)}</small></span></div>)}</section><section className="lhs-summary-text"><div className="table-head"><span>{t('lhs.runnerSummary')}</span></div><pre>{result.summary_text}</pre></section></div>
}

function SampleInspector({ sample, index, variables, selectedVariable }: { sample: LhsSample; index: number; variables: LhsVariable[]; selectedVariable: string }) {
  const { t } = useTranslation()
  return <section className="lhs-sample-inspector"><span className="eyebrow">{t('lhs.selectedSample')}</span><h2>{t('lhs.sampleLabel')} {String(index + 1).padStart(2, '0')}</h2><div className="lhs-sample-output-grid">{sampleOutputs.map((output) => <span key={output}><small>{t(`lhs.outputs.${output}`)}</small><strong>{number(sample[output], 2)}</strong></span>)}</div><div className="table-head"><span>{t('lhs.parameterVector')}</span><code>10D</code></div><dl>{variables.map((variable) => <div key={variable.name} className={selectedVariable === variable.name ? 'active' : ''}><dt>{t(`lhs.variables.${variable.name}`)}</dt><dd>{number(sample[variable.name as keyof LhsSample] as number, 4)} <small>{variable.unit}</small></dd></div>)}</dl></section>
}
