import { useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { CircleAlert, ExternalLink, LockKeyhole, Network, ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import type { WorkflowInputContract } from '../lib/types'
import StockInputPolicyPanel from './StockInputPolicyPanel'

const workflows: WorkflowInputContract['workflow'][] = ['builder', 'simulation', 'neighborhood', 'city', 'lhs']

export default function InputContractsPanel() {
  const { t, i18n } = useTranslation()
  const [workflow, setWorkflow] = useState<WorkflowInputContract['workflow']>('builder')
  const contract = useQuery({ queryKey: ['workflow-input-policy', workflow], queryFn: () => api.workflowInputPolicy(workflow), staleTime: 60_000 })
  const copy = i18n.language.startsWith('tr') ? 'tr' : 'en'
  const selectWorkflow = (next: WorkflowInputContract['workflow']) => setWorkflow(next)
  const handleWorkflowKey = (event: KeyboardEvent<HTMLButtonElement>, current: WorkflowInputContract['workflow']) => {
    const index = workflows.indexOf(current)
    let next: number
    if (event.key === 'ArrowRight') next = (index + 1) % workflows.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + workflows.length) % workflows.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = workflows.length - 1
    else return
    event.preventDefault()
    selectWorkflow(workflows[next])
    document.getElementById(`input-workflow-tab-${workflows[next]}`)?.focus()
  }
  const grouped = (contract.data?.passive_fields ?? []).reduce<Record<string, WorkflowInputContract['passive_fields']>>((result, item) => {
    result[item.effect] = [...(result[item.effect] ?? []), item]
    return result
  }, {})
  return <section className="health-band input-contract-band" aria-labelledby="input-contract-title">
    <header><Network size={18} /><div><span>{t('health.inputContract.eyebrow')}</span><h2 id="input-contract-title">{t('health.inputContract.title')}</h2></div></header>
    <p className="band-note">{t('health.inputContract.intro')}</p>
    <div className="input-workflow-tabs" role="tablist" aria-label={t('health.inputContract.workflowLabel')}>{workflows.map((item) => <button key={item} id={`input-workflow-tab-${item}`} role="tab" aria-selected={workflow === item} aria-controls={`input-workflow-panel-${item}`} tabIndex={workflow === item ? 0 : -1} className={workflow === item ? 'active' : ''} onClick={() => selectWorkflow(item)} onKeyDown={(event) => handleWorkflowKey(event, item)}>{t(`health.dictionary.workflows.${item}`)}</button>)}</div>
    {contract.isLoading ? <div className="dictionary-loading"><span className="spinner" />{t('health.inputContract.loading')}</div> : null}
    {contract.error instanceof Error ? <div className="page-error" role="alert"><CircleAlert size={16} />{contract.error.message}</div> : null}
    {contract.data ? <div id={`input-workflow-panel-${workflow}`} role="tabpanel" aria-labelledby={`input-workflow-tab-${workflow}`} className="input-contract-content">
      <div className={`input-contract-status ${contract.data.locked ? 'is-locked' : 'is-configurable'}`}><span>{contract.data.locked ? <LockKeyhole size={17} /> : <ShieldCheck size={17} />}</span><div><strong>{contract.data.status}</strong><p>{contract.data.summary}</p></div></div>
      <div className="input-contract-grid">{contract.data.inputs.map((item) => <article key={item.key}>
        <span className={`effect-badge effect-${item.effect}`}><i aria-hidden="true" />{t(`health.dictionary.effects.${item.effect}`)}</span>
        <h3>{item.label}</h3><code>{item.value}</code><p>{item.detail}</p><small>{t('health.inputContract.source')}: {item.source}</small>
      </article>)}</div>
      {workflow === 'neighborhood' || workflow === 'city' ? <StockInputPolicyPanel workflow={workflow} mode="project" /> : null}
      <div className="editor-contract-link"><div><ExternalLink size={16} /><span><strong>{t('health.inputContract.physicsOverrides')}</strong><small>{t('health.inputContract.physicsOverridesNote')}</small></span></div><Link className="secondary-button" to={contract.data.editor_link}>{t('health.inputContract.openRuns')}</Link></div>
      <div className="passive-contract">
        <div className="passive-heading"><ShieldCheck size={17} /><div><span>{t('health.inputContract.verification')}</span><h3>{t('health.inputContract.passiveTitle')}</h3><p>{t('health.inputContract.passiveIntro')}</p></div></div>
        <div className="passive-groups">{Object.entries(grouped).map(([effect, items]) => <section key={effect}><h4>{t(`health.dictionary.effects.${effect}`)} <span>{items.length}</span></h4><ul>{items.map((item) => <li key={item.field}><code>{item.field}</code><span>{item.meaning[copy]}</span><small>{t(`health.dictionary.status.${item.definition_status}`, { defaultValue: item.definition_status.replaceAll('_', ' ') })}</small></li>)}</ul></section>)}</div>
      </div>
    </div> : null}
  </section>
}
