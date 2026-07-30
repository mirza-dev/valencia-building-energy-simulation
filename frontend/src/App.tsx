import { lazy, Suspense } from 'react'
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom'
import AppShell from './components/AppShell'
import { useTranslation } from 'react-i18next'
import { ActiveBuildingProvider } from './lib/activeBuilding'

const BatchPage = lazy(() => import('./components/BatchPage'))
const BuilderPage = lazy(() => import('./components/BuilderPage'))
const ComparePage = lazy(() => import('./components/ComparePage'))
const HealthPage = lazy(() => import('./components/HealthPage'))
const RunsPage = lazy(() => import('./components/RunsPage'))
const SimulationPage = lazy(() => import('./components/SimulationPage'))
const NeighborhoodPage = lazy(() => import('./components/NeighborhoodPage'))
const CityPage = lazy(() => import('./components/CityPage'))
const LhsPage = lazy(() => import('./components/LhsPage'))
const ModelInspectorPage = lazy(() => import('./components/ModelInspectorPage'))

export default function App() {
  const { t } = useTranslation()
  return (
    <HashRouter>
      <ActiveBuildingProvider>
        <AppShell>
          <Suspense fallback={<div className="page-loading"><span className="spinner" />{t('common.loading')}</div>}>
            <Routes>
              <Route path="/builder" element={<BuilderPage />} />
              <Route path="/simulation" element={<SimulationPage />} />
              <Route path="/model/:modelId" element={<ModelInspectorPage />} />
              <Route path="/runs" element={<RunsPage />} />
              <Route path="/compare" element={<ComparePage />} />

              {/* Historical stock/system surfaces remain callable by direct URL,
                  but are intentionally absent from the single-building product flow. */}
              <Route path="/neighborhood" element={<NeighborhoodPage />} />
              <Route path="/city" element={<CityPage />} />
              <Route path="/lhs" element={<LhsPage />} />
              <Route path="/batch" element={<BatchPage />} />
              <Route path="/health" element={<HealthPage />} />
              <Route path="*" element={<Navigate to="/builder" replace />} />
            </Routes>
          </Suspense>
        </AppShell>
      </ActiveBuildingProvider>
    </HashRouter>
  )
}
