<script lang="ts">
	import { onMount } from 'svelte';
	import SectionArt from './SectionArt.svelte';

	// The splash screen lives in app.html and is only hidden by
	// Map.svelte — on this non-map page it would stay forever. Hide it
	// on mount, mirroring Map.svelte's hideSplash.
	onMount(() => {
		const s = document.getElementById('kora-splash');
		if (s) {
			s.classList.add('kora-splash-hidden');
			setTimeout(() => s.remove(), 400);
		}
	});

	const sections = [
		{
			kind: 'transit' as const,
			title: 'Focus on Public Transit',
			body: "Kora Maps doesn't only focus on public transit, it also shows speed and frequency by line color and thickness."
		},
		{
			kind: 'walk' as const,
			title: 'Cycling and Walking',
			body: 'While not yet perfect, Kora Maps wants to highlight cycling and pedestrian infrastructure and have amazing routing for both. This is not yet where it’s supposed to be, but a stated goal of Kora Maps.'
		},
		{
			kind: 'car' as const,
			title: 'Hidden Car Infrastructure',
			body: "Highways are just a dotted line from afar, and a gray 'dead zone' from up close. Large roads are also rather hidden than highlighted."
		}
	];

	const cases = [
		{
			icon: 'place',
			accent: '#740013',
			title: 'Find a place for vacation',
			body: "If you're planning your vacation, there's almost no tools that allow you to see if you're able to get around without a car at your destination. With Kora Maps, that's just a quick look."
		},
		{
			icon: 'map',
			accent: '#1fb6b6',
			title: 'Understand the transit system',
			body: 'Find out where transit lines go and how the system works on the graphical map.'
		},
		{
			icon: 'directions',
			accent: '#1f51b6',
			title: 'Amazing routing and usability',
			body: 'In Switzerland, we have the SBB App, which is really good. The goal is to bring that quality to the whole world.'
		}
	];
</script>

<svelte:head>
	<title>About — Kora Maps</title>
</svelte:head>

<div class="page">
	<nav class="topbar">
		<a class="back" href="/">
			<span class="material-symbols-outlined back-arrow">chevron_right</span>
			back to map
		</a>
		<a class="brandmark" href="/" aria-label="Kora Maps">
			<img src="/kora_maps_landscape.svg" alt="Kora Maps" />
		</a>
	</nav>

	<header class="hero">
		<div class="hero-text">
			<span class="eyebrow">About</span>
			<h1>A map for people instead of cars.</h1>
			<p class="lede">If you want a map to find out where to go, and how to go there, but you're not driving a car, Kora Maps is for you.</p>
			<div class="hero-actions">
				<span class="beta-pill">Beta</span>
			</div>
		</div>
		<div class="hero-logo" aria-hidden="true">
			<img src="/logo.svg" alt="" />
		</div>
	</header>

	<section class="problem">
		<aside class="explainer">
			<span class="explainer-icon material-symbols-outlined">directions_car</span>
			<div class="explainer-body">
				<h2 class="problem-statement">Almost every map we know is built around <span class="car-hl">cars</span>.</h2>
				<p>The main thing you see is highways and major roads. But if you don't own a car or don't use the map to find a way to drive, that's the wrong approach.</p>
			</div>
		</aside>
	</section>

	<main class="sections">
		{#each sections as s, i}
			<section class="section" class:flip={i % 2 === 1}>
				<div class="section-text">
					<h2>{s.title}</h2>
					<p>{s.body}</p>
				</div>
				<div class="art-card">
					<SectionArt kind={s.kind} />
				</div>
			</section>
		{/each}
	</main>

	<section class="uses">
		<h3 class="band-title">Use Cases</h3>
		<div class="cards">
			{#each cases as c}
				<article class="card" style="--accent: {c.accent}">
					<span class="card-chip">
						<span class="material-symbols-outlined card-icon">{c.icon}</span>
					</span>
					<h4>{c.title}</h4>
					<p>{c.body}</p>
				</article>
			{/each}
		</div>
	</section>

	<section class="closing">
		<div class="close-block beta">
			<h3>Beta</h3>
			<p>Kora Maps is in early development, most features are still incomplete or missing. While the app is generally working, nothing is guaranteed to work or return accurate results.</p>
		</div>

		<div class="close-block cofounder">
			<h3>Looking for Co-Founder(s)</h3>
			<p>If you share the vision of this project and want to be a part of it: get in touch. However, I'm not trying to go the VC route, nor am I trying to get rich here. I want this platform to thrive without losing control to some investors. This means there's no income from the project in the beginning.</p>
			<a class="contact" href="mailto:info@steppenwolfx.ch">info@steppenwolfx.ch</a>
		</div>
	</section>

	<footer class="foot">
		<a href="/"><img src="/icon.svg" alt="" /> koramaps.app</a>
	</footer>
</div>

<style>
	/* The map page locks html/body to overflow:hidden; on this scrolling
	   page, re-enable normal document flow. */
	:global(html), :global(body) {
		overflow: auto;
		height: auto;
		background: #f6f1e6;
	}

	.page {
		font-family: 'Saira', 'Helvetica Neue', Arial, sans-serif;
		color: #2a241c;
		max-width: 62rem;
		margin: 0 auto;
		padding: 0 1.5rem 4.5rem;
	}

	/* ---------- Top bar ---------- */
	.topbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 1.1rem 0;
	}
	.back {
		display: inline-flex;
		align-items: center;
		gap: 0.15rem;
		font-size: 0.9rem;
		font-weight: 600;
		color: #6e6155;
		text-decoration: none;
		padding: 0.3rem 0;
		transition: color 0.15s ease;
	}
	.back:hover { color: #740013; }
	.back-arrow {
		font-size: 20px;
		transform: rotate(180deg);
	}
	.brandmark {
		display: inline-flex;
		align-items: center;
		text-decoration: none;
	}
	.brandmark img { height: 2.5rem; width: auto; display: block; }

	/* ---------- Hero logo ---------- */
	.hero-logo {
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.hero-logo img {
		display: block;
		width: min(100%, 17rem);
		height: auto;
		opacity: 0;
		transform: translateY(10px);
		animation: logo-rise 0.7s cubic-bezier(0.3, 0.8, 0.3, 1) 0.15s forwards;
	}
	@keyframes logo-rise {
		to { opacity: 1; transform: translateY(0); }
	}
	@media (prefers-reduced-motion: reduce) {
		.hero-logo img { animation: none; opacity: 1; transform: none; }
	}

	/* ---------- Hero ---------- */
	.hero {
		display: grid;
		grid-template-columns: minmax(0, 1.02fr) minmax(0, 0.98fr);
		align-items: center;
		gap: 3rem;
		padding: 2.4rem 0 3.2rem;
		border-bottom: 1px solid rgba(116, 0, 19, 0.12);
	}
	.eyebrow {
		font-weight: 600;
		font-size: 0.78rem;
		letter-spacing: 0.22em;
		text-transform: uppercase;
		color: #740013;
	}
	.hero h1 {
		font-weight: 800;
		font-size: clamp(2.1rem, 4.6vw, 3.1rem);
		line-height: 1.06;
		letter-spacing: -0.025em;
		margin: 0.6rem 0 0;
	}
	.lede {
		font-size: 1.1rem;
		line-height: 1.6;
		color: #4f463c;
		margin-top: 1.1rem;
		max-width: 26rem;
	}
	.hero-actions {
		display: flex;
		align-items: center;
		gap: 0.8rem;
		margin-top: 1.6rem;
	}
	.beta-pill {
		display: inline-block;
		background: #740013;
		color: #fff;
		font-weight: 700;
		font-size: 0.72rem;
		letter-spacing: 0.05em;
		padding: 0.3rem 0.6rem;
		border-radius: 999px;
	}

	/* ---------- Problem statement ---------- */
	.problem {
		padding: 3.2rem 0 1rem;
	}
	.problem-statement {
		font-weight: 700;
		font-size: clamp(1.5rem, 3.2vw, 2.1rem);
		line-height: 1.2;
		letter-spacing: -0.015em;
		margin: 0;
	}
	.car-hl {
		color: #740013;
		background: linear-gradient(to top, rgba(116, 0, 19, 0.14) 38%, transparent 38%);
		padding: 0 0.08em;
	}
	.explainer {
		display: grid;
		grid-template-columns: auto 1fr;
		align-items: flex-start;
		gap: 1.4rem;
		padding: 1.8rem 2rem 1.9rem;
		background: #efe7d6;
		border: 1px solid rgba(42, 36, 28, 0.1);
		border-left: 4px solid #9aa097;
		border-radius: 0.5rem 0.9rem 0.9rem 0.5rem;
	}
	.explainer-icon {
		font-size: 48px;
		color: #5b5346;
		line-height: 1;
		margin-top: -0.1rem;
	}
	.explainer-body {
		min-width: 0;
		display: grid;
		grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
		gap: 1rem 2.2rem;
		align-items: start;
	}
	.explainer p {
		font-size: 1rem;
		line-height: 1.6;
		color: #3c3529;
		margin: 0;
	}
	@media (max-width: 720px) {
		.explainer-body { grid-template-columns: 1fr; }
	}

	/* ---------- Narrative sections ---------- */
	.sections { padding: 1.2rem 0 0.5rem; }
	.section {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		align-items: center;
		gap: 1.5rem;
		margin-top: 3.2rem;
	}
	.section.flip .section-text { order: 2; }
	.section.flip .art-card { order: 1; }
	.section-text {
		padding: 1.3rem 1.4rem 1.4rem 1.6rem;
		border: 1px solid rgba(42, 36, 28, 0.1);
		border-left: 4px solid #740013;
		border-radius: 0.5rem 0.9rem 0.9rem 0.5rem;
	}
	.section.flip .section-text {
		padding: 1.3rem 1.6rem 1.4rem 1.4rem;
		border: 1px solid rgba(42, 36, 28, 0.1);
		border-right: 4px solid #740013;
		border-radius: 0.9rem 0.5rem 0.5rem 0.9rem;
	}
	.section h2 {
		font-weight: 800;
		font-size: 1.55rem;
		line-height: 1.15;
		letter-spacing: -0.02em;
		margin: 0;
		max-width: 30rem;
	}
	.section-text p {
		margin-top: 0.9rem;
		font-size: 1.02rem;
		line-height: 1.65;
		color: #3c3529;
		max-width: 32rem;
	}
	.art-card {
		background: #fbf8f0;
		border: 1px solid rgba(42, 36, 28, 0.08);
		border-radius: 1rem;
		box-shadow: 0 14px 30px -18px rgba(42, 36, 28, 0.25);
		padding: 0.5rem;
	}

	/* ---------- Use cases band ---------- */
	.uses {
		margin: 4.2rem 0 0;
		padding: 2.6rem 2rem 2.8rem;
		background: #c99884;
		color: #2a241c;
		border-radius: 1.3rem;
	}
	.band-title {
		font-weight: 700;
		letter-spacing: 0.2em;
		text-transform: uppercase;
		font-size: 0.82rem;
		color: #6b2f27;
		margin-bottom: 1.6rem;
	}
	.cards {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 1.3rem;
	}
	.card {
		background: rgba(255, 250, 235, 0.35);
		border: 1px solid rgba(42, 36, 28, 0.15);
		border-radius: 0.9rem;
		padding: 1.35rem 1.3rem 1.5rem;
		transition: transform 0.18s ease, background 0.18s ease;
	}
	.card:hover {
		transform: translateY(-3px);
		background: rgba(255, 250, 235, 0.55);
	}
	.card-chip {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 46px;
		height: 46px;
		border-radius: 14px;
		background: color-mix(in srgb, var(--accent) 22%, transparent);
	}
	.card-icon {
		font-size: 26px;
		color: var(--accent);
	}
	.card h4 {
		font-weight: 700;
		font-size: 1.08rem;
		line-height: 1.25;
		margin: 0.85rem 0 0.5rem;
		color: #2a241c;
	}
	.card p {
		font-size: 0.92rem;
		line-height: 1.6;
		color: #3c3529;
	}

	/* ---------- Beta + co-founder ---------- */
	.closing {
		margin-top: 3.2rem;
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 1.6rem;
	}
	.close-block {
		padding: 1.5rem 1.5rem 1.6rem;
		border-radius: 1rem;
		background: #fbf8f0;
		border: 1px solid rgba(42, 36, 28, 0.1);
		border-top: 3px solid #9aa097;
	}
	.close-block.beta {
		background: #eeddd7;
		border-color: rgba(116, 0, 19, 0.22);
		border-top-color: #740013;
	}
	.close-block h3 {
		font-weight: 800;
		font-size: 1.08rem;
		margin-bottom: 0.6rem;
	}
	.close-block.beta h3 { color: #740013; }
	.close-block p {
		font-size: 0.95rem;
		line-height: 1.65;
		color: #3c3529;
	}
	.contact {
		display: inline-block;
		margin-top: 1.1rem;
		font-weight: 700;
		font-size: 0.92rem;
		color: #740013;
		text-decoration: none;
		border: 1.5px solid #740013;
		border-radius: 999px;
		padding: 0.42rem 0.9rem;
		transition: background 0.15s ease, color 0.15s ease;
	}
	.contact:hover {
		background: #740013;
		color: #fff;
	}

	/* ---------- Footer ---------- */
	.foot {
		margin-top: 3.8rem;
		padding-top: 1.5rem;
		border-top: 1px solid rgba(116, 0, 19, 0.12);
		text-align: center;
	}
	.foot a {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		font-size: 0.85rem;
		font-weight: 600;
		color: #6e6155;
		text-decoration: none;
	}
	.foot img { height: 1.5rem; width: 1.5rem; }
	.foot a:hover { color: #740013; }

	/* ---------- Responsive ---------- */
	@media (max-width: 860px) {
		.hero {
			grid-template-columns: 1fr;
			gap: 2.2rem;
			padding: 1.8rem 0 2.6rem;
		}
		.section {
			grid-template-columns: 1fr;
			gap: 1.6rem;
			margin-top: 2.8rem;
		}
		.section.flip .section-text { order: 1; }
		.section.flip .art-card { order: 2; }
		.art-card { order: 2; }
		.section.flip .section-text {
			padding: 1.3rem 1.4rem 1.4rem 1.6rem;
			border-left: 4px solid #740013;
			border-right: 1px solid rgba(42, 36, 28, 0.1);
			border-radius: 0.5rem 0.9rem 0.9rem 0.5rem;
		}
		.cards { grid-template-columns: 1fr; gap: 1rem; }
		.closing { grid-template-columns: 1fr; gap: 1.2rem; }
	}
</style>
