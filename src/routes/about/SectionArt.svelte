<script lang="ts">
	// Illustrations for the narrative sections on the about page.
	// Photographic illustrations (JPEG) for transit and walk are in
	// static/illustration/; car still uses the SVG placeholder until a
	// final asset lands.
	let { kind }: { kind: 'transit' | 'walk' | 'car' } = $props();
</script>

<div class="art" aria-hidden="true">
	{#if kind === 'transit'}
		<img src="/illustration/train.jpeg" alt="" />
	{:else if kind === 'walk'}
		<img src="/illustration/walk_cycle.jpeg" alt="" />
	{:else}
		<svg viewBox="0 0 180 120" role="img">
			<!-- highway interchange placeholder: cloverleaf in muted gray -->
			<g fill="none" stroke="#9aa097" stroke-linecap="round">
				<line x1="0" y1="60" x2="180" y2="60" stroke-width="15" stroke-linecap="butt" />
				<line x1="90" y1="0" x2="90" y2="120" stroke-width="15" stroke-linecap="butt" />
				<g stroke-width="6.5">
					<path d="M 108 52 Q 108 40 98 40" />
					<path d="M 108 68 Q 108 80 98 80" />
					<path d="M 72 68 Q 72 80 82 80" />
					<path d="M 72 52 Q 72 40 82 40" />
				</g>
				<g stroke-width="6">
					<path d="M 98 27 A 25 25 0 1 1 123 52" />
					<path d="M 98 93 A 25 25 0 1 0 123 68" />
					<path d="M 82 93 A 25 25 0 1 1 57 68" />
					<path d="M 82 27 A 25 25 0 1 0 57 52" />
				</g>
			</g>
		</svg>
	{/if}
</div>

<style>
	.art {
		width: 100%;
		aspect-ratio: 2752 / 1536;
		border-radius: 0.6rem;
		overflow: hidden;
		background: #efe7d6;
	}
	.art img,
	.art svg {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
</style>
