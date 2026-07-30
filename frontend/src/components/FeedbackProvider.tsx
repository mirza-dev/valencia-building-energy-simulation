import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { CheckCircle2, CircleAlert, Info, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

type NoticeKind = 'success' | 'error' | 'info'
type Notice = { id: string; kind: NoticeKind; message: string }
type FeedbackContextValue = { notify: (message: string, kind?: NoticeKind) => void }

const FeedbackContext = createContext<FeedbackContextValue | null>(null)

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const [notices, setNotices] = useState<Notice[]>([])
  const dismiss = useCallback((id: string) => setNotices((current) => current.filter((item) => item.id !== id)), [])
  const notify = useCallback((message: string, kind: NoticeKind = 'info') => {
    const id = crypto.randomUUID()
    setNotices((current) => [...current.slice(-2), { id, kind, message }])
    window.setTimeout(() => dismiss(id), 5200)
  }, [dismiss])
  const value = useMemo(() => ({ notify }), [notify])

  return (
    <FeedbackContext.Provider value={value}>
      {children}
      <div className="toast-region" aria-live="polite" aria-atomic="false">
        {notices.map((notice) => (
          <div className={`toast ${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'} key={notice.id}>
            {notice.kind === 'success' ? <CheckCircle2 size={17} /> : notice.kind === 'error' ? <CircleAlert size={17} /> : <Info size={17} />}
            <span>{notice.message}</span>
            <button type="button" onClick={() => dismiss(notice.id)} aria-label={t('common.notificationsClose')}><X size={15} /></button>
          </div>
        ))}
      </div>
    </FeedbackContext.Provider>
  )
}

export function useFeedback() {
  const context = useContext(FeedbackContext)
  if (!context) throw new Error('useFeedback must be used inside FeedbackProvider')
  return context
}
