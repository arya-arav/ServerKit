import { useTranslation } from 'react-i18next';
import NotificationBell from './NotificationBell';
import AccountMenu from './AccountMenu';
import SetupHealthIndicator from './SetupHealthIndicator';

// Slim bar across the top of the page column (desktop sidebar and rail
// layouts): notifications and the account menu on the right. Hidden on mobile,
// where MobileTopBar takes the top edge, and in the top-bar layout, where the
// navigation itself is the top bar. Fork: arya-arav/ServerKit.
const AppTopBar = () => {
    const { t } = useTranslation();
    return (
        <header className="app-topbar" aria-label={t('nav.accountBar', 'Account and notifications')}>
            <div className="app-topbar__actions">
                <SetupHealthIndicator />
                <NotificationBell />
                <AccountMenu placement="top" />
            </div>
        </header>
    );
};

export default AppTopBar;
