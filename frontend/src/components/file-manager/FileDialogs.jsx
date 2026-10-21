import { useState, useId } from 'react';
import { useTranslation } from 'react-i18next';
import Modal from '@/components/Modal';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select';
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
            <div className="form-group">
                <Label htmlFor={`${fieldId}-name`}>{label}</Label>
                <Input
                    id={`${fieldId}-name`}
                    type="text"
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    placeholder={placeholder}
                    autoFocus
                    onKeyDown={(e) => e.key === 'Enter' && submit()}
                />
            </div>
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
    const [mode, setMode] = useState(entry.permissions_octal || '755');
    const submit = () => {
        if (mode.trim()) onSubmit(mode);
    };
    return (
        <Modal open onClose={onClose} title={t('app.fileManager.changePermissions', 'Change permissions')}>
            <div className="form-group">
                <Label htmlFor={`${fieldId}-mode`}>{t('app.fileManager.permissionsOctal', 'Permissions (octal)')}</Label>
                <Input
                    id={`${fieldId}-mode`}
                    type="text"
                    value={mode}
                    onChange={(e) => setMode(e.target.value)}
                    placeholder="755"
                    maxLength={4}
                    autoFocus
                />
            </div>
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
                <Button onClick={submit}>{t('app.fileManager.apply', 'Apply')}</Button>
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
            <div className="form-group">
                <Label htmlFor={`${fieldId}-name`}>{t('app.fileManager.archiveName', 'Archive name')}</Label>
                <Input
                    id={`${fieldId}-name`}
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    autoFocus
                    onKeyDown={(e) => e.key === 'Enter' && submit()}
                />
            </div>
            <div className="form-group">
                <Label htmlFor={`${fieldId}-format`}>{t('app.fileManager.archiveFormat', 'Format')}</Label>
                <Select value={format} onValueChange={setFormat}>
                    <SelectTrigger id={`${fieldId}-format`}><SelectValue /></SelectTrigger>
                    <SelectContent>
                        {ARCHIVE_FORMATS.map((f) => (
                            <SelectItem key={f.id} value={f.id}>{f.ext}</SelectItem>
                        ))}
                    </SelectContent>
                </Select>
            </div>
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
            <div className="form-group">
                <Label htmlFor={`${fieldId}-dest`}>{t('app.fileManager.extractTo', 'Extract to folder')}</Label>
                <Input
                    id={`${fieldId}-dest`}
                    type="text"
                    value={dest}
                    onChange={(e) => setDest(e.target.value)}
                    autoFocus
                    onKeyDown={(e) => e.key === 'Enter' && submit()}
                />
            </div>
            <p className="text-muted">{t('app.fileManager.extractHint', "The folder is created if it doesn't exist. Links and unsafe paths in the archive are skipped or refused.")}</p>
            <div className="modal-actions">
                <Button variant="outline" onClick={onClose}>{t('common.actions.cancel', 'Cancel')}</Button>
                <Button onClick={submit}>{t('app.fileManager.extract', 'Extract')}</Button>
            </div>
        </Modal>
    );
}
