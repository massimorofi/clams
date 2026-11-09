import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The SPA is served by the Clams backend in production; in dev Vite proxies
// /api, /ws and /healthz to it so there is no CORS or origin mismatch.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8200', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8200', ws: true },
      '/healthz': { target: 'http://127.0.0.1:8200', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
