import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '../core/webui_static',
    emptyOutDir: true,
    sourcemap: false,
  },
});
