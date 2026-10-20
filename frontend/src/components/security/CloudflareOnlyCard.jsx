import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import api from '../../services/api';
import { useToast } from '../../contexts/useToast.js';
import { useConfirm } from '@/hooks/useConfirm';
import useFormat from '@/hooks/useFormat';
import { InfoList, InfoItem } from '../InfoList';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Pill } from '@/components/ds';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { errorReason } from '@/utils/errorMessage';

// Cloudflare-only web traffic: ports 80/443 accept Cloudflare's edge and
// nobody else. Fork: arya-arav/ServerKit.

const LAYER_LABELS = {
    host_iptables: 'Host iptables (rules.v4)',
    ufw: 'ufw',
};

export default function CloudflareOnlyCard() {
    const { t } = useTranslation();
    const toast = useToast();
    const { confirm } = useConfirm();
    const { formatDateTime } = useFormat();
    const [info, setInfo] = useState(null);
    const [busy, setBusy] = useState(false);

    const load = useCallback(async () => {
        try {
            setInfo(await api.getCloudflareOnly());
        } catch (error) {
            console.error('Failed to load Cloudflare-only status:', error);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    const apply = async (enabled, force = false) => {
        setBusy(true);
        try {
            setInfo(await api.setCloudflareOnly(enabled, force));
            toast.success(enabled
                ? t('app.cloudflareOnly.turnedOn', 'Web ports now accept Cloudflare only')
                : t('app.cloudflareOnly.turnedOff', 'Web ports are open to everyone again'));
        } catch (error) {
            const blocked = error?.status === 409 ? error.data?.domains || [] : [];
            if (blocked.length) {
                setBusy(false);
                const go = await confirm({
                    title: t('app.cloudflareOnly.unproxiedTitle', 'Some sites skip Cloudflare'),
                    message: t('app.cloudflareOnly.unproxiedMessage',
                        'These domains point straight at the server and will stop loading: {{hosts}}. Turn on the orange cloud for them in Cloudflare first, or continue anyway.',
                        { hosts: blocked.map((d) => d.host).join(', ') }),
                    confirmText: t('app.cloudflareOnly.continueAnyway', 'Continue anyway'),
                    variant: 'danger',
                });
                if (go) await apply(true, true);
                return;
            }
            toast.error(t('app.cloudflareOnly.failed', "Couldn't change Cloudflare-only mode. {{message}}", { message: errorReason(error) }));
            await load();
        } finally {
            setBusy(false);
        }
    };

    const handleToggle = async (enabled) => {
        if (!enabled) {
            const go = await confirm({
                title: t('app.cloudflareOnly.disableTitle', 'Open web ports to everyone'),
                message: t('app.cloudflareOnly.disableMessage', 'Ports 80 and 443 will accept connections from any address, including visitors who bypass Cloudflare.'),
                confirmText: t('app.cloudflareOnly.disableConfirm', 'Open ports'),
                variant: 'danger',
            });
            if (!go) return;
        }
        await apply(enabled);
    };

    const handleRefresh = async () => {
        setBusy(true);
        try {
            setInfo(await api.refreshCloudflareOnly());
            toast.success(t('app.cloudflareOnly.refreshed', 'Cloudflare IP ranges updated'));
        } catch (error) {
            toast.error(t('app.cloudflareOnly.refreshFailed', "Couldn't update Cloudflare IP ranges. {{message}}", { message: errorReason(error) }));
        } finally {
            setBusy(false);
        }
    };

    if (!info) return null;

    const mode = info.mode || 'off';
    const modeLabel = {
        on: t('app.cloudflareOnly.modeOn', 'On'),
        partial: t('app.cloudflareOnly.modePartial', 'Partly on'),
        off: t('app.cloudflareOnly.modeOff', 'Off'),
        unsupported: t('app.cloudflareOnly.modeUnsupported', 'No firewall to manage'),
    }[mode];
    const legacy = Object.values(info.layers || {}).some((layer) => layer.legacy > 0);
    const unproxied = (info.domains || []).filter((d) => d.state === 'not_proxied');
    const fetchedAt = info.ranges?.fetched_at ? formatDateTime(info.ranges.fetched_at) : null;

    return (
        <Card variant="legacy" className="card">
            <CardHeader variant="legacy" className="card-header">
                <h3>{t('app.cloudflareOnly.title', 'Cloudflare-only web traffic')}</h3>
                <Pill kind={mode === 'on' ? 'green' : mode === 'partial' ? 'amber' : 'gray'}>{modeLabel}</Pill>
            </CardHeader>
            <CardContent variant="legacy" className="card-body">
                <InfoList>
                    <InfoItem label={t('app.cloudflareOnly.toggleLabel', 'Accept ports 80 and 443 from Cloudflare only')}>
                        <Switch
                            checked={info.setting === true || (info.setting == null && mode === 'on')}
                            onCheckedChange={handleToggle}
                            disabled={busy || mode === 'unsupported'}
                            aria-label={t('app.cloudflareOnly.toggleAria', 'Toggle Cloudflare-only web traffic')}
                        />
                    </InfoItem>
                    {Object.entries(info.layers || {}).map(([name, layer]) => (
                        <InfoItem
                            key={name}
                            label={LAYER_LABELS[name] || name}
                            value={t('app.cloudflareOnly.layerSummary', '{{managed}} Cloudflare rules, {{open}} open to everyone', {
                                managed: layer.managed + layer.legacy,
                                open: layer.open_to_all,
                            })}
                        />
                    ))}
                    <InfoItem label={t('app.cloudflareOnly.ranges', 'Cloudflare IP ranges')}>
                        <span>
                            {t('app.cloudflareOnly.rangeCount', '{{v4}} IPv4, {{v6}} IPv6', { v4: info.ranges?.v4, v6: info.ranges?.v6 })}
                            {' · '}
                            {fetchedAt
                                ? t('app.cloudflareOnly.fetchedAt', 'updated {{when}}', { when: fetchedAt })
                                : t('app.cloudflareOnly.bundled', 'built-in list')}
                        </span>
                    </InfoItem>
                    <InfoItem label={t('app.cloudflareOnly.realIp', 'Real visitor IPs in nginx')}
                        value={info.realip ? t('app.cloudflareOnly.yes', 'Yes') : t('app.cloudflareOnly.no', 'No')} />
                </InfoList>
                {unproxied.length > 0 && (
                    <p className="sec-hint">
                        {t('app.cloudflareOnly.unproxiedHint', 'Not proxied through Cloudflare: {{hosts}}', {
                            hosts: unproxied.map((d) => d.host).join(', '),
                        })}
                    </p>
                )}
                {legacy && info.setting == null && (
                    <p className="sec-hint">
                        {t('app.cloudflareOnly.legacyHint', 'Cloudflare rules were added by hand on this server. Turn the switch on to let ServerKit manage and update them.')}
                    </p>
                )}
                <p className="sec-hint">
                    {t('app.cloudflareOnly.hint', 'Visitors who find the server IP cannot bypass Cloudflare. The range list is refreshed weekly; SSH is never affected.')}
                </p>
                <Button variant="outline" size="sm" onClick={handleRefresh} disabled={busy}>
                    {t('app.cloudflareOnly.refresh', 'Update Cloudflare IPs now')}
                </Button>
            </CardContent>
        </Card>
    );
}
