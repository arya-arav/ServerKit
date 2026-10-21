import { ChevronRight, ChevronDown, Folder, FolderOpen } from 'lucide-react';
import Spinner from '../Spinner';
import { useTranslation } from 'react-i18next';
import { Button as SharedButton } from '@/components/ui/button';

function TreeNode({ node, level, expanded, treeCache, treeLoading, currentPath, onNavigate, onToggle, dropTarget, dropProps }) {
    const { t } = useTranslation();
    const isExpanded = expanded.has(node.path);
    const isActive = currentPath === node.path;
    const isLoading = treeLoading.has(node.path);
    const children = treeCache.get(node.path);

    return (
        <div className="tree-node">
            <div
                className={`tree-row ${isActive ? 'active' : ''}${dropTarget === node.path ? ' drop-target' : ''}`}
                {...(dropProps ? dropProps(node.path) : {})}
                style={{ paddingLeft: 6 + level * 14 }}
                onClick={() => onNavigate(node.path)}
            >
                <SharedButton variant="unstyled" type="button"
                    className="tree-chevron"
                    onClick={(e) => { e.stopPropagation(); onToggle(node.path); }}
                    aria-label={isExpanded ? t('app.folderTree.collapse', 'Collapse') : t('app.folderTree.expand', 'Expand')}
                >
                    {isLoading ? (
                        <span className="tree-chevron-spinner"><Spinner size="sm" /></span>
                    ) : isExpanded ? (
                        <ChevronDown size={12} />
                    ) : (
                        <ChevronRight size={12} />
                    )}
                </SharedButton>
                {isExpanded ? (
                    <FolderOpen size={14} className="tree-folder-icon open" />
                ) : (
                    <Folder size={14} className="tree-folder-icon" fill="currentColor" fillOpacity={0.15} />
                )}
                <span className="tree-name" title={node.path}>{node.name}</span>
            </div>
            {isExpanded && children && children.length > 0 && (
                <div className="tree-children">
                    {children.map((child) => (
                        <TreeNode
                            key={child.path}
                            node={child}
                            level={level + 1}
                            expanded={expanded}
                            treeCache={treeCache}
                            treeLoading={treeLoading}
                            currentPath={currentPath}
                            onNavigate={onNavigate}
                            onToggle={onToggle}
                            dropTarget={dropTarget}
                            dropProps={dropProps}
                        />
                    ))}
                </div>
            )}
            {isExpanded && children && children.length === 0 && !isLoading && (
                <div className="tree-empty" style={{ paddingLeft: 26 + level * 14 }}>
                    {t('app.folderTree.empty', 'Empty')}
                </div>
            )}
        </div>
    );
}

export default function FolderTree({
    roots,
    expanded,
    treeCache,
    treeLoading,
    currentPath,
    onNavigate,
    onToggle,
    // Drag to move (fork): the folder being hovered, and drop handlers per path.
    dropTarget = null,
    dropProps = null,
}) {
    return (
        <div className="folder-tree">
            {roots.map((root) => (
                <TreeNode
                    key={root.path}
                    node={root}
                    level={0}
                    expanded={expanded}
                    treeCache={treeCache}
                    treeLoading={treeLoading}
                    currentPath={currentPath}
                    onNavigate={onNavigate}
                    onToggle={onToggle}
                    dropTarget={dropTarget}
                    dropProps={dropProps}
                />
            ))}
        </div>
    );
}
