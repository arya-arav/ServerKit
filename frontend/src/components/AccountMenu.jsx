import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Settings, LogOut, Sun, Moon, Monitor, ChevronRight, ChevronUp, ChevronDown, Palette, PanelLeft, PanelLeftClose, PanelTop } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/useAuth.js';
import { useTheme } from '../contexts/useTheme.js';
import { useLayout } from '../contexts/useLayout.js';
import { Button as SharedButton } from '@/components/ui/button';

// The account trigger (avatar, name) and its menu: theme, layout, settings,
// sign out. `placement="top"` sits in the app top bar and opens downward;
// `placement="sidebar"` sits in the sidebar footer and opens upward.
// Fork: arya-arav/ServerKit (moved out of Sidebar for the app top bar).
const AccountMenu = ({ placement = 'sidebar' }) => {
    const { t } = useTranslation();
    const { user, logout } = useAuth();
    const { theme, setTheme } = useTheme();
    const { layout, setLayout } = useLayout();
    const navigate = useNavigate();
    const [menuOpen, setMenuOpen] = useState(false);
    const menuRef = useRef(null);
    const top = placement === 'top';

    // Close on outside click or Escape; return focus to the trigger when
    // Escape dismisses it.
    useEffect(() => {
        if (!menuOpen) return undefined;
        const handleClickOutside = (e) => {
            if (menuRef.current && !menuRef.current.contains(e.target)) {
                setMenuOpen(false);
            }
        };
        const handleKeyDown = (e) => {
            if (e.key === 'Escape') {
                setMenuOpen(false);
                menuRef.current?.querySelector('.user-mini')?.focus();
            }
        };
        document.addEventListener('mousedown', handleClickOutside);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('mousedown', handleClickOutside);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [menuOpen]);

    const go = (path) => { navigate(path); setMenuOpen(false); };
    const Arrow = top ? ChevronDown : ChevronUp;
    const menuId = `user-context-menu-${placement}`;

    return (
        <div className={`account-menu account-menu--${placement}`} ref={menuRef}>
            {menuOpen && (
                <div className={`user-context-menu${top ? ' user-context-menu--down' : ''}`} id={menuId}
                    aria-label={t('nav.accountMenu', 'Account and settings')}>
                    <div className="context-menu-section">
                        <div className="context-menu-label" id={`${menuId}-theme`}>{t('nav.theme', 'Theme')}</div>
                        <div className="theme-switcher" role="group" aria-labelledby={`${menuId}-theme`}>
                            <SharedButton variant="unstyled"
                                type="button"
                                className={`theme-btn ${theme === 'dark' ? 'active' : ''}`}
                                onClick={() => setTheme('dark')}
                                aria-label={t('nav.themeDarkLong', 'Dark theme')}
                                aria-pressed={theme === 'dark'}
                                title={t('nav.themeDark', 'Dark')}
                            >
                                <Moon size={14} aria-hidden="true" />
                            </SharedButton>
                            <SharedButton variant="unstyled"
                                type="button"
                                className={`theme-btn ${theme === 'light' ? 'active' : ''}`}
                                onClick={() => setTheme('light')}
                                aria-label={t('nav.themeLightLong', 'Light theme')}
                                aria-pressed={theme === 'light'}
                                title={t('nav.themeLight', 'Light')}
                            >
                                <Sun size={14} aria-hidden="true" />
                            </SharedButton>
                            <SharedButton variant="unstyled"
                                type="button"
                                className={`theme-btn ${theme === 'system' ? 'active' : ''}`}
                                onClick={() => setTheme('system')}
                                aria-label={t('nav.themeSystemLong', 'System theme')}
                                aria-pressed={theme === 'system'}
                                title={t('nav.themeSystem', 'System')}
                            >
                                <Monitor size={14} aria-hidden="true" />
                            </SharedButton>
                        </div>
                    </div>
                    <div className="context-menu-section">
                        <div className="context-menu-label" id={`${menuId}-layout`}>{t('nav.layout', 'Layout')}</div>
                        <div className="theme-switcher" role="group" aria-labelledby={`${menuId}-layout`}>
                            <SharedButton variant="unstyled"
                                type="button"
                                className={`theme-btn ${layout === 'sidebar' ? 'active' : ''}`}
                                onClick={() => setLayout('sidebar')}
                                aria-label={t('nav.layoutSidebarLong', 'Sidebar layout')}
                                aria-pressed={layout === 'sidebar'}
                                title={t('nav.layoutSidebar', 'Sidebar')}
                            >
                                <PanelLeft size={14} aria-hidden="true" />
                            </SharedButton>
                            <SharedButton variant="unstyled"
                                type="button"
                                className={`theme-btn ${layout === 'rail' ? 'active' : ''}`}
                                onClick={() => setLayout('rail')}
                                aria-label={t('nav.layoutCompactLong', 'Compact rail layout')}
                                aria-pressed={layout === 'rail'}
                                title={t('nav.layoutCompact', 'Compact')}
                            >
                                <PanelLeftClose size={14} aria-hidden="true" />
                            </SharedButton>
                            <SharedButton variant="unstyled"
                                type="button"
                                className={`theme-btn ${layout === 'topbar' ? 'active' : ''}`}
                                onClick={() => setLayout('topbar')}
                                aria-label={t('nav.layoutTopbarLong', 'Top bar layout')}
                                aria-pressed={layout === 'topbar'}
                                title={t('nav.layoutTopbar', 'Top bar')}
                            >
                                <PanelTop size={14} aria-hidden="true" />
                            </SharedButton>
                        </div>
                    </div>
                    <div className="context-menu-divider" />
                    <SharedButton variant="unstyled" type="button" className="context-menu-item"
                        onClick={() => go('/settings/appearance')}>
                        <Palette size={15} aria-hidden="true" />
                        {t('nav.appearance', 'Appearance')}
                        <ChevronRight size={14} className="context-menu-arrow" aria-hidden="true" />
                    </SharedButton>
                    <SharedButton variant="unstyled" type="button" className="context-menu-item"
                        onClick={() => go('/settings/sidebar')}>
                        <PanelLeft size={15} aria-hidden="true" />
                        {t('nav.customizeSidebar', 'Customize sidebar')}
                        <ChevronRight size={14} className="context-menu-arrow" aria-hidden="true" />
                    </SharedButton>
                    <SharedButton variant="unstyled" type="button" className="context-menu-item"
                        onClick={() => go('/settings')}>
                        <Settings size={15} aria-hidden="true" />
                        {t('nav.allSettings', 'All settings')}
                        <ChevronRight size={14} className="context-menu-arrow" aria-hidden="true" />
                    </SharedButton>
                    <div className="context-menu-divider" />
                    <SharedButton variant="unstyled" type="button" className="context-menu-item danger" onClick={logout}>
                        <LogOut size={15} aria-hidden="true" />
                        {t('common.actions.logOut', 'Sign out')}
                    </SharedButton>
                </div>
            )}
            <SharedButton variant="unstyled"
                type="button"
                className="user-mini"
                onClick={() => setMenuOpen(!menuOpen)}
                aria-haspopup="true"
                aria-expanded={menuOpen}
                aria-controls={menuId}
            >
                <span className="user-avatar" aria-hidden="true">
                    {user?.username?.charAt(0).toUpperCase() || 'U'}
                </span>
                <span className="user-meta">
                    <span className="user-handle">{user?.username || t('nav.userFallback', 'User')}</span>
                    <span className="user-status">{t('nav.online', 'Online')}</span>
                </span>
                <Arrow size={14} className={`user-menu-arrow ${menuOpen ? 'open' : ''}`} aria-hidden="true" />
            </SharedButton>
        </div>
    );
};

export default AccountMenu;
