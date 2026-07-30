import { ClipboardCheck, ExternalLink, FileCode2, FileWarning, TableProperties } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { viewableSimulationArtifacts, type ViewableSimulationArtifact } from '../lib/runArtifacts'
import type { RunRecord } from '../lib/types'

const iconFor = (artifact: ViewableSimulationArtifact) => {
  if (artifact.kind === 'report') return <TableProperties size={16} />
  if (artifact.kind === 'log') return <FileWarning size={16} />
  if (artifact.kind === 'qa') return <ClipboardCheck size={16} />
  return <FileCode2 size={16} />
}

const formatBytes = (value: number) => {
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 ** 2).toFixed(2)} MB`
}

export default function RunArtifactLinks({
  run,
  compact = false,
}: {
  run: Pick<RunRecord, 'id' | 'run_type' | 'verification_status' | 'artifacts'>
  compact?: boolean
}) {
  const { t } = useTranslation()
  const artifacts = viewableSimulationArtifacts(run)
  if (!artifacts.length) return null

  return <div className={`run-artifact-viewer${compact ? ' compact' : ''}`} data-testid="energyplus-artifact-files">
    <div className="run-artifact-viewer-heading">
      <span><TableProperties size={15} /><strong>{t('simulation.artifactFiles.title')}</strong></span>
      <p>{t('simulation.artifactFiles.description')}</p>
    </div>
    <div className="run-artifact-link-list">
      {artifacts.map((artifact) => <a
        key={artifact.name}
        data-artifact-name={artifact.name}
        href={api.artifactViewUrl(run.id, artifact.name)}
        target="_blank"
        rel="noopener noreferrer"
      >
        <i>{iconFor(artifact)}</i>
        <span>
          <strong>{t(artifact.labelKey)}</strong>
          <small>{artifact.name} · {formatBytes(artifact.size_bytes)}</small>
        </span>
        <code title={artifact.sha256}>{artifact.sha256.slice(0, 10)}</code>
        <ExternalLink size={14} aria-hidden="true" />
      </a>)}
    </div>
  </div>
}
