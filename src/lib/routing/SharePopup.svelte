<script lang="ts">
	// Share bubble (connection-sharing.md § Share button): a speech bubble
	// anchored to the share button, shown once the share has been created.
	// Presents the short link with an explicit Copy button and — where the
	// browser offers it — the native share sheet. Rendered position:fixed so
	// the routing panel's scroll container can't clip it; the anchor rect is
	// captured at click time.
	let { url, anchor, onClose }: {
		url: string;
		/** Viewport rect of the share button (captured on click). */
		anchor: DOMRect;
		onClose: () => void;
	} = $props();

	const BUBBLE_W = 300;
	const GAP = 12;       // distance between button and bubble (tail lives here)
	const MARGIN = 8;     // min viewport inset

	const pos = $derived.by(() => {
		const anchorCx = anchor.left + anchor.width / 2;
		// Mostly to the left of the button (it sits near the panel's right
		// edge), clamped into the viewport.
		const left = Math.min(
			Math.max(anchorCx - BUBBLE_W + 56, MARGIN),
			Math.max(window.innerWidth - BUBBLE_W - MARGIN, MARGIN)
		);
		const tailX = Math.min(Math.max(anchorCx - left, 20), BUBBLE_W - 20);
		// Above the button when there's headroom, otherwise below.
		return { left, tailX, above: anchor.top >= 240 };
	});

	let copied = $state(false);
	let inputEl: HTMLInputElement | null = $state(null);
	const canNativeShare = typeof navigator !== 'undefined' && typeof navigator.share === 'function';

	async function copy() {
		try {
			await navigator.clipboard.writeText(url);
		} catch {
			// Clipboard API blocked (e.g. non-secure context) — fall back to
			// selecting the text so a manual ⌘C works.
			inputEl?.select();
			document.execCommand?.('copy');
		}
		copied = true;
		setTimeout(() => { copied = false; }, 2000);
	}

	async function nativeShare() {
		try {
			await navigator.share({ title: 'Kora Maps', url });
			onClose();
		} catch {
			// Abort = user closed the sheet — keep the bubble open.
		}
	}
</script>

<svelte:window
	onkeydown={(e) => { if (e.key === 'Escape') onClose(); }}
	onresize={onClose}
/>

<!-- Transparent click-catcher: closes the bubble on any outside click and
     keeps the click from reaching the card underneath. No dimming — this is
     a bubble, not a modal. -->
<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="share-catcher" onclick={(e) => { e.stopPropagation(); onClose(); }}></div>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions, a11y_click_events_have_key_events -->
<div
	class="share-bubble"
	class:below={!pos.above}
	role="dialog"
	aria-label="Share this connection"
	tabindex="-1"
	style="left:{pos.left}px; {pos.above
		? `top:${anchor.top - GAP}px; transform:translateY(-100%);`
		: `top:${anchor.bottom + GAP}px;`} --tail-x:{pos.tailX}px; width:{BUBBLE_W}px;"
	onclick={(e) => e.stopPropagation()}
>
	<input
		class="share-url"
		type="text"
		readonly
		value={url}
		bind:this={inputEl}
		onfocus={(e) => (e.currentTarget as HTMLInputElement).select()}
	/>
	<div class="share-actions">
		<button class="share-btn share-btn-primary" type="button" onclick={copy}>
			{copied ? 'Copied!' : 'Copy link'}
		</button>
		{#if canNativeShare}
			<button class="share-btn" type="button" onclick={nativeShare}>Share…</button>
		{/if}
	</div>
</div>

<style>
	.share-catcher {
		position: fixed;
		inset: 0;
		z-index: 60;
		cursor: default;
	}
	.share-bubble {
		position: fixed;
		z-index: 61;
		background: #ffffff;
		border-radius: 0.7rem;
		box-shadow: 0 2px 14px rgba(0, 0, 0, 0.28);
		padding: 0.6rem;
		font-family: 'Saira', sans-serif;
		display: flex;
		flex-direction: column;
		gap: 0.45rem;
		text-align: left;
	}
	/* Speech-bubble tail, pointing at the share button. */
	.share-bubble::after {
		content: '';
		position: absolute;
		left: var(--tail-x);
		margin-left: -9px;
		border: 9px solid transparent;
	}
	.share-bubble:not(.below)::after {
		top: 100%;
		border-top-color: #ffffff;
	}
	.share-bubble.below::after {
		bottom: 100%;
		border-bottom-color: #ffffff;
	}

	.share-url {
		width: 100%;
		border: 1px solid #ddd;
		border-radius: 0.5rem;
		background: #f7f7f7;
		font-family: inherit;
		font-size: 0.8rem;
		color: #333;
		padding: 0.4rem 0.5rem;
	}
	.share-url:focus { outline: none; border-color: #999; }

	.share-actions {
		display: flex;
		gap: 0.4rem;
	}
	.share-btn {
		flex: 1 1 auto;
		border: 1px solid #ddd;
		background: #f5f5f5;
		color: #222;
		font-family: inherit;
		font-size: 0.82rem;
		font-weight: 600;
		padding: 0.4rem 0.55rem;
		border-radius: 0.5rem;
		cursor: pointer;
		transition: background 0.12s, border-color 0.12s;
	}
	.share-btn:hover { background: #ebebeb; border-color: #bbb; }
	.share-btn-primary {
		background: #740013;
		border-color: #740013;
		color: #fff;
	}
	.share-btn-primary:hover { background: #8a0418; border-color: #8a0418; }
</style>
