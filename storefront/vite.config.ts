import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],

  server: {
    // Same-origin in development, matching how this deploys.
    //
    // The session cookie is SameSite=Lax, so the browser only sends it back to the
    // origin that set it. With the storefront on :5173 and the engine on :8000
    // those are two origins and the cookie would never arrive - and the
    // alternative, SameSite=None, needs HTTPS and gives up the browser's own CSRF
    // protection.
    //
    // Proxying means one origin here and one in production, so what gets tested is
    // what gets shipped.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
})