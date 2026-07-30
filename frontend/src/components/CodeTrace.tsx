import { useQuery } from '@tanstack/react-query'
import { Braces, Copy, FileCode2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'

export default function CodeTrace({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const { t } = useTranslation()
  const query = useQuery({ queryKey: ['code', symbol], queryFn: () => api.code(symbol) })
  return (
    <aside className="code-drawer" aria-labelledby="code-trace-title">
      <header>
        <div><span className="eyebrow"><Braces size={13} /> {t('code.readonly')}</span><h2 id="code-trace-title">{symbol}()</h2></div>
        <button className="icon-button" onClick={onClose} title={t('common.close')} aria-label={t('common.close')}><X size={18} /></button>
      </header>
      {query.data ? (
        <>
          <div className="code-meta">
            <span><FileCode2 size={13} /> {query.data.file.split('/').slice(-2).join('/')}</span>
            <span>L{query.data.start_line}–{query.data.end_line}</span>
            <code>{query.data.sha256.slice(0, 12)}</code>
            <button className="icon-button" onClick={() => void navigator.clipboard.writeText(query.data.source)} title={t('common.copy')} aria-label={t('common.copy')}><Copy size={14} /></button>
          </div>
          <pre><code>{query.data.source}</code></pre>
        </>
      ) : <div className="empty-state">{query.error instanceof Error ? query.error.message : t('code.loading')}</div>}
    </aside>
  )
}
