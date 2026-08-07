import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { RootErrorBoundary } from './components/ErrorBoundary'

const el = document.getElementById('root')
if (!el) throw new Error('#root missing from index.html')

// Two boundaries, not one. This is the outer one: it catches a throw in the SHELL
// (sidebar, top bar, the poll's own render path), where there is no view to isolate
// and the alternative is a blank page. The inner `ViewErrorBoundary` in App.tsx
// catches everything else and keeps the chrome alive, so it is the one that
// normally fires — this one existing at all should be rare.
createRoot(el).render(
  <StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </StrictMode>,
)
