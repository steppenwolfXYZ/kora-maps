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
	// Transient toast: the locate button's and navigation's geolocation
	// errors, navigation's sensor hints. `error` level renders red and
	// stays longer — for messages the user must not miss. Re-showing
	// resets the timer.
	toast = $state<string | null>(null);
	toastLevel = $state<'info' | 'error'>('info');
	/** Bold title line above the message (error toasts). */
	toastTitle = $state<string | null>(null);
	private toastTimer: ReturnType<typeof setTimeout> | null = null;

	setView = (mode: ViewMode) => {
		this.viewMode = mode;
		if (this.mapRef) applyViewMode(this.mapRef, mode);
	};

	setContours = (enabled: boolean) => {
		this.contoursEnabled = enabled;
		if (this.mapRef) setContoursVisible(this.mapRef, enabled);
	};

	toggleContours = () => this.setContours(!this.contoursEnabled);

	closeMenuOnSmallScreen = () => {
		if (this.menuOpen && window.innerWidth <= MENU_AUTOCLOSE_MAX_WIDTH) this.menuOpen = false;
	};

	showToast = (message: string, level: 'info' | 'error' = 'info', title: string | null = null) => {
		this.toast = message;
		this.toastLevel = level;
		this.toastTitle = title;
		if (this.toastTimer) clearTimeout(this.toastTimer);
		const ms = level === 'error' ? 7000 : 4000;
		this.toastTimer = setTimeout(() => { this.toast = null; this.toastTimer = null; }, ms);
	};
}

export const mapUi = new MapUiState();
