import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { BookOpenText, Check, ChevronDown, ChevronRight, CircleAlert, Search, Tag } from 'lucide-react'
import { api } from '../lib/api'
import type { DatasetRecord, DictionaryColumn, DictionaryEffect } from '../lib/types'
import { useFeedback } from './FeedbackProvider'

const effects: DictionaryEffect[] = [
  'physical_model', 'stock_method', 'scaling', 'validation_only', 'reporting_only', 'not_used',
]

function percent(value: number, language: string) {
  return new Intl.NumberFormat(language, { maximumFractionDigits: 3 }).format(value)
}

function DictionaryDetail({ datasetId, column }: { datasetId: string; column: DictionaryColumn }) {
  const { t, i18n } = useTranslation()
  const { notify } = useFeedback()
  const queryClient = useQueryClient()
  const [note, setNote] = useState(column.user_note)
  const [label, setLabel] = useState(column.user_semantic_label)
  useEffect(() => { setNote(column.user_note); setLabel(column.user_semantic_label) }, [column.field, column.user_note, column.user_semantic_label])
  const save = useMutation({
    mutationFn: () => api.updateDatasetFieldNote(datasetId, column.field, note, label),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['dataset-dictionary', datasetId] })
      notify(t('health.dictionary.noteSaved'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const copy = i18n.language.startsWith('tr') ? 'tr' : 'en'
  const evidence = column.evidence
  return <div className="dictionary-detail" id={`dictionary-detail-${column.field}`}>
    <div className="dictionary-evidence-grid">
      <dl>
        <div><dt>{t('health.dictionary.validityRule')}</dt><dd>{column.validity_rule[copy]}</dd></div>
        <div><dt>{t('health.dictionary.coverageBasis')}</dt><dd><code>{column.coverage_basis}</code></dd></div>
        <div><dt>{t('health.dictionary.missingInvalid')}</dt><dd>{column.missing_count} / {column.invalid_count}</dd></div>
        <div><dt>{t('health.dictionary.zeroValues')}</dt><dd>{column.zero_count}</dd></div>
      </dl>
      <dl>
        <div><dt>{t('health.dictionary.provenance')}</dt><dd>{column.source}</dd></div>
        {column.scope ? <div><dt>{t('health.dictionary.scope')}</dt><dd>{column.scope[copy]}</dd></div> : null}
        {evidence.dataset_median !== undefined ? <div><dt>{t('health.dictionary.datasetEvidence')}</dt><dd>{t('health.dictionary.medianEvidence', { median: evidence.dataset_median, reference: evidence.author_reference, unit: evidence.unit })}</dd></div> : null}
      </dl>
    </div>
    <div className="dictionary-note-editor">
      <div><Tag size={15} /><span><strong>{t('health.dictionary.projectAnnotation')}</strong><small>{t('health.dictionary.annotationNote')}</small></span></div>
      <label><span>{t('health.dictionary.semanticLabel')}</span><input value={label} maxLength={80} onChange={(event) => setLabel(event.target.value)} placeholder={t('health.dictionary.semanticLabelPlaceholder')} /></label>
      <label><span>{t('health.dictionary.projectNote')}</span><textarea value={note} maxLength={1000} rows={3} onChange={(event) => setNote(event.target.value)} placeholder={t('health.dictionary.projectNotePlaceholder')} /></label>
      <button className="secondary-button" disabled={save.isPending || (note === column.user_note && label === column.user_semantic_label)} onClick={() => save.mutate()}><Check size={14} />{t('health.dictionary.saveNote')}</button>
    </div>
  </div>
}

export default function DataDictionaryPanel({ datasets, activeDatasetId }: { datasets: DatasetRecord[]; activeDatasetId: string | null }) {
  const { t, i18n } = useTranslation()
  const available = useMemo(() => datasets.filter((item) => item.kind === 'gis' || item.kind === 'companion'), [datasets])
  const [datasetId, setDatasetId] = useState('')
  const [query, setQuery] = useState('')
  const [effect, setEffect] = useState<DictionaryEffect | 'all'>('all')
  const [expanded, setExpanded] = useState('')
  useEffect(() => {
    if (datasetId && available.some((item) => item.id === datasetId)) return
    setDatasetId(activeDatasetId && available.some((item) => item.id === activeDatasetId) ? activeDatasetId : available[0]?.id ?? '')
  }, [activeDatasetId, available, datasetId])
  const dictionary = useQuery({
    queryKey: ['dataset-dictionary', datasetId],
    queryFn: () => api.datasetDictionary(datasetId),
    enabled: Boolean(datasetId),
    staleTime: Number.POSITIVE_INFINITY,
  })
  const copy = i18n.language.startsWith('tr') ? 'tr' : 'en'
  const rows = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase(i18n.language)
    return (dictionary.data?.columns ?? []).filter((item) => {
      if (effect !== 'all' && item.effect !== effect) return false
      if (!needle) return true
      return `${item.field} ${item.meaning[copy]} ${item.user_note} ${item.user_semantic_label}`.toLocaleLowerCase(i18n.language).includes(needle)
    })
  }, [copy, dictionary.data?.columns, effect, i18n.language, query])

  return <section className="health-band dictionary-band" aria-labelledby="dictionary-title">
    <header><BookOpenText size={18} /><div><span>{t('health.dictionary.eyebrow')}</span><h2 id="dictionary-title">{t('health.dictionary.title')}</h2></div>{dictionary.data ? <code className="dictionary-version">{dictionary.data.registry_version}</code> : null}</header>
    <p className="band-note">{t('health.dictionary.intro')}</p>
    <div className="dictionary-toolbar">
      <label><span>{t('health.dictionary.dataset')}</span><select value={datasetId} onChange={(event) => { setDatasetId(event.target.value); setExpanded('') }}>{available.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.kind}</option>)}</select></label>
      <label className="dictionary-search"><span>{t('health.dictionary.search')}</span><div><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('health.dictionary.searchPlaceholder')} /></div></label>
      <label><span>{t('health.dictionary.effect')}</span><select value={effect} onChange={(event) => setEffect(event.target.value as DictionaryEffect | 'all')}><option value="all">{t('health.dictionary.allEffects')}</option>{effects.map((item) => <option key={item} value={item}>{t(`health.dictionary.effects.${item}`)}</option>)}</select></label>
    </div>
    {dictionary.isLoading ? <div className="dictionary-loading"><span className="spinner" />{t('health.dictionary.loading')}</div> : null}
    {dictionary.error instanceof Error ? <div className="page-error" role="alert"><CircleAlert size={16} />{dictionary.error.message}</div> : null}
    {dictionary.data ? <>
      <div className="dictionary-summary" aria-live="polite"><strong>{dictionary.data.dataset.rows.toLocaleString(i18n.language)}</strong><span>{t('common.rows')}</span><strong>{rows.length}</strong><span>{t('health.dictionary.fieldsShown')}</span><code title={dictionary.data.dataset.snapshot_hash}>{dictionary.data.dataset.snapshot_hash.slice(0, 16)}</code></div>
      {dictionary.data.companion_links.map((item) => <div className="companion-coverage" key={item.dataset_id}>
        <DatabaseLinkIcon /><span><strong>{t('health.dictionary.companion')}</strong><small>{item.name} · {item.join.coverage_basis ?? item.join.reason}</small></span>
        {item.join.residential_area ? <dl><div><dt>{t('health.dictionary.resAreaJoin')}</dt><dd>{percent(item.join.residential_area.joined_pct, i18n.language)}%</dd></div><div><dt>{t('health.dictionary.groundJoin')}</dt><dd>{percent(item.join.ground_rule?.joined_pct ?? 0, i18n.language)}%</dd></div></dl> : null}
        <button className="text-button" onClick={() => { setDatasetId(item.dataset_id); setExpanded('') }}>{t('health.dictionary.openCompanion')}</button>
      </div>)}
      <div className="dictionary-table-wrap">
        <table className="dictionary-table">
          <caption className="sr-only">{t('health.dictionary.tableCaption', { dataset: dictionary.data.dataset.name })}</caption>
          <thead><tr><th scope="col">{t('health.dictionary.column')}</th><th scope="col">{t('health.dictionary.meaning')}</th><th scope="col">{t('health.dictionary.source')}</th><th scope="col">{t('health.dictionary.type')}</th><th scope="col">{t('health.dictionary.coverage')}</th><th scope="col">{t('health.dictionary.effect')}</th><th scope="col">{t('health.dictionary.usedIn')}</th><th scope="col">{t('health.dictionary.note')}</th></tr></thead>
          <tbody>{rows.map((column) => {
            const open = expanded === column.field
            return <FragmentRow key={column.field} column={column} open={open} copy={copy} datasetId={datasetId} onToggle={() => setExpanded(open ? '' : column.field)} />
          })}</tbody>
        </table>
        {!rows.length ? <div className="dictionary-empty">{t('health.dictionary.empty')}</div> : null}
      </div>
    </> : null}
  </section>
}

function FragmentRow({ column, open, copy, datasetId, onToggle }: { column: DictionaryColumn; open: boolean; copy: 'en' | 'tr'; datasetId: string; onToggle: () => void }) {
  const { t, i18n } = useTranslation()
  return <>
    <tr className={open ? 'is-expanded' : ''}>
      <th scope="row"><button className="dictionary-expand" aria-expanded={open} aria-controls={`dictionary-detail-${column.field}`} onClick={onToggle}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<code>{column.field}</code></button></th>
      <td><span className="dictionary-meaning">{column.meaning[copy]}</span><span className={`definition-status status-${column.definition_status}`}>{t(`health.dictionary.status.${column.definition_status}`, { defaultValue: column.definition_status.replaceAll('_', ' ') })}</span></td>
      <td><span className="dictionary-source" title={column.source}>{column.source}</span></td>
      <td><code>{column.inferred_type}</code></td>
      <td><div className="coverage-pair"><span><b>{percent(column.present_pct, i18n.language)}%</b><small>{t('health.dictionary.present')}</small></span><span className={column.usable_pct < column.present_pct ? 'coverage-warning' : ''}><b>{percent(column.usable_pct, i18n.language)}%</b><small>{t('health.dictionary.usable')}</small></span></div></td>
      <td><span className={`effect-badge effect-${column.effect}`}><i aria-hidden="true" />{t(`health.dictionary.effects.${column.effect}`)}</span></td>
      <td><div className="workflow-chips">{column.workflows.length ? column.workflows.map((item) => <span key={item}>{t(`health.dictionary.workflows.${item}`)}</span>) : <span>{t('common.none')}</span>}</div></td>
      <td><button className="note-indicator" onClick={onToggle} aria-label={column.user_note || column.user_semantic_label ? t('health.dictionary.editNoteFor', { field: column.field }) : t('health.dictionary.addNoteFor', { field: column.field })}>{column.user_note || column.user_semantic_label ? <Check size={13} /> : <BookOpenText size={13} />}{column.user_semantic_label || t(column.user_note ? 'health.dictionary.noted' : 'health.dictionary.addNote')}</button></td>
    </tr>
    {open ? <tr className="dictionary-detail-row"><td colSpan={8}><DictionaryDetail datasetId={datasetId} column={column} /></td></tr> : null}
  </>
}

function DatabaseLinkIcon() {
  return <span className="companion-icon" aria-hidden="true"><BookOpenText size={16} /></span>
}
