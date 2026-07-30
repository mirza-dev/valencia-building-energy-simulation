import { CircleAlert, RotateCcw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

export default function MapErrorNotice({ message, onRetry }: { message: string; onRetry: () => void }) {
  const { t } = useTranslation()

  return <div className="map-runtime-error" role="alert">
    <CircleAlert size={19} aria-hidden="true" />
    <span>
      <strong>{t('map.runtimeError')}</strong>
      <code>{message}</code>
    </span>
    <button className="secondary-button" type="button" onClick={onRetry}>
      <RotateCcw size={14} aria-hidden="true" />
      {t('common.retry')}
    </button>
  </div>
}
