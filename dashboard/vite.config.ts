import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vuetify from 'vite-plugin-vuetify'
import { fileURLToPath, URL } from 'node:url'
import { mdiSubset } from './vite/mdi-subset'

export default defineConfig({
  plugins: [
    mdiSubset(),
    vue(),
    vuetify({ autoImport: true }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8420',
      '/sse': 'http://localhost:8420',
      '/messages': 'http://localhost:8420',
    },
  },
})
