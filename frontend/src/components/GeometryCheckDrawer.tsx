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
  // comparing two buildings is the actual check.  Debounced so arrow-keying
  // down the table does not queue one OpenStudio load per row.
  const [settled, setSettled] = useState(reference)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(reference), 150)
    return () => clearTimeout(timer)
  }, [reference])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const scene = useQuery({
    queryKey: ['stock-building-scene', run, settled],
    queryFn: () => api.stockBuildingScene(run, settled),
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
