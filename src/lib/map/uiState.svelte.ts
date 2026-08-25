// Shared UI state around the map: the map instance itself plus the
// small pieces of chrome state that both the map wiring (createMap.ts)
// and the overlay chrome (MapChrome.svelte) touch. Singleton, following
// the routingState / lineDetailState pattern. Methods are arrow-function
// fields so they can be passed as props without losing `this`.

import type maplibregl from 'maplibre-gl';
import { applyViewMode, type ViewMode } from './layers';
import { setContoursVisible } from './contours';

// Dev override: transit-focus while stop rendering is under active work.
// The concept (view-modes.md) specifies 'standard' as the shipped default.
export const DEFAULT_VIEW = 'transit-focus' as ViewMode;

export const MENU_AUTOCLOSE_MAX_WIDTH = 600;

class MapUiState {
	mapRef = $state.raw<maplibregl.Map | null>(null);
	zoom = $state(0);
	viewMode = $state<ViewMode>(DEFAULT_VIEW);
	contoursEnabled = $state(false);
	// Menu panel state (bound into MapMenu). Non-modal: stays open during
	// map interaction on large screens; on small screens any map move or
	// click closes it (breakpoint matches the .top-controls media query).
	menuOpen = $state(false);
	// Map context menu (right-click / long-press). See MapContextMenu.svelte
	// and transit-routing.md § Entry points / Map context menu.
	contextAnchor = $state<{ x: number; y: number; lng: number; lat: number } | null>(null);
	// Transient error toast (currently only fed by the locate button's
	// geolocation errors). Re-showing resets the timer.
	toast = $state<string | null>(null);
	private toastTimer: ReturnType<typeof setTimeout> | null = null;

	setView = (mode: ViewMode) => {
		this.viewMode = mode;
		if (this.mapRef) applyViewMode(this.mapRef, mode);
	};

	toggleContours = () => {
		this.contoursEnabled = !this.contoursEnabled;
		if (this.mapRef) setContoursVisible(this.mapRef, this.contoursEnabled);
	};

	closeMenuOnSmallScreen = () => {
		if (this.menuOpen && window.innerWidth <= MENU_AUTOCLOSE_MAX_WIDTH) this.menuOpen = false;
	};

	showToast = (message: string) => {
		this.toast = message;
		if (this.toastTimer) clearTimeout(this.toastTimer);
		this.toastTimer = setTimeout(() => { this.toast = null; this.toastTimer = null; }, 4000);
	};
}

export const mapUi = new MapUiState();
