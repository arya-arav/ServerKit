import { useState } from 'react';
import { usePolling } from '../hooks/usePolling';
import { useNavigate } from 'react-router-dom';
import { ShieldAlert, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import api from '../services/api';
import { useAuth } from '../contexts/useAuth.js';
import { Button as SharedButton } from '@/components/ui/button';

// Setup health as an icon in the app top bar (admins): green shield when
// everything is set, amber/red with a count when items are open. Opens the
// Doctor. Replaces the dashboard's "Setup complete" strip. Fork: arya-arav/ServerKit.
const REFRESH_MS = 10 * 60 * 1000;

const SetupHealthIndicator = () => {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const { user } = useAuth();
    const [summary, setSummary] = useState(null);
    const isAdmin = Boolean(user?.is_admin);

    usePolling(
        () => api.getSetupHealth().then((res) => setSummary(res?.summary || null)),
        REFRESH_MS,
        { enabled: isAdmin, onError: () => { /* stays as it was on error */ } },
    );

    if (!isAdmin || !summary) return null;

    const critical = Number(summary.critical_open || 0);
    const open = critical + Number(summary.recommended_open || 0);
    const tone = critical > 0 ? 'critical' : open > 0 ? 'warning' : 'ok';
    const label = open === 0
        ? t('app.setupHealthIndicator.complete', 'Setup complete: {{score}}% setup health', { score: summary.score })
        : t('app.setupHealthIndicator.open', 'Setup health {{score}}%: {{count}} item(s) to review', {
            score: summary.score, count: open,
        });

    return (
        <SharedButton variant="unstyled"
            type="button"
            className={`setup-indicator setup-indicator--${tone}`}
            onClick={() => navigate('/monitoring/doctor')}
            aria-label={label}
            title={label}
        >
            {open === 0
                ? <ShieldCheck size={16} aria-hidden="true" />
                : <ShieldAlert size={16} aria-hidden="true" />}
            {open > 0 && <span className="setup-indicator__badge" aria-hidden="true">{open}</span>}
        </SharedButton>
    );
};

export default SetupHealthIndicator;
