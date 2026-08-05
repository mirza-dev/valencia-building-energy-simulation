import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Building2, CheckCircle2, Clock3, MapPinned, Play, RotateCcw, Square, TerminalSquare } from 'lucide-react'
import { api } from '../lib/api'
import { countDone, formatDuration, formatProductBytes, safeRunName } from '../lib/productStock'
import type { ProductPreflight } from '../lib/types'
import { useFeedback } from './FeedbackProvider'

type Scope = 'references' | 'district' | 'all'

function initialRunName() {
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '')
  return `stock_${stamp}`
}

export default function RunPage() {
  const queryClient = useQueryClient()
  const { notify } = useFeedback()
  const [scope, setScope] = useState<Scope>('references')
  const [district, setDistrict] = useState('BENICALAP')
  const [referencesText, setReferencesText] = useState('4252702YJ2745A')
  const [name, setName] = useState(initialRunName)
  const [confirmedAll, setConfirmedAll] = useState(false)
  const [preflight, setPreflight] = useState<ProductPreflight | null>(null)
  const [selectedRun, setSelectedRun] = useState<string | null>(null)

  const profile = useQuery({ queryKey: ['stock-profile'], queryFn: api.stockProfile })
  const districts = useQuery({ queryKey: ['stock-districts'], queryFn: api.stockDistricts })
  const runs = useQuery({ queryKey: ['stock-runs'], queryFn: api.stockRuns, refetchInterval: 5_000 })
  const currentRun = selectedRun ?? runs.data?.runs.find((item) => item.running)?.run ?? null
  const detail = useQuery({
    queryKey: ['stock-run', currentRun], queryFn: () => api.stockRun(currentRun!), enabled: Boolean(currentRun),
    refetchInterval: (query) => query.state.data?.running ? 2_000 : 10_000,
  })
  const log = useQuery({
    queryKey: ['stock-log', currentRun], queryFn: () => api.stockLog(currentRun!), enabled: Boolean(currentRun),
    refetchInterval: detail.data?.running ? 2_000 : false,
  })

  const references = useMemo(() => referencesText.split(/[\s,;]+/).map((item) => item.trim()).filter(Boolean), [referencesText])
  const payload = () => ({
    scope, district: scope === 'district' ? district : undefined,
    references: scope === 'references' ? references : undefined,
    keep: 'full' as const, workers: 6,
  })

  const preflightMutation = useMutation({
    mutationFn: () => api.stockPreflight(payload()),
    onSuccess: (value) => setPreflight(value),
    onError: (error) => notify(error instanceof Error ? error.message : 'Preflight failed.', 'error'),
  })
  const startMutation = useMutation({
    mutationFn: (resume: boolean) => api.startStockRun({ ...payload(), name: safeRunName(name), resume }),
    onSuccess: async (value) => {
      setSelectedRun(value.started.run)
      setPreflight(value.estimate)
      await queryClient.invalidateQueries({ queryKey: ['stock-runs'] })
      notify(`Run ${value.started.run} started.`, 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : 'Run could not start.', 'error'),
  })
  const stopMutation = useMutation({
    mutationFn: () => api.stopStockRun(currentRun!),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['stock-runs'] }),
        queryClient.invalidateQueries({ queryKey: ['stock-run', currentRun] }),
      ])
      notify('Stop requested. The durable ledger can be resumed.', 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : 'Stop failed.', 'error'),
  })

  const inputsReady = profile.data?.missing_inputs.length === 0
  const canPreflight = inputsReady && (scope !== 'references' || references.length > 0) && (scope !== 'district' || Boolean(district))
  const canStart = Boolean(preflight?.ok && safeRunName(name)) && (scope !== 'all' || confirmedAll)
  const counts = detail.data?.progress.counts
  const completed = countDone(counts)
  const total = preflight?.buildings_in_scope ?? detail.data?.summary?.coverage.buildings_in_scope ?? completed
  const pct = total ? Math.min(100, completed / total * 100) : 0

  return <div className="product-page run-page">
    <header className="product-page-header">
      <div><span>02 / EXECUTION</span><h1>Run</h1><p>Choose a scope, review the engine’s own preflight, then start or resume the per-building pipeline.</p></div>
      <div className="profile-badge-inline"><span>PROFILE</span><code>{profile.data?.profile.fingerprint.slice(0, 14) ?? 'loading…'}</code></div>
    </header>
    <div className="product-scroll run-layout">
      <section className="run-setup-panel">
        <header><span>NEW RUN</span><h2>1. Choose scope</h2></header>
        <div className="scope-grid" role="radiogroup" aria-label="Run scope">
          {([
            ['references', Building2, 'Selected buildings', 'Test or reproduce exact cadastral references.'],
            ['district', MapPinned, 'One district', 'Run every eligible building in a named district.'],
            ['all', AlertTriangle, 'All Valencia', 'Long-running full-stock production run.'],
          ] as const).map(([value, Icon, title, copy]) => <button type="button" role="radio" aria-checked={scope === value} className={scope === value ? 'active' : ''} key={value} onClick={() => { setScope(value); setPreflight(null) }}>
            <Icon size={20} /><span><strong>{title}</strong><small>{copy}</small></span>
          </button>)}
        </div>

        <div className="run-form-grid">
          {scope === 'references' && <label className="run-field wide"><span>CADASTRAL REFERENCES</span><textarea rows={4} value={referencesText} onChange={(event) => { setReferencesText(event.target.value); setPreflight(null) }} placeholder="One or more refparcela values" /></label>}
          {scope === 'district' && <label className="run-field wide"><span>DISTRICT</span><select value={district} onChange={(event) => { setDistrict(event.target.value); setPreflight(null) }}>{districts.data?.districts.map((item) => <option key={item}>{item}</option>)}</select></label>}
          {scope === 'all' && <div className="full-run-warning"><AlertTriangle size={19} /><div><strong>Full-city scope</strong><p>This can take many hours and requires protected disk capacity. Stop is resumable from the durable ledger.</p></div></div>}
          <label className="run-field wide"><span>RUN NAME</span><input value={name} onChange={(event) => { setName(event.target.value); setPreflight(null) }} /><small>Filesystem-safe name: {safeRunName(name) || 'required'}</small></label>
        </div>

        <button className="secondary-button preflight-button" type="button" disabled={!canPreflight || preflightMutation.isPending} onClick={() => preflightMutation.mutate()}>
          {preflightMutation.isPending ? <span className="spinner" /> : <CheckCircle2 size={15} />} Run preflight
        </button>

        {preflight && <section className={`preflight-result ${preflight.ok ? 'ready' : 'blocked'}`}>
          <header><strong>{preflight.ok ? 'PREFLIGHT PASSED' : 'PREFLIGHT BLOCKED'}</strong><code>{preflight.stock_source_fingerprint?.slice(0, 12)}</code></header>
          {preflight.ok ? <>
            <div className="preflight-metrics">
              <div><span>In scope</span><strong>{preflight.buildings_in_scope?.toLocaleString()}</strong></div>
              <div><span>Runnable</span><strong>{preflight.runnable?.toLocaleString()}</strong></div>
              <div><span>Excluded</span><strong>{preflight.excluded?.toLocaleString()}</strong></div>
              <div><span>Estimated time</span><strong>{formatDuration(preflight.estimated_minutes)}</strong></div>
              <div><span>Estimated storage</span><strong>{formatProductBytes(preflight.estimated_bytes)}</strong></div>
            </div>
            {Object.keys(preflight.exclusion_reasons ?? {}).length > 0 && <div className="exclusion-list">{Object.entries(preflight.exclusion_reasons ?? {}).map(([reason, value]) => <span key={reason}><code>{value}</code>{reason}</span>)}</div>}
          </> : <p>Missing inputs: {preflight.missing_inputs?.join(', ')}</p>}
        </section>}

        {scope === 'all' && preflight?.ok && <label className="run-confirm"><input type="checkbox" checked={confirmedAll} onChange={(event) => setConfirmedAll(event.target.checked)} /><span>I reviewed the scope, exclusion count, duration and protected-storage estimate.</span></label>}
        <div className="run-actions">
          <button className="primary-button" disabled={!canStart || startMutation.isPending} onClick={() => startMutation.mutate(false)}><Play size={15} /> Start run</button>
          <button className="secondary-button" disabled={!canStart || startMutation.isPending} onClick={() => startMutation.mutate(true)}><RotateCcw size={15} /> Resume same run</button>
        </div>
      </section>

      <section className="run-monitor-panel">
        <header><div><span>LIVE LEDGER</span><h2>{currentRun ?? 'No active run'}</h2></div>{detail.data?.running && <span className="live-chip"><i />RUNNING</span>}</header>
        {currentRun ? <>
          <div className="run-progress-card">
            <div><strong>{pct.toFixed(1)}%</strong><span>{completed.toLocaleString()} / {total.toLocaleString()} terminal records</span></div>
            <progress max={Math.max(1, total)} value={completed} />
            <dl>
              <div><dt>OK</dt><dd>{counts?.ok ?? 0}</dd></div><div><dt>FAILED</dt><dd>{counts?.failed ?? 0}</dd></div><div><dt>EXCLUDED</dt><dd>{counts?.excluded ?? 0}</dd></div><div><dt>CPU</dt><dd>{formatDuration((detail.data?.progress.cpu_seconds ?? 0) / 60)}</dd></div>
            </dl>
          </div>
          <div className="run-monitor-actions">
            <button className="secondary-button" disabled={!detail.data?.running || stopMutation.isPending} onClick={() => stopMutation.mutate()}><Square size={14} /> Stop safely</button>
            <Clock3 size={14} /><span>Counts are read from the fsynced ledger, not process memory.</span>
          </div>
          <div className="run-log"><header><TerminalSquare size={14} /><span>PROCESS LOG · LAST 160 KB</span></header><pre>{log.data || 'Waiting for process output…'}</pre></div>
        </> : <div className="run-monitor-empty"><Play size={28} /><strong>Nothing is running</strong><p>Complete preflight to start a new durable stock run.</p></div>}
      </section>
    </div>
  </div>
}
