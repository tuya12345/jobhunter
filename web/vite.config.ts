import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 后端默认地址：`jh leads serve` 只绑回环地址 127.0.0.1:8787（见 docs/P7-消费端API.md §0）。
const BACKEND = 'http://127.0.0.1:8787';

export default defineConfig({
  plugins: [react()],

  server: {
    // ★ 和 `jh leads serve` 同一条纪律：本地隐私工具，只监听回环地址，不绑 0.0.0.0。
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // 页面只认同源的 /api/*：开发时由 Vite 转给后端，生产时由 FastAPI 自己提供。
      // 这样前端代码里没有"后端地址"这个概念，也就没有第二份地址配置。
      '/api': { target: BACKEND, changeOrigin: true },
    },
  },

  preview: {
    host: '127.0.0.1',
    port: 4173,
    // preview 也走同一份代理，方便 `npm run build && npm run preview` 对着假后端看产物。
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
    },
  },

  build: {
    outDir: 'dist', // = web/dist，由 `jh leads serve` 当静态目录挂出去
    emptyOutDir: true,
    sourcemap: false,
  },
});
