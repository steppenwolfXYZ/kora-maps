// Google-encoded polyline decoder. MOTIS emits leg geometries in this
// format with a per-geometry `precision` (5 for lat/lng, 6 for tighter
// encodings — some MOTIS builds default to 6). Returns [lon, lat] pairs
// so the output plugs directly into GeoJSON LineString coordinates.

export function decodePolyline(encoded: string, precision = 5): [number, number][] {
	const factor = Math.pow(10, precision);
	const coords: [number, number][] = [];
	let index = 0;
	let lat = 0;
	let lng = 0;
	while (index < encoded.length) {
		let byte = 0;
		let shift = 0;
		let result = 0;
		do {
			byte = encoded.charCodeAt(index++) - 63;
			result |= (byte & 0x1f) << shift;
			shift += 5;
		} while (byte >= 0x20);
		lat += (result & 1) ? ~(result >> 1) : (result >> 1);

		shift = 0;
		result = 0;
		do {
			byte = encoded.charCodeAt(index++) - 63;
			result |= (byte & 0x1f) << shift;
			shift += 5;
		} while (byte >= 0x20);
		lng += (result & 1) ? ~(result >> 1) : (result >> 1);

		coords.push([lng / factor, lat / factor]);
	}
	return coords;
}
