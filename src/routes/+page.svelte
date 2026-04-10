<script lang="ts">
	import Map from '$lib/Map.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
</script>

<svelte:head>
	<title>Map</title>
</svelte:head>

<Map style={data.style} />
