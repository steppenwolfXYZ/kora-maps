// The top-right "start navigation" map control (bicycle-navigation.md
// § Entering and leaving): a MapLibre IControl so it stacks in the same
// column as the zoom / compass / locate controls. Shown only while a
// cycling route is selected and navigation is not running — the
// orchestration toggles it. Styled by app.css § MapLibre controls
// (vendor-style DOM, not a Svelte component).

import type maplibregl from 'maplibre-gl';

export class NavStartControl implements maplibregl.IControl {
	private container: HTMLDivElement | null = null;
	private button: HTMLButtonElement | null = null;

	constructor(private readonly onStart: () => void) {}

	onAdd(): HTMLElement {
		const container = document.createElement('div');
		container.className = 'maplibregl-ctrl kora-nav-start';
		container.hidden = true;
		const button = document.createElement('button');
		button.type = 'button';
		button.className = 'control-disc nav-start-btn';
		button.title = 'Start navigation';
		button.setAttribute('aria-label', 'Start navigation');
		button.innerHTML = '<span class="material-symbols-outlined" aria-hidden="true">navigation</span>';
		button.addEventListener('click', () => this.onStart());
		container.appendChild(button);
		this.container = container;
		this.button = button;
		return container;
	}

	onRemove(): void {
		this.container?.remove();
		this.container = null;
		this.button = null;
	}

	setVisible(visible: boolean): void {
		if (this.container) this.container.hidden = !visible;
	}

	/** While the first position fix is awaited. */
	setBusy(busy: boolean): void {
		if (!this.button) return;
		this.button.disabled = busy;
		this.button.classList.toggle('busy', busy);
	}
}
