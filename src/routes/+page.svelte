<script lang="ts">
	import Map from '$lib/Map.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
</script>

<Map style={data.style} />
