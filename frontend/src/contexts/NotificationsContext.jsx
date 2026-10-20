import { useState, useEffect, useCallback } from 'react';
import api from '../services/api';
import socketService from '../services/socket';
import { useAuth } from './useAuth.js';
import { getDismissedNotices, addDismissedNotice, noticeToItem } from '../utils/dismissedNotices';

// Global in-app notification state: the bell's unread badge + recent list, kept
// live via the Socket.IO 'notification' push (see services/socket.js). The full
// history page paginates independently; this context holds only the recent slice.
//
// It also folds in live "system notices" (admin misconfiguration hints from
// /system/notices) so they surface in the notification center instead of stacking
// as banners at the top of the dashboard — only genuinely urgent ones get a banner.
import { NotificationsContext } from './useNotifications.js';

const RECENT_LIMIT = 12;
const MAX_BUFFERED = 30;



export function NotificationsProvider({ children }) {
    const { isAuthenticated, isAdmin } = useAuth();
    const [items, setItems] = useState([]);
    const [busUnread, setBusUnread] = useState(0);
    const [loading, setLoading] = useState(false);

    const refresh = useCallback(async () => {
        if (!isAuthenticated) return;
        setLoading(true);
        try {
            const [inbox, noticesRes] = await Promise.all([
                api.getInbox({ limit: RECENT_LIMIT }),
                isAdmin
                    ? api.getSystemNotices().catch(() => ({ notices: [] }))
                    : Promise.resolve({ notices: [] }),
            ]);
            const dismissed = getDismissedNotices();
            const noticeItems = (noticesRes?.notices || [])
                .filter((n) => !dismissed.includes(n.id))
                .map(noticeToItem);
            // Standing config notices sit above the recent bus deliveries.
            setItems([...noticeItems, ...(inbox.items || [])]);
            setBusUnread(inbox.unread_count || 0);
        } catch {
            // Non-fatal: the bell just stays at its last known state.
        } finally {
            setLoading(false);
        }
    }, [isAuthenticated, isAdmin]);

    useEffect(() => {
        if (!isAuthenticated) {
            socketService.disconnect();
            setItems([]);
            setBusUnread(0);
            return undefined;
        }
        refresh();
        socketService.connect();
        const off = socketService.on('notification', (n) => {
            setItems((prev) => [{ ...n, read: false }, ...prev].slice(0, MAX_BUFFERED));
            setBusUnread((count) => count + 1);
        });
        return off;
    }, [isAuthenticated, refresh]);

    const markRead = useCallback(async (deliveryId) => {
        setItems((prev) => prev.map((it) => (
            it.delivery_id === deliveryId ? { ...it, read: true } : it
        )));
        try {
            const res = await api.markNotificationRead(deliveryId);
            if (res && typeof res.unread_count === 'number') setBusUnread(res.unread_count);
        } catch {
            // ignore — the next refresh reconciles
        }
    }, []);

    const markAllRead = useCallback(async () => {
        // Notices aren't "read" — they clear when fixed or dismissed, so leave them.
        setItems((prev) => prev.map((it) => (it.kind === 'notice' ? it : { ...it, read: true })));
        setBusUnread(0);
        try {
            await api.markAllNotificationsRead();
        } catch {
            // ignore — the next refresh reconciles
        }
    }, []);

    const dismissNotice = useCallback((noticeId) => {
        addDismissedNotice(noticeId);
        setItems((prev) => prev.filter((it) => it.notice_id !== noticeId));
    }, []);

    // Remove one bus notification from the bell. Fork: arya-arav/ServerKit.
    const dismiss = useCallback(async (deliveryId) => {
        setItems((prev) => prev.filter((it) => it.delivery_id !== deliveryId));
        try {
            const res = await api.dismissNotification(deliveryId);
            if (res && typeof res.unread_count === 'number') setBusUnread(res.unread_count);
        } catch {
            // ignore — the next refresh reconciles
        }
    }, []);

    // Remove every read bus notification (notices clear when fixed or dismissed).
    const clearRead = useCallback(async () => {
        setItems((prev) => prev.filter((it) => it.kind === 'notice' || !it.read));
        try {
            await api.clearNotifications();
        } catch {
            // ignore — the next refresh reconciles
        }
    }, []);

    const noticeCount = items.reduce((n, it) => (it.kind === 'notice' ? n + 1 : n), 0);
    const unreadCount = busUnread + noticeCount;

    const value = {
        items, unreadCount, loading, refresh, markRead, markAllRead, dismissNotice, dismiss, clearRead,
    };
    return (
        <NotificationsContext.Provider value={value}>
            {children}
        </NotificationsContext.Provider>
    );
}
