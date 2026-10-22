/**
 * Extension route resolver.
 *
 * Splits contributed routes into two buckets based on their `layout`:
 *
 *   dashboardRoutes   — render inside <DashboardLayout/> alongside core
 *                       pages (layout: 'padded' (default) | 'full').
 *
 *   groupRoutes       — routes with a `group` field render INSIDE that
 *                       core tab group's TabGroupLayout (tab-group
 *                       contribution, #43). App.jsx mounts
 *                       groupRoutes[<groupId>] as children of the
 *                       matching group's parent <Route>, so the shared
 *                       PageTopbar chrome stays. Pair with a `tabs`
 *                       contribution so the strip shows the tab.
 *
 *   standaloneGroups  — render OUTSIDE DashboardLayout. One group per
 *                       distinct layout id (built-in 'bare' or any
 *                       plugin-contributed layout). App.jsx wraps each
 *                       group's routes in a parent <Route> that uses
 *                       the layout component.
 *
 * Routes whose `component` (or whose custom `layout`) can't be resolved
 * are skipped + logged in dev so a misconfigured plugin can't blow up
 * the whole route tree.
 */
import { Route } from 'react-router-dom';
import StandaloneLayout from '../layouts/StandaloneLayout';
import {
    useContributions,
    resolveComponent,
    resolveCustomLayout,
    isInsideDashboard,
} from './contributions';
import { getRuntimeLoadState, isRuntimeFrontend } from './runtime/loader';
import { ExtensionErrorBoundary, ExtensionFailureCard } from './runtime/ExtensionBoundary';
import GroupTabShell from './GroupTabShell';
import { t } from '../i18n/t';

function devWarn(msg) {
    if (import.meta.env.DEV) {
        console.warn(`[plugins] ${msg}`);
    }
}

function buildRoute(contrib, key, frontends) {
    const Component = resolveComponent(contrib.plugin, contrib.component);
    if (!Component) {
        // Fail soft, loudly (Decision 5): if the slug is delivered as a runtime
        // bundle that failed to load, render a failure card on its routes rather
        // than dropping them silently. Unknown (non-runtime) components still
        // just skip + warn — a misconfigured builtin shouldn't card every route.
        if (isRuntimeFrontend(frontends, contrib.plugin)) {
            const state = getRuntimeLoadState(contrib.plugin);
            return (
                <Route
                    key={key}
                    path={contrib.path}
                    element={(
                        <ExtensionFailureCard
                            slug={contrib.plugin}
                            message={state?.error || t('app.extensionRoutes.componentNotFoundInBundle', 'component "{{name}}" not found in bundle', { name: contrib.component })}
                        />
                    )}
                />
            );
        }
        devWarn(
            `Cannot resolve component "${contrib.component}" for plugin `
            + `"${contrib.plugin}" (route ${contrib.path})`
        );
        return null;
    }
    const page = (
        <ExtensionErrorBoundary slug={contrib.plugin}>
            <Component />
        </ExtensionErrorBoundary>
    );
    return (
        <Route
            key={key}
            path={contrib.path}
            element={contrib.group ? <GroupTabShell group={contrib.group}>{page}</GroupTabShell> : page}
        />
    );
}

// Built-in layout components for non-dashboard groups. Custom plugin
// layouts get resolved via resolveCustomLayout().
const BUILTIN_LAYOUT_COMPONENTS = {
    bare: StandaloneLayout,
};

export default function useExtensionRoutes() {
    const { routes, layouts, frontends } = useContributions();

    const dashboardRoutes = [];
    const groupRoutes = {};
    const groupsByLayout = new Map();

    (routes || []).forEach((contrib, i) => {
        const key = `${contrib.plugin}:${contrib.path}:${i}`;
        const layoutId = contrib.layout || 'padded';

        // Tab-group nested route (#43): collected per core group id and
        // mounted by App.jsx inside that group's TabGroupLayout <Route>.
        if (contrib.group) {
            const node = buildRoute(contrib, key, frontends);
            if (node) {
                if (!groupRoutes[contrib.group]) groupRoutes[contrib.group] = [];
                groupRoutes[contrib.group].push(node);
            }
            return;
        }

        if (isInsideDashboard(layoutId)) {
            const node = buildRoute(contrib, key, frontends);
            if (node) dashboardRoutes.push(node);
            return;
        }

        // Standalone (bare or custom). Resolve the layout component once
        // per group; skip the whole route if its layout can't be found.
        let LayoutComponent = BUILTIN_LAYOUT_COMPONENTS[layoutId]
            || resolveCustomLayout(layoutId, layouts);
        if (!LayoutComponent) {
            devWarn(
                `Cannot resolve layout "${layoutId}" for plugin `
                + `"${contrib.plugin}" (route ${contrib.path})`
            );
            return;
        }

        const node = buildRoute(contrib, key, frontends);
        if (!node) return;

        let group = groupsByLayout.get(layoutId);
        if (!group) {
            group = { layoutId, LayoutComponent, routes: [] };
            groupsByLayout.set(layoutId, group);
        }
        group.routes.push(node);
    });

    return {
        dashboardRoutes,
        groupRoutes,
        standaloneGroups: Array.from(groupsByLayout.values()),
    };
}
