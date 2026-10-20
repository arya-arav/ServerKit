// Cloudflare-only web ports — limit 80/443 to Cloudflare's edge. Fork: arya-arav/ServerKit.

export async function getCloudflareOnly() {
    return this.request('/firewall/cloudflare-only');
}

export async function setCloudflareOnly(enabled, force = false) {
    return this.request('/firewall/cloudflare-only', {
        method: 'PUT',
        body: { enabled, force }
    });
}

export async function refreshCloudflareOnly() {
    return this.request('/firewall/cloudflare-only/refresh', { method: 'POST' });
}
