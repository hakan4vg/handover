/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DATA_MODE: 'mock' | 'native';
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
