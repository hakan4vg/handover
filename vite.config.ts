import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 4173,
    strictPort: true,
    host: '127.0.0.1',
    watch: {
      ignored: ['**/artifacts/**', '**/src-tauri/target/**', '**/extension/dist/**'],
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
  },
});
