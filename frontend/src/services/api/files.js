// File manager, FTP, Git server, domains, DNS zones, status pages

// File Manager endpoints
export async function browseFiles(path = '/home', showHidden = false) {
    const params = new URLSearchParams({ path, show_hidden: showHidden });
    return this.request(`/files/browse?${params}`);
}

export async function getFileInfo(path) {
    return this.request(`/files/info?path=${encodeURIComponent(path)}`);
}

export async function readFile(path) {
    return this.request(`/files/read?path=${encodeURIComponent(path)}`);
}

export async function writeFile(path, content, createBackup = true) {
    return this.request('/files/write', {
        method: 'POST',
        body: { path, content, create_backup: createBackup }
    });
}

export async function createFile(path, content = '') {
    return this.request('/files/create', {
        method: 'POST',
        body: { path, content }
    });
}

export async function createDirectory(path) {
    return this.request('/files/mkdir', {
        method: 'POST',
        body: { path }
    });
}

export async function deleteFile(path) {
    return this.request(`/files/delete?path=${encodeURIComponent(path)}`, {
        method: 'DELETE'
    });
}

export async function renameFile(path, newName) {
    return this.request('/files/rename', {
        method: 'POST',
        body: { path, new_name: newName }
    });
}

export async function copyFile(src, dest) {
    return this.request('/files/copy', {
        method: 'POST',
        body: { src, dest }
    });
}

export async function moveFile(src, dest) {
    return this.request('/files/move', {
        method: 'POST',
        body: { src, dest }
    });
}

export async function changeFilePermissions(path, mode) {
    return this.request('/files/chmod', {
        method: 'POST',
        body: { path, mode }
    });
}

export async function searchFiles(directory, pattern, maxResults = 100) {
    const params = new URLSearchParams({ directory, pattern, max_results: maxResults });
    return this.request(`/files/search?${params}`);
}

export async function getDiskUsage(path = '/') {
    return this.request(`/files/disk-usage?path=${encodeURIComponent(path)}`);
}

export async function getAllDiskMounts() {
    return this.request('/files/disk-mounts');
}

export async function analyzeDirectory(path = '/home', depth = 2, limit = 20) {
    const params = new URLSearchParams({ path, depth, limit });
    return this.request(`/files/analyze?${params}`);
}

export async function getFileTypeBreakdown(path = '/home', maxDepth = 3) {
    const params = new URLSearchParams({ path, max_depth: maxDepth });
    return this.request(`/files/type-breakdown?${params}`);
}

export async function downloadFile(path) {
    const token = this.getToken();
    const url = `${this.baseUrl}/files/download?path=${encodeURIComponent(path)}`;
    window.open(`${url}&token=${encodeURIComponent(token)}`, '_blank');
}

export async function getFileBlob(path) {
    return this.requestBlob(`/files/download?path=${encodeURIComponent(path)}`);
}

export async function uploadFile(destination, file, onProgress = null) {
    const token = this.getToken();
    const formData = new FormData();
    formData.append('file', file);
    formData.append('destination', destination);

    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', `${this.baseUrl}/files/upload`);
        xhr.setRequestHeader('Authorization', `Bearer ${token}`);

        if (onProgress) {
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) {
                    onProgress((e.loaded / e.total) * 100);
                }
            };
        }

        xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                resolve(JSON.parse(xhr.responseText));
            } else {
                reject(new Error(JSON.parse(xhr.responseText).error || 'Upload failed'));
            }
        };

        xhr.onerror = () => reject(new Error('Upload failed'));
        xhr.send(formData);
    });
}

// S3 / object-storage browser (reuses the configured backup storage creds).
// Paths are slash-rooted (e.g. "/photos/cat.jpg"); the backend maps them to keys.
export async function browseS3(path = '/') {
    return this.request(`/files/s3/browse?path=${encodeURIComponent(path)}`);
}

export async function readS3(path) {
    return this.request(`/files/s3/read?path=${encodeURIComponent(path)}`);
}

export async function writeS3(path, content) {
    return this.request('/files/s3/write', { method: 'POST', body: { path, content } });
}

export async function deleteS3(path) {
    return this.request(`/files/s3/delete?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
}

export async function getS3DownloadUrl(path) {
    return this.request(`/files/s3/download-url?path=${encodeURIComponent(path)}`);
}

export async function downloadS3File(path) {
    const res = await this.getS3DownloadUrl(path);
    if (res && res.url) window.open(res.url, '_blank');
    return res;
}

export async function uploadS3(destination, file, onProgress = null) {
    const token = this.getToken();
    const formData = new FormData();
    formData.append('file', file);
    formData.append('destination', destination);

    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', `${this.baseUrl}/files/s3/upload`);
        xhr.setRequestHeader('Authorization', `Bearer ${token}`);

        if (onProgress) {
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) onProgress((e.loaded / e.total) * 100);
            };
        }

        xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
            else reject(new Error(JSON.parse(xhr.responseText).error || 'Upload failed'));
        };
        xhr.onerror = () => reject(new Error('Upload failed'));
        xhr.send(formData);
    });
}

// FTP Server endpoints
export async function getFTPStatus() {
    return this.request('/ftp/status');
}

export async function controlFTPService(action, service = null) {
    return this.request(`/ftp/service/${action}`, {
        method: 'POST',
        body: service ? { service } : {}
    });
}

export async function getFTPConfig(service = null) {
    const params = service ? `?service=${encodeURIComponent(service)}` : '';
    return this.request(`/ftp/config${params}`);
}

export async function updateFTPConfig(config, service = null) {
    return this.request('/ftp/config', {
        method: 'POST',
        body: { config, service }
    });
}

export async function getFTPUsers() {
    return this.request('/ftp/users');
}

export async function createFTPUser(username, password = null, homeDir = null) {
    return this.request('/ftp/users', {
        method: 'POST',
        body: { username, password, home_dir: homeDir }
    });
}

export async function deleteFTPUser(username, deleteHome = false) {
    const params = deleteHome ? '?delete_home=true' : '';
    return this.request(`/ftp/users/${username}${params}`, {
        method: 'DELETE'
    });
}

export async function changeFTPPassword(username, password = null) {
    return this.request(`/ftp/users/${username}/password`, {
        method: 'POST',
        body: password ? { password } : {}
    });
}

export async function toggleFTPUser(username, enabled) {
    return this.request(`/ftp/users/${username}/toggle`, {
        method: 'POST',
        body: { enabled }
    });
}

export async function getFTPConnections() {
    return this.request('/ftp/connections');
}

export async function disconnectFTPSession(pid) {
    return this.request(`/ftp/connections/${pid}`, {
        method: 'DELETE'
    });
}

export async function getFTPLogs(lines = 100) {
    return this.request(`/ftp/logs?lines=${encodeURIComponent(lines)}`);
}

export async function installFTPServer(service = 'vsftpd') {
    return this.request('/ftp/install', {
        method: 'POST',
        body: { service }
    });
}

export async function testFTPConnection(host = 'localhost', port = 21, username = null, password = null) {
    return this.request('/ftp/test', {
        method: 'POST',
        body: { host, port, username, password }
    });
}

// The Gitea self-host surface (server lifecycle + repo browsing) moved into
// the serverkit-git extension with the Git page - its own
// client lives there. Core keeps the webhook + deployment endpoints below
// (they ARE the deploy pipeline) and three fail-soft Gitea aliases the
// app-creation repo pickers probe; those return 404 when the extension is
// absent and the pickers already degrade to "not installed".

// Git Webhooks
export async function getWebhooks() {
    return this.request('/git/webhooks');
}

export async function getWebhook(webhookId) {
    return this.request(`/git/webhooks/${webhookId}`);
}

export async function createWebhook(data) {
    return this.request('/git/webhooks', {
        method: 'POST',
        body: data
    });
}

export async function updateWebhook(webhookId, data) {
    return this.request(`/git/webhooks/${webhookId}`, {
        method: 'PUT',
        body: data
    });
}

export async function deleteWebhook(webhookId) {
    return this.request(`/git/webhooks/${webhookId}`, { method: 'DELETE' });
}

export async function toggleWebhook(webhookId) {
    return this.request(`/git/webhooks/${webhookId}/toggle`, { method: 'POST' });
}

export async function getGitWebhookLogs(webhookId, limit = 50) {
    return this.request(`/git/webhooks/${webhookId}/logs?limit=${encodeURIComponent(limit)}`);
}

export async function testWebhook(webhookId) {
    return this.request(`/git/webhooks/${webhookId}/test`, { method: 'POST' });
}

// Self-documenting aliases for the local Gitea endpoints used by repo pickers.
export async function getGiteaStatus() {
    return this.request('/git/status');
}

export async function getGiteaRepositories(limit = 50) {
    return this.request(`/git/repos?limit=${encodeURIComponent(limit)}`);
}

export async function getGiteaBranches(owner, repo) {
    return this.request(`/git/repos/${owner}/${repo}/branches`);
}

// Git Deployments
export async function getAppDeployments(appId, limit = 20) {
    return this.request(`/git/deployments/app/${appId}?limit=${encodeURIComponent(limit)}`);
}

export async function getDeployment(deploymentId, includeLogs = false) {
    return this.request(`/git/deployments/${deploymentId}?logs=${encodeURIComponent(includeLogs)}`);
}

export async function triggerGitDeploy(appId, branch = null) {
    return this.request(`/git/deployments/app/${appId}/deploy`, {
        method: 'POST',
        body: branch ? { branch } : {}
    });
}

export async function rollbackDeployment(appId, targetVersion = null) {
    return this.request(`/git/deployments/app/${appId}/rollback`, {
        method: 'POST',
        body: targetVersion ? { targetVersion } : {}
    });
}

export async function getWebhookDeployments(webhookId, limit = 20) {
    return this.request(`/git/deployments/webhook/${webhookId}?limit=${encodeURIComponent(limit)}`);
}

// Domains endpoints
export async function getDomains() {
    return this.request('/domains');
}

export async function getDomain(id) {
    return this.request(`/domains/${id}`);
}

export async function createDomain(domainData) {
    return this.request('/domains', {
        method: 'POST',
        body: domainData,
    });
}

export async function updateDomain(id, domainData) {
    return this.request(`/domains/${id}`, {
        method: 'PUT',
        body: domainData,
    });
}

export async function deleteDomain(id) {
    return this.request(`/domains/${id}`, {
        method: 'DELETE',
    });
}

// Move a domain to another service in one step (was: delete + re-add).
export async function moveDomain(id, applicationId) {
    return this.request(`/domains/${id}/move`, {
        method: 'POST',
        body: { application_id: applicationId },
    });
}

export async function enableSsl(domainId, email) {
    return this.request(`/domains/${domainId}/ssl/enable`, {
        method: 'POST',
        body: { email }
    });
}

export async function disableSsl(domainId) {
    return this.request(`/domains/${domainId}/ssl/disable`, { method: 'POST' });
}

export async function renewDomainSsl(domainId) {
    return this.request(`/domains/${domainId}/ssl/renew`, { method: 'POST' });
}

export async function verifyDomain(domainId) {
    return this.request(`/domains/${domainId}/verify`);
}

// Suggest a managed-sites subdomain (<slug>.<base>) for an app so the UI can
// prefill the "give this a subdomain" action.
export async function suggestSubdomain(applicationId, base) {
    const q = base ? `&base=${encodeURIComponent(base)}` : '';
    return this.request(`/domains/suggest-subdomain?application_id=${encodeURIComponent(applicationId)}${q}`);
}

// Base domains a site can be published under (<slug>.<base>), for the picker.
export async function getSiteBaseDomains() {
    return this.request('/domains/base-domains');
}

// Publish an app at <label>.<base> in one click. Label and base are optional —
// omit them when falsy so the backend derives the label from the app name and
// uses the default base domain.
export async function giveSubdomain(applicationId, label, base) {
    const body = { application_id: applicationId };
    if (label) body.label = label;
    if (base) body.base = base;
    return this.request('/domains/give-subdomain', { method: 'POST', body });
}

export async function getDomainsNginxSites() {
    return this.request('/domains/nginx/sites');
}

export async function getDomainsSslStatus() {
    return this.request('/domains/ssl/status');
}

// Status Pages endpoints
export async function getStatusPages() {
    return this.request('/status/');
}

export async function getStatusPage(id) {
    return this.request(`/status/${id}`);
}

export async function createStatusPage(data) {
    return this.request('/status/', { method: 'POST', body: data });
}

export async function updateStatusPage(id, data) {
    return this.request(`/status/${id}`, { method: 'PUT', body: data });
}

export async function deleteStatusPage(id) {
    return this.request(`/status/${id}`, { method: 'DELETE' });
}

export async function getPublicStatusPage(slug) {
    return this.request(`/status/public/${slug}`);
}

export async function getStatusPageComponents(pageId) {
    return this.request(`/status/${pageId}/components`);
}

export async function createStatusComponent(pageId, data) {
    return this.request(`/status/${pageId}/components`, { method: 'POST', body: data });
}

export async function updateStatusComponent(compId, data) {
    return this.request(`/status/components/${compId}`, { method: 'PUT', body: data });
}

export async function deleteStatusComponent(compId) {
    return this.request(`/status/components/${compId}`, { method: 'DELETE' });
}

export async function runStatusCheck(compId) {
    return this.request(`/status/components/${compId}/check`, { method: 'POST' });
}

export async function getStatusCheckHistory(compId, hours = 24) {
    return this.request(`/status/components/${compId}/history?hours=${encodeURIComponent(hours)}`);
}

export async function getStatusPageIncidents(pageId) {
    return this.request(`/status/${pageId}/incidents`);
}

export async function createStatusIncident(pageId, data) {
    return this.request(`/status/${pageId}/incidents`, { method: 'POST', body: data });
}

export async function updateStatusIncident(incidentId, data) {
    return this.request(`/status/incidents/${incidentId}`, { method: 'PUT', body: data });
}

export async function deleteStatusIncident(incidentId) {
    return this.request(`/status/incidents/${incidentId}`, { method: 'DELETE' });
}

export async function getStatusBadge(slug) {
    return this.request(`/status/badge/${slug}`);
}
