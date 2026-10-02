import { defineConfig, type Plugin } from 'vite';
import { copyFileSync } from 'node:fs';
import { resolve } from 'node:path';

function copyManifest(): Plugin {
  return {
    name: 'copy-manifest',
    closeBundle: () => copyFileSync(resolve(__dirname, 'manifest.json'), resolve(__dirname, 'dist/manifest.json')),
  };
}

/** page-probe.js runs in the page's own JavaScript world, where a classic
 *  script's top-level const/let/class share one scope with the page's
 *  scripts: unwrapped, its minified names would collide with the page's own
 *  (an earlier in-page script broke Google's account menu that way). Wrapped,
 *  it declares nothing. */
function isolatePageScript(): Plugin {
  return {
    name: 'isolate-page-script',
    generateBundle(_, bundle) {
      const chunk = bundle['page-probe.js'];
      if (chunk?.type === 'chunk') chunk.code = `(() => {
${chunk.code}
})();
`;
    },
  };
}

export default defineConfig({
  root: __dirname,
  publicDir: false,
  plugins: [copyManifest(), isolatePageScript()],
  build: {
    outDir: resolve(__dirname, 'dist'),
    emptyOutDir: true,
    rollupOptions: {
      input: {
        background: resolve(__dirname, 'src/background.ts'),
        content: resolve(__dirname, 'src/content.ts'),
        'page-probe': resolve(__dirname, 'src/page-probe.ts'),
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
