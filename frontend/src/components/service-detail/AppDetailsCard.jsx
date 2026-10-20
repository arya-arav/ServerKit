import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import api from '../../services/api';
import { InfoList, InfoItem } from '../InfoList';
import { Pill, statusKind } from '@/components/ds';
import { formatDate } from '@/utils/intl';

// Everything about how this service was installed and what runs it: the
// template and the options chosen (PHP version, …), each container's image,
// ports and PHP version, the folder on disk and the domains. The template
// options exist only in the app's own files, so nothing else on the page
// showed them. Secrets arrive masked from the API.
export default function AppDetailsCard({ app }) {
    const { t } = useTranslation();
    const [details, setDetails] = useState(null);

    useEffect(() => {
        let cancelled = false;
        api.getAppDetails(app.id)
            .then((data) => { if (!cancelled) setDetails(data); })
            .catch(() => { if (!cancelled) setDetails(null); });
        return () => { cancelled = true; };
    }, [app.id]);

    if (!details) return null;

    const { service, template, containers = [], domains = [] } = details;
    const day = (iso) => (iso ? formatDate(iso, { parts: { year: 'numeric', month: 'short', day: 'numeric' } }) : '—');

    return (
        <div className="overview-tab__card">
            <h3 className="overview-tab__card-title">{t('app.appDetails.title', 'App details')}</h3>
            <InfoList className="overview-tab__info-list">
                {template && (
                    <InfoItem label={t('app.appDetails.template', 'Template')}>
                        <span className="info-value">
                            {template.name || template.id}
                            {template.version && ` · v${template.version}`}
                            {template.installed_at && ` · ${t('app.appDetails.installed', 'installed {{date}}', { date: day(template.installed_at) })}`}
                        </span>
                    </InfoItem>
                )}
                {template && template.variables.map((v) => (
                    <InfoItem key={v.name} label={v.name}>
                        <span className="info-value"><code>{v.value || '—'}</code></span>
                    </InfoItem>
                ))}
                {containers.map((c) => (
                    <InfoItem key={c.name} label={t('app.appDetails.container', 'Container {{name}}', { name: c.name })}>
                        <span className="info-value">
                            <Pill kind={statusKind(c.state)}>{c.state || t('app.appDetails.unknown', 'unknown')}</Pill>
                            {' '}<code>{c.image || '—'}</code>
                            {c.php_version && ` · ${t('app.appDetails.php', 'PHP {{version}}', { version: c.php_version })}`}
                            {c.ports.length > 0 && ` · ${c.ports.join(', ')}`}
                        </span>
                    </InfoItem>
                ))}
                {domains.map((d) => (
                    <InfoItem key={d.name} label={d.primary ? t('app.appDetails.primaryDomain', 'Primary domain') : t('app.appDetails.domain', 'Domain')}>
                        <span className="info-value">
                            {d.name}
                            {' · '}
                            {d.ssl
                                ? t('app.appDetails.httpsUntil', 'HTTPS until {{date}}', { date: day(d.ssl_expires_at) })
                                : t('app.appDetails.noHttps', 'no HTTPS')}
                        </span>
                    </InfoItem>
                ))}
                {service.root_path && (
                    <InfoItem label={t('app.appDetails.folder', 'Folder on server')}>
                        <span className="info-value"><code>{service.root_path}</code></span>
                    </InfoItem>
                )}
                {service.compose_file && (
                    <InfoItem label={t('app.appDetails.composeFile', 'Compose file')}>
                        <span className="info-value"><code>{service.compose_file}</code></span>
                    </InfoItem>
                )}
                {(service.managed_by || service.source) && (
                    <InfoItem label={t('app.appDetails.managedBy', 'Managed by')}>
                        <span className="info-value">{[service.managed_by, service.source].filter(Boolean).join(' · ')}</span>
                    </InfoItem>
                )}
            </InfoList>
        </div>
    );
}
