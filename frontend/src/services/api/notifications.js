// Notification Bus API methods. Mixed into ApiService (see ./index.js) so each
// function runs with `this` bound to the client and calls `this.request(...)`.

// --- In-app notification center (the bell + history) ---

export async function getInbox(params = {}) {
    const query = new URLSearchParams();
    if (params.limit) query.append('limit', params.limit);
    if (params.offset) query.append('offset', params.offset);
    if (params.unread) query.append('unread', '1');
    if (params.category) query.append('category', params.category);
    if (params.severity) query.append('severity', params.severity);
    const suffix = query.toString() ? `?${query}` : '';
    return this.request(`/notifications/inbox${suffix}`);
}

export async function getNotificationUnreadCount() {
    return this.request('/notifications/inbox/unread-count');
}

export async function markNotificationRead(deliveryId) {
    return this.request(`/notifications/inbox/${deliveryId}/read`, { method: 'POST' });
}

export async function markAllNotificationsRead(category = null) {
    const suffix = category ? `?category=${encodeURIComponent(category)}` : '';
    return this.request(`/notifications/inbox/read-all${suffix}`, { method: 'POST' });
}

// Remove from the bell (the admin delivery log keeps them). Fork: arya-arav/ServerKit.
export async function dismissNotification(deliveryId) {
    return this.request(`/notifications/inbox/${deliveryId}/dismiss`, { method: 'POST' });
}

export async function clearNotifications(all = false) {
    return this.request(`/notifications/inbox/clear${all ? '?all=1' : ''}`, { method: 'POST' });
}

// --- Org chat/webhook connections (admin) ---

export async function getChatConnections() {
    return this.request('/notifications/admin/chat-connections');
}

export async function addChatConnection(data) {
    return this.request('/notifications/admin/chat-connections', {
        method: 'POST',
        body: JSON.stringify(data),
    });
}

export async function updateChatConnection(connectionId, data) {
    return this.request(`/notifications/admin/chat-connections/${connectionId}`, {
        method: 'PUT',
        body: JSON.stringify(data),
    });
}

export async function testChatConnection(connectionId) {
    return this.request(`/notifications/admin/chat-connections/${connectionId}/test`, { method: 'POST' });
}

export async function setDefaultChatConnection(connectionId) {
    return this.request(`/notifications/admin/chat-connections/${connectionId}/default`, { method: 'POST' });
}

export async function deleteChatConnection(connectionId) {
    return this.request(`/notifications/admin/chat-connections/${connectionId}`, { method: 'DELETE' });
}

// --- Delivery log / ops (admin) ---

export async function getDeliveryLog(params = {}) {
    const query = new URLSearchParams();
    if (params.status) query.append('status', params.status);
    if (params.channel) query.append('channel', params.channel);
    if (params.limit) query.append('limit', params.limit);
    if (params.offset) query.append('offset', params.offset);
    const suffix = query.toString() ? `?${query}` : '';
    return this.request(`/notifications/admin/deliveries${suffix}`);
}

export async function retryDelivery(deliveryId) {
    return this.request(`/notifications/admin/deliveries/${deliveryId}/retry`, { method: 'POST' });
}

// --- Email provider integrations (admin) ---

export async function getEmailProviders() {
    return this.request('/notifications/admin/email-providers');
}

export async function addEmailProvider(data) {
    return this.request('/notifications/admin/email-providers', {
        method: 'POST',
        body: JSON.stringify(data),
    });
}

export async function testEmailProvider(providerId) {
    return this.request(`/notifications/admin/email-providers/${providerId}/test`, { method: 'POST' });
}

export async function setDefaultEmailProvider(providerId) {
    return this.request(`/notifications/admin/email-providers/${providerId}/default`, { method: 'POST' });
}

export async function deleteEmailProvider(providerId) {
    return this.request(`/notifications/admin/email-providers/${providerId}`, { method: 'DELETE' });
}
