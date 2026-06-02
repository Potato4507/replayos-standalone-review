import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig({
  plugins: [react()],
  base: '/native-viewer/',
  assetsInclude: ['**/*.glb', '**/*.png', '**/*.jpg', '**/*.jpeg', '**/*.svg'],
  build: {
    outDir: path.resolve(__dirname, '../frontend/public/native-viewer'),
    emptyOutDir: true,
  },
});
