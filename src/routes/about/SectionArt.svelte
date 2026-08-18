<script lang="ts">
	// Photographic illustrations for the narrative sections on the
	// about page. Files in static/illustration/.
	let { kind }: { kind: 'transit' | 'walk' | 'car' } = $props();

	const src = {
		transit: '/illustration/train.jpeg',
		walk: '/illustration/walk_cycle.jpeg',
		car: '/illustration/car_infrastructure.jpeg'
	}[kind];
</script>

<div class="art" aria-hidden="true">
	<img {src} alt="" />
</div>

<style>
	.art {
		width: 100%;
		aspect-ratio: 2752 / 1536;
		border-radius: 0.6rem;
		overflow: hidden;
		background: #efe7d6;
	}
	.art img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
</style>
