import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  // Multi-entry: one bundle per page context
  build: {
    outDir: 'app/server/static/dist',
    rollupOptions: {
      input: {
        // Shared vendor chunk (Alpine + Chart + Leaflet + Socket.IO)
        vendor: resolve(__dirname, 'app/server/static/js/vendor.js'),
        base: resolve(__dirname, 'app/server/static/js/base-entry.js'),
        // App-specific entry points
        map: resolve(__dirname, 'app/server/static/js/map-entry.js'),
        dashboard: resolve(__dirname, 'app/server/static/js/dashboard-entry.js'),
      },
      output: {
        // Hashed filenames for cache busting
        entryFileNames: 'js/[name]-[hash].js',
        chunkFileNames: 'js/[name]-[hash].js',
        assetFileNames: 'css/[name]-[hash].[ext]',
        // Manual chunks: extract large vendor libs separately
        manualChunks: {
          leaflet: ['leaflet'],
          chartjs: ['chart.js', 'chart.js/auto'],
          socketio: ['socket.io-client'],
          alpine: ['alpinejs'],
        },
      },
    },
    // Target modern browsers for smaller output
    target: 'es2020',
    // Enable minification
    minify: 'terser',
    terserOptions: {
      compress: { drop_console: true, drop_debugger: true },
    },
    // Generate manifest for Flask integration
    manifest: true,
    // Smaller CSS output
    cssMinify: 'lightningcss',
    // Chunk size warning threshold
    chunkSizeWarningLimit: 500,
  },

  // Dev server proxies API requests to Flask
  server: {
    port: 3000,
    proxy: {
      '/api': 'http://127.0.0.1:5000',
      '/login': 'http://127.0.0.1:5000',
      '/logout': 'http://127.0.0.1:5000',
      '/register': 'http://127.0.0.1:5000',
      '/list': 'http://127.0.0.1:5000',
      '/': 'http://127.0.0.1:5000',
    },
  },

});
