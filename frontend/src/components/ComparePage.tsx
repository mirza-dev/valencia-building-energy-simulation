import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Activity, ArrowLeftRight, Boxes, GitCompareArrows, Search } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import PageHeader from './PageHeader'
import ModelViewer from './ModelViewer'
import { SimulationResultPanel } from './SimulationResultPanel'
import { api } from '../lib/api'
import { useActiveBuilding } from '../lib/activeBuilding'
import { formatModelDisplayText } from '../lib/modelGraph'
import { formatDate } from '../lib/locale'

type CompareMode = 'model' | 'simulation'
const compareModes: CompareMode[] = ['model', 'simulation']

export function formatDiffValue(value: unknown): string {
  if (value == null) return '—'
  if (typeof value === 'string') return value || '—'
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try { return JSON.stringify(value) }
  catch { return String(value) }
}

export function summarizeDiffValue(field: string, value: unknown): string {
  if (field === 'editor.patch_journal' && Array.isArray(value)) {
    if (!value.length) return 'No authored edits'
    const operations = new Map<string, number>()
    for (const item of value) {
      const record = typeof item === 'object' && item ? item as {
        op?: unknown
        patch?: { op?: unknown }
        report?: { op?: unknown }
      } : null
      const op = String(record?.op ?? record?.patch?.op ?? record?.report?.op ?? 'unknown')
      operations.set(op, (operations.get(op) ?? 0) + 1)
    }
    const breakdown = [...operations.entries()].map(([op, count]) => `${op} × ${count}`).join(' · ')
    return `${value.length} authored edit${value.length === 1 ? '' : 's'} · ${breakdown}`
  }
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? '' : 's'}`
  if (typeof value === 'object' && value) {
    const keys = Object.keys(value as Record<string, unknown>)
    return `${keys.length} field${keys.length === 1 ? '' : 's'} · ${keys.slice(0, 5).join(' · ')}${keys.length > 5 ? ` · +${keys.length - 5}` : ''}`
  }
  return formatDiffValue(value)
}

export default function ComparePage() {
  const { t, i18n } = useTranslation()
  const { activeBuilding } = useActiveBuilding()
  const [searchParams, setSearchParams] = useSearchParams()
  const runs = useQuery({ queryKey: ['runs'], queryFn: api.runs })
  const requestedMode = searchParams.get('mode') as CompareMode | null
  const [mode, setMode] = useState<CompareMode>(requestedMode && compareModes.includes(requestedMode) ? requestedMode : 'model')
  const [left, setLeft] = useState('')
  const [right, setRight] = useState('')
  const [filter, setFilter] = useState('')
  const initializedMode = useRef<CompareMode | null>(null)
  const choices = useMemo(
    () => (runs.data ?? []).filter(
      (run) => run.refparcela === activeBuilding && run.run_type === mode,
    ),
    [activeBuilding, mode, runs.data],
  )
  useEffect(() => {
    if (!choices.length || initializedMode.current === mode) return
    const queryMatchesMode = searchParams.get('mode') === mode
    const requestedLeft = queryMatchesMode ? searchParams.get('left') ?? '' : ''
    const requestedRight = queryMatchesMode ? searchParams.get('right') ?? '' : ''
    const nextLeft = choices.some((run) => run.id === requestedLeft) ? requestedLeft : choices[0]?.id ?? ''
    const nextRight = choices.some((run) => run.id === requestedRight && run.id !== nextLeft)
      ? requestedRight : choices.find((run) => run.id !== nextLeft)?.id ?? ''
    setLeft(nextLeft)
    setRight(nextRight)
    initializedMode.current = mode
  }, [choices, mode, searchParams])
  const pairReady = Boolean(left && right && left !== right)
  const modelCompare = useQuery({ queryKey: ['compare-model', left, right], queryFn: () => api.compare(left, right), enabled: mode === 'model' && pairReady })
  const simulationCompare = useQuery({ queryKey: ['compare-simulation', left, right], queryFn: () => api.compareSimulations(left, right), enabled: mode === 'simulation' && pairReady })
  const leftScene = useQuery({ queryKey: ['run-scene', left], queryFn: () => api.scene(left), enabled: mode === 'model' && Boolean(left) })
  const rightScene = useQuery({ queryKey: ['run-scene', right], queryFn: () => api.scene(right), enabled: mode === 'model' && Boolean(right) })
  const differences = useMemo(() => (modelCompare.data?.differences ?? []).filter((item) => item.field.toLowerCase().includes(filter.toLowerCase())), [modelCompare.data, filter])
  const writeSelection = (nextMode: CompareMode, nextLeft: string, nextRight: string) => {
    const next = new URLSearchParams({ mode: nextMode })
    if (nextLeft) next.set('left', nextLeft)
    if (nextRight) next.set('right', nextRight)
    setSearchParams(next, { replace: true })
  }
  const selectLeft = (nextLeft: string) => { setLeft(nextLeft); writeSelection(mode, nextLeft, right) }
  const selectRight = (nextRight: string) => { setRight(nextRight); writeSelection(mode, left, nextRight) }
  const swap = () => { setLeft(right); setRight(left); writeSelection(mode, right, left) }
  const switchMode = (next: CompareMode) => {
    initializedMode.current = null
    setMode(next)
    setLeft('')
    setRight('')
    setFilter('')
    writeSelection(next, '', '')
  }
  const error = modelCompare.error ?? simulationCompare.error
  const syncText = mode === 'model' ? t('compare.synced')
    : simulationCompare.data?.same_basis ? t('compare.sameBasis') : t('compare.absoluteOnly')

  return <div className="page compare-page">
    <PageHeader eyebrow={t('compare.eyebrow')} title={t('compare.title')} subtitle={t('compare.subtitle')} actions={<div className="compare-actions"><div className="segmented-control compare-mode-control" aria-label={t('compare.mode')}><button className={mode === 'model' ? 'active' : ''} onClick={() => switchMode('model')}><Boxes size={14} />{t('compare.modelMode')}</button><button className={mode === 'simulation' ? 'active' : ''} onClick={() => switchMode('simulation')}><Activity size={14} />{t('compare.simulationMode')}</button></div><div className="compare-selectors">
      <select data-testid="compare-left-run" value={left} onChange={(event) => selectLeft(event.target.value)} aria-label={t('compare.runA')}><option value="">{t('compare.selectFirst')}</option>{choices.map((run) => <option key={run.id} value={run.id} disabled={run.id === right}>A · {run.scenario_name} · {formatDate(run.created_at, i18n.language)} · {run.id.slice(0, 8)}</option>)}</select>
      <button className="icon-button" onClick={swap} disabled={!left || !right} title={t('compare.swap')} aria-label={t('compare.swap')}><ArrowLeftRight size={16} /></button>
      <select data-testid="compare-right-run" value={right} onChange={(event) => selectRight(event.target.value)} aria-label={t('compare.runB')}><option value="">{t('compare.selectSecond')}</option>{choices.map((run) => <option key={run.id} value={run.id} disabled={run.id === left}>B · {run.scenario_name} · {formatDate(run.created_at, i18n.language)} · {run.id.slice(0, 8)}</option>)}</select>
    </div></div>} />
    <div className="compare-sync-strip"><GitCompareArrows size={14} />{syncText}<code>{choices.length} {t('compare.availableRuns')}</code></div>
    {mode === 'model' ? <div className="compare-layout">
      <section className="compare-view"><header><span>A</span><strong title={modelCompare.data?.left.scenario_name}>{modelCompare.data?.left.scenario_name ?? t('common.notSelected')}</strong></header>{leftScene.data ? <ModelViewer scene={leftScene.data} compact syncKey="comparison" /> : <div className="model-empty">{leftScene.isLoading ? <span className="spinner" /> : null}</div>}</section>
      <section className="compare-view"><header><span>B</span><strong title={modelCompare.data?.right.scenario_name}>{modelCompare.data?.right.scenario_name ?? t('common.notSelected')}</strong></header>{rightScene.data ? <ModelViewer scene={rightScene.data} compact syncKey="comparison" /> : <div className="model-empty">{rightScene.isLoading ? <span className="spinner" /> : null}</div>}</section>
      <section className="diff-ledger">
        {modelCompare.data ? <div className="compare-evidence"><div><span>{t('compare.integrity')}</span><strong>{modelCompare.data.left.verification_status}</strong><strong>{modelCompare.data.right.verification_status}</strong></div><div><span>QA</span><strong>{modelCompare.data.left.qa.all_pass ? 'PASS' : 'FAIL'}</strong><strong>{modelCompare.data.right.qa.all_pass ? 'PASS' : 'FAIL'}</strong></div><div><span>{t('compare.surfaces')}</span><strong>{String(modelCompare.data.left.stats.n_surfaces ?? leftScene.data?.surfaces.length ?? '—')}</strong><strong>{String(modelCompare.data.right.stats.n_surfaces ?? rightScene.data?.surfaces.length ?? '—')}</strong></div><div><span>{t('compare.canonical')}</span><code>{modelCompare.data.left.canonical_fingerprint?.slice(0, 10) ?? 'legacy'}</code><code>{modelCompare.data.right.canonical_fingerprint?.slice(0, 10) ?? 'legacy'}</code></div><div><span>{t('compare.snapshots')}</span><strong>{Object.keys(modelCompare.data.evidence.left.input_snapshots).length}</strong><strong>{Object.keys(modelCompare.data.evidence.right.input_snapshots).length}</strong></div><div><span>{t('compare.overrides')}</span><strong>{modelCompare.data.evidence.left.provenance.overrides?.length ?? 0}</strong><strong>{modelCompare.data.evidence.right.provenance.overrides?.length ?? 0}</strong></div><div><span>{t('compare.facadeRows')}</span><strong>{modelCompare.data.evidence.left.facade_qa.length}</strong><strong>{modelCompare.data.evidence.right.facade_qa.length}</strong></div>
          {Array.from(new Set([...Object.keys(modelCompare.data.evidence.left.input_snapshots), ...Object.keys(modelCompare.data.evidence.right.input_snapshots)])).map((role) => <div key={role}><span>{role}</span><code>{modelCompare.data.evidence.left.input_snapshots[role]?.snapshot_hash.slice(0, 10) ?? '—'}</code><code>{modelCompare.data.evidence.right.input_snapshots[role]?.snapshot_hash.slice(0, 10) ?? '—'}</code></div>)}</div> : <div className="empty-state"><p>{t('compare.selectTwo')}</p></div>}
        <div className="diff-filter"><div className="table-head"><span>{t('compare.delta')}</span><code>{t('compare.fields', { count: differences.length })}</code></div><label className="search-field"><Search size={14} /><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t('compare.searchDiff')} /></label></div>
        <table><thead><tr><th>{t('compare.field')}</th><th>A</th><th>B</th></tr></thead><tbody>{differences.map((item) => { const leftRaw = formatDiffValue(item.left); const rightRaw = formatDiffValue(item.right); const leftValue = formatModelDisplayText(summarizeDiffValue(item.field, item.left), i18n.language); const rightValue = formatModelDisplayText(summarizeDiffValue(item.field, item.right), i18n.language); return <tr key={item.field}><td><code>{item.field}</code></td><td><code className="structured-diff-value" title={leftRaw.length <= 500 ? leftRaw : undefined}>{leftValue}</code></td><td><code className="structured-diff-value" title={rightRaw.length <= 500 ? rightRaw : undefined}>{rightValue}</code></td></tr> })}</tbody></table>
        {modelCompare.data && !differences.length ? <div className="empty-state compact-empty"><p>{t('compare.noDiff')}</p></div> : null}
      </section>
    </div> : mode === 'simulation' ? <div className="simulation-compare-layout">
      <section className="simulation-compare-result"><header><span>A</span><strong title={simulationCompare.data?.left.scenario_name}>{simulationCompare.data?.left.scenario_name ?? t('common.notSelected')}</strong></header><SimulationResultPanel result={simulationCompare.data?.left.result} /></section>
      <section className="simulation-compare-result"><header><span>B</span><strong title={simulationCompare.data?.right.scenario_name}>{simulationCompare.data?.right.scenario_name ?? t('common.notSelected')}</strong></header><SimulationResultPanel result={simulationCompare.data?.right.result} /></section>
      <section className="diff-ledger simulation-delta-ledger"><div className="table-head"><span>{t('compare.energyDelta')}</span><code>{simulationCompare.data?.same_basis ? '%' : t('compare.absolute')}</code></div>{simulationCompare.data ? <><div className="compare-evidence"><div><span>{t('compare.integrity')}</span><strong>{simulationCompare.data.left.verification_status}</strong><strong>{simulationCompare.data.right.verification_status}</strong></div><div><span>{t('compare.scientific')}</span><strong>{simulationCompare.data.left.qa.scientific_status}</strong><strong>{simulationCompare.data.right.qa.scientific_status}</strong></div><div><span>EPW</span><code>{simulationCompare.data.left.result?.settings.weather_snapshot_hash.slice(0, 10)}</code><code>{simulationCompare.data.right.result?.settings.weather_snapshot_hash.slice(0, 10)}</code></div></div><table><thead><tr><th>{t('compare.field')}</th><th>A</th><th>B / Δ</th></tr></thead><tbody>{Object.entries(simulationCompare.data.metrics).map(([key, metric]) => <tr key={key}><td className="metric-name-cell"><span>{t(`compare.metrics.${key}`, { defaultValue: key })}</span><code>{key}</code></td><td>{metric.left?.toFixed(2) ?? '—'}</td><td>{metric.right?.toFixed(2) ?? '—'}<small>{metric.delta == null ? '' : ` Δ ${metric.delta.toFixed(2)}`}{metric.percent == null ? '' : ` · ${metric.percent.toFixed(1)}%`}</small></td></tr>)}</tbody></table></> : <div className="empty-state"><p>{t('compare.selectTwo')}</p></div>}</section>
    </div> : null}
    {error instanceof Error ? <div className="page-error" role="alert">{error.message}</div> : null}
  </div>
}
