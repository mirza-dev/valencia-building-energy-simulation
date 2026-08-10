import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Archive, Download, GitBranch, GitCompareArrows, LockKeyhole, Play, ShieldCheck } from 'lucide-react'
import { Navigate, Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import PageHeader from './PageHeader'
import { SimulationEvidencePanel, SimulationFocusedResult, SimulationResultPanel } from './SimulationResultPanel'
import { FocusedWorkspace } from './FocusedWorkspace'
import SimulationJobControl from './SimulationJobControl'
import NeighborhoodJobControl from './NeighborhoodJobControl'
import ScenarioSetupPanel from './ScenarioSetupPanel'
import { useFeedback } from './FeedbackProvider'
import { api, ApiError } from '../lib/api'
import { formatDate } from '../lib/locale'
import { resolveFocusedStatus, type FocusedPanel } from '../lib/focusedWorkspace'
import { useActiveBuilding } from '../lib/activeBuilding'
import type { JobRecord, ScenarioRequest, SimulationPairResponse } from '../lib/types'

export default function SimulationPage() {
  const { t, i18n } = useTranslation()
  const { notify } = useFeedback()
  const client = useQueryClient()
  const { activeBuilding, setActiveBuilding } = useActiveBuilding()
  const [searchParams, setSearchParams] = useSearchParams()
  const jobId = searchParams.get('job') ?? ''
  const scenarioJobId = searchParams.get('scenario_job') ?? ''
  const requestedModelId = searchParams.get('model') ?? ''
  const setJobRoutes = useCallback((simulationId = '', scenarioId = '') => {
    const next = new URLSearchParams(searchParams)
    if (simulationId) next.set('job', simulationId)
    else next.delete('job')
    if (scenarioId) next.set('scenario_job', scenarioId)
    else next.delete('scenario_job')
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])

  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities })
  const simulationReady = capabilities.data?.capabilities.simulation?.runtime_ready === true
  const scenarioReady = capabilities.data?.capabilities.scenario?.runtime_ready === true
  const [mode, setMode] = useState<'baseline' | 'scenario'>(scenarioJobId ? 'scenario' : 'baseline')
  const eligible = useQuery({
    queryKey: ['simulation-eligible'], queryFn: api.eligibleSimulationModels, enabled: simulationReady,
  })
  const scenarioOptions = useQuery({
    queryKey: ['scenario-options'], queryFn: api.scenarioOptions, enabled: simulationReady && scenarioReady,
  })
  const history = useQuery({
    queryKey: ['simulations'], queryFn: api.simulations, enabled: simulationReady, refetchInterval: 10_000,
  })
  const [parentId, setParentId] = useState('')
  const [selectedRunId, setSelectedRunId] = useState('')
  const [panel, setPanel] = useState<FocusedPanel | null>(null)
  const selectParentModel = useCallback((modelId: string) => {
    const next = new URLSearchParams(searchParams)
    next.delete('model')
    setSearchParams(next, { replace: true })
    setParentId(modelId)
  }, [searchParams, setSearchParams])
  const handledTerminalJob = useRef('')
  const handledScenarioJob = useRef('')
  const handledInvalidJob = useRef('')
  const previousSelectedRunId = useRef('')
  const allAvailableParents = mode === 'scenario' ? scenarioOptions.data?.parents : eligible.data
  const availableParents = allAvailableParents?.filter((item) => item.refparcela === activeBuilding)
  const buildingHistory = useMemo(
    () => (history.data ?? []).filter((item) => item.refparcela === activeBuilding),
    [activeBuilding, history.data],
  )
  const selectedParent = availableParents?.find((item) => item.id === parentId)

  const activeJob = useQuery({
    queryKey: ['simulation-active-job'], queryFn: () => api.activeSimulationJob(),
    enabled: simulationReady && !jobId && !scenarioJobId && !requestedModelId, refetchInterval: 2500,
  })
  const activeScenario = useQuery({
    queryKey: ['scenario-active-job'], queryFn: api.activeScenarioJob,
    enabled: scenarioReady && !jobId && !scenarioJobId && !requestedModelId, refetchInterval: 2500,
  })
  const job = useQuery({
    queryKey: ['job', jobId], queryFn: () => api.simulationJob(jobId), enabled: Boolean(jobId),
    refetchInterval: (query) => ['completed', 'failed', 'canceled'].includes(query.state.data?.status ?? '') ? false : 700,
    retry: (count, error) => !(error instanceof ApiError && [404, 422].includes(error.status)) && count < 3,
  })
  const scenarioJob = useQuery({
    queryKey: ['scenario-job', scenarioJobId], queryFn: () => api.scenarioJob(scenarioJobId), enabled: Boolean(scenarioJobId),
    refetchInterval: (query) => ['completed', 'failed', 'canceled'].includes(query.state.data?.status ?? '') ? false : 700,
    retry: (count, error) => !(error instanceof ApiError && [404, 422].includes(error.status)) && count < 3,
  })
  const selectedRun = buildingHistory.find((item) => item.id === selectedRunId)
  const selectedResult = selectedRun?.result
  const automaticBaselineRunId = selectedRun?.automatic_baseline?.simulation_run_id ?? ''

  useEffect(() => {
    if (!requestedModelId) return
    const requested = allAvailableParents?.find((item) => item.id === requestedModelId)
    if (requested && requested.refparcela !== activeBuilding) setActiveBuilding(requested.refparcela)
    if (requested) {
      setSelectedRunId('')
      setPanel('setup')
    }
  }, [activeBuilding, allAvailableParents, requestedModelId, setActiveBuilding])
  useEffect(() => {
    const jobReference = job.data?.refparcela ?? scenarioJob.data?.refparcela
    if (jobReference && jobReference !== activeBuilding) setActiveBuilding(jobReference)
  }, [activeBuilding, job.data?.refparcela, scenarioJob.data?.refparcela, setActiveBuilding])
  useEffect(() => {
    if (!availableParents?.length) return
    const requested = mode === 'baseline' && availableParents.some((item) => item.id === requestedModelId)
      ? requestedModelId : ''
    if (requested && parentId !== requested) setParentId(requested)
    else if (!availableParents.some((item) => item.id === parentId)) setParentId(availableParents[0].id)
  }, [availableParents, mode, parentId, requestedModelId])
  useEffect(() => {
    if (jobId || scenarioJobId) return
    const simulation = activeJob.data?.job?.refparcela === activeBuilding ? activeJob.data.job : null
    const scenario = activeScenario.data?.job?.refparcela === activeBuilding ? activeScenario.data.job : null
    if (simulation) {
      client.setQueryData(['job', simulation.id], simulation)
      setSelectedRunId('')
      setJobRoutes(simulation.id)
      notify(t('simulation.job.restored'), 'success')
    } else if (scenario) {
      setMode('scenario')
      client.setQueryData(['scenario-job', scenario.id], scenario)
      setSelectedRunId('')
      setJobRoutes('', scenario.id)
      notify(t('scenario.job.restored'), 'success')
    }
  }, [activeBuilding, activeJob.data, activeScenario.data, client, jobId, notify, scenarioJobId, setJobRoutes, t])
  useEffect(() => {
    const jobParent = job.data?.payload?.parent_run_id
    if (mode === 'baseline' && typeof jobParent === 'string' && jobParent !== parentId) setParentId(jobParent)
  }, [job.data, mode, parentId])
  useEffect(() => {
    const invalidSimulation = job.error instanceof ApiError && [404, 422].includes(job.error.status)
    const invalidScenario = scenarioJob.error instanceof ApiError && [404, 422].includes(scenarioJob.error.status)
    const invalidId = invalidSimulation ? jobId : invalidScenario ? scenarioJobId : ''
    if (invalidId && handledInvalidJob.current !== invalidId) {
      handledInvalidJob.current = invalidId
      setJobRoutes()
      notify(t('simulation.job.invalidRecovery'), 'info')
    }
  }, [job.error, jobId, notify, scenarioJob.error, scenarioJobId, setJobRoutes, t])
  useEffect(() => {
    if (requestedModelId && !jobId && !scenarioJobId) return
    const recovering = !requestedModelId && !jobId && !scenarioJobId && (
      activeJob.isPending || activeScenario.isPending || Boolean(activeJob.data?.job) || Boolean(activeScenario.data?.job)
    )
    if (!jobId && !scenarioJobId && !recovering && !selectedRunId && buildingHistory[0]) {
      setSelectedRunId(buildingHistory[0].id)
    }
  }, [activeJob.data, activeJob.isPending, activeScenario.data, activeScenario.isPending, buildingHistory, jobId, requestedModelId, scenarioJobId, selectedRunId])
  useEffect(() => {
    if (job.data?.status === 'completed' && job.data.run_id && handledTerminalJob.current !== job.data.id) {
      handledTerminalJob.current = job.data.id
      setSelectedRunId(job.data.run_id)
      void client.invalidateQueries({ queryKey: ['simulations'] })
      void client.invalidateQueries({ queryKey: ['runs'] })
      notify(t('simulation.completed'), 'success')
    }
  }, [client, job.data, notify, t])
  useEffect(() => {
    const current = scenarioJob.data
    if (current?.status !== 'completed' || handledScenarioJob.current === current.id) return
    handledScenarioJob.current = current.id
    void client.invalidateQueries({ queryKey: ['runs'] })
    void client.invalidateQueries({ queryKey: ['simulation-eligible'] })
    void client.invalidateQueries({ queryKey: ['scenario-options'] })
    const childId = current.payload?.simulation_job_id
    if (typeof childId === 'string' && childId) {
      setJobRoutes(childId)
      notify(t('scenario.variantCommitted'), 'success')
    } else {
      notify(String(current.payload?.simulation_error ?? t('scenario.simulationNotQueued')), 'error')
    }
  }, [client, notify, scenarioJob.data, setJobRoutes, t])
  useEffect(() => {
    if (selectedResult?.qa.scientific_status === 'INVALID') setPanel('evidence')
    else if (selectedRunId && selectedRunId !== previousSelectedRunId.current) {
      previousSelectedRunId.current = selectedRunId
      setPanel((current) => current === 'setup' ? current : null)
    } else if (!selectedRunId) {
      previousSelectedRunId.current = ''
      if (!jobId && !scenarioJobId && history.isSuccess && !buildingHistory.length) setPanel('setup')
    }
  }, [buildingHistory.length, history.isSuccess, jobId, scenarioJobId, selectedResult?.qa.scientific_status, selectedRunId])

  const start = useMutation<JobRecord | SimulationPairResponse, Error, void>({
    mutationFn: () => selectedParent?.provenance === 'authored'
      ? api.createSimulationPair(parentId)
      : api.createSimulation(parentId),
    onSuccess: (created) => {
      handledTerminalJob.current = ''
      setSelectedRunId('')
      setPanel(null)
      if ('authored' in created) {
        if (created.authored.simulation_run_id) {
          setSelectedRunId(created.authored.simulation_run_id)
          setJobRoutes()
          void client.invalidateQueries({ queryKey: ['simulations'] })
        } else if (created.authored.job) {
          client.setQueryData(['job', created.authored.job.id], created.authored.job)
          setJobRoutes(created.authored.job.id)
        }
        notify(t('simulation.pairQueued'), 'success')
      } else {
        client.setQueryData(['job', created.id], created)
        setJobRoutes(created.id)
        notify(t('simulation.queued'), 'success')
      }
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const startScenario = useMutation({
    mutationFn: (request: ScenarioRequest) => api.createScenario(request),
    onSuccess: (created) => {
      handledScenarioJob.current = ''
      setSelectedRunId('')
      setPanel(null)
      client.setQueryData(['scenario-job', created.id], created)
      setJobRoutes('', created.id)
      notify(t('scenario.queued'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const resumeSimulation = useMutation({
    mutationFn: () => api.createSimulation(String(scenarioJob.data?.payload?.model_run_id)),
    onSuccess: (created) => { client.setQueryData(['job', created.id], created); setJobRoutes(created.id) },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelJob(id),
    onSuccess: (updated) => {
      client.setQueryData(updated.kind === 'scenario' ? ['scenario-job', updated.id] : ['job', updated.id], updated)
      notify(t('simulation.job.cancelRequested'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const retry = useMutation({
    mutationFn: (id: string) => api.retryJob(id),
    onSuccess: (created) => {
      setSelectedRunId('')
      if (created.kind === 'scenario') {
        handledScenarioJob.current = ''
        client.setQueryData(['scenario-job', created.id], created)
        setJobRoutes('', created.id)
      } else {
        handledTerminalJob.current = ''
        client.setQueryData(['job', created.id], created)
        setJobRoutes(created.id)
      }
      notify(t('simulation.job.retryQueued'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const simulationRunning = Boolean(jobId && ['queued', 'running'].includes(job.data?.status ?? 'queued'))
  const scenarioRunning = Boolean(scenarioJobId && ['queued', 'running'].includes(scenarioJob.data?.status ?? 'queued'))
  const running = simulationRunning || scenarioRunning
  const invalid = selectedResult?.qa.scientific_status === 'INVALID'
  const verifiedScientificRun = Boolean(selectedRun?.verification_status === 'VERIFIED' && selectedRun.verification?.ok && selectedResult?.qa.scientific_status === 'VALIDATED')
  const statusLevel = resolveFocusedStatus({ blocked: invalid, verified: verifiedScientificRun })
  const settingsRows = useMemo(() => selectedParent ? [
    [t('simulation.period'), t('simulation.annual')],
    [t('simulation.weather'), selectedParent.weather_snapshot_hash.slice(0, 16)],
    [t('simulation.timestep'), `${selectedParent.settings.timestep_per_hour} / h`],
    [t('simulation.outputs'), `${selectedParent.settings.output_variables.length} · RunPeriod`],
    [t('simulation.energyBasis'), t(selectedParent.settings.energy_basis === 'detailed_hvac_consumption' ? 'simulation.detailedHvacBasis' : 'simulation.idealLoadsBasis')],
    [t('simulation.areaBasis'), t('simulation.conditionedResidential')],
  ] : [], [selectedParent, t])

  if (capabilities.isPending) return <div className="page-loading"><span className="spinner" />{t('common.loading')}</div>
  if (!simulationReady) return <Navigate to="/builder" replace />
  return <div className="page simulation-page">
    <PageHeader eyebrow={t('simulation.eyebrow')} title={t('simulation.title')} subtitle={mode === 'scenario' ? t('scenario.subtitle') : t('simulation.subtitle')} />
    <FocusedWorkspace
      className="focused-simulation"
      panel={panel}
      onPanelChange={setPanel}
      status={{ level: statusLevel, label: t(`focused.${statusLevel === 'verified' ? 'verified' : statusLevel === 'blocked' ? 'blocked' : 'pending'}`), detail: selectedRun ? `${selectedRun.verification_status} · ${selectedResult?.qa.scientific_status ?? '—'}` : t('focused.pending') }}
      runs={buildingHistory.map((item) => ({ id: item.id, label: item.scenario_name, meta: `${formatDate(item.created_at, i18n.language)} · ${item.id.slice(0, 8)}` }))}
      selectedRunId={selectedRunId}
      onSelectRun={setSelectedRunId}
      tabs={[
        { id: 'setup', label: selectedRun ? t('focused.newRun') : t('focused.setup'), content: <div className="simulation-setup-panel">
          <div className="simulation-mode-switch segmented-control" aria-label={t('scenario.mode')}><button className={mode === 'baseline' ? 'active' : ''} onClick={() => setMode('baseline')} disabled={running}><Play size={14} />{t('scenario.baselineMode')}</button>{scenarioReady ? <button className={mode === 'scenario' ? 'active' : ''} onClick={() => setMode('scenario')} disabled={running}><GitBranch size={14} />{t('scenario.scenarioMode')}</button> : null}</div>
          <section className="simulation-start-block"><span className="eyebrow">{t('simulation.parentModel')}</span><label><span>{t('simulation.verifiedParent')}</span><select data-testid="simulation-parent-model" value={parentId} onChange={(event) => selectParentModel(event.target.value)} disabled={running}>{availableParents?.map((item) => <option key={item.id} value={item.id}>{item.refparcela} · {item.scenario_name}</option>)}</select></label>
            {selectedParent ? <div className="parent-hash"><ShieldCheck size={15} /><span><strong>VERIFIED</strong><code>{selectedParent.raw_model_sha256.slice(0, 20)}</code></span></div> : null}
            {mode === 'baseline' ? <><div className="locked-settings"><div className="inspector-subhead"><LockKeyhole size={13} />{t('simulation.lockedSettings')}</div>{settingsRows.map(([label, value]) => <div key={label}><span>{label}</span><code title={value}>{value}</code></div>)}</div><button className="primary-button simulation-start" onClick={() => start.mutate()} disabled={!parentId || running || start.isPending}><Play size={16} />{t(selectedParent?.provenance === 'authored' ? 'simulation.startPair' : 'simulation.start')}</button></> : scenarioOptions.data ? <ScenarioSetupPanel options={scenarioOptions.data} parent={scenarioOptions.data.parents.find((item) => item.id === parentId)} disabled={running} pending={startScenario.isPending} onStart={(request) => startScenario.mutate(request)} /> : <div className="skeleton-stack compact"><i /><i /><i /></div>}
          </section>
        </div> },
        { id: 'evidence', label: t('focused.evidence'), alert: invalid, content: <>
          {selectedRun ? <div className="focused-drawer-actions">{automaticBaselineRunId ? <Link data-testid="compare-automatic-baseline" className="secondary-button" to={`/compare?mode=simulation&left=${automaticBaselineRunId}&right=${selectedRun.id}`}><GitCompareArrows size={16} />{t('simulation.compareAutomaticBaseline')}</Link> : null}<Link className="secondary-button" to={`/runs?run=${selectedRun.id}`}><Archive size={16} />{t('simulation.openRun')}</Link><a className="secondary-button" href={api.exportUrl(selectedRun.id)}><Download size={16} />{t('common.export')}</a></div> : null}
          <SimulationEvidencePanel run={selectedRun} />
        </> },
        { id: 'metrics', label: t('focused.metrics'), hidden: !selectedResult, content: <SimulationResultPanel result={selectedResult} /> },
      ]}
    >
      <div className="simulation-main-panel">
        {scenarioJobId ? <NeighborhoodJobControl translationRoot="scenario" jobId={scenarioJobId} job={scenarioJob.data} onCancel={() => cancel.mutate(scenarioJobId)} onRetry={() => retry.mutate(scenarioJobId)} onDismiss={() => setJobRoutes()} /> : null}
        {jobId ? <SimulationJobControl jobId={jobId} job={job.data} cancelPending={cancel.isPending} retryPending={retry.isPending} onCancel={() => cancel.mutate(jobId)} onRetry={() => retry.mutate(jobId)} onDismiss={() => setJobRoutes()} /> : null}
        {scenarioJob.data?.status === 'completed' && scenarioJob.data.payload?.simulation_error ? <div className="scenario-resume"><span><strong>{t('scenario.variantReady')}</strong><small>{String(scenarioJob.data.payload.simulation_error)}</small></span><button className="primary-button" onClick={() => resumeSimulation.mutate()} disabled={resumeSimulation.isPending}><Play size={15} />{t('scenario.retrySimulation')}</button></div> : null}
        <SimulationFocusedResult result={selectedResult} />
      </div>
    </FocusedWorkspace>
  </div>
}
