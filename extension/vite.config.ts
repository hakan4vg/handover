import { defineConfig, type Plugin } from 'vite';
import { copyFileSync } from 'node:fs';
import { resolve } from 'node:path';

function copyManifest(): Plugin {
  return {
    name: 'copy-manifest',
    closeBundle: () => copyFileSync(resolve(__dirname, 'manifest.json'), resolve(__dirname, 'dist/manifest.json')),
  };
}

export default defineConfig({
  root: __dirname,
  publicDir: false,
  plugins: [copyManifest()],
  build: {
    outDir: resolve(__dirname, 'dist'),
    emptyOutDir: true,
    rollupOptions: {
      input: {
        background: resolve(__dirname, 'src/background.ts'),
        'page-media': resolve(__dirname, 'src/page-media.ts'),
        content: resolve(__dirname, 'src/content.ts'),
        popup: resolve(__dirname, 'popup.html'),
      },
      output: {
        entryFileNames: '[name].js',
        chunkFileNames: '[name].js',
        assetFileNames: '[name].[ext]',
      },
    },
  },
});
