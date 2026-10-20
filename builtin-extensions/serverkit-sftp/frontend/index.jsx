// SFTP Access: jailed, per-folder SFTP accounts (FileZilla / WinSCP) over the
// server's existing SSH port. Page + /api/v1/sftp backend both live in this
// extension; it contributes the /sftp tab to the core Files group.
import { Ban, Check, KeyRound, RefreshCw, ShieldCheck, Trash2, UserPlus } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';

import {
    api,
    Button,
    CopyField,
    DataTable,
    EmptyState,
    InfoItem,
    InfoList,
    Input,
    Label,
    LoadingState,
    Modal,
    Pill,
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
    Switch,
    Textarea,
    formatDateTime,
    useConfirm,
    useToast,
    useTopbarActions,
    useTranslation,
} from '../sdk';

import './styles/sftp-access.scss';

const CUSTOM = '__custom__';
const EMPTY_FORM = {
    username: '',
    target: '',
    customPath: '',
    generatePassword: true,
    password: '',
    publicKeys: '',
};

function messageOf(err, fallback) {
    return err?.message || fallback;
}

function SetupCard({ status, onSetup, busy }) {
    const { t } = useTranslation();
    const checks = status?.checks || {};
    const items = [
        ['acl_tools', t('sftpAccess.checkAcl', 'ACL tools installed (scoped write access)')],
        ['group', t('sftpAccess.checkGroup', 'sk-sftp group created')],
        ['jail_folder', t('sftpAccess.checkJails', 'Jail folder /srv/sftp-jails')],
        ['sshd_rule', t('sftpAccess.checkSshd', 'SSH rule for the sk-sftp group (validated with sshd -t)')],
    ];
    return (
        <div className="sftp-access__panel sftp-access__setup">
            <div className="sftp-access__panel-head">
                <h3>{t('sftpAccess.setupTitle', 'Set up SFTP access')}</h3>
            </div>
            <div className="sftp-access__panel-body">
                <p className="sftp-access__muted">
                    {t('sftpAccess.setupIntro', 'One-time preparation. Accounts are locked into one folder, have no shell, and connect over the SSH port that is already open. Your own SSH login is not changed.')}
                </p>
                <ul className="sftp-access__checks">
                    {items.map(([key, label]) => (
                        <li key={key}>
                            <Pill kind={checks[key] ? 'green' : 'gray'}>
                                {checks[key] ? t('sftpAccess.done', 'Done') : t('sftpAccess.pending', 'Pending')}
                            </Pill>
                            <span>{label}</span>
                        </li>
                    ))}
                </ul>
                <Button onClick={onSetup} disabled={busy}>
                    <ShieldCheck size={16} />
                    {busy ? t('sftpAccess.settingUp', 'Setting up…') : t('sftpAccess.runSetup', 'Set up SFTP')}
                </Button>
            </div>
        </div>
    );
}

function ConnectionCard({ status, onSaveHost }) {
    const { t } = useTranslation();
    const [editing, setEditing] = useState(false);
    const [host, setHost] = useState(status?.host || '');

    useEffect(() => { setHost(status?.host || ''); }, [status?.host]);

    return (
        <div className="sftp-access__panel sftp-access__connection">
            <div className="sftp-access__panel-head">
                <h3>{t('sftpAccess.connectionTitle', 'How to connect')}</h3>
            </div>
            <div className="sftp-access__panel-body">
                {status?.host && !editing ? (
                    <InfoList>
                        <InfoItem label={t('sftpAccess.protocol', 'Protocol')} value="SFTP" />
                        <InfoItem label={t('sftpAccess.host', 'Host')}>
                            <CopyField value={status.host} />
                        </InfoItem>
                        <InfoItem label={t('sftpAccess.port', 'Port')} value={status.port} mono />
                    </InfoList>
                ) : (
                    <div className="sftp-access__host-form">
                        <Label htmlFor="sftp-host">{t('sftpAccess.serverAddress', 'Server address (IP or a DNS-only hostname)')}</Label>
                        <Input
                            id="sftp-host"
                            value={host}
                            onChange={(e) => setHost(e.target.value)}
                            placeholder="203.0.113.10"
                        />
                        <p className="sftp-access__muted">
                            {t('sftpAccess.hostHint', 'A Cloudflare-proxied domain will not work for SFTP; use the server IP or a grey-cloud record.')}
                        </p>
                        <Button size="sm" onClick={async () => { await onSaveHost(host); setEditing(false); }}>
                            {t('common.actions.save', 'Save')}
                        </Button>
                    </div>
                )}
                {status?.host && !editing && (
                    <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
                        {t('sftpAccess.changeAddress', 'Change address')}
                    </Button>
                )}
            </div>
        </div>
    );
}

function CreateModal({ open, onClose, targets, onCreated }) {
    const { t } = useTranslation();
    const toast = useToast();
    const [form, setForm] = useState(EMPTY_FORM);
    const [saving, setSaving] = useState(false);

    useEffect(() => { if (open) setForm(EMPTY_FORM); }, [open]);

    const set = (key) => (value) => setForm((f) => ({ ...f, [key]: value }));
    const chosen = targets.find((tg) => String(tg.app_id) === form.target);
    const path = form.target === CUSTOM ? form.customPath.trim() : chosen?.path || '';

    const submit = async () => {
        setSaving(true);
        try {
            const account = await api.request('/sftp/accounts', {
                method: 'POST',
                body: {
                    username: form.username.trim(),
                    path,
                    app_id: chosen ? chosen.app_id : null,
                    generate_password: form.generatePassword,
                    password: form.generatePassword ? null : form.password,
                    public_keys: form.publicKeys,
                },
            });
            onCreated(account);
        } catch (err) {
            toast.error(messageOf(err, t('sftpAccess.createFailed', 'Could not create the account')));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal
            open={open}
            onClose={onClose}
            title={t('sftpAccess.newAccount', 'New SFTP account')}
            onSubmit={submit}
            footer={(
                <>
                    <Button type="button" variant="outline" onClick={onClose}>{t('common.actions.cancel', 'Cancel')}</Button>
                    <Button type="submit" disabled={saving || !form.username || !path}>
                        {saving ? t('sftpAccess.creating', 'Creating…') : t('sftpAccess.create', 'Create account')}
                    </Button>
                </>
            )}
        >
            <div className="sftp-access__field">
                <Label htmlFor="sftp-username">{t('sftpAccess.username', 'Username')}</Label>
                <Input id="sftp-username" value={form.username} autoComplete="off"
                    onChange={(e) => set('username')(e.target.value.toLowerCase())}
                    placeholder="shop-dev" />
            </div>
            <div className="sftp-access__field">
                <Label>{t('sftpAccess.folder', 'Folder')}</Label>
                <Select value={form.target} onValueChange={set('target')}>
                    <SelectTrigger><SelectValue placeholder={t('sftpAccess.chooseFolder', 'Choose an app or a folder')} /></SelectTrigger>
                    <SelectContent>
                        {targets.map((tg) => (
                            <SelectItem key={tg.app_id} value={String(tg.app_id)}>
                                {tg.name}{tg.domain ? ` (${tg.domain})` : ''}
                            </SelectItem>
                        ))}
                        <SelectItem value={CUSTOM}>{t('sftpAccess.customFolder', 'Another folder…')}</SelectItem>
                    </SelectContent>
                </Select>
                {chosen && <p className="sftp-access__muted sftp-access__mono">{chosen.path}</p>}
                {form.target === CUSTOM && (
                    <Input value={form.customPath} onChange={(e) => set('customPath')(e.target.value)}
                        placeholder="/srv/shop/htdocs/shop.example.com" className="sftp-access__mono" />
                )}
                <p className="sftp-access__muted">
                    {t('sftpAccess.folderHint', 'The account sees only this folder. Allowed: app folders and subfolders of /srv, /var/www or /home/<user>.')}
                </p>
            </div>
            <div className="sftp-access__field sftp-access__switch">
                <Switch checked={form.generatePassword} onCheckedChange={set('generatePassword')} id="sftp-genpw" />
                <Label htmlFor="sftp-genpw">{t('sftpAccess.generatePassword', 'Generate a strong password')}</Label>
            </div>
            {!form.generatePassword && (
                <div className="sftp-access__field">
                    <Label htmlFor="sftp-password">{t('sftpAccess.passwordOptional', 'Password (12+ characters, or leave empty for key-only)')}</Label>
                    <Input id="sftp-password" type="password" value={form.password} autoComplete="new-password"
                        onChange={(e) => set('password')(e.target.value)} />
                </div>
            )}
            <div className="sftp-access__field">
                <Label htmlFor="sftp-keys">{t('sftpAccess.publicKeys', 'Public keys (optional, one per line)')}</Label>
                <Textarea id="sftp-keys" rows={3} value={form.publicKeys} className="sftp-access__mono"
                    onChange={(e) => set('publicKeys')(e.target.value)} placeholder="ssh-ed25519 AAAA… you@laptop" />
            </div>
        </Modal>
    );
}

function CredentialsModal({ result, host, port, onClose }) {
    const { t } = useTranslation();
    if (!result) return null;
    return (
        <Modal
            open
            onClose={onClose}
            title={t('sftpAccess.credentialsTitle', 'Account ready')}
            footer={<Button onClick={onClose}>{t('sftpAccess.savedIt', 'I saved it')}</Button>}
        >
            <p className="sftp-access__muted">
                {t('sftpAccess.credentialsHint', 'Use these in FileZilla or WinSCP with protocol SFTP. The password is shown only now; save it in your password manager.')}
            </p>
            <CopyField label={t('sftpAccess.host', 'Host')} value={host || t('sftpAccess.setAddress', 'set the server address')} />
            <CopyField label={t('sftpAccess.port', 'Port')} value={String(port || 22)} />
            <CopyField label={t('sftpAccess.username', 'Username')} value={result.username} />
            {result.password && <CopyField label={t('sftpAccess.password', 'Password')} value={result.password} secret />}
        </Modal>
    );
}

function KeysModal({ account, onClose, onSaved }) {
    const { t } = useTranslation();
    const toast = useToast();
    const [text, setText] = useState('');
    const [saving, setSaving] = useState(false);

    useEffect(() => { setText((account?.keys || []).join('\n')); }, [account]);
    if (!account) return null;

    const save = async () => {
        setSaving(true);
        try {
            await api.request(`/sftp/accounts/${encodeURIComponent(account.username)}/keys`, {
                method: 'PUT', body: { public_keys: text },
            });
            toast.success(t('sftpAccess.keysSaved', 'Keys saved'));
            onSaved();
        } catch (err) {
            toast.error(messageOf(err, t('sftpAccess.keysFailed', 'Could not save the keys')));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal
            open
            onClose={onClose}
            title={t('sftpAccess.keysTitle', 'Public keys for {{name}}', { name: account.username })}
            onSubmit={save}
            footer={(
                <>
                    <Button type="button" variant="outline" onClick={onClose}>{t('common.actions.cancel', 'Cancel')}</Button>
                    <Button type="submit" disabled={saving}>{t('common.actions.save', 'Save')}</Button>
                </>
            )}
        >
            <Textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} className="sftp-access__mono"
                placeholder="ssh-ed25519 AAAA… you@laptop" />
        </Modal>
    );
}

function SftpAccess() {
    const { t } = useTranslation();
    const toast = useToast();
    const { confirm } = useConfirm();
    const [status, setStatus] = useState(null);
    const [accounts, setAccounts] = useState([]);
    const [targets, setTargets] = useState([]);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [creating, setCreating] = useState(false);
    const [credentials, setCredentials] = useState(null);
    const [keysFor, setKeysFor] = useState(null);

    const load = useCallback(async () => {
        try {
            const [st, acc, tg] = await Promise.all([
                api.request('/sftp/status'),
                api.request('/sftp/accounts'),
                api.request('/sftp/targets'),
            ]);
            setStatus(st);
            setAccounts(acc.accounts || []);
            setTargets(tg.targets || []);
        } catch (err) {
            toast.error(messageOf(err, t('sftpAccess.loadFailed', 'Could not load SFTP access')));
        } finally {
            setLoading(false);
        }
    }, [t, toast]);

    useEffect(() => { load(); }, [load]);

    const ready = Boolean(status?.ready);

    useTopbarActions(() => (ready ? (
        <>
            <Button variant="outline" onClick={load}><RefreshCw size={16} />{t('common.actions.refresh', 'Refresh')}</Button>
            <Button onClick={() => setCreating(true)}><UserPlus size={16} />{t('sftpAccess.newAccount', 'New SFTP account')}</Button>
        </>
    ) : null), [ready, load, t]);

    const runSetup = async () => {
        setBusy(true);
        try {
            setStatus(await api.request('/sftp/setup', { method: 'POST' }));
            toast.success(t('sftpAccess.setupDone', 'SFTP access is ready'));
        } catch (err) {
            toast.error(messageOf(err, t('sftpAccess.setupFailed', 'Setup failed')));
        } finally {
            setBusy(false);
        }
    };

    const saveHost = async (host) => {
        try {
            await api.request('/sftp/settings', { method: 'PUT', body: { host } });
            await load();
        } catch (err) {
            toast.error(messageOf(err, t('sftpAccess.hostFailed', 'Could not save the address')));
        }
    };

    const resetPassword = async (account) => {
        const ok = await confirm({
            title: t('sftpAccess.resetTitle', 'Reset password'),
            message: t('sftpAccess.resetMessage', 'Generate a new password for {{name}}? The old one stops working immediately.', { name: account.username }),
            variant: 'warning',
        });
        if (!ok) return;
        try {
            const res = await api.request(`/sftp/accounts/${encodeURIComponent(account.username)}/password`, { method: 'POST', body: {} });
            setCredentials(res);
            load();
        } catch (err) {
            toast.error(messageOf(err, t('sftpAccess.resetFailed', 'Could not reset the password')));
        }
    };

    const toggle = async (account) => {
        try {
            await api.request(`/sftp/accounts/${encodeURIComponent(account.username)}/toggle`, {
                method: 'POST', body: { enabled: !account.enabled },
            });
            toast.success(account.enabled ? t('sftpAccess.disabled', 'Account disabled') : t('sftpAccess.enabled', 'Account enabled'));
            load();
        } catch (err) {
            toast.error(messageOf(err, t('sftpAccess.toggleFailed', 'Could not update the account')));
        }
    };

    const remove = async (account) => {
        const ok = await confirm({
            title: t('sftpAccess.deleteTitle', 'Delete SFTP account'),
            message: t('sftpAccess.deleteMessage', 'Delete {{name}}? Its access is removed; the folder and its files stay, and files it uploaded are handed back to the folder owner.', { name: account.username }),
            variant: 'danger',
        });
        if (!ok) return;
        try {
            await api.request(`/sftp/accounts/${encodeURIComponent(account.username)}`, { method: 'DELETE' });
            toast.success(t('sftpAccess.deleted', 'Account deleted'));
            load();
        } catch (err) {
            toast.error(messageOf(err, t('sftpAccess.deleteFailed', 'Could not delete the account')));
        }
    };

    const columns = useMemo(() => [
        {
            key: 'username', header: t('sftpAccess.username', 'Username'), sortable: true, hideable: false,
            value: (a) => a.username,
            render: (a) => <span className="sftp-access__mono">{a.username}</span>,
        },
        {
            key: 'folder', header: t('sftpAccess.folder', 'Folder'), sortable: true,
            value: (a) => a.path,
            render: (a) => (
                <div className="sftp-access__folder">
                    {a.app_name && <strong>{a.app_name}{a.domain ? ` · ${a.domain}` : ''}</strong>}
                    <span className="sftp-access__mono sftp-access__muted">{a.path}</span>
                </div>
            ),
        },
        {
            key: 'login', header: t('sftpAccess.login', 'Login'), sortable: false,
            render: (a) => (
                <span className="sftp-access__pills">
                    {a.has_password && <Pill kind="cyan" dot={false}>{t('sftpAccess.password', 'Password')}</Pill>}
                    {a.keys?.length > 0 && <Pill kind="violet" dot={false}>{t('sftpAccess.keyCount', '{{count}} key', { count: a.keys.length })}</Pill>}
                </span>
            ),
        },
        {
            key: 'status', header: t('sftpAccess.status', 'Status'), sortable: true,
            value: (a) => (a.enabled && a.mounted ? 'active' : a.enabled ? 'unmounted' : 'disabled'),
            render: (a) => {
                if (!a.enabled) return <Pill kind="gray">{t('sftpAccess.statusDisabled', 'Disabled')}</Pill>;
                if (!a.mounted || !a.exists) return <Pill kind="amber">{t('sftpAccess.statusBroken', 'Needs attention')}</Pill>;
                return <Pill kind="green">{t('sftpAccess.statusActive', 'Active')}</Pill>;
            },
        },
        {
            key: 'last_login', header: t('sftpAccess.lastLogin', 'Last login'), sortable: true,
            value: (a) => a.last_login || '',
            render: (a) => (a.last_login ? formatDateTime(a.last_login) : <span className="sftp-access__muted">{t('sftpAccess.never', 'Never')}</span>),
        },
        {
            key: 'actions', header: '', sortable: false, hideable: false, className: 'text-right',
            render: (a) => (
                <div className="sftp-access__actions">
                    <Button variant="outline" size="sm" title={t('sftpAccess.resetTitle', 'Reset password')} onClick={() => resetPassword(a)}>
                        <RefreshCw size={14} />
                    </Button>
                    <Button variant="outline" size="sm" title={t('sftpAccess.editKeys', 'Public keys')} onClick={() => setKeysFor(a)}>
                        <KeyRound size={14} />
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => toggle(a)}
                        title={a.enabled ? t('common.actions.disable', 'Disable') : t('common.actions.enable', 'Enable')}>
                        {a.enabled ? <Ban size={14} /> : <Check size={14} />}
                    </Button>
                    <Button variant="destructive" size="sm" title={t('common.actions.delete', 'Delete')} onClick={() => remove(a)}>
                        <Trash2 size={14} />
                    </Button>
                </div>
            ),
        },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    ], [t]);

    if (loading) return <LoadingState />;

    return (
        <div className="sk-tabgroup__inner sftp-access">
            {!ready ? (
                <SetupCard status={status} onSetup={runSetup} busy={busy} />
            ) : (
                <>
                    <ConnectionCard status={status} onSaveHost={saveHost} />
                    <DataTable
                        columns={columns}
                        data={accounts}
                        keyField="username"
                        storageKey="serverkit-table-sftp-accounts"
                        emptyState={(
                            <EmptyState
                                icon={UserPlus}
                                title={t('sftpAccess.emptyTitle', 'No SFTP accounts yet')}
                                description={t('sftpAccess.emptyText', 'Create one to give FileZilla or WinSCP access to a single site folder.')}
                                action={<Button onClick={() => setCreating(true)}>{t('sftpAccess.newAccount', 'New SFTP account')}</Button>}
                            />
                        )}
                    />
                </>
            )}
            <CreateModal
                open={creating}
                onClose={() => setCreating(false)}
                targets={targets}
                onCreated={(account) => { setCreating(false); setCredentials(account); load(); }}
            />
            <CredentialsModal result={credentials} host={status?.host} port={status?.port} onClose={() => setCredentials(null)} />
            <KeysModal account={keysFor} onClose={() => setKeysFor(null)} onSaved={() => { setKeysFor(null); load(); }} />
        </div>
    );
}

export function SftpAccessPage() {
    return <SftpAccess />;
}
