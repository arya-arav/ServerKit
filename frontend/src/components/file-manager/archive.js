// Archives in the file manager: compress, extract, zip for download.
// The server runs each as a job; these start it and wait for it to end.
// Fork: arya-arav/ServerKit.
import { api } from '../../services/api';

export const ARCHIVE_FORMATS = [
    { id: 'zip', ext: '.zip' },
    { id: 'tar.gz', ext: '.tar.gz' },
    { id: 'tar', ext: '.tar' },
];

const ARCHIVE_SUFFIXES = ['.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz'];

export const isArchive = (entry) => Boolean(entry && !entry.is_dir
    && ARCHIVE_SUFFIXES.some((s) => entry.name.toLowerCase().endsWith(s)));

// "site.tar.gz" -> "site"
export function archiveStem(name) {
    const lower = name.toLowerCase();
    const suffix = [...ARCHIVE_SUFFIXES].sort((a, b) => b.length - a.length).find((s) => lower.endsWith(s));
    return (suffix ? name.slice(0, -suffix.length) : name) || name;
}

// A sensible archive name for a selection: the item's own name, or the
// folder's when several are packed together.
export function defaultArchiveName(entries, currentPath) {
    if (entries.length === 1) return entries[0].name;
    const folder = currentPath.replace(/\/+$/, '').split('/').pop();
    return folder || 'archive';
}

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// Starts a job and resolves with it once it ends. Failed and cancelled jobs
// reject with the server's message.
async function runJob(start) {
    const { job_id: jobId } = await start();
    for (let delay = 500; ; delay = Math.min(delay * 1.5, 3000)) {
        await sleep(delay);
        const job = await api.getFileJob(jobId);
        if (!TERMINAL.has(job.status)) continue;
        if (job.status !== 'succeeded') throw new Error(job.error || job.status);
        return job;
    }
}

export const compressInto = (paths, dest, format) => runJob(() => api.compressPaths(paths, dest, format));

// Resolves with the job result: { files, skipped, ... } or, when files would
// be replaced and overwrite wasn't asked for, { conflicts, examples }.
export const extractTo = (path, dest, overwrite = false) => runJob(() => api.extractArchive(path, dest, overwrite))
    .then((job) => job.result || {});

export async function downloadAsZip(paths) {
    const job = await runJob(() => api.prepareZipDownload(paths));
    api.downloadFileJob(job.id);
    return job.result || {};
}
