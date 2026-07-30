import type { RunRecord } from './types'

export const VIEWABLE_SIMULATION_ARTIFACTS = [
  { name: 'eplustbl.htm', labelKey: 'simulation.artifactFiles.resultTables', kind: 'report' },
  { name: 'model.idf', labelKey: 'simulation.artifactFiles.energyPlusInput', kind: 'model' },
  { name: 'model_python.osm', labelKey: 'simulation.artifactFiles.openStudioModel', kind: 'model' },
  { name: 'parent_model.osm', labelKey: 'simulation.artifactFiles.parentModel', kind: 'model' },
  { name: 'eplusout.err', labelKey: 'simulation.artifactFiles.diagnostics', kind: 'log' },
  { name: 'qa_report.txt', labelKey: 'simulation.artifactFiles.qaReport', kind: 'qa' },
] as const

export type ViewableSimulationArtifactName = typeof VIEWABLE_SIMULATION_ARTIFACTS[number]['name']

export interface ViewableSimulationArtifact {
  name: ViewableSimulationArtifactName
  labelKey: string
  kind: 'report' | 'model' | 'log' | 'qa'
  sha256: string
  size_bytes: number
}

export function viewableSimulationArtifacts(
  run: Pick<RunRecord, 'run_type' | 'verification_status' | 'artifacts'> | null | undefined,
): ViewableSimulationArtifact[] {
  if (run?.run_type !== 'simulation' || run.verification_status !== 'VERIFIED') return []
  const manifest = new Map((run.artifacts ?? []).map((artifact) => [artifact.name, artifact]))
  return VIEWABLE_SIMULATION_ARTIFACTS.flatMap((definition) => {
    const artifact = manifest.get(definition.name)
    return artifact ? [{ ...definition, sha256: artifact.sha256, size_bytes: artifact.size_bytes }] : []
  })
}
