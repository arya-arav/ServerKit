import { useState, useId, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import Modal from '@/components/Modal';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FormField } from '@/components/FormField';
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select';
import { Checkbox } from '@/components/ui/checkbox';
import { api } from '@/services/api';
import { ARCHIVE_FORMATS } from './archive';

// The File Manager's small one-field dialogs. Each owns its input value; the
// page only says which dialog is open and what to do with the answer. Mount
// with a `key` per target so reopening starts from a fresh value.

export function NamePromptModal({
    title, label, initialValue = '', placeholder, hint, submitLabel, onSubmit, onClose,
}) {
    const fieldId = useId();
    const { t } = useTranslation();
    const [value, setValue] = useState(initialValue);
    const submit = () => {
        if (value.trim()) onSubmit(value);
    };
    return (
        <Modal open onClose={onClose} title={title}>
            <FormField htmlFor={`${fieldId}-name`} label={label}>
                <Input
                    id={`${fieldId}-name`}
                    type="text"
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    placeholder={placeholder}
                    autoFocus
                    onKeyDown={(e) => e.key === 'Enter' && submit()}
                />
            </FormField>
            {hint}
            <div className="modal-actions">
                <Button variant="outline" onClick={onClose}>{t('common.actions.cancel', 'Cancel')}</Button>
                <Button onClick={submit}>{submitLabel}</Button>
            </div>
        </Modal>
    );
}

export function PermissionsModal({ entry, onSubmit, onClose }) {
    const fieldId = useId();
    const { t } = useTranslation();
    const [mode, setMode] = useState(String(entry.permissions_octal || '755').slice(-3));
    // Folders (fork): optionally apply inside, with files getting their own
    // mode so 755 on a tree doesn't make every file executable.
    const [recursive, setRecursive] = useState(false);
    const [fileMode, setFileMode] = useState('644');
    const valid = (m) => /^[0-7]{3}$/.test(m);
    const bits = valid(mode) ? parseInt(mode, 8) : 0;
    const toggle = (mask) => setMode((bits ^ mask).toString(8).padStart(3, '0'));
    const who = [
        { key: 'owner', label: t('app.fileManager.permOwner', 'Owner'), shift: 6 },
        { key: 'group', label: t('app.fileManager.permGroup', 'Group'), shift: 3 },
        { key: 'others', label: t('app.fileManager.permOthers', 'Others'), shift: 0 },
    ];
    const what = [
        { key: 'r', label: t('app.fileManager.permRead', 'Read'), bit: 4 },
        { key: 'w', label: t('app.fileManager.permWrite', 'Write'), bit: 2 },
        { key: 'x', label: t('app.fileManager.permExecute', 'Execute'), bit: 1 },
    ];
    const canSubmit = valid(mode) && (!recursive || valid(fileMode));
    const submit = () => {
        if (canSubmit) onSubmit({ mode, recursive, fileMode: recursive ? fileMode : null });
    };
    return (
        <Modal open onClose={onClose} title={t('app.fileManager.changePermissions', 'Change permissions')}>
            <div className="fm-perm-grid" role="group" aria-label={t('app.fileManager.changePermissions', 'Change permissions')}>
                <span />
                {what.map((w) => <span key={w.key} className="fm-perm-grid__head">{w.label}</span>)}
                {who.map((p) => (
                    <div key={p.key} className="fm-perm-grid__row">
                        <span className="fm-perm-grid__who">{p.label}</span>
                        {what.map((w) => {
                            const mask = w.bit << p.shift;
                            return (
                                <span key={w.key} className="fm-perm-grid__cell">
                                    <Checkbox
                                        checked={Boolean(bits & mask)}
                                        onCheckedChange={() => toggle(mask)}
                                        aria-label={`${p.label} ${w.label}`}
                                    />
                                </span>
                            );
                        })}
                    </div>
                ))}
            </div>
            <FormField htmlFor={`${fieldId}-mode`} label={t('app.fileManager.permissionsOctal', 'Permissions (octal)')}>
                <Input
                    id={`${fieldId}-mode`}
                    type="text"
                    value={mode}
                    onChange={(e) => setMode(e.target.value.replace(/[^0-7]/g, '').slice(0, 3))}
                    placeholder="755"
                    maxLength={3}
                    autoFocus
                    onKeyDown={(e) => e.key === 'Enter' && submit()}
                />
            </FormField>
            {entry.is_dir && (
                <>
                    <label className="fm-check">
                        <Checkbox checked={recursive} onCheckedChange={(v) => setRecursive(Boolean(v))} />
                        <span>{t('app.fileManager.applyInside', 'Also apply to everything inside')}</span>
                    </label>
                    {recursive && (
                        <FormField htmlFor={`${fieldId}-file-mode`} label={t('app.fileManager.filesInsideGet', 'Files inside get')}>
                            <Input
                                id={`${fieldId}-file-mode`}
                                type="text"
                                value={fileMode}
                                onChange={(e) => setFileMode(e.target.value.replace(/[^0-7]/g, '').slice(0, 3))}
                                placeholder="644"
                                maxLength={3}
                            />
                            <p className="text-muted">{t('app.fileManager.foldersInsideGet', 'Folders inside get {{mode}}. Links are skipped.', { mode })}</p>
                        </FormField>
                    )}
                </>
            )}
            <p className="text-muted">{t('app.fileManager.current', 'Current:')} {entry.permissions} ({entry.permissions_octal})</p>
            <div className="permissions-help">
                <p>{t('app.fileManager.commonValues', 'Common values:')}</p>
                <ul>
                    <li><code>755</code> {t('app.fileManager.ownerRwxGroupOtherRxDirectories', 'Owner: rwx, Group/Other: rx (directories)')}</li>
                    <li><code>644</code> {t('app.fileManager.ownerRwGroupOtherRFiles', 'Owner: rw, Group/Other: r (files)')}</li>
                    <li><code>600</code> {t('app.fileManager.ownerRwOnlyPrivateFiles', 'Owner: rw only (private files)')}</li>
                </ul>
            </div>
            <div className="modal-actions">
                <Button variant="outline" onClick={onClose}>{t('common.actions.cancel', 'Cancel')}</Button>
                <Button onClick={submit} disabled={!canSubmit}>{t('app.fileManager.apply', 'Apply')}</Button>
            </div>
        </Modal>
    );
}

// Change owner (fork, admins only): the server's users and groups.
export function OwnerModal({ entry, onSubmit, onClose }) {
    const fieldId = useId();
    const { t } = useTranslation();
    const [choices, setChoices] = useState(null);
    const [owner, setOwner] = useState(entry.owner || 'root');
    const [group, setGroup] = useState(entry.group || 'root');
    const [recursive, setRecursive] = useState(false);
    useEffect(() => {
        let cancelled = false;
        api.getFileOwners()
            .then((data) => { if (!cancelled) setChoices(data); })
            .catch(() => { if (!cancelled) setChoices({ users: [], groups: [] }); });
        return () => { cancelled = true; };
    }, []);
    // Keep the current owner selectable even if the list filters it out.
    const withCurrent = (list, current) => (current && !list.includes(current) ? [current, ...list] : list);
    const users = withCurrent(choices?.users || [], entry.owner);
    const groups = withCurrent(choices?.groups || [], entry.group);
    return (
        <Modal open onClose={onClose} title={t('app.fileManager.changeOwnerTitle', 'Change owner of {{name}}', { name: entry.name })}>
            <FormField htmlFor={`${fieldId}-owner`} label={t('app.fileManager.ownerUser', 'User')}>
                <Select value={owner} onValueChange={setOwner} disabled={!choices}>
                    <SelectTrigger id={`${fieldId}-owner`}><SelectValue /></SelectTrigger>
                    <SelectContent>
                        {users.map((u) => <SelectItem key={u} value={u}>{u}</SelectItem>)}
                    </SelectContent>
                </Select>
            </FormField>
            <FormField htmlFor={`${fieldId}-group`} label={t('app.fileManager.ownerGroup', 'Group')}>
                <Select value={group} onValueChange={setGroup} disabled={!choices}>
                    <SelectTrigger id={`${fieldId}-group`}><SelectValue /></SelectTrigger>
                    <SelectContent>
                        {groups.map((g) => <SelectItem key={g} value={g}>{g}</SelectItem>)}
                    </SelectContent>
                </Select>
            </FormField>
            {entry.is_dir && (
                <label className="fm-check">
                    <Checkbox checked={recursive} onCheckedChange={(v) => setRecursive(Boolean(v))} />
                    <span>{t('app.fileManager.applyInside', 'Also apply to everything inside')}</span>
                </label>
            )}
            <p className="text-muted">{t('app.fileManager.currentOwner', 'Current: {{owner}}:{{group}}', { owner: entry.owner, group: entry.group })}</p>
            <div className="modal-actions">
                <Button variant="outline" onClick={onClose}>{t('common.actions.cancel', 'Cancel')}</Button>
                <Button onClick={() => onSubmit({ owner, group, recursive })} disabled={!choices}>{t('app.fileManager.apply', 'Apply')}</Button>
            </div>
        </Modal>
    );
}

// Compress (fork): the archive's name and format. Submits the full file name.
export function CompressModal({ items, initialName, folder, onSubmit, onClose }) {
    const fieldId = useId();
    const { t } = useTranslation();
    const [name, setName] = useState(initialName);
    const [format, setFormat] = useState('zip');
    const ext = ARCHIVE_FORMATS.find((f) => f.id === format)?.ext || '.zip';
    const fileName = `${name.trim()}${ext}`;
    const submit = () => {
        if (name.trim()) onSubmit({ fileName, format });
    };
    return (
        <Modal open onClose={onClose} title={t('app.fileManager.compressTitle', { count: items, defaultValue_one: 'Compress 1 item', defaultValue_other: 'Compress {{count}} items' })}>
            <FormField htmlFor={`${fieldId}-name`} label={t('app.fileManager.archiveName', 'Archive name')}>
                <Input
                    id={`${fieldId}-name`}
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    autoFocus
                    onKeyDown={(e) => e.key === 'Enter' && submit()}
                />
            </FormField>
            <FormField htmlFor={`${fieldId}-format`} label={t('app.fileManager.archiveFormat', 'Format')}>
                <Select value={format} onValueChange={setFormat}>
                    <SelectTrigger id={`${fieldId}-format`}><SelectValue /></SelectTrigger>
                    <SelectContent>
                        {ARCHIVE_FORMATS.map((f) => (
                            <SelectItem key={f.id} value={f.id}>{f.ext}</SelectItem>
                        ))}
                    </SelectContent>
                </Select>
            </FormField>
            <p className="text-muted">{t('app.fileManager.willBeSavedAs', 'Will be saved as:')} <code>{folder.replace(/\/+$/, '')}/{fileName}</code></p>
            <div className="modal-actions">
                <Button variant="outline" onClick={onClose}>{t('common.actions.cancel', 'Cancel')}</Button>
                <Button onClick={submit}>{t('app.fileManager.compress', 'Compress')}</Button>
            </div>
        </Modal>
    );
}

// Extract (fork): where to unpack. Replacing existing files is asked later,
// only if the archive actually collides with something.
export function ExtractModal({ entry, initialDest, onSubmit, onClose }) {
    const fieldId = useId();
    const { t } = useTranslation();
    const [dest, setDest] = useState(initialDest);
    const submit = () => {
        if (dest.trim()) onSubmit(dest.trim());
    };
    return (
        <Modal open onClose={onClose} title={t('app.fileManager.extractTitle', 'Extract {{name}}', { name: entry.name })}>
            <FormField htmlFor={`${fieldId}-dest`} label={t('app.fileManager.extractTo', 'Extract to folder')}>
                <Input
                    id={`${fieldId}-dest`}
                    type="text"
                    value={dest}
                    onChange={(e) => setDest(e.target.value)}
                    autoFocus
                    onKeyDown={(e) => e.key === 'Enter' && submit()}
                />
            </FormField>
            <p className="text-muted">{t('app.fileManager.extractHint', "The folder is created if it doesn't exist. Links and unsafe paths in the archive are skipped or refused.")}</p>
            <div className="modal-actions">
                <Button variant="outline" onClick={onClose}>{t('common.actions.cancel', 'Cancel')}</Button>
                <Button onClick={submit}>{t('app.fileManager.extract', 'Extract')}</Button>
            </div>
        </Modal>
    );
}
