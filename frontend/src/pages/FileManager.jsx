import { useState, useEffect, useRef, useMemo, useCallback, lazy, Suspense } from 'react';
import { api } from '../services/api';
import { useToast } from '../contexts/useToast.js';
import ConfirmDialog from '../components/ConfirmDialog';
import EmptyState from '../components/EmptyState';
import ErrorState from '../components/ErrorState';
import {
    Folder, FolderOpen, File, Upload, FolderPlus,
    ArrowLeft, ArrowRight, ArrowUp, Search, X, RefreshCw, Eye, EyeOff,
    Download, Edit3, Trash2, ChevronDown, ChevronRight,
    HardDrive, Clock, PanelLeftClose, PanelLeftOpen,
    LayoutGrid, List, Home, CloudUpload,
    Check, Copy, ArrowUpDown, Zap, Globe, Boxes, SlidersHorizontal, FileText,
    FolderTree as FolderTreeIcon, Terminal, Files, Scissors, ClipboardPaste, FilePlus, FolderUp,
    FileArchive,
} from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';

import FolderTree from '../components/file-manager/FolderTree';
import FileCard from '../components/file-manager/FileCard';
import FileRow from '../components/file-manager/FileRow';
import PreviewDrawer from '../components/file-manager/PreviewDrawer';
// CodeMirror loads only when a file is opened for editing (fork: arya-arav/ServerKit).
const EditorWorkspace = lazy(() => import('../components/file-manager/editor/EditorWorkspace'));
import ContextMenu from '../components/file-manager/ContextMenu';
import { NamePromptModal, PermissionsModal, CompressModal, ExtractModal } from '../components/file-manager/FileDialogs';
import {
    archiveStem, defaultArchiveName, compressInto, extractTo, downloadAsZip,
} from '../components/file-manager/archive';
import {
    INTERNAL_DRAG_TYPE, isInternalDrag, isExternalFileDrag, joinPath, parentOf, baseName,
    isInside, transferItems, readDraggedItems, collectDroppedFiles,
    moveInto, copyInto, uploadInto, createEmptyFile, listSubfolders,
} from '../components/file-manager/transfer';
import ServerPicker from '../components/ServerPicker';
import { serverTarget, targetServerId } from '../utils/serverTarget';
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select';
import { TREE_ROOTS, getFileType, formatBytes } from '../components/file-manager/fileTypes';
import { useClipboard } from '@/hooks/useClipboard';
import { useTranslation } from 'react-i18next';
import { useTopbarActions } from '@/hooks/useTopbarActions';
import { errorReason, toastError } from '@/utils/errorMessage';
import usePersistentState from '@/hooks/usePersistentState';
import { useStableHandler } from '@/hooks/useStableHandler';
import { formatTime } from '@/utils/intl';

// Demo rail shortcuts (Quick access) — one-click jumps to the paths people
// actually visit on a ServerKit host. "Stack" starts at the default install
// location and is re-pointed from /system/version's install_dir, so a custom
// SERVERKIT_DIR install jumps to the real tree.
const QUICK_ACCESS = [
    { labelKey: 'app.fileManager.sites', label: 'Sites', path: '/var/www', icon: Globe },
    { labelKey: 'app.fileManager.stack', label: 'Stack', path: '/opt/serverkit', icon: Boxes },
    { labelKey: 'app.fileManager.webConfig', label: 'Web config', path: '/etc/nginx', icon: SlidersHorizontal },
    { labelKey: 'common.labels.logs', label: 'Logs', path: '/var/log', icon: FileText },
];

// Remote-agent rails: the panel stack doesn't exist on an agent box, so link
// the agent's own footprint instead. These paths are fixed by the agent
// installers the panel serves (scripts/install.sh: /etc/serverkit-agent;
// scripts/install.ps1: ProgramData\ServerKit\Agent) — agents run on the wire
// with forward-slash paths, Windows included.
const QUICK_ACCESS_AGENT = {
    linux: [
        { labelKey: 'app.fileManager.sites', label: 'Sites', path: '/var/www', icon: Globe },
        { labelKey: 'app.fileManager.agent', label: 'Agent', path: '/etc/serverkit-agent', icon: Boxes },
        { labelKey: 'app.fileManager.webConfig', label: 'Web config', path: '/etc/nginx', icon: SlidersHorizontal },
        { labelKey: 'common.labels.logs', label: 'Logs', path: '/var/log', icon: FileText },
    ],
    windows: [
        { labelKey: 'app.fileManager.agent', label: 'Agent', path: 'C:/ProgramData/ServerKit/Agent', icon: Boxes },
        { labelKey: 'app.fileManager.agentLogs', label: 'Agent logs', path: 'C:/ProgramData/ServerKit/Agent/logs', icon: FileText },
        { labelKey: 'app.fileManager.users', label: 'Users', path: 'C:/Users', icon: Home },
    ],
    darwin: [
        { labelKey: 'app.fileManager.home', label: 'Home', path: '/Users', icon: Home },
        { labelKey: 'common.labels.logs', label: 'Logs', path: '/var/log', icon: FileText },
    ],
};

// File manager operations that the agent can serve over file:* commands.
// Anything else (mkdir, delete, rename, copy, chmod, search, disk usage,
// upload/download) is panel-host-only until the matching agent verbs land.
const REMOTE_SUPPORTED = new Set(['browse', 'read', 'write']);

// Operations the S3 target can't serve (no real directories, permissions, or
// in-place rename). Everything else — browse/read/write/delete/upload/download —
// works against the bucket.
const S3_BLOCKED = new Set(['create file', 'create folder', 'rename', 'change permissions']);

function deriveParent(path) {
    if (!path || path === '/' || path === '') return null;
    // A Windows drive root ("C:/" or "C:") sits directly under the agent's
    // drive list, which the agent serves for the "/" path. Remote agents
    // emit forward-slash paths on the wire, so we only ever see "/" here.
    if (/^[A-Za-z]:\/?$/.test(path)) return '/';
    const trimmed = path.replace(/\/+$/, '');
    const idx = trimmed.lastIndexOf('/');
    if (idx <= 0) return '/';
    const parent = trimmed.slice(0, idx);
    // Keep a bare drive letter as a drive root ("C:" -> "C:/") so navigating
    // up doesn't hit Windows' "current directory on C:" semantics.
    if (/^[A-Za-z]:$/.test(parent)) return parent + '/';
    return parent;
}

function unwrapAgentData(res) {
    // Remote endpoints return the agent payload directly
    // (RemoteFileService → _agent_result unwraps {data}). Defensive
    // unwrap covers both shapes for older agent responses.
    if (res && typeof res === 'object' && 'success' in res && 'data' in res) {
        return res.data;
    }
    return res;
}

function mountDisplayName(mount) {
    if (!mount?.mountpoint || mount.mountpoint === '/') return null;
    const parts = mount.mountpoint.split('/').filter(Boolean);
    const leaf = parts[parts.length - 1] || mount.mountpoint;
    return leaf === 'docker-desktop-user-distro' ? 'docker-desktop' : leaf;
}

const STORAGE = {
    sidebar: 'serverkit-fm-sidebar',
    treeCollapsed: 'serverkit-fm-tree-collapsed',
    expanded: 'serverkit-fm-tree-expanded',
    viewMode: 'serverkit-fm-view-mode',
    sortBy: 'serverkit-fm-sort-by',
    sortDir: 'serverkit-fm-sort-dir',
};

const serializeExpanded = (set) => JSON.stringify([...set]);
const parseExpanded = (stored) => {
    try {
        return stored ? new Set(JSON.parse(stored)) : new Set();
    } catch {
        return new Set();
    }
};
const isNarrowScreen = () => typeof window !== 'undefined'
    && window.matchMedia('(max-width: 1024px)').matches;

const FILTER_OPTIONS = [
    { id: 'all', labelKey: 'common.labels.all', label: 'All' },
    { id: 'folder', labelKey: 'app.fileManager.folders', label: 'Folders' },
    { id: 'image', labelKey: 'app.fileManager.images', label: 'Images' },
    { id: 'code', labelKey: 'app.fileManager.code', label: 'Code' },
    { id: 'text', labelKey: 'app.fileManager.documents', label: 'Documents' },
    { id: 'data', labelKey: 'app.fileManager.data', label: 'Data' },
    { id: 'video', labelKey: 'app.fileManager.videos', label: 'Videos' },
    { id: 'audio', labelKey: 'app.fileManager.audio', label: 'Audio' },
    { id: 'archive', labelKey: 'app.fileManager.archives', label: 'Archives' },
];

function FileManager() {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const [searchParams, setSearchParams] = useSearchParams();

    // ─── target ──────────────────────────────────────────
    // Local panel host by default. Switching to an agent re-routes the
    // browse/read/write verbs through /servers/<id>/files/* and disables
    // operations the agent can't serve yet.
    const [target, setTarget] = useState({ kind: 'local' });
    const isRemote = target.kind === 'agent';
    const isS3 = target.kind === 's3';
    const { copy: copyText } = useClipboard({
        successMessage: t('app.fileManager.pathCopied', 'Path copied'),
        errorMessage: t('app.fileManager.couldNotCopyPath', "Couldn't copy the path."),
    });
    const previousTargetRef = useRef({ kind: 'local', server_id: null });

    // The "S3 bucket" target is offered only when an S3-compatible backup
    // destination is configured (Connections → Storage, or the Backups page).
    const [s3Available, setS3Available] = useState(false);
    const s3Options = useMemo(
        () => (s3Available ? [{ value: 's3', label: t('app.fileManager.s3Bucket', 'S3 bucket') }] : []),
        [s3Available, t],
    );
    useEffect(() => {
        let cancelled = false;
        api.getStorageConfig()
            .then((c) => {
                if (cancelled) return;
                const p = c?.provider;
                setS3Available((p === 's3' || p === 'b2') && Boolean(c?.[p]?.bucket));
            })
            .catch(() => {});
        return () => { cancelled = true; };
    }, []);

    // The "Stack" quick link tracks the panel's real install dir (custom
    // SERVERKIT_DIR installs aren't at /opt/serverkit).
    const [panelInstallDir, setPanelInstallDir] = useState('/opt/serverkit');
    useEffect(() => {
        let cancelled = false;
        api.getVersion()
            .then((v) => {
                if (!cancelled && v?.install_dir) setPanelInstallDir(v.install_dir);
            })
            .catch(() => {});
        return () => { cancelled = true; };
    }, []);

    // Per-target rail: agents get their OS's set (unknown os_type — an agent
    // predating sysinfo reporting — is treated as linux, the historical
    // behavior), with the agent's SELF-REPORTED config dir preferred over the
    // installer convention when the agent sent one (system_info footprint);
    // the local host gets the panel set with the resolved Stack dir.
    const quickAccess = useMemo(() => {
        if (isRemote) {
            const os = (target.os_type || 'linux').toLowerCase();
            const rail = QUICK_ACCESS_AGENT[os] || QUICK_ACCESS_AGENT.linux;
            // Windows agents report filepath-style backslashes; the file
            // wire protocol speaks forward slashes.
            const configDir = target.agentConfigDir
                ? target.agentConfigDir.replace(/\\/g, '/').replace(/\/+$/, '')
                : null;
            if (!configDir) return rail;
            return rail.map((q) => {
                if (q.label === 'Agent') return { ...q, path: configDir };
                if (q.label === 'Agent logs') return { ...q, path: `${configDir}/logs` };
                return q;
            });
        }
        return QUICK_ACCESS.map((q) =>
            q.label === 'Stack' ? { ...q, path: panelInstallDir } : q
        );
    }, [isRemote, target.os_type, target.agentConfigDir, panelInstallDir]);

    // ─── core ────────────────────────────────────────────
    const [currentPath, setCurrentPath] = useState(() => searchParams.get('path') || '/home');
    const [entries, setEntries] = useState([]);
    const [parentPath, setParentPath] = useState(null);
    const [loading, setLoading] = useState(true);
    // The open folder's own load failure, shown in the listing in place of
    // "This folder is empty". Navigation into a folder that fails still
    // toasts and steps back, since the previous folder is what stays shown.
    const [dirError, setDirError] = useState(null);
    const [showHidden, setShowHidden] = useState(false);

    // ─── search ──────────────────────────────────────────
    const [searchQuery, setSearchQuery] = useState('');
    const [searchResults, setSearchResults] = useState(null);

    // ─── selection ───────────────────────────────────────
    const [selectedPaths, setSelectedPaths] = useState(new Set());
    const [lastClickedPath, setLastClickedPath] = useState(null);
    const [selectMode, setSelectMode] = useState(false);

    // ─── preview ─────────────────────────────────────────
    const [previewFile, setPreviewFile] = useState(null);
    const [fileContent, setFileContent] = useState('');

    // ─── code editor (fork: arya-arav/ServerKit) ─────────
    // Open files as tabs: { path, name, content (as saved), draft, version, readOnly }.
    const [editorTabs, setEditorTabs] = useState([]);
    const [editorActive, setEditorActive] = useState(null);
    const [editorVisible, setEditorVisible] = useState(false);
    const [savingPaths, setSavingPaths] = useState(new Set());
    const editorTabsRef = useRef(editorTabs);
    editorTabsRef.current = editorTabs;

    // ─── modals ──────────────────────────────────────────
    // { kind: 'newFolder' } | { kind: 'rename', entry } | { kind: 'permissions', entry } | null
    const [dialog, setDialog] = useState(null);
    const closeDialog = () => setDialog(null);
    const [confirmDialog, setConfirmDialog] = useState(null);

    // ─── upload ──────────────────────────────────────────
    const [uploads, setUploads] = useState([]);
    const [dragActive, setDragActive] = useState(false);
    const fileInputRef = useRef(null);
    const folderInputRef = useRef(null);
    const dragCounter = useRef(0);

    // ─── move / copy (fork) ──────────────────────────────
    // { mode: 'copy' | 'cut', items: [{ path, name, is_dir }] }
    const [clipboard, setClipboard] = useState(null);
    // The folder a dragged item is hovering over.
    const [dropTarget, setDropTarget] = useState(null);

    // ─── view prefs ──────────────────────────────────────
    const [viewMode, setViewMode] = usePersistentState(STORAGE.viewMode, (v) => v || 'list');
    const gridSize = 'md';
    const [sortBy, setSortBy] = usePersistentState(STORAGE.sortBy, (v) => v || 'name');
    const [sortDir, setSortDir] = usePersistentState(STORAGE.sortDir, (v) => v || 'asc');
    const [activeFilter, setActiveFilter] = useState('all');

    // ─── left sidebar ────────────────────────────────────
    const [sidebarVisible, setSidebarVisible] = usePersistentState(
        STORAGE.sidebar, (v) => (isNarrowScreen() ? false : v !== null ? v === 'true' : true));
    const [treeCollapsed, setTreeCollapsed] = usePersistentState(STORAGE.treeCollapsed, (v) => v === 'true');

    // ─── folder tree state ───────────────────────────────
    const [treeExpanded, setTreeExpanded] = usePersistentState(STORAGE.expanded, parseExpanded, serializeExpanded);
    const [treeCache, setTreeCache] = useState(new Map());
    const [treeLoading, setTreeLoading] = useState(new Set());

    // ─── disk ────────────────────────────────────────────
    const [diskMounts, setDiskMounts] = useState([]);
    const [diskLastUpdated, setDiskLastUpdated] = useState(null);
    const [diskLoading, setDiskLoading] = useState(false);

    // ─── history ─────────────────────────────────────────
    const [history, setHistory] = useState(['/home']);
    const [historyIdx, setHistoryIdx] = useState(0);
    const navByHistory = useRef(false);
    const lastValidPathRef = useRef('/home');
    // Only the newest browse may write: a slow answer for the folder the
    // user just left used to land last and navigate them back to it.
    const loadSeqRef = useRef(0);

    // ─── context menu ────────────────────────────────────
    const [contextMenu, setContextMenu] = useState(null);

    const toast = useToast();

    useEffect(() => {
        const pathFromUrl = searchParams.get('path') || '/home';
        if (pathFromUrl !== currentPath) {
            setCurrentPath(pathFromUrl);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [searchParams]);

    useEffect(() => {
        const pathFromUrl = searchParams.get('path') || '/home';
        if (pathFromUrl === currentPath) return;

        const nextParams = new URLSearchParams(searchParams);
        nextParams.set('path', currentPath);
        setSearchParams(nextParams, { replace: true });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [currentPath]);

    // ─── file API adapter ────────────────────────────────
    // Routes the three verbs the agent supports through the remote
    // endpoints when target is an agent; falls back to the panel-local
    // FileService otherwise. Other ops keep going to panel-local — the
    // remoteGuard helper short-circuits them with a toast when the user
    // is on a remote target so we don't accidentally write to the panel
    // host while they think they're editing a remote server.
    const fileApi = useMemo(() => ({
        browse: async (path, hidden) => {
            if (isS3) return api.browseS3(path);
            if (isRemote) {
                return unwrapAgentData(await api.browseRemoteFiles(target.server_id, path));
            }
            return api.browseFiles(path, hidden);
        },
        read: async (path) => {
            if (isS3) return api.readS3(path);
            if (isRemote) {
                return unwrapAgentData(await api.readRemoteFile(target.server_id, path));
            }
            return api.readFile(path);
        },
        write: async (path, content, expectedVersion = null) => {
            if (isS3) return api.writeS3(path, content);
            if (isRemote) {
                return unwrapAgentData(await api.writeRemoteFile(target.server_id, path, content));
            }
            return api.writeFile(path, content, true, expectedVersion);
        },
        del: async (path) => (isS3 ? api.deleteS3(path) : api.deleteFile(path)),
        download: (entry) => (isS3 ? api.downloadS3File(entry.path) : api.downloadFile(entry.path)),
    }), [isRemote, isS3, target]);

    const remoteGuard = useCallback((op) => {
        if (isRemote && !REMOTE_SUPPORTED.has(op)) {
            toast.error(t('app.fileManager.isNotYetSupportedOnRemote', '{{op}} is not yet supported on remote agents', { op: op }));
            return true;
        }
        if (isS3 && S3_BLOCKED.has(op)) {
            toast.error(t('app.fileManager.isnTAvailableOnS3Buckets', '{{op}} isn\'t available on S3 buckets', { op: op }));
            return true;
        }
        return false;
    }, [isRemote, isS3, t, toast]);

    // When the user switches to a remote target, jump to its first
    // advertised allowed_path so they don't see a "panel /home" view
    // that doesn't exist on the remote host.
    useEffect(() => {
        const previousTarget = previousTargetRef.current;
        // `?? null`: the local target has no server_id (undefined) while the
        // initial ref holds null; comparing them raw made every fresh load
        // "change target" and drop the ?path= deep link for /home (fork).
        const targetChanged = previousTarget.kind !== target.kind
            || (previousTarget.server_id ?? null) !== (target.server_id ?? null);
        previousTargetRef.current = { kind: target.kind, server_id: target.server_id };

        if (!targetChanged) return;

        if (target.kind === 'agent' && Array.isArray(target.allowedPaths) && target.allowedPaths.length > 0) {
            setCurrentPath(target.allowedPaths[0]);
        } else if (target.kind === 's3') {
            setCurrentPath('/');
        } else if (target.kind === 'local') {
            setCurrentPath('/home');
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [target.kind, target.server_id]);

    // ─── load directory ──────────────────────────────────
    const loadDirectory = useCallback(async (path) => {
        const seq = ++loadSeqRef.current;
        setLoading(true);
        setSearchResults(null);
        setSelectedPaths(new Set());
        setSelectMode(false);
        try {
            const data = await fileApi.browse(path, showHidden);
            if (seq !== loadSeqRef.current) return;
            // Agent file:list returns {path, files: [...]} with a flat
            // entry shape; panel browseFiles returns {path, parent,
            // entries: [...]}. Normalize so the UI can keep using
            // entries/parent.
            const entriesList = data.entries || data.files || [];
            setEntries(entriesList);
            setParentPath(data.parent ?? deriveParent(data.path || path));
            setCurrentPath(data.path || path);
            lastValidPathRef.current = data.path || path;
            setDirError(null);
        } catch (error) {
            if (seq !== loadSeqRef.current) return;
            if (path !== lastValidPathRef.current) {
                toast.error(t('app.fileManager.failedToLoadDirectory', "Couldn't load the folder. {{message}}", { message: errorReason(error) }));
                setCurrentPath(lastValidPathRef.current);
            } else {
                setEntries([]);
                setDirError(error);
            }
        } finally {
            if (seq === loadSeqRef.current) setLoading(false);
        }
    }, [fileApi, showHidden, toast, t]);

    useEffect(() => {
        loadDirectory(currentPath);
    // fileApi, not loadDirectory: switching target must reload even when the
    // path stays the same, but loadDirectory also changes with toast/t.
    }, [currentPath, showHidden, fileApi]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        loadDiskMounts();
    }, []);

    // history tracking
    useEffect(() => {
        if (navByHistory.current) {
            navByHistory.current = false;
            return;
        }
        setHistory((h) => {
            const trimmed = h.slice(0, historyIdx + 1);
            if (trimmed[trimmed.length - 1] === currentPath) return h;
            return [...trimmed, currentPath];
        });
        setHistoryIdx((i) => (history[i] === currentPath ? i : i + 1));
    }, [currentPath]); // eslint-disable-line react-hooks/exhaustive-deps

    // ─── disk mounts ─────────────────────────────────────
    const loadDiskMounts = async () => {
        setDiskLoading(true);
        try {
            const data = await api.getAllDiskMounts();
            setDiskMounts(data.mounts || []);
            setDiskLastUpdated(new Date());
        } catch (e) {
            console.error('Failed to load disk mounts:', e);
        } finally {
            setDiskLoading(false);
        }
    };

    // ─── tree expand/collapse ────────────────────────────
    const toggleTreeExpand = useCallback(async (path) => {
        if (treeExpanded.has(path)) {
            const next = new Set(treeExpanded);
            next.delete(path);
            setTreeExpanded(next);
            return;
        }
        if (!treeCache.has(path)) {
            setTreeLoading((s) => { const n = new Set(s); n.add(path); return n; });
            try {
                const data = await fileApi.browse(path, false);
                const entries = data.entries || data.files || [];
                const folders = entries.filter((e) => e.is_dir).map((e) => ({
                    path: e.path,
                    name: e.name,
                }));
                setTreeCache((c) => { const n = new Map(c); n.set(path, folders); return n; });
            } catch {
                setTreeCache((c) => { const n = new Map(c); n.set(path, []); return n; });
            } finally {
                setTreeLoading((s) => { const n = new Set(s); n.delete(path); return n; });
            }
        }
        setTreeExpanded((s) => { const n = new Set(s); n.add(path); return n; });
    }, [treeExpanded, treeCache, fileApi, setTreeExpanded]);

    // Auto-expand the tree along the current path so the active row is visible.
    useEffect(() => {
        const parts = currentPath.split('/').filter(Boolean);
        const ancestors = [];
        let acc = '';
        for (const p of parts) {
            acc += '/' + p;
            ancestors.push(acc);
        }
        ancestors.forEach((a) => {
            const isUnderRoot = TREE_ROOTS.some((r) => a === r.path || a.startsWith(r.path + '/') || r.path.startsWith(a + '/'));
            if (isUnderRoot && !treeExpanded.has(a) && a !== currentPath) {
                toggleTreeExpand(a);
            }
        });
    }, [currentPath]); // eslint-disable-line react-hooks/exhaustive-deps

    // ─── search ──────────────────────────────────────────
    const handleSearch = async () => {
        if (remoteGuard('search')) { setSearchResults([]); return; }
        if (!searchQuery.trim()) { setSearchResults(null); return; }
        setLoading(true);
        try {
            const data = await api.searchFiles(currentPath, searchQuery);
            setSearchResults(data.results || []);
        } catch (error) {
            toast.error(t('app.fileManager.searchFailed', "Couldn't search. {{message}}", { message: errorReason(error) }));
        } finally {
            setLoading(false);
        }
    };

    // ─── navigation ──────────────────────────────────────
    const navigateTo = (path) => {
        setPreviewFile(null);
        setCurrentPath(path);
    };

    const goBack = () => {
        if (historyIdx > 0) {
            navByHistory.current = true;
            setHistoryIdx(historyIdx - 1);
            setCurrentPath(history[historyIdx - 1]);
        }
    };
    const goForward = () => {
        if (historyIdx < history.length - 1) {
            navByHistory.current = true;
            setHistoryIdx(historyIdx + 1);
            setCurrentPath(history[historyIdx + 1]);
        }
    };
    const goUp = () => parentPath && navigateTo(parentPath);

    const handleOpen = async (entry) => {
        if (entry.is_dir) {
            navigateTo(entry.path);
        } else {
            setPreviewFile(entry);
            if (entry.is_editable) {
                try {
                    const data = await fileApi.read(entry.path);
                    setFileContent(data.content);
                } catch (error) {
                    toast.error(t('app.fileManager.failedToReadFile', "Couldn't read the file. {{message}}", { message: errorReason(error) }));
                }
            }
        }
    };

    // ─── selection ───────────────────────────────────────
    const handleToggleSelect = (entry, e) => {
        const path = entry.path;
        if (e?.shiftKey && lastClickedPath) {
            const list = sortedFiltered;
            const a = list.findIndex((x) => x.path === lastClickedPath);
            const b = list.findIndex((x) => x.path === path);
            if (a >= 0 && b >= 0) {
                const [from, to] = [Math.min(a, b), Math.max(a, b)];
                const rangePaths = list.slice(from, to + 1).map((x) => x.path);
                const next = new Set([...selectedPaths, ...rangePaths]);
                setSelectedPaths(next);
                setSelectMode(next.size > 0);
            }
        } else {
            const next = new Set(selectedPaths);
            if (next.has(path)) next.delete(path); else next.add(path);
            setSelectedPaths(next);
            setSelectMode(next.size > 0);
            setLastClickedPath(path);
        }
    };

    const clearSelection = () => {
        setSelectedPaths(new Set());
        setSelectMode(false);
    };

    // ─── code editor ─────────────────────────────────────
    const openInEditor = async (entry) => {
        if (!entry || entry.is_dir) return;
        if (editorTabsRef.current.some((tab) => tab.path === entry.path)) {
            setEditorActive(entry.path);
            setEditorVisible(true);
            return;
        }
        try {
            const data = await fileApi.read(entry.path);
            const readOnly = !isS3 && !isRemote && Boolean(entry.read_only || data.read_only);
            setEditorTabs((tabs) => (tabs.some((tab) => tab.path === entry.path) ? tabs : [...tabs, {
                path: entry.path, name: entry.name, content: data.content ?? '', draft: data.content ?? '',
                version: data.version || null, readOnly,
            }]));
            setEditorActive(entry.path);
            setEditorVisible(true);
        } catch (error) {
            toast.error(t('app.fileManager.failedToReadFile', "Couldn't read the file. {{message}}", { message: errorReason(error) }));
        }
    };

    const updateTab = (path, patch) => setEditorTabs((tabs) => tabs.map((tab) => (
        tab.path === path ? { ...tab, ...patch } : tab)));

    const changeDraft = useCallback((path, draft) => updateTab(path, { draft }), []);

    const saveTab = useCallback(async (path, { force = false } = {}) => {
        const tab = editorTabsRef.current.find((x) => x.path === path);
        if (!tab || tab.readOnly || tab.draft === tab.content) return;
        setSavingPaths((s) => new Set(s).add(path));
        const text = tab.draft;
        try {
            const res = await fileApi.write(path, text, force ? null : tab.version);
            updateTab(path, { content: text, version: res?.version || tab.version });
            toast.success(t('app.fileEditor.savedName', 'Saved {{name}}', { name: tab.name }));
            if (previewFile?.path === path) setFileContent(text);
        } catch (error) {
            if (error?.code === 'file_changed') {
                setConfirmDialog({
                    title: t('app.fileEditor.changedTitle', 'File changed on the server'),
                    message: t('app.fileEditor.changedMessage',
                        '{{name}} was changed on the server after you opened it. Overwrite it with your version? Cancel keeps the server\'s version; use Reload to see it.',
                        { name: tab.name }),
                    confirmText: t('app.fileEditor.overwrite', 'Overwrite'),
                    variant: 'danger',
                    onConfirm: () => { setConfirmDialog(null); saveTab(path, { force: true }); },
                    onCancel: () => setConfirmDialog(null),
                });
            } else {
                toast.error(t('app.fileManager.failedToSave', "Couldn't save. {{message}}", { message: errorReason(error) }));
            }
        } finally {
            setSavingPaths((s) => { const next = new Set(s); next.delete(path); return next; });
        }
    }, [fileApi, previewFile]); // eslint-disable-line react-hooks/exhaustive-deps

    const saveAllTabs = () => editorTabsRef.current
        .filter((tab) => tab.draft !== tab.content && !tab.readOnly)
        .forEach((tab) => saveTab(tab.path));

    const reloadTab = (path) => {
        const tab = editorTabsRef.current.find((x) => x.path === path);
        if (!tab) return;
        const reload = async () => {
            try {
                const data = await fileApi.read(path);
                updateTab(path, { content: data.content ?? '', draft: data.content ?? '', version: data.version || null });
            } catch (error) {
                toast.error(t('app.fileManager.failedToReadFile', "Couldn't read the file. {{message}}", { message: errorReason(error) }));
            }
        };
        if (tab.draft === tab.content) { reload(); return; }
        setConfirmDialog({
            title: t('app.fileEditor.reloadTitle', 'Reload from the server'),
            message: t('app.fileEditor.reloadMessage', 'Discard your unsaved changes to {{name}} and load the version on the server?', { name: tab.name }),
            confirmText: t('app.fileEditor.discardReload', 'Discard and reload'),
            variant: 'danger',
            onConfirm: () => { setConfirmDialog(null); reload(); },
            onCancel: () => setConfirmDialog(null),
        });
    };

    const closeTab = (path) => {
        const tab = editorTabsRef.current.find((x) => x.path === path);
        const close = () => {
            setEditorTabs((tabs) => {
                const rest = tabs.filter((x) => x.path !== path);
                if (rest.length === 0) setEditorVisible(false);
                setEditorActive((current) => (current === path ? rest[rest.length - 1]?.path || null : current));
                return rest;
            });
        };
        if (!tab || tab.draft === tab.content) { close(); return; }
        setConfirmDialog({
            title: t('app.fileEditor.unsavedTitle', 'Unsaved changes'),
            message: t('app.fileEditor.unsavedMessage', 'Close {{name}} without saving? Your changes will be lost.', { name: tab.name }),
            confirmText: t('app.fileEditor.closeWithoutSaving', 'Close without saving'),
            variant: 'danger',
            onConfirm: () => { setConfirmDialog(null); close(); },
            onCancel: () => setConfirmDialog(null),
        });
    };

    const editorDirty = editorTabs.some((tab) => tab.draft !== tab.content);

    // Leaving or reloading the page with unsaved edits asks first.
    useEffect(() => {
        if (!editorDirty) return undefined;
        const onBeforeUnload = (e) => { e.preventDefault(); e.returnValue = ''; };
        window.addEventListener('beforeunload', onBeforeUnload);
        return () => window.removeEventListener('beforeunload', onBeforeUnload);
    }, [editorDirty]);

    // ─── ops ─────────────────────────────────────────────
    const handleCreateFolder = async (name) => {
        if (remoteGuard('create folder')) return;
        try {
            await api.createDirectory(`${currentPath}/${name}`);
            toast.success(t('app.fileManager.folderCreated', 'Folder created'));
            closeDialog();
            loadDirectory(currentPath);
            // Refresh tree cache for parent so the new folder appears in the tree
            refreshTreeNode(currentPath);
        } catch (error) {
            toast.error(t('app.fileManager.failedToCreateFolder', "Couldn't create the folder. {{message}}", { message: errorReason(error) }));
        }
    };

    const handleDelete = (target) => {
        const items = Array.isArray(target) ? target : [target];
        if (items.length === 0) return;
        if (remoteGuard('delete')) return;
        const message = items.length === 1
            ? `Delete "${items[0].name}"?${items[0].is_dir ? ' All contents inside will be removed.' : ''}`
            : `Delete ${items.length} items? This cannot be undone.`;
        setConfirmDialog({
            titleKey: 'app.fileManager.deleteConfirmation', title: 'Delete confirmation',
            message,
            confirmTextKey: 'common.actions.delete', confirmText: 'Delete',
            variant: 'danger',
            onConfirm: async () => {
                const failures = [];
                for (const it of items) {
                    try {
                        await fileApi.del(it.path);
                    } catch (error) {
                        failures.push(`${it.name}: ${error.message}`);
                    }
                }
                if (failures.length === 0) toast.success(t('app.fileManager.deletedItems', { count: items.length, defaultValue_one: 'Deleted 1 item', defaultValue_other: 'Deleted {{count}} items' }));
                else toast.error(t('app.fileManager.failed', "Couldn't finish for: {{value}}", { value: failures.join(', ') }));
                if (previewFile && items.some((i) => i.path === previewFile.path)) setPreviewFile(null);
                clearSelection();
                loadDirectory(currentPath);
                setConfirmDialog(null);
            },
            onCancel: () => setConfirmDialog(null),
        });
    };

    const handleRename = async (entry, name) => {
        if (remoteGuard('rename')) return;
        try {
            await api.renameFile(entry.path, name);
            toast.success(t('app.fileManager.renamed', 'Renamed'));
            closeDialog();
            loadDirectory(currentPath);
        } catch (error) {
            toast.error(t('app.fileManager.failedToRename', "Couldn't rename it. {{message}}", { message: errorReason(error) }));
        }
    };

    const handleChangePermissions = async (entry, mode) => {
        if (remoteGuard('change permissions')) return;
        try {
            await api.changeFilePermissions(entry.path, mode);
            toast.success(t('app.fileManager.permissionsUpdated', 'Permissions updated'));
            closeDialog();
            loadDirectory(currentPath);
        } catch (error) {
            toast.error(t('app.fileManager.failed2', "Couldn't do that. {{message}}", { message: errorReason(error) }));
        }
    };

    const openRenameModal = (entry) => setDialog({ kind: 'rename', entry });
    const openPermissionsModal = (entry) => setDialog({ kind: 'permissions', entry });

    // ─── upload ──────────────────────────────────────────
    // Takes Files or { file, relPath } (folder uploads keep their structure).
    // Existing files aren't replaced silently: they're collected and the user
    // is asked once for the whole batch (fork).
    const uploadFiles = async (files, { overwrite = false, dir = currentPath } = {}) => {
        const items = Array.from(files).map((f) => (f instanceof File
            ? { file: f, relPath: f.webkitRelativePath || f.name }
            : f));
        if (items.length === 0) return;
        if (remoteGuard('upload')) return;
        const queue = items.map((it, i) => ({
            id: `${Date.now()}-${i}`,
            name: it.relPath,
            size: it.file.size,
            progress: 0,
            status: 'pending',
        }));
        setUploads((p) => [...p, ...queue]);

        let succeeded = 0;
        const existing = [];
        for (let i = 0; i < items.length; i++) {
            const itemId = queue[i].id;
            try {
                setUploads((p) => p.map((u) => u.id === itemId ? { ...u, status: 'uploading' } : u));
                const onProgress = (progress) => {
                    setUploads((p) => p.map((u) => u.id === itemId ? { ...u, progress } : u));
                };
                await uploadInto(dir, items[i], onProgress, { isS3, overwrite });
                setUploads((p) => p.map((u) => u.id === itemId ? { ...u, status: 'done', progress: 100 } : u));
                succeeded++;
            } catch (error) {
                if (error.code === 'file_exists') existing.push(items[i]);
                setUploads((p) => p.map((u) => u.id === itemId ? { ...u, status: 'error', error: error.message } : u));
            }
        }
        if (succeeded > 0) toast.success(t('app.fileManager.uploadedFiles', { count: items.length, succeeded, defaultValue_one: 'Uploaded {{succeeded}} of 1 file', defaultValue_other: 'Uploaded {{succeeded}} of {{count}} files' }));
        loadDirectory(currentPath);
        setTimeout(() => {
            setUploads((p) => p.filter((u) => u.status === 'uploading' || u.status === 'pending'));
        }, 4000);
        if (existing.length > 0) {
            setConfirmDialog({
                title: t('app.fileManager.replaceFilesTitle', 'Replace existing files?'),
                message: t('app.fileManager.replaceFilesMessage', {
                    count: existing.length, name: existing[0].relPath,
                    defaultValue_one: '"{{name}}" already exists here. Replace it with the uploaded file?',
                    defaultValue_other: '{{count}} files already exist here, such as "{{name}}". Replace them with the uploaded files?',
                }),
                confirmText: t('app.fileManager.replace', 'Replace'),
                variant: 'danger',
                onConfirm: () => { setConfirmDialog(null); uploadFiles(existing, { overwrite: true, dir }); },
                onCancel: () => setConfirmDialog(null),
            });
        }
    };

    const handleUploadInput = (e) => {
        if (e.target.files) uploadFiles(e.target.files);
        if (e.target) e.target.value = '';
    };

    const handleDragEnter = (e) => {
        e.preventDefault(); e.stopPropagation();
        // Rows dragged inside the page are moves, not uploads.
        if (!isExternalFileDrag(e)) return;
        dragCounter.current += 1;
        if (e.dataTransfer.items?.length > 0) setDragActive(true);
    };
    const handleDragLeave = (e) => {
        e.preventDefault(); e.stopPropagation();
        if (!isExternalFileDrag(e)) return;
        dragCounter.current -= 1;
        if (dragCounter.current === 0) setDragActive(false);
    };
    const handleDragOver = (e) => { e.preventDefault(); e.stopPropagation(); };
    const handleDrop = (e) => {
        e.preventDefault(); e.stopPropagation();
        dragCounter.current = 0;
        setDragActive(false);
        setDropTarget(null);
        if (!isExternalFileDrag(e)) return;
        collectDroppedFiles(e.dataTransfer)
            .then((items) => uploadFiles(items))
            .catch((error) => toast.error(t('app.fileManager.couldNotReadDrop', "Couldn't read the dropped files. {{message}}", { message: errorReason(error) })));
    };

    // ─── move / copy ─────────────────────────────────────
    const transfersEnabled = !isRemote && !isS3;

    const copyToClipboard = (mode, entries = selectedEntries) => {
        if (!transfersEnabled || entries.length === 0) return;
        setClipboard({ mode, items: transferItems(entries) });
        toast.success(mode === 'cut'
            ? t('app.fileManager.cutItems', { count: entries.length, defaultValue_one: 'Cut 1 item. Paste it into another folder.', defaultValue_other: 'Cut {{count}} items. Paste them into another folder.' })
            : t('app.fileManager.copiedItems', { count: entries.length, defaultValue_one: 'Copied 1 item', defaultValue_other: 'Copied {{count}} items' }));
    };

    // Re-reads a folder the tree has cached, so moved folders show up there.
    const refreshTreeNode = async (path) => {
        if (!treeCache.has(path)) return;
        try {
            const folders = await listSubfolders(path);
            setTreeCache((c) => { const n = new Map(c); n.set(path, folders); return n; });
        } catch { /* the tree refreshes on next expand */ }
    };

    // Moves ('cut') or copies items into targetDir. A copy into the folder it
    // came from (or onto a taken name) becomes "name (copy)".
    const transferInto = async (targetDir, items, mode) => {
        // Callers only offer moves and copies on the panel server.
        if (!items?.length || !transfersEnabled) return;
        let done = 0;
        const failures = [];
        const touched = new Set([targetDir]);
        for (const it of items) {
            const name = it.name || baseName(it.path);
            if (it.path === targetDir || (mode === 'cut' && parentOf(it.path) === targetDir)) continue;
            if (it.is_dir && isInside(targetDir, it.path)) {
                failures.push(`${name}: ${t('app.fileManager.intoItself', "can't go inside itself")}`);
                continue;
            }
            try {
                if (mode === 'cut') {
                    await moveInto(it, targetDir);
                    touched.add(parentOf(it.path));
                } else {
                    await copyInto(it, targetDir);
                }
                done++;
            } catch (error) {
                failures.push(`${name}: ${errorReason(error)}`);
            }
        }
        if (done > 0) {
            toast.success(mode === 'cut'
                ? t('app.fileManager.movedItems', { count: done, defaultValue_one: 'Moved 1 item', defaultValue_other: 'Moved {{count}} items' })
                : t('app.fileManager.pastedItems', { count: done, defaultValue_one: 'Copied 1 item here', defaultValue_other: 'Copied {{count}} items here' }));
        }
        if (failures.length > 0) toast.error(t('app.fileManager.failed', "Couldn't finish for: {{value}}", { value: failures.join(', ') }));
        if (previewFile && mode === 'cut' && items.some((i) => isInside(previewFile.path, i.path))) setPreviewFile(null);
        loadDirectory(currentPath);
        touched.forEach((dir) => refreshTreeNode(dir));
        return done;
    };

    // Cut items are pasted once; copied ones can be pasted again elsewhere.
    // (A drag is its own transfer and leaves the clipboard alone.)
    const pasteInto = async (targetDir = currentPath) => {
        if (!clipboard) return;
        const done = await transferInto(targetDir, clipboard.items, clipboard.mode);
        if (clipboard.mode === 'cut' && done > 0) setClipboard(null);
    };

    // Rows and cards: dragging a selected item drags the whole selection.
    const handleItemDragStart = (e, entry) => {
        const entries = selectedPaths.has(entry.path) ? selectedEntries : [entry];
        e.dataTransfer.setData(INTERNAL_DRAG_TYPE, JSON.stringify(transferItems(entries)));
        e.dataTransfer.setData('text/plain', entries.map((x) => x.path).join('\n'));
        e.dataTransfer.effectAllowed = 'copyMove';
    };
    // Folders (rows, cards, tree, breadcrumbs) take drops. Ctrl/Alt copies.
    const handleFolderDragOver = (e, dirPath) => {
        if (!transfersEnabled || !isInternalDrag(e)) return;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = e.ctrlKey || e.altKey ? 'copy' : 'move';
        if (dropTarget !== dirPath) setDropTarget(dirPath);
    };
    const handleFolderDragLeave = (e, dirPath) => {
        if (e.currentTarget.contains(e.relatedTarget)) return;
        setDropTarget((cur) => (cur === dirPath ? null : cur));
    };
    const handleFolderDrop = (e, dirPath) => {
        if (!transfersEnabled || !isInternalDrag(e)) return;
        e.preventDefault();
        e.stopPropagation();
        setDropTarget(null);
        transferInto(dirPath, readDraggedItems(e), e.ctrlKey || e.altKey ? 'copy' : 'cut');
    };
    const folderDropProps = (dirPath) => (transfersEnabled ? {
        onDragOver: (e) => handleFolderDragOver(e, dirPath),
        onDragLeave: (e) => handleFolderDragLeave(e, dirPath),
        onDrop: (e) => handleFolderDrop(e, dirPath),
    } : {});

    // ─── new file ────────────────────────────────────────
    const handleCreateFile = async (name) => {
        if (remoteGuard('create file')) return;
        const path = joinPath(currentPath, name);
        try {
            await createEmptyFile(path);
            toast.success(t('app.fileManager.fileCreated', 'File created'));
            closeDialog();
            loadDirectory(currentPath);
            openInEditor({ path, name, is_dir: false });
        } catch (error) {
            toast.error(t('app.fileManager.failedToCreateFile', "Couldn't create the file. {{message}}", { message: errorReason(error) }));
        }
    };

    // ─── archives (fork) ─────────────────────────────────
    // Server jobs (compress, extract, zip for download) sit in the upload
    // tray while they run. A failure is toasted here with failMessage and
    // resolves to null.
    const runTask = async (label, failMessage, work) => {
        const id = `task-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
        setUploads((p) => [...p, { id, name: label, size: 0, progress: 100, status: 'uploading', task: true }]);
        try {
            const result = await work();
            setUploads((p) => p.map((u) => (u.id === id ? { ...u, status: 'done' } : u)));
            return result;
        } catch (error) {
            setUploads((p) => p.map((u) => (u.id === id ? { ...u, status: 'error', error: error.message } : u)));
            toastError(toast, failMessage, error);
            return null;
        } finally {
            setTimeout(() => setUploads((p) => p.filter((u) => u.id !== id)), 4000);
        }
    };

    const openCompress = (entries) => {
        if (!transfersEnabled || entries.length === 0) return;
        setDialog({ kind: 'compress', entries });
    };

    const handleCompress = async (entries, { fileName, format }) => {
        closeDialog();
        const dir = currentPath;
        const result = await runTask(
            t('app.fileManager.compressing', 'Compressing to {{name}}', { name: fileName }),
            t('app.fileManager.couldNotCompress', "Couldn't compress."),
            () => compressInto(entries.map((e) => e.path), joinPath(dir, fileName), format),
        );
        if (result) {
            toast.success(t('app.fileManager.archiveCreated', 'Created {{name}}', { name: fileName })
                + (result?.result?.skipped ? ` ${t('app.fileManager.linksSkipped', { count: result.result.skipped, defaultValue_one: '(1 link skipped: zip has no links)', defaultValue_other: '({{count}} links skipped: zip has no links)' })}` : ''));
            if (dir === currentPath) loadDirectory(currentPath);
        }
    };

    const openExtract = (entry) => {
        if (!transfersEnabled) return;
        setDialog({ kind: 'extract', entry });
    };

    const handleExtract = async (entry, dest, overwrite = false) => {
        closeDialog();
        const result = await runTask(
            t('app.fileManager.extracting', 'Extracting {{name}}', { name: entry.name }),
            t('app.fileManager.couldNotExtract', "Couldn't extract."),
            () => extractTo(entry.path, dest, overwrite),
        );
        if (!result) return;
        if (result.conflicts) {
            setConfirmDialog({
                title: t('app.fileManager.replaceFilesTitle', 'Replace existing files?'),
                message: t('app.fileManager.extractConflicts', {
                    count: result.conflicts, name: result.examples?.[0] || '', dest,
                    defaultValue_one: '"{{name}}" already exists in {{dest}}. Replace it with the file from the archive?',
                    defaultValue_other: '{{count}} files already exist in {{dest}}, such as "{{name}}". Replace them with the files from the archive?',
                }),
                confirmText: t('app.fileManager.replace', 'Replace'),
                variant: 'danger',
                onConfirm: () => { setConfirmDialog(null); handleExtract(entry, dest, true); },
                onCancel: () => setConfirmDialog(null),
            });
            return;
        }
        toast.success(t('app.fileManager.extractedFiles', { count: result.files || 0, defaultValue_one: 'Extracted 1 file', defaultValue_other: 'Extracted {{count}} files' })
            + (result.skipped ? ` ${t('app.fileManager.extractSkipped', { count: result.skipped, defaultValue_one: '(1 link or special file skipped)', defaultValue_other: '({{count}} links or special files skipped)' })}` : ''));
        loadDirectory(currentPath);
        refreshTreeNode(parentOf(dest));
    };

    const handleDownloadZip = async (entries) => {
        if (entries.length === 0) return;
        const name = entries.length === 1 ? `${entries[0].name}.zip` : 'files.zip';
        await runTask(
            t('app.fileManager.preparingDownload', 'Preparing {{name}}', { name }),
            t('app.fileManager.couldNotZip', "Couldn't prepare the download."),
            () => downloadAsZip(entries.map((e) => e.path)),
        );
    };

    // ─── derived ─────────────────────────────────────────
    const breadcrumbs = useMemo(() => {
        const parts = currentPath.split('/').filter(Boolean);
        // Windows drive-rooted path ("C:/Users/Juan"): the first segment is
        // the drive and the root crumb is the agent's drive list. Building
        // crumbs with a leading "/" (the POSIX branch below) would produce
        // bogus "/C:" paths that the agent can't resolve.
        if (/^[A-Za-z]:$/.test(parts[0] || '')) {
            const crumbs = [{ name: 'Drives', path: '/' }];
            let acc = parts[0];
            crumbs.push({ name: parts[0], path: acc + '/' });
            for (let i = 1; i < parts.length; i++) {
                acc += '/' + parts[i];
                crumbs.push({ name: parts[i], path: acc });
            }
            return crumbs;
        }
        const crumbs = [{ name: '/', path: '/' }];
        let acc = '';
        parts.forEach((p) => { acc += '/' + p; crumbs.push({ name: p, path: acc }); });
        return crumbs;
    }, [currentPath]);

    const sortedFiltered = useMemo(() => {
        let list = [...(searchResults || entries)];
        if (activeFilter !== 'all') list = list.filter((e) => getFileType(e) === activeFilter);
        const dir = sortDir === 'asc' ? 1 : -1;
        list.sort((a, b) => {
            if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
            switch (sortBy) {
                case 'size': return ((a.size || 0) - (b.size || 0)) * dir;
                case 'modified': return (new Date(a.modified) - new Date(b.modified)) * dir;
                case 'type': return getFileType(a).localeCompare(getFileType(b)) * dir;
                case 'name':
                default: return a.name.localeCompare(b.name) * dir;
            }
        });
        return list;
    }, [entries, searchResults, sortBy, sortDir, activeFilter]);

    const filterCounts = useMemo(() => {
        const counts = { all: entries.length };
        FILTER_OPTIONS.forEach((c) => { if (c.id !== 'all') counts[c.id] = 0; });
        entries.forEach((e) => { const t = getFileType(e); if (counts[t] !== undefined) counts[t]++; });
        return counts;
    }, [entries]);

    const stats = useMemo(() => {
        const list = sortedFiltered;
        const folders = list.filter((e) => e.is_dir).length;
        const files = list.length - folders;
        const totalBytes = list.reduce((s, e) => s + (e.size || 0), 0);
        const selectedList = list.filter((e) => selectedPaths.has(e.path));
        const selectedBytes = selectedList.reduce((s, e) => s + (e.size || 0), 0);
        return { folders, files, totalBytes, total: list.length, selectedCount: selectedList.length, selectedBytes };
    }, [sortedFiltered, selectedPaths]);

    const activeMount = useMemo(() => diskMounts
        .filter((mount) => {
            const mountPath = mount.mountpoint || '/';
            if (mountPath === '/') return currentPath.startsWith('/');
            return currentPath === mountPath || currentPath.startsWith(`${mountPath.replace(/\/$/, '')}/`);
        })
        .sort((a, b) => (b.mountpoint || '/').length - (a.mountpoint || '/').length)[0] || null,
    [currentPath, diskMounts]);

    const activeUploads = uploads.filter((u) => u.status === 'uploading' || u.status === 'pending');
    const totalUploadProgress = activeUploads.length > 0
        ? activeUploads.reduce((s, u) => s + u.progress, 0) / activeUploads.length
        : 0;

    const sortValue = `${sortBy}-${sortDir}`;
    const filterLabels = {
        all: t('common.labels.all', 'All'),
        folder: t('app.fileManager.folders', 'Folders'),
        image: t('app.fileManager.images', 'Images'),
        code: t('app.fileManager.code', 'Code'),
        text: t('app.fileManager.documents', 'Documents'),
        data: t('app.fileManager.data', 'Data'),
        video: t('app.fileManager.videos', 'Videos'),
        audio: t('app.fileManager.audio', 'Audio'),
        archive: t('app.fileManager.archives', 'Archives'),
    };
    const handleSortChange = (value) => {
        const [nextSortBy, nextSortDir] = value.split('-');
        setSortBy(nextSortBy);
        setSortDir(nextSortDir);
    };

    const selectedEntries = useMemo(
        () => sortedFiltered.filter((e) => selectedPaths.has(e.path)),
        [sortedFiltered, selectedPaths],
    );

    // ─── shortcuts ───────────────────────────────────────
    useEffect(() => {
        const handler = (e) => {
            // The code editor is a contenteditable, not an input: without these
            // checks Backspace there went up a folder and Delete deleted files.
            if (editorVisible) return;
            const inInput = ['INPUT', 'TEXTAREA'].includes(e.target.tagName)
                || e.target.isContentEditable;
            if (inInput) return;
            if (e.key === 'Escape') {
                if (contextMenu) setContextMenu(null);
                else if (previewFile) setPreviewFile(null);
                else if (selectedPaths.size > 0) clearSelection();
            }
            if ((e.key === 'Delete' || (e.key === 'Backspace' && e.metaKey)) && selectedEntries.length > 0) {
                e.preventDefault();
                handleDelete(selectedEntries);
            }
            if (e.key === 'F2' && selectedEntries.length === 1) openRenameModal(selectedEntries[0]);
            if ((e.ctrlKey || e.metaKey) && !e.shiftKey && transfersEnabled) {
                const key = e.key.toLowerCase();
                if ((key === 'c' || key === 'x') && selectedEntries.length > 0 && !window.getSelection()?.toString()) {
                    e.preventDefault();
                    copyToClipboard(key === 'x' ? 'cut' : 'copy');
                }
                if (key === 'v' && clipboard) { e.preventDefault(); pasteInto(currentPath); }
            }
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
                e.preventDefault();
                setSelectedPaths(new Set(sortedFiltered.map((x) => x.path)));
                setSelectMode(sortedFiltered.length > 0);
            }
            if (e.key === 'Backspace' && !e.metaKey && parentPath) { e.preventDefault(); goUp(); }
        };
        window.addEventListener('keydown', handler);
        return () => window.removeEventListener('keydown', handler);
    }, [selectedEntries, sortedFiltered, contextMenu, previewFile, parentPath, selectedPaths, editorVisible, clipboard, currentPath, transfersEnabled]); // eslint-disable-line

    // ─── close popovers ──────────────────────────────────
    useEffect(() => {
        if (!contextMenu) return;
        const close = () => { setContextMenu(null); };
        document.addEventListener('click', close);
        return () => document.removeEventListener('click', close);
    }, [contextMenu]);

    const openContextMenu = (e, entry) => {
        e.preventDefault();
        e.stopPropagation();
        if (!selectedPaths.has(entry.path)) {
            setSelectedPaths(new Set([entry.path]));
            setSelectMode(true);
            setLastClickedPath(entry.path);
        }
        setContextMenu({ x: e.clientX, y: e.clientY, entry });
    };

    // Stable identities for the memoized rows/cards: a fresh handler on every
    // render made each keystroke or selection re-render every row.
    const rowOpen = useStableHandler(handleOpen);
    const rowToggleSelect = useStableHandler(handleToggleSelect);
    const rowContext = useStableHandler(openContextMenu);
    const rowDownload = useStableHandler((entry) => fileApi.download(entry));
    const rowRename = useStableHandler(openRenameModal);
    const rowPermissions = useStableHandler(openPermissionsModal);
    const rowDelete = useStableHandler((entry) => handleDelete(entry));
    const rowDragStart = useStableHandler(handleItemDragStart);
    const rowDragOver = useStableHandler(handleFolderDragOver);
    const rowDragLeave = useStableHandler(handleFolderDragLeave);
    const rowDrop = useStableHandler(handleFolderDrop);
    const rowDragProps = (entry) => (transfersEnabled ? {
        draggable: true,
        dropActive: dropTarget === entry.path,
        onDragStartItem: rowDragStart,
        onDragOverItem: rowDragOver,
        onDragLeaveItem: rowDragLeave,
        onDropItem: rowDrop,
    } : {});

    const copyPathToClipboard = (path) => copyText(path);

    const downloadSelected = () => {
        if (selectedEntries.length === 1 && !selectedEntries[0].is_dir) fileApi.download(selectedEntries[0]);
        else if (transfersEnabled) handleDownloadZip(selectedEntries);
        else selectedEntries.filter((e) => !e.is_dir).forEach((e) => fileApi.download(e));
    };

    const getDiskColor = (percent) => {
        if (percent >= 90) return 'critical';
        if (percent >= 70) return 'warning';
        return 'healthy';
    };

    useTopbarActions(() => (
        <>
            <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setDialog({ kind: 'newFile' })}
                disabled={isRemote || isS3}
            >
                <FilePlus size={14} /> {t('app.fileManager.newFile', 'New file')}
            </Button>
            <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setDialog({ kind: 'newFolder' })}
                disabled={isRemote || isS3}
            >
                <FolderPlus size={14} /> {t('app.fileManager.newFolder', 'New folder')}
            </Button>
            <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => fileInputRef.current?.click()}
                disabled={isRemote}
            >
                <Upload size={14} /> {t('app.fileManager.upload', 'Upload')}
            </Button>
            <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => folderInputRef.current?.click()}
                disabled={isRemote}
            >
                <FolderUp size={14} /> {t('app.fileManager.uploadFolder', 'Upload folder')}
            </Button>
            {!isS3 && (
                <Button type="button" size="sm" onClick={() => navigate('/terminal')}>
                    <Terminal size={14} /> {t('app.fileManager.terminal', 'Terminal')}
                </Button>
            )}
        </>
    ), [isRemote, isS3, currentPath]);

    // ─── render ──────────────────────────────────────────
    return (
        <div
            className={`sk-tabgroup__fill file-manager-page file-manager fullscreen ${sidebarVisible ? 'sidebar-open' : ''} view-${viewMode} grid-${gridSize} ${selectMode ? 'select-mode' : ''}`}
            onDragEnter={handleDragEnter}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
        >
            <input
                type="file"
                ref={fileInputRef}
                multiple
                className="hidden"
                onChange={handleUploadInput}
            />
            <input
                type="file"
                ref={folderInputRef}
                webkitdirectory=""
                multiple
                className="hidden"
                onChange={handleUploadInput}
            />

            {isRemote && (
                <div className="file-manager-target-banner">
                    {t('app.fileManager.browsingOn', 'Browsing on')} <strong>{target.name}</strong> {t('app.fileManager.readWriteOnlyMkdirDeleteRename', "(read/write only). Mkdir/delete/rename/upload aren't yet supported on remote agents.")}
                </div>
            )}

            {isS3 && (
                <div className="file-manager-target-banner">
                    {t('app.fileManager.browsingYour', 'Browsing your')} <strong>{t('app.fileManager.s3Bucket', 'S3 bucket')}</strong>.{' '}
                    {t('app.fileManager.s3SupportedOperations', "Upload, download, edit and delete work; folders, rename and permissions don't apply to object storage.")}
                </div>
            )}

            {uploads.length > 0 && (
                <div className="upload-tray">
                    <div className="upload-tray-header">
                        <CloudUpload size={16} />
                        <span>
                            {activeUploads.length > 0
                                ? (activeUploads.every((u) => u.task)
                                    ? t('app.fileManager.working', 'Working…')
                                    : t('app.fileManager.uploadingFiles', { count: activeUploads.length, defaultValue_one: 'Uploading 1 file…', defaultValue_other: 'Uploading {{count}} files…' }))
                                : t('app.fileManager.uploadsComplete', 'Uploads complete')}
                        </span>
                        {activeUploads.some((u) => !u.task) && (
                            <span className="upload-tray-percent">{Math.round(totalUploadProgress)}%</span>
                        )}
                        <Button variant="unstyled" type="button" className="toolbar-icon-btn small" onClick={() => setUploads([])} title={t('common.actions.clear', 'Clear')}>
                            <X size={14} />
                        </Button>
                    </div>
                    <div className="upload-tray-list">
                        {uploads.map((u) => (
                            <div key={u.id} className={`upload-tray-item status-${u.status}`}>
                                <span className="upload-name">{u.name}</span>
                                <div className="upload-bar">
                                    <div className={`upload-bar-fill${u.task && u.status === 'uploading' ? ' is-working' : ''}`} style={{ width: `${u.progress}%` }} />
                                </div>
                                <span className="upload-status">
                                    {u.status === 'done' ? t('common.actions.done', 'Done') : u.status === 'error' ? t('app.fileManager.uploadFailed', 'Failed') : u.task ? '…' : `${Math.round(u.progress)}%`}
                                </span>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            <div className="file-manager-toolbar">
                <div className="toolbar-left">
                    <Button variant="unstyled" type="button"
                        className="toolbar-icon-btn"
                        onClick={() => setSidebarVisible(!sidebarVisible)}
                        title={sidebarVisible ? t('app.fileManager.hideSidebar', 'Hide sidebar') : t('app.fileManager.showSidebar', 'Show sidebar')}
                    >
                        {sidebarVisible ? <PanelLeftClose size={14} /> : <PanelLeftOpen size={14} />}
                    </Button>
                    <div className="nav-buttons">
                        <Button variant="unstyled" type="button" className="nav-btn" onClick={goBack} disabled={historyIdx === 0} title={t('common.actions.back', 'Back')}>
                            <ArrowLeft size={14} />
                        </Button>
                        <Button variant="unstyled" type="button" className="nav-btn" onClick={goForward} disabled={historyIdx >= history.length - 1} title={t('app.fileManager.forward', 'Forward')}>
                            <ArrowRight size={14} />
                        </Button>
                        <Button variant="unstyled" type="button" className="nav-btn" onClick={goUp} disabled={!parentPath} title="Up">
                            <ArrowUp size={14} />
                        </Button>
                        <Button variant="unstyled" type="button" className="nav-btn" onClick={() => navigateTo(isS3 ? '/' : '/home')} title={t('app.fileManager.home', 'Home')}>
                            <Home size={14} />
                        </Button>
                    </div>
                    <div className="path-breadcrumb">
                        {breadcrumbs.map((crumb, idx) => (
                            <span key={crumb.path + idx} className="crumb-segment">
                                {idx > 0 && <span className="crumb-separator">/</span>}
                                <Button variant="unstyled" type="button"
                                    className={`crumb ${idx === breadcrumbs.length - 1 ? 'crumb-active' : ''}${dropTarget === crumb.path ? ' drop-target' : ''}`}
                                    onClick={() => navigateTo(crumb.path)}
                                    {...folderDropProps(crumb.path)}
                                >
                                    {crumb.name}
                                </Button>
                            </span>
                        ))}
                    </div>
                    {activeMount && !isRemote && !isS3 && (
                        <span className="file-mount-chip" title={`${activeMount.device} · ${activeMount.total_human}`}>
                            <HardDrive size={12} />
                            <span>{activeMount.mountpoint === '/' ? t('app.fileManager.rootDisk', 'Root disk') : activeMount.mountpoint}</span>
                        </span>
                    )}
                </div>
                <div className="toolbar-right">
                    <Select value={activeFilter} onValueChange={setActiveFilter}>
                        <SelectTrigger
                            className="file-toolbar-select"
                            title={t('app.fileManager.types', 'Types')}
                            aria-label={t('app.fileManager.types', 'Types')}
                        >
                            <File size={13} aria-hidden="true" />
                            <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                            {FILTER_OPTIONS.map((option) => (
                                <SelectItem key={option.id} value={option.id}>
                                    {filterLabels[option.id]} ({filterCounts[option.id] ?? 0})
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                    <Select value={sortValue} onValueChange={handleSortChange}>
                        <SelectTrigger
                            className="file-toolbar-select"
                            title={t('app.fileManager.sort', 'Sort')}
                            aria-label={t('app.fileManager.sort', 'Sort')}
                        >
                            <ArrowUpDown size={13} aria-hidden="true" />
                            <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                            <SelectItem value="name-asc">{t('app.fileManager.nameAZ', 'Name A-Z')}</SelectItem>
                            <SelectItem value="name-desc">{t('app.fileManager.nameZA', 'Name Z-A')}</SelectItem>
                            <SelectItem value="modified-desc">{t('app.fileManager.newest', 'Newest')}</SelectItem>
                            <SelectItem value="modified-asc">{t('app.fileManager.oldest', 'Oldest')}</SelectItem>
                            <SelectItem value="size-desc">{t('app.fileManager.largest', 'Largest')}</SelectItem>
                            <SelectItem value="size-asc">{t('app.fileManager.smallest', 'Smallest')}</SelectItem>
                            <SelectItem value="type-asc">{t('common.labels.type', 'Type')}</SelectItem>
                            <SelectItem value="type-desc">{t('app.fileManager.typeZA', 'Type Z-A')}</SelectItem>
                        </SelectContent>
                    </Select>
                    <div className="search-field">
                        <Search size={14} className="search-field-icon" />
                        <input
                            type="text"
                            placeholder={t('app.fileManager.searchFiles', 'Search files…')}
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                            onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                        />
                        {(searchResults || searchQuery) && (
                            <Button variant="unstyled" type="button"
                                className="search-field-clear"
                                onClick={() => { setSearchResults(null); setSearchQuery(''); }}
                                title={t('common.actions.clear', 'Clear')}
                            >
                                <X size={12} />
                            </Button>
                        )}
                    </div>
                    <div className="view-toggle">
                        <Button variant="unstyled" type="button"
                            className={`view-toggle-btn ${viewMode === 'grid' ? 'active' : ''}`}
                            onClick={() => setViewMode('grid')}
                            title={t('app.fileManager.gridView', 'Grid view')}
                        >
                            <LayoutGrid size={14} />
                        </Button>
                        <Button variant="unstyled" type="button"
                            className={`view-toggle-btn ${viewMode === 'list' ? 'active' : ''}`}
                            onClick={() => setViewMode('list')}
                            title={t('app.fileManager.listView', 'List view')}
                        >
                            <List size={14} />
                        </Button>
                    </div>
                    <Button variant="unstyled" type="button"
                        className={`toolbar-icon-btn ${showHidden ? 'active' : ''}`}
                        onClick={() => setShowHidden(!showHidden)}
                        title={t('app.fileManager.toggleHiddenFiles', 'Toggle hidden files')}
                    >
                        {showHidden ? <Eye size={14} /> : <EyeOff size={14} />}
                    </Button>
                    <Button variant="unstyled" type="button"
                        className="toolbar-icon-btn"
                        onClick={() => loadDirectory(currentPath)}
                        title={t('common.actions.refresh', 'Refresh')}
                    >
                        <RefreshCw size={14} className={loading ? 'spinning' : ''} />
                    </Button>
                </div>
            </div>

            {selectedPaths.size > 0 && (
                <div className="bulk-bar">
                    <div className="bulk-bar-info">
                        <Check size={14} />
                        <span>{selectedPaths.size} {t('app.fileManager.selected', 'selected ·')} {formatBytes(stats.selectedBytes)}</span>
                    </div>
                    <div className="bulk-bar-actions">
                        <Button variant="unstyled" type="button" className="bulk-btn" onClick={downloadSelected}>
                            <Download size={14} /> {t('common.actions.download', 'Download')}
                        </Button>
                        {transfersEnabled && (
                            <>
                                <Button variant="unstyled" type="button" className="bulk-btn" onClick={() => copyToClipboard('copy')}>
                                    <Files size={14} /> {t('app.fileManager.copy', 'Copy')}
                                </Button>
                                <Button variant="unstyled" type="button" className="bulk-btn" onClick={() => copyToClipboard('cut')}>
                                    <Scissors size={14} /> {t('app.fileManager.cut', 'Cut')}
                                </Button>
                                <Button variant="unstyled" type="button" className="bulk-btn" onClick={() => openCompress(selectedEntries)}>
                                    <FileArchive size={14} /> {t('app.fileManager.compress', 'Compress')}
                                </Button>
                            </>
                        )}
                        {selectedEntries.length === 1 && (
                            <>
                                <Button variant="unstyled" type="button" className="bulk-btn" onClick={() => openRenameModal(selectedEntries[0])}>
                                    <Edit3 size={14} /> {t('app.fileManager.rename', 'Rename')}
                                </Button>
                                <Button variant="unstyled" type="button" className="bulk-btn" onClick={() => copyPathToClipboard(selectedEntries[0].path)}>
                                    <Copy size={14} /> {t('app.fileManager.copyPath', 'Copy path')}
                                </Button>
                            </>
                        )}
                        <Button variant="unstyled" type="button" className="bulk-btn danger" onClick={() => handleDelete(selectedEntries)}>
                            <Trash2 size={14} /> {t('common.actions.delete', 'Delete')}
                        </Button>
                        <Button variant="unstyled" type="button" className="bulk-btn ghost" onClick={clearSelection}>
                            <X size={14} /> {t('common.actions.clear', 'Clear')}
                        </Button>
                    </div>
                </div>
            )}

            {clipboard && transfersEnabled && (
                <div className="bulk-bar clipboard-bar">
                    <div className="bulk-bar-info">
                        {clipboard.mode === 'cut' ? <Scissors size={14} /> : <Files size={14} />}
                        <span>
                            {clipboard.mode === 'cut'
                                ? t('app.fileManager.clipboardCut', { count: clipboard.items.length, defaultValue_one: '1 item to move', defaultValue_other: '{{count}} items to move' })
                                : t('app.fileManager.clipboardCopy', { count: clipboard.items.length, defaultValue_one: '1 item to copy', defaultValue_other: '{{count}} items to copy' })}
                        </span>
                    </div>
                    <div className="bulk-bar-actions">
                        <Button variant="unstyled" type="button" className="bulk-btn" onClick={() => pasteInto(currentPath)}>
                            <ClipboardPaste size={14} /> {t('app.fileManager.pasteHere', 'Paste here')}
                        </Button>
                        <Button variant="unstyled" type="button" className="bulk-btn ghost" onClick={() => setClipboard(null)}>
                            <X size={14} /> {t('common.actions.cancel', 'Cancel')}
                        </Button>
                    </div>
                </div>
            )}

            <div className={`file-manager-body ${previewFile ? 'has-preview' : ''}`}>
                {sidebarVisible && (
                    <aside className="file-manager-sidebar left">
                        <div className="file-manager-source">
                            <Button variant="unstyled"
                                type="button"
                                className="file-manager-source__close"
                                onClick={() => setSidebarVisible(false)}
                                aria-label={t('app.fileManager.hideSidebar', 'Hide sidebar')}
                            >
                                <X size={14} />
                            </Button>
                            <ServerPicker
                                capability="files"
                                value={targetServerId(target)}
                                onChange={(id, server) => setTarget(serverTarget(id, server || (id === 's3' ? null : {})))}
                                extraOptions={s3Options}
                                className="file-manager-source__picker"
                            />
                            <div className="file-manager-source__meta">
                                <span className={`file-manager-source__dot${isS3 ? ' is-cloud' : ''}`} aria-hidden="true" />
                                <span>
                                    {isRemote
                                        ? t('app.fileManager.remoteAgent', 'Remote agent')
                                        : isS3
                                            ? t('app.fileManager.objectStorage', 'Object storage')
                                            : t('app.fileManager.localPanelHost', 'Panel server')}
                                </span>
                            </div>
                        </div>

                        {!isRemote && !isS3 && (
                            <div className="sidebar-section sidebar-section--volumes">
                                <div className="sidebar-section-header sidebar-section-header--split">
                                    <span className="sidebar-section-title" title={t('app.fileManager.diskUsage', 'Disk usage')}>
                                        <HardDrive size={13} />
                                        <span>{t('app.fileManager.volumes', 'Volumes')}</span>
                                    </span>
                                    <Button variant="unstyled" type="button" className="sidebar-action-btn" onClick={loadDiskMounts} disabled={diskLoading} title={t('common.actions.refresh', 'Refresh')}>
                                        <RefreshCw size={12} className={diskLoading ? 'spinning' : ''} />
                                    </Button>
                                </div>
                                <div className="sidebar-section-content disk-mount-list">
                                    {diskMounts.map((mount) => (
                                        <Button variant="unstyled"
                                            type="button"
                                            key={`${mount.device}:${mount.mountpoint}`}
                                            className={`disk-mount-item${activeMount?.mountpoint === mount.mountpoint ? ' active' : ''}`}
                                            onClick={() => navigateTo(mount.mountpoint)}
                                            title={`${mount.device} · ${mount.used_human} / ${mount.total_human}`}
                                        >
                                            <div className="disk-mount-header">
                                                <span className="disk-mount-point">
                                                    {mount.mountpoint === '/'
                                                        ? t('app.fileManager.rootDisk', 'Root disk')
                                                        : mountDisplayName(mount)}
                                                </span>
                                                <span className={`disk-percent ${getDiskColor(mount.percent)}`}>
                                                    {mount.percent}%
                                                </span>
                                            </div>
                                            <div className={`disk-progress ${getDiskColor(mount.percent)}`}>
                                                <span className="disk-progress-fill" style={{ width: `${mount.percent}%` }} />
                                            </div>
                                        </Button>
                                    ))}
                                    {diskLastUpdated && (
                                        <span className="disk-updated disk-updated--footer">
                                            <Clock size={11} />
                                            {t('app.fileManager.updatedAt', 'Updated {{time}}', { time: formatTime(diskLastUpdated, { seconds: true }) })}
                                        </span>
                                    )}
                                </div>
                            </div>
                        )}

                        {!isS3 && (<>
                        <div className="sidebar-section">
                            <div className="sidebar-section-header static">
                                <Zap size={16} />
                                <span>{t('app.fileManager.quickAccess', 'Quick access')}</span>
                            </div>
                            <div className="sidebar-section-content quick-access-list">
                                {quickAccess.map(q => (
                                    <Button variant="unstyled" type="button"
                                        key={q.label}
                                        className={`quick-access-item ${currentPath === q.path ? 'active' : ''}`}
                                        onClick={() => navigateTo(q.path)}
                                    >
                                        <q.icon size={14} />
                                        <span>{q.label}</span>
                                    </Button>
                                ))}
                            </div>
                        </div>

                        <div className="sidebar-section">
                            <div className="sidebar-section-header sidebar-section-header--split">
                                <Button variant="unstyled"
                                    type="button"
                                    className="sidebar-section-toggle"
                                    onClick={() => setTreeCollapsed(!treeCollapsed)}
                                    title={t('app.fileManager.folders', 'Folders')}
                                >
                                    <FolderTreeIcon size={16} />
                                    <span>{t('app.fileManager.fileSystem', 'File system')}</span>
                                    {treeCollapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
                                </Button>
                                <Button variant="unstyled" type="button"
                                    className="sidebar-action-btn"
                                    onClick={() => setDialog({ kind: 'newFolder' })}
                                    disabled={isRemote}
                                    title={t('app.fileManager.newFolder', 'New folder')}
                                >
                                    <FolderPlus size={14} />
                                </Button>
                            </div>
                            {!treeCollapsed && (
                                <div className="sidebar-section-content tree-content">
                                    <FolderTree
                                        roots={TREE_ROOTS}
                                        expanded={treeExpanded}
                                        treeCache={treeCache}
                                        treeLoading={treeLoading}
                                        currentPath={currentPath}
                                        onNavigate={navigateTo}
                                        onToggle={toggleTreeExpand}
                                        dropTarget={dropTarget}
                                        dropProps={transfersEnabled ? folderDropProps : null}
                                    />
                                </div>
                            )}
                        </div>
                        </>)}

                    </aside>
                )}

                <main className="file-manager-main">
                    <div
                        className="file-list-container"
                        onClick={(e) => {
                            if (e.target === e.currentTarget) clearSelection();
                        }}
                    >
                        {dragActive && (
                            <div className="drag-overlay">
                                <div className="drag-overlay-inner">
                                    <CloudUpload size={56} strokeWidth={1.5} />
                                    <h3>{t('app.fileManager.dropToUpload', 'Drop to upload')}</h3>
                                    <p>{t('app.fileManager.filesWillBeUploadedTo', 'Files will be uploaded to')} <code>{currentPath}</code></p>
                                </div>
                            </div>
                        )}

                        {loading ? (
                            <EmptyState loading loadingVariant="tree" title={t('app.fileManager.loadingFiles', 'Loading files')} />
                        ) : dirError && !searchResults ? (
                            <ErrorState
                                title={t('app.fileManager.couldntOpenThisFolder', "Couldn't open this folder.")}
                                error={dirError}
                                onRetry={() => loadDirectory(currentPath)}
                            />
                        ) : sortedFiltered.length === 0 ? (
                            <EmptyState
                                icon={FolderOpen}
                                title={searchResults ? t('app.fileManager.noMatches', 'No matches') : activeFilter !== 'all' ? t('app.fileManager.noFiles', 'No {{activeFilter}} files', { activeFilter: activeFilter }) : t('app.fileManager.thisFolderIsEmpty', 'This folder is empty')}
                                description={searchResults
                                    ? t('app.fileManager.tryADifferentSearchTermOr', 'Try a different search term or browse another folder.')
                                    : activeFilter !== 'all'
                                        ? t('app.fileManager.tryADifferentFilter', 'Try a different filter.')
                                        : t('app.fileManager.dropFilesHereOrUseThe', 'Drop files here, or use the buttons above to create something new.')}
                            />
                        ) : viewMode === 'grid' ? (
                            <div className="file-grid">
                                {sortedFiltered.map((entry) => (
                                    <FileCard
                                        key={entry.path}
                                        entry={entry}
                                        selected={selectedPaths.has(entry.path)}
                                        selectMode={selectMode}
                                        onOpen={rowOpen}
                                        onToggleSelect={rowToggleSelect}
                                        onContext={rowContext}
                                        isS3={isS3}
                                        {...rowDragProps(entry)}
                                    />
                                ))}
                            </div>
                        ) : (
                            <div className="file-list">
                                <div className="file-list-header">
                                    <span className="col-check">
                                        <Button variant="unstyled" type="button"
                                            className="checkbox-btn"
                                            onClick={() => {
                                                if (selectedPaths.size === sortedFiltered.length) clearSelection();
                                                else {
                                                    setSelectedPaths(new Set(sortedFiltered.map((x) => x.path)));
                                                    setSelectMode(sortedFiltered.length > 0);
                                                }
                                            }}
                                        >
                                            <span className={`checkbox ${selectedPaths.size === sortedFiltered.length && sortedFiltered.length > 0 ? 'checked' : ''}`}>
                                                {selectedPaths.size === sortedFiltered.length && sortedFiltered.length > 0 && <Check size={12} />}
                                            </span>
                                        </Button>
                                    </span>
                                    <span className="col-name">{t('common.labels.name', 'Name')}</span>
                                    <span className="col-size">{t('common.labels.size', 'Size')}</span>
                                    <span className="col-modified">{t('app.fileManager.modified', 'Modified')}</span>
                                    <span className="col-permissions">{t('common.labels.permissions', 'Permissions')}</span>
                                    <span className="col-owner">{t('app.fileManager.owner', 'Owner')}</span>
                                    <span className="col-actions">{t('common.labels.actions', 'Actions')}</span>
                                </div>
                                {sortedFiltered.map((entry) => (
                                    <FileRow
                                        key={entry.path}
                                        entry={entry}
                                        selected={selectedPaths.has(entry.path)}
                                        selectMode={selectMode}
                                        onOpen={rowOpen}
                                        onToggleSelect={rowToggleSelect}
                                        onContext={rowContext}
                                        onDownload={rowDownload}
                                        onRename={rowRename}
                                        onPermissions={rowPermissions}
                                        onDelete={rowDelete}
                                        {...rowDragProps(entry)}
                                    />
                                ))}
                            </div>
                        )}
                    </div>
                </main>

                <PreviewDrawer
                    inline
                    isS3={isS3}
                    file={previewFile}
                    fileContent={fileContent}
                    onStartEdit={() => openInEditor(previewFile)}
                    onClose={() => setPreviewFile(null)}
                    onDownload={(e) => fileApi.download(e)}
                    onRename={openRenameModal}
                    onPermissions={openPermissionsModal}
                    onCopyPath={copyPathToClipboard}
                    onDelete={(e) => handleDelete(e)}
                />
            </div>

            {editorVisible && editorTabs.length > 0 && (
                <Suspense fallback={<div className="fm-editor fm-editor--loading">{t('app.fileEditor.loading', 'Loading the editor…')}</div>}>
                    <EditorWorkspace
                        tabs={editorTabs}
                        activePath={editorActive}
                        savingPaths={savingPaths}
                        onActivate={setEditorActive}
                        onChange={changeDraft}
                        onSave={saveTab}
                        onSaveAll={saveAllTabs}
                        onReload={reloadTab}
                        onCloseTab={closeTab}
                        onExit={() => setEditorVisible(false)}
                    />
                </Suspense>
            )}

            <div className="status-bar">
                <div className="status-bar-left">
                    <span className="status-item">
                        <span className="status-label">{t('app.fileManager.total', 'Total')}</span>
                        <span className="status-value">{stats.total} item{stats.total !== 1 ? 's' : ''}</span>
                    </span>
                    <span className="status-divider" />
                    <span className="status-item">
                        <Folder size={12} />
                        <span>{stats.folders} folder{stats.folders !== 1 ? 's' : ''}</span>
                    </span>
                    <span className="status-item">
                        <File size={12} />
                        <span>{stats.files} file{stats.files !== 1 ? 's' : ''}</span>
                    </span>
                    {stats.totalBytes > 0 && (
                        <>
                            <span className="status-divider" />
                            <span className="status-item">
                                <span className="status-label">{t('common.labels.size', 'Size')}</span>
                                <span className="status-value">{formatBytes(stats.totalBytes)}</span>
                            </span>
                        </>
                    )}
                </div>
                <div className="status-bar-right">
                    {stats.selectedCount > 0 && (
                        <span className="status-selection">
                            {stats.selectedCount} {t('app.fileManager.selected', 'selected ·')} {formatBytes(stats.selectedBytes)}
                        </span>
                    )}
                    <span className="status-shortcuts" title={t('app.fileManager.keyboardShortcuts', 'Keyboard shortcuts')}>
                        {t('app.fileManager.shortcutHint', '⌫ Up · Del Delete · F2 Rename · ⌘C/X/V Copy, cut, paste · ⌘A All')}
                    </span>
                </div>
            </div>

            <ContextMenu
                menu={contextMenu}
                selectionCount={selectedEntries.length}
                onClose={() => setContextMenu(null)}
                onOpen={handleOpen}
                onEdit={openInEditor}
                onDownload={(e) => fileApi.download(e)}
                onRename={openRenameModal}
                onPermissions={openPermissionsModal}
                onCopyPath={copyPathToClipboard}
                onDelete={(e) => handleDelete(selectedEntries.length > 1 ? selectedEntries : e)}
                onCopy={transfersEnabled ? () => copyToClipboard('copy', selectedEntries.length > 0 ? selectedEntries : [contextMenu.entry]) : undefined}
                onCut={transfersEnabled ? () => copyToClipboard('cut', selectedEntries.length > 0 ? selectedEntries : [contextMenu.entry]) : undefined}
                onPasteInto={transfersEnabled && clipboard ? pasteInto : undefined}
                onCompress={transfersEnabled ? () => openCompress(selectedEntries.length > 0 ? selectedEntries : [contextMenu.entry]) : undefined}
                onExtract={transfersEnabled ? openExtract : undefined}
                onDownloadZip={transfersEnabled ? () => handleDownloadZip(selectedEntries.length > 0 ? selectedEntries : [contextMenu.entry]) : undefined}
            />

            {dialog?.kind === 'newFolder' && (
                <NamePromptModal
                    title={t('app.fileManager.createNewFolder', 'Create new folder')}
                    label={t('app.fileManager.folderName', 'Folder name')}
                    placeholder="new-folder"
                    hint={<p className="text-muted">{t('app.fileManager.willBeCreatedIn', 'Will be created in:')} <code>{currentPath}</code></p>}
                    submitLabel={t('app.fileManager.createFolder', 'Create folder')}
                    onSubmit={handleCreateFolder}
                    onClose={closeDialog}
                />
            )}
            {dialog?.kind === 'newFile' && (
                <NamePromptModal
                    title={t('app.fileManager.createNewFile', 'Create new file')}
                    label={t('app.fileManager.fileName', 'File name')}
                    placeholder="index.php"
                    hint={<p className="text-muted">{t('app.fileManager.willBeCreatedIn', 'Will be created in:')} <code>{currentPath}</code></p>}
                    submitLabel={t('app.fileManager.createFile', 'Create file')}
                    onSubmit={handleCreateFile}
                    onClose={closeDialog}
                />
            )}
            {dialog?.kind === 'compress' && (
                <CompressModal
                    items={dialog.entries.length}
                    initialName={defaultArchiveName(dialog.entries, currentPath)}
                    folder={currentPath}
                    onSubmit={(choice) => handleCompress(dialog.entries, choice)}
                    onClose={closeDialog}
                />
            )}
            {dialog?.kind === 'extract' && (
                <ExtractModal
                    key={dialog.entry.path}
                    entry={dialog.entry}
                    initialDest={joinPath(parentOf(dialog.entry.path), archiveStem(dialog.entry.name))}
                    onSubmit={(dest) => handleExtract(dialog.entry, dest)}
                    onClose={closeDialog}
                />
            )}
            {dialog?.kind === 'rename' && (
                <NamePromptModal
                    key={dialog.entry.path}
                    title={t('app.fileManager.rename2', 'Rename {{value}}', { value: dialog.entry.is_dir ? 'Folder' : 'File' })}
                    label={t('app.fileManager.newName', 'New name')}
                    initialValue={dialog.entry.name}
                    submitLabel={t('app.fileManager.rename', 'Rename')}
                    onSubmit={(name) => handleRename(dialog.entry, name)}
                    onClose={closeDialog}
                />
            )}
            {dialog?.kind === 'permissions' && (
                <PermissionsModal
                    key={dialog.entry.path}
                    entry={dialog.entry}
                    onSubmit={(mode) => handleChangePermissions(dialog.entry, mode)}
                    onClose={closeDialog}
                />
            )}

            {confirmDialog && (
                <ConfirmDialog
                    title={confirmDialog.title}
                    message={confirmDialog.message}
                    confirmText={confirmDialog.confirmText}
                    variant={confirmDialog.variant}
                    onConfirm={confirmDialog.onConfirm}
                    onCancel={confirmDialog.onCancel}
                />
            )}
        </div>
    );
}

export default FileManager;
