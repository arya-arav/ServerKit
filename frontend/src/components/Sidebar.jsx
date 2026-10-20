import React, { useState, useEffect, useRef, useMemo } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/useAuth.js';
import { useTheme } from '../contexts/useTheme.js';
import { useLayout } from '../contexts/useLayout.js';
import { Star, ChevronRight, Layers, PanelLeftClose, PanelLeftOpen, X, Server } from 'lucide-react';
import { api } from '../services/api';
import { SIDEBAR_CATEGORIES, SIDEBAR_CATEGORY_LABELS, getHiddenItemIds, getVisibleItems, applyWorkspaceNavPermissions } from './sidebarItems';
import { useTranslation } from 'react-i18next';
import useLabel from '../i18n/labels';
import { useContributions } from '../plugins/contributions';
import { sanitizeSvgInner } from '../utils/sanitizeSvg';
import useModules from '../hooks/useModules';
import useDevMode from '../hooks/useDevMode';
import { useManagedProfile } from '../contexts/useManagedProfile';
import { useWorkspace } from '../contexts/useWorkspace.js';
import { Button as SharedButton } from '@/components/ui/button';
import { useShortcut } from '../hooks/useShortcut';
import AccountMenu from './AccountMenu';

const Sidebar = ({ mobileOpen = false, isMobile = false, onMobileClose = () => {} }) => {
    const { t } = useTranslation();
    const label = useLabel();
    const { user, hasPermission } = useAuth();
    const { whiteLabel } = useTheme();
    const { layout, setLayout } = useLayout();
    const { activeWorkspace } = useWorkspace();
    const [wpInstalled, setWpInstalled] = useState(false);
    const [gpuAvailable, setGpuAvailable] = useState(false);
    const sidebarRef = useRef(null);

    // One-click switch between the full sidebar and the compact icon rail
    // (the same 'rail' layout the user menu offers). Desktop side layouts only:
    // the mobile drawer and the top bar have their own geometry.
    const collapsible = !isMobile && layout !== 'topbar';
    const collapsed = layout === 'rail';
    const toggleCollapsed = () => setLayout(collapsed ? 'sidebar' : 'rail');
    // Bottom-left of the sidebar; the account sits in the app top bar.
    const collapseToggle = collapsible && (
        <SharedButton variant="unstyled"
            type="button"
            className="sidebar-collapse-toggle"
            onClick={toggleCollapsed}
            aria-pressed={collapsed}
            aria-label={collapsed
                ? t('nav.expandSidebarLong', 'Expand the sidebar')
                : t('nav.collapseSidebarLong', 'Collapse the sidebar')}
            title={collapsed
                ? t('nav.expandSidebarHint', 'Expand (Ctrl+B)')
                : t('nav.collapseSidebarHint', 'Collapse (Ctrl+B)')}
        >
            {collapsed
                ? <PanelLeftOpen size={18} aria-hidden="true" />
                : <PanelLeftClose size={18} aria-hidden="true" />}
            <span className="sidebar-collapse-toggle__label">
                {collapsed ? t('nav.expandSidebar', 'Expand') : t('nav.collapseSidebar', 'Collapse')}
            </span>
        </SharedButton>
    );

    useShortcut({
        id: 'toggle-sidebar',
        label: t('nav.toggleSidebar', 'Collapse or expand the sidebar'),
        group: 'shell',
        keys: [{ key: 'b', ctrlOrMeta: true }],
        handler: () => {
            if (collapsible) toggleCollapsed();
        },
    });

    // When collapsed to a drawer and closed, take the whole subtree out of the
    // tab order and the accessibility tree. `inert` is set imperatively so it
    // works on React 18 (which doesn't forward the attribute).
    useEffect(() => {
        const el = sidebarRef.current;
        if (!el) return;
        el.toggleAttribute('inert', isMobile && !mobileOpen);
    }, [isMobile, mobileOpen]);

    // Open drawer: focus the first control, trap Tab, close on Escape, and
    // return focus to the menu toggle on close.
    useEffect(() => {
        if (!isMobile || !mobileOpen) return undefined;
        const el = sidebarRef.current;
        if (!el) return undefined;

        const getFocusable = () => Array.from(
            el.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
            )
        ).filter((node) => node.offsetParent !== null);

        const focusables = getFocusable();
        (focusables[0] || el).focus();

        const handleKeyDown = (e) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                onMobileClose();
                return;
            }
            if (e.key !== 'Tab') return;
            const items = getFocusable();
            if (items.length === 0) return;
            const first = items[0];
            const last = items[items.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        };

        el.addEventListener('keydown', handleKeyDown);
        return () => {
            el.removeEventListener('keydown', handleKeyDown);
            document.querySelector('.mobile-topbar__toggle')?.focus();
        };
    }, [isMobile, mobileOpen, onMobileClose]);

    const { nav: pluginNav, tabs: pluginTabs } = useContributions();

    // Runtime conditions are probed ONLY when a contributed nav item actually
    // asks for one. These two probes used to fire unconditionally on every app
    // load, and both endpoints belong to extensions — /gpu/ to serverkit-gpu,
    // /wordpress/standalone/status to serverkit-wordpress. With the extension
    // absent the route is not registered, so the call is a guaranteed 404 that
    // cannot change a single rendered pixel: the only manifest declaring
    // `requiresCondition: "gpuAvailable"` ships with the GPU extension itself,
    // so when it is gone nothing reads the answer.
    //
    // They were not free. In dev the API shares the browser's 6-connection
    // HTTP/1.1 pool with Vite's module graph, so two doomed requests hold a
    // third of the page's connection budget for their entire lifetime.
    const needsGpu = (pluginNav || []).some((i) => i?.requiresCondition === 'gpuAvailable');
    const needsWp = (pluginNav || []).some((i) => i?.requiresCondition === 'wpInstalled');

    useEffect(() => {
        if (!needsWp) { setWpInstalled(false); return; }
        api.getWordPressStatus()
            .then(data => setWpInstalled(!!data?.installed))
            .catch(() => setWpInstalled(false));
    }, [needsWp]);

    useEffect(() => {
        if (!needsGpu) { setGpuAvailable(false); return; }
        api.getGpuInfo()
            .then(data => setGpuAvailable(!!data?.available))
            .catch(() => setGpuAvailable(false));
    }, [needsGpu]);

    // Feature-module toggles (WordPress; Email is now an extension). Default to
    // enabled until the shared module state loads so items never flicker/hide.
    const { isEnabled: isModuleEnabled } = useModules();
    const wordpressEnabled = isModuleEnabled('wordpress');

    // Developer-only items (Test Sandbox) — same source the route guard reads.
    const devMode = useDevMode();

    // Sidebar items ServerKit Cloud holds under the managed profile.
    const { hiddenSidebarIds: managedHidden } = useManagedProfile();

    const conditions = { wpInstalled, gpuAvailable, wordpressEnabled, devMode };
    const [manualExpanded, setManualExpanded] = useState({});
    const [autoExpanded, setAutoExpanded] = useState(null);
    const location = useLocation();

    const toggleExpand = (itemId) => {
        const currentlyExpanded = manualExpanded[itemId] ?? (autoExpanded === itemId);
        setManualExpanded(prev => ({ ...prev, [itemId]: !currentlyExpanded }));
    };

    const visibleItems = useMemo(() => {
        const core = getVisibleItems(user?.sidebar_config);
        const hiddenIds = getHiddenItemIds(user?.sidebar_config);
        // Merge contributed nav items, dedup by id (core wins). Plugins
        // can claim a category; default to 'system' so they always land
        // somewhere visible.
        const existingIds = new Set(core.map((i) => i.id));
        const fromPlugins = (pluginNav || [])
            .filter((item) => (
                item && item.id && item.route
                && !existingIds.has(item.id)
                && !hiddenIds.has(item.id)
            ))
            .map((item) => ({
                ...item,
                category: item.category || 'system',
            }));
        // Top-level items can gate on a runtime condition (e.g. GPU Monitor
        // only when a GPU is present, the Email/WordPress modules being
        // enabled, or dev-only tooling), mirroring sub-item requiresCondition.
        const conds = { wpInstalled, gpuAvailable, wordpressEnabled, devMode };
        let items = [...core, ...fromPlugins].filter(
            (item) => !item.requiresCondition || conds[item.requiresCondition]
        );
        // Per-user feature permissions: a user whose files.read is revoked
        // (custom permissions override the role template) gets 403s from every
        // /api/v1/files endpoint, so don't surface the File Manager at all.
        if (!hasPermission('files', 'read')) {
            items = items.filter((item) => item.id !== 'files');
        }
        // Extension-contributed tab-group tabs (#43) keep the host group's
        // sidebar item lit on extension-owned tab routes (group id == sidebar
        // item id) — the core matchPrefixes only cover the group's own tabs.
        const tabPrefixes = {};
        for (const t of (pluginTabs || [])) {
            if (!t || !t.group || !t.to) continue;
            (tabPrefixes[t.group] = tabPrefixes[t.group] || []).push(t.to);
        }
        items = items.map((item) => (
            tabPrefixes[item.id]
                ? { ...item, matchPrefixes: [...(item.matchPrefixes || []), ...tabPrefixes[item.id]] }
                : item
        ));
        // Apply workspace-level nav permissions if an active workspace is set
        // and it defines a nav map. This lets a workspace restrict which sidebar
        // items its members see based on their effective workspace role.
        items = applyWorkspaceNavPermissions(items, activeWorkspace, user);
        // The managed profile: capabilities ServerKit Cloud holds
        // remove their sidebar items from every preset, the same way a
        // workspace nav map removes them.
        if (managedHidden.size > 0) {
            items = items.filter((item) => !managedHidden.has(item.id));
        }
        return items;
    }, [pluginNav, pluginTabs, wpInstalled, gpuAvailable, wordpressEnabled, devMode, user, hasPermission, activeWorkspace, managedHidden]);

    // Group visible items by category
    const groupedItems = useMemo(() => {
        const groups = {};
        for (const cat of SIDEBAR_CATEGORIES) {
            const items = visibleItems.filter(item => item.category === cat);
            if (items.length > 0) {
                groups[cat] = items;
            }
        }
        return groups;
    }, [visibleItems]);

    // Auto-expand the active parent (or parent of active sub-item), auto-close others
    useEffect(() => {
        const path = location.pathname;
        let activeParent = null;
        for (const item of visibleItems) {
            if (!item.subItems?.length) continue;
            // Expand if on the parent route itself or any sub-item route
            if (path === item.route || path.startsWith(item.route + '/') ||
                item.subItems.some(sub => path === sub.route || path.startsWith(sub.route + '/'))) {
                activeParent = item.id;
                break;
            }
        }
        setAutoExpanded(activeParent);
        setManualExpanded({});
    }, [location.pathname, visibleItems]);

    const renderNavItem = (item) => {
        const hasChildren = item.subItems && item.subItems.length > 0;
        // Show expanded if manually toggled OR auto-expanded by active route
        const isExpanded = manualExpanded[item.id] ?? (autoExpanded === item.id);
        const visibleSubs = hasChildren
            ? item.subItems.filter(sub => !sub.requiresCondition || conditions[sub.requiresCondition])
            : [];
        // Items can claim extra active paths (e.g. Servers stays lit across its
        // Agent Fleet / Fleet Monitor / Cloud / Config Templates tabs) so the
        // highlight doesn't drop when a sub-tab lives on its own route.
        const groupActive = item.matchPrefixes?.some(
            (p) => location.pathname === p || location.pathname.startsWith(p + '/')
        );

        return (
            <React.Fragment key={item.id}>
                <div className={`nav-item-row ${hasChildren ? 'has-children' : ''}`}>
                    <NavLink
                        to={item.route}
                        className={({ isActive }) => `nav-item ${isActive || groupActive ? 'active' : ''}`}
                        end={item.end || hasChildren}
                        title={collapsed && collapsible ? label(item) : undefined}
                    >
                        <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                            aria-hidden="true" focusable="false"
                            dangerouslySetInnerHTML={{ __html: sanitizeSvgInner(item.icon) }}
                        />
                        {label(item)}
                    </NavLink>
                    {visibleSubs.length > 0 && (
                        <SharedButton variant="unstyled"
                            type="button"
                            className={`nav-expand-btn ${isExpanded ? 'expanded' : ''}`}
                            aria-expanded={isExpanded}
                            aria-label={isExpanded
                                ? t('nav.collapseGroup', 'Collapse {{group}}', { group: label(item) })
                                : t('nav.expandGroup', 'Expand {{group}}', { group: label(item) })}
                            onClick={(e) => { e.stopPropagation(); toggleExpand(item.id); }}
                        >
                            <ChevronRight size={14} aria-hidden="true" />
                        </SharedButton>
                    )}
                </div>
                {isExpanded && visibleSubs.map(sub => (
                    <NavLink
                        key={sub.id}
                        to={sub.route}
                        className={({ isActive }) => `nav-item nav-sub-item ${isActive ? 'active' : ''}`}
                        title={collapsed && collapsible ? label(sub) : undefined}
                    >
                        <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                            aria-hidden="true" focusable="false"
                            dangerouslySetInnerHTML={{ __html: sanitizeSvgInner(sub.icon) }}
                        />
                        {label(sub)}
                    </NavLink>
                ))}
            </React.Fragment>
        );
    };

    return (
        <aside
            ref={sidebarRef}
            id="primary-navigation"
            className={`sidebar${mobileOpen ? ' sidebar--mobile-open' : ''}`}
            aria-label={t('nav.mainNavigation', 'Main navigation')}
        >
            {isMobile && (
                <SharedButton variant="unstyled"
                    type="button"
                    className="sidebar__close"
                    aria-label={t('nav.closeMenu', 'Close navigation menu')}
                    onClick={onMobileClose}
                >
                    <X size={20} aria-hidden="true" />
                </SharedButton>
            )}
            {whiteLabel.enabled ? (
                <div className="brand-section brand-section--custom">
                    {whiteLabel.mode === 'image_full' ? (
                        <div className="brand-custom-banner">
                            {whiteLabel.logoData ? (
                                <img src={whiteLabel.logoData} alt={whiteLabel.brandName || t('nav.brand', 'Brand')} />
                            ) : (
                                <Layers size={32} />
                            )}
                        </div>
                    ) : whiteLabel.mode === 'text_only' ? (
                        <span className="brand-custom-text">
                            {whiteLabel.brandName || t('nav.brandFallback', 'Brand')}
                        </span>
                    ) : (
                        <>
                            <div className="brand-custom-logo">
                                {whiteLabel.logoData ? (
                                    <img src={whiteLabel.logoData} alt={whiteLabel.brandName || t('nav.brand', 'Brand')} />
                                ) : (
                                    <Layers size={20} />
                                )}
                            </div>
                            <span className="brand-custom-text">
                                {whiteLabel.brandName || t('nav.brandFallback', 'Brand')}
                            </span>
                            </>
                        )}
                </div>
            ) : (
                <div className="brand-section">
                    <div className="brand-logo">
                        <Server size={19} strokeWidth={2} aria-hidden="true" />
                    </div>
                    <span className="brand-text">{t('common.labels.serverKit', 'ServerKit')}</span>
                    <a
                        href="https://github.com/jhd3197/ServerKit"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="brand-star"
                        aria-label={t('nav.starOnGithubLong', 'Star ServerKit on GitHub')}
                        title={t('nav.starOnGithub', 'Star on GitHub')}
                    >
                        <Star size={14} aria-hidden="true" />
                    </a>
                </div>
            )}

            <div className="nav-scroll">
                {SIDEBAR_CATEGORIES.map(cat => {
                    const items = groupedItems[cat];
                    if (!items) return null;
                    return (
                        <React.Fragment key={cat}>
                            <div className="nav-category">{label(SIDEBAR_CATEGORY_LABELS[cat])}</div>
                            <nav className="nav">
                                {items.map(renderNavItem)}
                            </nav>
                        </React.Fragment>
                    );
                })}
            </div>

            {devMode && (
                <>
                    <div className="nav-category nav-category--dev">{t('nav.devTools', 'Dev tools')}</div>
                    <nav className="nav">
                        {import.meta.env.DEV && (
                            <>
                                <NavLink
                                    to="/app-map"
                                    className={({ isActive }) => `nav-item nav-item--dev ${isActive ? 'active' : ''}`}
                                >
                                    <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                        <polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"/>
                                        <line x1="8" y1="2" x2="8" y2="18"/>
                                        <line x1="16" y1="6" x2="16" y2="22"/>
                                    </svg>
                                    {t('nav.appMap', 'App map')}
                                </NavLink>
                                <NavLink
                                    to="/documentation"
                                    className={({ isActive }) => `nav-item nav-item--dev ${isActive ? 'active' : ''}`}
                                >
                                    <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                                        <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                                    </svg>
                                    {t('nav.documentation', 'Documentation')}
                                </NavLink>
                                <NavLink
                                    to="/style-guide"
                                    className={({ isActive }) => `nav-item nav-item--dev ${isActive ? 'active' : ''}`}
                                >
                                    <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor">
                                        <circle cx="13.5" cy="6.5" r="2.5"/><path d="M17 2H7a5 5 0 0 0-5 5v10a5 5 0 0 0 5 5h10a5 5 0 0 0 5-5V7a5 5 0 0 0-5-5z"/><path d="M9.5 14.5l-3 3"/><path d="M14.5 9.5l3-3"/>
                                    </svg>
                                    {t('nav.styleGuide', 'Style guide')}
                                </NavLink>
                            </>
                        )}
                        {/* Test Sandbox is developer tooling, not an operator
                            surface: shown in dev builds and to admins with Site
                            Settings → Developer mode on — same useDevMode flag
                            the route guard reads. */}
                        <NavLink
                            to="/test-sandbox"
                            className={({ isActive }) => `nav-item nav-item--dev ${isActive ? 'active' : ''}`}
                        >
                            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127A2 2 0 0 1 14 9.527V2"/><path d="M8.5 2h7"/><path d="M7 16h10"/>
                            </svg>
                            {t('nav.testSandbox', 'Test sandbox')}
                        </NavLink>
                    </nav>
                </>
            )}

            <div className="sidebar-footer">
                {collapseToggle}
                {/* Phones and the top-bar layout have no app top bar, so the
                    account stays here for them. */}
                {(isMobile || layout === 'topbar') && (
                    <div className="sidebar-footer__row">
                        <AccountMenu placement="sidebar" />
                    </div>
                )}
            </div>
        </aside>
    );
};

export default Sidebar;
