import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend runs on :8500 (see backend/app.py). The dev server proxies
// /api/* so the frontend never hardcodes a host.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8500',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
