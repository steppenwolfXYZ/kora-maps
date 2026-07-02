import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch }) => {
	const res = await fetch('/map-assets/style.json');

	if (!res.ok) {
		throw new Error(
			`Failed to load style.json (${res.status}). ` +
				`Make sure you placed the file in the static/map-assets/ directory.`
		);
	}

	const style = await res.json();
	return { style };
};
