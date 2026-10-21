// Moves, copies and folder uploads for the file manager: path helpers and the
// drag-and-drop plumbing the page and its rows share. Fork: arya-arav/ServerKit.

import { api } from '../../services/api';

// Marks a drag that started on a file row or card. Files dragged in from the
// desktop carry 'Files' instead, and are uploads rather than moves.
export const INTERNAL_DRAG_TYPE = 'application/x-serverkit-paths';

const dragTypes = (e) => Array.from(e.dataTransfer?.types || []);
export const isInternalDrag = (e) => dragTypes(e).includes(INTERNAL_DRAG_TYPE);
export const isExternalFileDrag = (e) => !isInternalDrag(e) && dragTypes(e).includes('Files');

export function joinPath(dir, name) {
    return `${dir.replace(/\/+$/, '')}/${name}`;
}

export function parentOf(path) {
    const trimmed = path.replace(/\/+$/, '');
    const idx = trimmed.lastIndexOf('/');
    return idx <= 0 ? '/' : trimmed.slice(0, idx);
}

export const baseName = (path) => path.replace(/\/+$/, '').split('/').pop();

// True when `dest` is `src` or somewhere inside it.
export function isInside(dest, src) {
    const base = src.replace(/\/+$/, '');
    return dest === base || dest.startsWith(`${base}/`);
}

// "report.pdf" -> "report (copy).pdf", then "report (copy 2).pdf", ...
export function copyName(name, isDir, n) {
    const suffix = n <= 1 ? ' (copy)' : ` (copy ${n})`;
    const dot = name.lastIndexOf('.');
    if (isDir || dot <= 0) return name + suffix;
    return name.slice(0, dot) + suffix + name.slice(dot);
}

// What a drag or the clipboard carries: just enough to move or copy.
export const transferItems = (entries) => entries.map(({ path, name, is_dir }) => ({ path, name, is_dir }));

export function readDraggedItems(e) {
    try {
        const items = JSON.parse(e.dataTransfer.getData(INTERNAL_DRAG_TYPE) || '[]');
        return Array.isArray(items) ? items : [];
    } catch {
        return [];
    }
}

// ─── operations (panel server) ───────────────────────

export const moveInto = (item, targetDir) => api.moveFile(item.path, joinPath(targetDir, item.name || baseName(item.path)));

// Copies into targetDir under the first free name: the original name, then
// "name (copy)", "name (copy 2)"... A copy into its own folder skips the
// original name. Any error other than a name clash (409) is thrown.
export async function copyInto(item, targetDir) {
    const name = item.name || baseName(item.path);
    for (let n = parentOf(item.path) === targetDir ? 1 : 0; ; n++) {
        try {
            return await api.copyFile(item.path, joinPath(targetDir, n === 0 ? name : copyName(name, item.is_dir, n)));
        } catch (error) {
            if (error?.status !== 409 || n >= 50) throw error;
        }
    }
}

// One upload. relPath with folders ("site/css/app.css") recreates them under
// dir; an existing file is replaced only with `overwrite`.
export function uploadInto(dir, { file, relPath }, onProgress, { isS3 = false, overwrite = false } = {}) {
    const subdir = relPath.includes('/') ? relPath.slice(0, relPath.lastIndexOf('/')) : '';
    if (isS3) return api.uploadS3(subdir ? joinPath(dir, subdir) : dir, file, onProgress);
    return api.uploadFile(subdir ? joinPath(dir, relPath) : dir, file, onProgress, { overwrite });
}

export const createEmptyFile = (path) => api.createFile(path, '');

// The sub-folders of path, in the shape the folder tree caches.
export async function listSubfolders(path) {
    const data = await api.browseFiles(path, false);
    return (data.entries || []).filter((e) => e.is_dir).map((e) => ({ path: e.path, name: e.name }));
}

// Files dropped from the desktop, folders included, as [{ file, relPath }]
// where relPath keeps the folder structure ("site/css/app.css").
export async function collectDroppedFiles(dataTransfer) {
    // Entries must be taken before the first await: the browser empties the
    // DataTransfer once the drop event returns.
    const entries = Array.from(dataTransfer.items || [])
        .map((item) => item.webkitGetAsEntry?.())
        .filter(Boolean);
    if (entries.length === 0) {
        return Array.from(dataTransfer.files || []).map((file) => ({ file, relPath: file.name }));
    }
    const out = [];
    const walk = async (entry, prefix) => {
        if (entry.isFile) {
            const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
            out.push({ file, relPath: prefix + file.name });
        } else if (entry.isDirectory) {
            const reader = entry.createReader();
            // readEntries hands back batches until it returns an empty one.
            for (;;) {
                const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
                if (batch.length === 0) break;
                for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
            }
        }
    };
    for (const entry of entries) await walk(entry, '');
    return out;
}
