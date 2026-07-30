import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import { CheckCircle2, ChevronDown, CircleAlert, FileCheck2, ListChecks, Settings2, SlidersHorizontal, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  limitPrimaryMetrics, recentRunItems, type FocusedPanel, type FocusedStatusLevel,
} from '../lib/focusedWorkspace'

type DrawerTab = {
  id: FocusedPanel
  label: string
  content: ReactNode
  hidden?: boolean
  alert?: boolean
}

type RunItem = {
  id: string
  label: string
  meta?: string
}

const panelIcons = {
  setup: Settings2,
  selection: SlidersHorizontal,
  evidence: FileCheck2,
  metrics: ListChecks,
}

export function FocusedWorkspace({
  className = '', panel, onPanelChange, tabs, toolbarLead, status, runs, selectedRunId, onSelectRun, children,
}: {
  className?: string
  panel: FocusedPanel | null
  onPanelChange: (panel: FocusedPanel | null) => void
  tabs: DrawerTab[]
  toolbarLead?: ReactNode
  status?: { level: FocusedStatusLevel; label: string; detail?: string }
  runs?: RunItem[]
  selectedRunId?: string
  onSelectRun?: (runId: string) => void
  children: ReactNode
}) {
  const { t } = useTranslation()
  const triggerRefs = useRef<Partial<Record<FocusedPanel, HTMLButtonElement | null>>>({})
  const closeRef = useRef<HTMLButtonElement | null>(null)
  const previousPanel = useRef<FocusedPanel | null>(null)
  const autoOpenedAlert = useRef('')
  const visibleTabs = tabs.filter((tab) => !tab.hidden)
  const activeTab = visibleTabs.find((tab) => tab.id === panel)
  const hasEvidence = visibleTabs.some((tab) => tab.id === 'evidence')

  useEffect(() => {
    if (!status || !['warning', 'blocked'].includes(status.level) || !hasEvidence) {
      autoOpenedAlert.current = ''
      return
    }
    const alertKey = `${status.level}:${status.label}:${status.detail ?? ''}`
    if (autoOpenedAlert.current === alertKey) return
    autoOpenedAlert.current = alertKey
    onPanelChange('evidence')
  }, [hasEvidence, onPanelChange, status])

  useEffect(() => {
    if (!panel) return
    previousPanel.current = panel
    const frame = window.requestAnimationFrame(() => closeRef.current?.focus())
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      const closingPanel = previousPanel.current
      onPanelChange(null)
      window.requestAnimationFrame(() => closingPanel && triggerRefs.current[closingPanel]?.focus())
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [onPanelChange, panel])

  const close = () => {
    const closingPanel = panel
    onPanelChange(null)
    window.requestAnimationFrame(() => closingPanel && triggerRefs.current[closingPanel]?.focus())
  }

  return <div className={`focused-workspace ${className}`.trim()} data-open-panel={panel ?? ''}>
    <div className="focused-toolbar">
      <div className="focused-toolbar-lead">{toolbarLead}</div>
      <div className="focused-toolbar-actions">
        {runs && onSelectRun ? <RecentRunPicker items={runs} selectedId={selectedRunId ?? ''} onSelect={onSelectRun} /> : null}
        {status ? <StatusSummary {...status} onClick={hasEvidence ? () => onPanelChange('evidence') : undefined} /> : null}
        <div className="focused-panel-triggers" aria-label={t('focused.panels')}>
          {visibleTabs.map((tab) => {
            const Icon = panelIcons[tab.id]
            return <button
              key={tab.id}
              ref={(node) => { triggerRefs.current[tab.id] = node }}
              className={`${panel === tab.id ? 'active' : ''} ${tab.alert ? 'has-alert' : ''}`.trim()}
              aria-expanded={panel === tab.id}
              aria-controls="focused-context-drawer"
              onClick={() => onPanelChange(panel === tab.id ? null : tab.id)}
            ><Icon size={16} />{tab.label}{tab.alert ? <CircleAlert size={13} aria-hidden="true" /> : null}</button>
          })}
        </div>
      </div>
    </div>
    <div className="focused-workspace-main">{children}</div>
    {activeTab ? <aside id="focused-context-drawer" className="focused-context-drawer" aria-label={activeTab.label}>
      <header><span>{activeTab.label}</span><button ref={closeRef} className="icon-button" onClick={close} aria-label={t('common.close')}><X size={18} /></button></header>
      <div className="focused-drawer-scroll">{activeTab.content}</div>
    </aside> : null}
  </div>
}

export function StatusSummary({ level, label, detail, onClick }: {
  level: FocusedStatusLevel
  label: string
  detail?: string
  onClick?: () => void
}) {
  const Icon = level === 'verified' ? CheckCircle2 : level === 'pending' ? ChevronDown : CircleAlert
  const content = <><Icon size={15} /><span><strong>{label}</strong>{detail ? <small>{detail}</small> : null}</span></>
  const statusClass = level === 'verified' ? 'validated' : level === 'blocked' ? 'invalid' : level
  if (onClick) return <button className={`focused-status scientific-banner ${statusClass}`} onClick={onClick}>{content}</button>
  return <div className={`focused-status scientific-banner ${statusClass}`}>{content}</div>
}

export function PrimaryMetricStrip({ metrics, className = '' }: {
  metrics: Array<{ label: string; value: string; unit: string; hint?: string }>
  className?: string
}) {
  const { t } = useTranslation()
  return <section className={`primary-metric-strip ${className}`.trim()} aria-label={t('focused.primaryMetrics')} data-testid="primary-metric-strip">
    {limitPrimaryMetrics(metrics).map((metric) => <div key={metric.label} title={metric.hint}><span>{metric.label}</span><strong>{metric.value}</strong><small>{metric.unit}</small></div>)}
  </section>
}

export function RecentRunPicker({ items, selectedId, onSelect }: {
  items: RunItem[]
  selectedId: string
  onSelect: (runId: string) => void
}) {
  const { t } = useTranslation()
  const visible = useMemo(() => recentRunItems(items, selectedId), [items, selectedId])
  if (!visible.length) return null
  return <div className="recent-run-picker">
    <label><span>{t('focused.recentRun')}</span><select value={selectedId} onChange={(event) => onSelect(event.target.value)} aria-label={t('focused.recentRun')}>
      {visible.map((item) => <option key={item.id} value={item.id}>{item.label}{item.meta ? ` · ${item.meta}` : ''}</option>)}
    </select></label>
    <Link to="/runs">{t('focused.allRuns')}</Link>
  </div>
}
