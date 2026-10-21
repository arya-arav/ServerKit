import { useTranslation } from 'react-i18next';
import {
    Folder, Eye, Download, Edit3, Lock, Copy, Trash2, FileCode, Files, Scissors, ClipboardPaste,
} from 'lucide-react';
import { Button as SharedButton } from '@/components/ui/button';

export default function ContextMenu({
    menu,                  // { x, y, entry }
    selectionCount,        // total items selected
    onClose,
    onOpen,
    onEdit,                // open an editable file in the code editor (fork)
    onDownload,
    onRename,
    onPermissions,
    onCopyPath,
    onDelete,
    // Clipboard (fork): copy/cut the selection, paste into a folder.
    onCopy,
    onCut,
    onPasteInto,
}) {
    const { t } = useTranslation();
    if (!menu) return null;
    const { x, y, entry } = menu;
    const multi = selectionCount > 1;

    return (
        <div
            className="context-menu"
            style={{ top: y, left: x }}
            onClick={(e) => e.stopPropagation()}
        >
            <SharedButton variant="unstyled" type="button" onClick={() => { onOpen(entry); onClose(); }}>
                {entry.is_dir ? <Folder size={14} /> : <Eye size={14} />}
                {entry.is_dir ? t('common.actions.open', 'Open') : t('app.contextMenu.preview', 'Preview')}
            </SharedButton>
            {!entry.is_dir && entry.is_editable && onEdit && (
                <SharedButton variant="unstyled" type="button" onClick={() => { onEdit(entry); onClose(); }}>
                    <FileCode size={14} /> {t('app.contextMenu.edit', 'Edit')}
                </SharedButton>
            )}
            {!entry.is_dir && (
                <SharedButton variant="unstyled" type="button" onClick={() => { onDownload(entry); onClose(); }}>
                    <Download size={14} /> {t('common.actions.download', 'Download')}
                </SharedButton>
            )}
            {onCopy && (
                <>
                    <div className="context-menu-divider" />
                    <SharedButton variant="unstyled" type="button" onClick={() => { onCopy(); onClose(); }}>
                        <Files size={14} /> {t('app.contextMenu.copy', 'Copy')}
                    </SharedButton>
                    <SharedButton variant="unstyled" type="button" onClick={() => { onCut(); onClose(); }}>
                        <Scissors size={14} /> {t('app.contextMenu.cut', 'Cut')}
                    </SharedButton>
                    {entry.is_dir && onPasteInto && (
                        <SharedButton variant="unstyled" type="button" onClick={() => { onPasteInto(entry.path); onClose(); }}>
                            <ClipboardPaste size={14} /> {t('app.contextMenu.pasteInto', 'Paste into folder')}
                        </SharedButton>
                    )}
                    <div className="context-menu-divider" />
                </>
            )}
            <SharedButton variant="unstyled" type="button" onClick={() => { onRename(entry); onClose(); }}>
                <Edit3 size={14} /> {t('app.contextMenu.rename', 'Rename')}
            </SharedButton>
            <SharedButton variant="unstyled" type="button" onClick={() => { onPermissions(entry); onClose(); }}>
                <Lock size={14} /> {t('common.labels.permissions', 'Permissions')}
            </SharedButton>
            <SharedButton variant="unstyled" type="button" onClick={() => { onCopyPath(entry.path); onClose(); }}>
                <Copy size={14} /> {t('app.contextMenu.copyPath', 'Copy path')}
            </SharedButton>
            <div className="context-menu-divider" />
            <SharedButton variant="unstyled" type="button" className="danger" onClick={() => { onDelete(entry); onClose(); }}>
                <Trash2 size={14} /> {multi
                    ? t('app.contextMenu.deleteItems', { count: selectionCount, defaultValue_one: 'Delete 1 item', defaultValue_other: 'Delete {{count}} items' })
                    : t('common.actions.delete', 'Delete')}
            </SharedButton>
        </div>
    );
}
