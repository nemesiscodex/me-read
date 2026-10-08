import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5183,
    strictPort: true,
    proxy: {
      '/api': process.env.ME_READ_API ?? 'http://127.0.0.1:8765',
    },
  },
  test: {
    include: ['src/**/*.test.ts'],
  },
})
