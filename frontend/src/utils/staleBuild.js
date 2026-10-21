// A tab opened before an update still runs the old build. Each page is a
// separate hashed file loaded on first visit, and the update removed the old
// ones, so the next new page 404s ("Failed to fetch dynamically imported
// module") and the error boundary shows "This page couldn't load". A reload
// picks up the new build. Fork: arya-arav/ServerKit.

const RELOAD_KEY = 'serverkit.staleBuildReloadAt';
// One automatic reload per window: if the fresh build fails the same way,
// something else is wrong and the error should stay on screen.
const RELOAD_WINDOW_MS = 30_000;

const STALE_BUILD_PATTERNS = [
    /Failed to fetch dynamically imported module/i,     // Chrome, Edge
    /error loading dynamically imported module/i,        // Firefox
    /Importing a module script failed/i,                 // Safari
    /Unable to preload CSS/i,                            // Vite CSS preload
];

export function isStaleBuildError(error) {
    const text = String(error?.message || error || '');
    return STALE_BUILD_PATTERNS.some((re) => re.test(text));
}

// Reloads the page unless it already did so moments ago. Returns whether it
// reloaded. Without session storage it never reloads, so it can't loop.
export function reloadForNewBuild() {
    try {
        const last = Number(window.sessionStorage.getItem(RELOAD_KEY) || 0);
        if (Date.now() - last < RELOAD_WINDOW_MS) return false;
        window.sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
    } catch {
        return false;
    }
    window.location.reload();
    return true;
}

// Vite fires this when a lazy page's file (or its CSS) fails to load.
export function installStaleBuildReload() {
    window.addEventListener('vite:preloadError', (event) => {
        if (reloadForNewBuild()) event.preventDefault();
    });
}
