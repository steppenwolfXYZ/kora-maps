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
	resolve: {
		alias: [{ find: /^maplibre-contour$/, replacement: mlcontourEsm }]
	},
	server: {
		// Reverse-proxy Valhalla same-origin in dev so the browser never
		// sends a cross-origin request. Valhalla's built-in HTTP server
		// emits no CORS headers, so a direct POST from the app's origin
		// (http://localhost:5173) is blocked by preflight. Production
		// solves the same problem with nginx `location /valhalla/`; this
		// mirrors that shape for dev, and lets PUBLIC_VALHALLA_URL stay
		// `/valhalla` in both .env and .env.production.
		proxy: {
			'/valhalla': {
				target: 'http://localhost:8002',
				changeOrigin: true,
				rewrite: (path) => path.replace(/^\/valhalla/, '')
			}
		}
	}
});
