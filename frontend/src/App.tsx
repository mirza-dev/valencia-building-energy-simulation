import { lazy, Suspense, useEffect } from 'react'
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
const FilesPage = lazy(() => import('./components/FilesPage'))
const ProductRunPage = lazy(() => import('./components/RunPage'))
const OutputsPage = lazy(() => import('./components/OutputsPage'))

export default function App() {
  const { t, i18n } = useTranslation()
  useEffect(() => {
    document.documentElement.lang = 'en'
    if (i18n.language !== 'en') void i18n.changeLanguage('en')
  }, [i18n])
  return (
    <HashRouter>
      <ActiveBuildingProvider>
        <AppShell>
          <Suspense fallback={<div className="page-loading"><span className="spinner" />{t('common.loading')}</div>}>
            <Routes>
              <Route path="/files" element={<FilesPage />} />
              <Route path="/run" element={<ProductRunPage />} />
              <Route path="/outputs" element={<OutputsPage />} />

              {/* Historical expert surfaces remain callable by direct URL,
                  but are absent from the operational stock navigation. */}
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
              <Route path="*" element={<Navigate to="/files" replace />} />
            </Routes>
          </Suspense>
        </AppShell>
      </ActiveBuildingProvider>
    </HashRouter>
  )
}
