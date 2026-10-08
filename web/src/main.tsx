import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './i18n'
import { captureTokenFromUrl } from './midi/bridgeToken'
import './index.css'
import App from './App.tsx'

captureTokenFromUrl()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
