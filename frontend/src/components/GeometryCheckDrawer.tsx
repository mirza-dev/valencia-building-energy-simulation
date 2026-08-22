import { lazy, Suspense, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Box, AlertTriangle } from 'lucide-react'
import { api } from '../lib/api'
import type { SceneModel } from '../lib/types'

// Lazily loaded, and this is load-bearing rather than tidy: `ModelViewer` owns
// every three / @react-three import in the app, and the product surface
// otherwise contains none of it.  A static import here would put the whole 3D
// stack in the first paint of Files and Run, which never draw a model.
const ModelViewer = lazy(() => import('./ModelViewer'))

export default function GeometryCheckDrawer({ run, reference, onClose }: {
  run: string
  reference: string
  onClose: () => void
}) {
  // The drawer follows the ledger selection rather than closing, because
  // comparing two buildings is the actual check.  Only *changes* are delayed -
  // `settled` starts on the opening reference, so the first extraction is
  // immediate.
  //
  // 350 ms, not the 150 ms this first shipped with: ledger rows respond to
  // clicks and to Enter/Space, not to arrow keys, so the cadence being batched
  // is a person clicking down the table at roughly 300-500 ms, and 150 ms
  // batched none of it.  The delay is the only thing here that limits server
  // work: `AbortSignal` below stops a superseded scene from being *rendered*,
  // but a sync FastAPI route already inside OpenStudio cannot be interrupted,
  // so every request issued is a load that runs to completion - which during a
  // multi-day city run is competing with six EnergyPlus workers.
  const [settled, setSettled] = useState(reference)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(reference), 350)
    return () => clearTimeout(timer)
  }, [reference])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Annotated because destructuring `signal` out of the query context defeats
  // inference here, and the fallback type it lands on silently makes
  // `placeholderData` look like the query's data.
  const scene = useQuery<SceneModel>({
    queryKey: ['stock-building-scene', run, settled],
    queryFn: ({ signal }) => api.stockBuildingScene(run, settled, signal),
    // The geometry of a finished building cannot change, so a revisit during
    // the session is free and there is nothing to poll for.
    staleTime: Infinity,
    retry: false,
    // Hold the previous building on screen while the next one is extracted,
    // rather than blinking through a spinner.  This also keeps one viewer
    // mounted across the swap, which is the case the frame-counter bug used to
    // break: a fresh mount always renders, a re-rendered one used to stick.
    placeholderData: (previous?: SceneModel) => previous,
  })

  // Named after the scene on screen, not after the row that is selected: while
  // the next building is being extracted the previous one is still drawn, and a
  // header that ran ahead of the geometry would be labelling the wrong building.
  const shown = scene.data?.refparcela ?? settled

  return <aside className="geometry-check-drawer" aria-label={`Geometry for ${shown}`}>
    <header>
      <div><Box size={16} /><span><small>GEOMETRY CHECK</small><strong>{shown}</strong></span></div>
      <button className="icon-button" onClick={onClose} aria-label="Close geometry check">×</button>
    </header>
    <div className="geometry-check-body">
      {scene.isPending ? <div className="model-empty"><span className="spinner" /><p>Rebuilding geometry from the preserved model…</p></div>
        : scene.isError ? <div className="model-empty geometry-check-error">
          <AlertTriangle size={17} />
          {/* Verbatim: the server distinguishes "no model preserved" from
              "model unreadable", and blurring them into one sentence would
              hide which of the two happened. */}
          <p>{scene.error instanceof Error ? scene.error.message : 'The preserved model could not be read.'}</p>
        </div>
          : <Suspense fallback={<div className="model-empty"><span className="spinner" /><p>Loading the viewer…</p></div>}>
            <ModelViewer scene={scene.data} />
          </Suspense>}
    </div>
    <p className="geometry-check-note">
      Rebuilt from this building’s <code>model_python.osm</code>. Facade WWR is blank because the
      pipeline’s target ratio is not preserved per building — it cannot be recovered from the model alone.
    </p>
  </aside>
}
