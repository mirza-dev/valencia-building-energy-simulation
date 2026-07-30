import '@fontsource-variable/ibm-plex-sans/index.css'
import '@fontsource/ibm-plex-mono/400.css'
import 'maplibre-gl/dist/maplibre-gl.css'
import './styles.css'
import './lib/i18n'

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { FeedbackProvider } from './components/FeedbackProvider'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: false },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <FeedbackProvider><App /></FeedbackProvider>
    </QueryClientProvider>
  </StrictMode>,
)
