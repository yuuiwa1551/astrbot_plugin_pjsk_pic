import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

const webuiRoot = fileURLToPath(new URL('.', import.meta.url));
const webuiStaticDir = fileURLToPath(new URL('../core/webui_static', import.meta.url));

export default defineConfig({
  root: webuiRoot,
  plugins: [vue()],
  build: {
    outDir: webuiStaticDir,
    emptyOutDir: true,
    sourcemap: false,
  },
});
