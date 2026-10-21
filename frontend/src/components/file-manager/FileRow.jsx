import { memo } from 'react';
import { Check, Download, Edit3, Lock, Trash2 } from 'lucide-react';
import FileIcon from './FileIcon';
import { useTranslation } from 'react-i18next';
import { Button as SharedButton } from '@/components/ui/button';
import { formatDate } from '@/utils/intl';

function FileRow({
    entry,
    selected,
    selectMode,
    onOpen,
    onToggleSelect,
    onContext,
    // Drag to move (fork): rows drag when `draggable`; folders take drops.
    draggable = false,
    dropActive = false,
    onDragStartItem,
    onDragOverItem,
    onDragLeaveItem,
    onDropItem,
    onDownload,
    onRename,
    onPermissions,
    onDelete,
}) {
    const { t } = useTranslation();
    return (
        <div
            className={`file-item ${selected ? 'selected' : ''} ${selectMode ? 'select-mode' : ''}${dropActive ? ' drop-target' : ''}`}
            draggable={draggable}
            onDragStart={draggable ? (e) => onDragStartItem(e, entry) : undefined}
            {...(entry.is_dir && onDropItem ? {
                onDragOver: (e) => onDragOverItem(e, entry.path),
                onDragLeave: (e) => onDragLeaveItem(e, entry.path),
                onDrop: (e) => onDropItem(e, entry.path),
            } : {})}
            onClick={(e) => {
                if (e.ctrlKey || e.metaKey || e.shiftKey || selectMode) {
                    onToggleSelect(entry, e);
                } else {
                    onOpen(entry);
                }
            }}
            onDoubleClick={(e) => {
                e.stopPropagation();
                onOpen(entry);
            }}
            onContextMenu={(e) => onContext(e, entry)}
        >
            <span className="col-check" onClick={(e) => e.stopPropagation()}>
                <SharedButton variant="unstyled" type="button"
                    className="checkbox-btn"
                    onClick={(e) => onToggleSelect(entry, { ...e, ctrlKey: true })}
                    aria-label={selected ? t('app.fileRow.deselect', 'Deselect') : t('app.fileRow.select', 'Select')}
                >
                    <span className={`checkbox ${selected ? 'checked' : ''}`}>
                        {selected && <Check size={12} />}
                    </span>
                </SharedButton>
            </span>
            <span className="col-name">
                <FileIcon entry={entry} size={16} />
                <span className="file-item-name">{entry.name}</span>
                {entry.is_link && <span className="link-indicator">↗</span>}
            </span>
            <span className="col-size">{entry.is_dir ? '—' : entry.size_human}</span>
            <span className="col-modified">
                {formatDate(entry.modified)}
            </span>
            <span className="col-permissions">{entry.permissions}</span>
            <span className="col-owner">{entry.owner}</span>
            <span className="col-actions" onClick={(e) => e.stopPropagation()}>
                {!entry.is_dir && (
                    <SharedButton variant="unstyled" type="button" className="row-action" onClick={() => onDownload(entry)} title={t('common.actions.download', 'Download')}>
                        <Download size={14} />
                    </SharedButton>
                )}
                <SharedButton variant="unstyled" type="button" className="row-action" onClick={() => onRename(entry)} title={t('app.fileRow.rename', 'Rename')}>
                    <Edit3 size={14} />
                </SharedButton>
                <SharedButton variant="unstyled" type="button" className="row-action" onClick={() => onPermissions(entry)} title={t('common.labels.permissions', 'Permissions')}>
                    <Lock size={14} />
                </SharedButton>
                <SharedButton variant="unstyled" type="button" className="row-action danger" onClick={() => onDelete(entry)} title={t('common.actions.delete', 'Delete')}>
                    <Trash2 size={14} />
                </SharedButton>
            </span>
        </div>
    );
}

export default memo(FileRow);
