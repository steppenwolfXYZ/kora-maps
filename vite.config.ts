import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

// maplibre-contour's `exports` map lacks an `import`/`default` condition, so
// ESM imports of the bare specifier fail to resolve. Alias straight to the
// ESM bundle's file path — a file path bypasses exports resolution entirely
// (a bare deep specifier like "maplibre-contour/dist/index.mjs" would not).
const mlcontourEsm = fileURLToPath(
	new URL('./node_modules/maplibre-contour/dist/index.mjs', import.meta.url)
);

export default defineConfig({
	plugins: [sveltekit()],
	// Expose ENVIRONMENT (dev / production / test) to client code as
	// import.meta.env.ENVIRONMENT — inlined at build time. Keep Vite's
	// default VITE_ prefix alongside.
	envPrefix: ['VITE_', 'ENVIRONMENT'],
	resolve: {
		alias: [{ find: /^maplibre-contour$/, replacement: mlcontourEsm }]
	}
});
