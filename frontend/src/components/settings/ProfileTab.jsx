import { useState, useEffect, useRef } from 'react';
import { useAuth } from '../../contexts/useAuth.js';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import useSettingFocus from '../../hooks/useSettingFocus';
import { useTranslation } from 'react-i18next';
import { formatDate } from '@/utils/intl';

// Profile pictures are cropped to a square and scaled down in the browser, so
// what reaches the server is a small image (the server allows 256 KB).
const AVATAR_SIZE = 256;
const AVATAR_MAX_BYTES = 256 * 1024;

function loadImage(file) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('unreadable')); };
        img.src = url;
    });
}

async function toAvatarDataUrl(file) {
    const img = await loadImage(file);
    const side = Math.min(img.naturalWidth, img.naturalHeight);
    const canvas = document.createElement('canvas');
    canvas.width = AVATAR_SIZE;
    canvas.height = AVATAR_SIZE;
    canvas.getContext('2d').drawImage(
        img,
        (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side,
        0, 0, AVATAR_SIZE, AVATAR_SIZE,
    );
    const webp = canvas.toDataURL('image/webp', 0.85);
    // Browsers that cannot encode WEBP hand back a PNG; JPEG is smaller then.
    return webp.startsWith('data:image/webp') ? webp : canvas.toDataURL('image/jpeg', 0.9);
}

const ProfileTab = () => {
    const { t } = useTranslation();
    const { user, updateUser } = useAuth();
    const register = useSettingFocus();
    const [formData, setFormData] = useState({
        first_name: '',
        last_name: '',
        username: '',
        email: ''
    });
    const [loading, setLoading] = useState(false);
    const [avatarBusy, setAvatarBusy] = useState(false);
    const [message, setMessage] = useState(null);
    const fileRef = useRef(null);

    useEffect(() => {
        if (user) {
            setFormData({
                first_name: user.first_name || '',
                last_name: user.last_name || '',
                username: user.username || '',
                email: user.email || ''
            });
        }
    }, [user]);

    async function saveAvatar(avatar) {
        setAvatarBusy(true);
        setMessage(null);
        try {
            await updateUser({ avatar });
            setMessage({ type: 'success', text: avatar
                ? t('app.profileTab.pictureUpdated', 'Profile picture updated')
                : t('app.profileTab.pictureRemoved', 'Profile picture removed') });
        } catch (err) {
            setMessage({ type: 'error', text: err.message });
        } finally {
            setAvatarBusy(false);
        }
    }

    async function handleAvatarFile(e) {
        const file = e.target.files?.[0];
        e.target.value = '';
        if (!file) return;
        if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
            setMessage({ type: 'error', text: t('app.profileTab.pictureType', 'Choose a PNG, JPEG or WEBP image.') });
            return;
        }
        let dataUrl;
        try {
            dataUrl = await toAvatarDataUrl(file);
        } catch {
            setMessage({ type: 'error', text: t('app.profileTab.pictureUnreadable', 'That image could not be read.') });
            return;
        }
        if (dataUrl.length * 0.75 > AVATAR_MAX_BYTES) {
            setMessage({ type: 'error', text: t('app.profileTab.pictureTooLarge', 'That image is too large.') });
            return;
        }
        await saveAvatar(dataUrl);
    }

    const initial = (user?.display_name || user?.username || 'U').charAt(0).toUpperCase();

    async function handleSubmit(e) {
        e.preventDefault();
        setLoading(true);
        setMessage(null);

        try {
            await updateUser(formData);
            setMessage({ type: 'success', text: 'Profile updated successfully' });
        } catch (err) {
            setMessage({ type: 'error', text: err.message });
        } finally {
            setLoading(false);
        }
    }

    return (
        <div className="settings-section">
            <div className="section-header">
                <h2>{t('app.profileTab.profileSettings', 'Profile settings')}</h2>
            </div>

            {message && (
                <div className={`alert alert-${message.type === 'success' ? 'success' : 'danger'}`}>
                    {message.text}
                </div>
            )}

            <div className="profile-picture">
                <span className="profile-picture__preview" aria-hidden="true">
                    {user?.avatar ? <img src={user.avatar} alt="" /> : initial}
                </span>
                <div className="profile-picture__body">
                    <span className="profile-picture__title">{t('app.profileTab.picture', 'Profile picture')}</span>
                    <span className="form-help">{t('app.profileTab.pictureHelp', 'PNG, JPEG or WEBP. Cropped to a square.')}</span>
                    <div className="profile-picture__actions">
                        <input
                            ref={fileRef}
                            type="file"
                            accept="image/png,image/jpeg,image/webp"
                            className="profile-picture__input"
                            onChange={handleAvatarFile}
                            aria-label={t('app.profileTab.choosePicture', 'Choose a profile picture')}
                        />
                        <Button type="button" variant="outline" size="sm" disabled={avatarBusy}
                            onClick={() => fileRef.current?.click()}>
                            {user?.avatar
                                ? t('app.profileTab.changePicture', 'Change picture')
                                : t('app.profileTab.uploadPicture', 'Upload picture')}
                        </Button>
                        {user?.avatar && (
                            <Button type="button" variant="ghost" size="sm" disabled={avatarBusy}
                                onClick={() => saveAvatar('')}>
                                {t('app.profileTab.removePicture', 'Remove')}
                            </Button>
                        )}
                    </div>
                </div>
            </div>

            <form onSubmit={handleSubmit} {...register('profile-username', 'settings-form')}>
                <div className="profile-names">
                    <div className="form-group">
                        <Label htmlFor="profile-first-name">{t('app.profileTab.firstName', 'First name')}</Label>
                        <Input
                            id="profile-first-name" type="text" autoComplete="given-name" maxLength={80}
                            value={formData.first_name}
                            onChange={(e) => setFormData({ ...formData, first_name: e.target.value })}
                        />
                    </div>
                    <div className="form-group">
                        <Label htmlFor="profile-last-name">{t('app.profileTab.lastName', 'Last name')}</Label>
                        <Input
                            id="profile-last-name" type="text" autoComplete="family-name" maxLength={80}
                            value={formData.last_name}
                            onChange={(e) => setFormData({ ...formData, last_name: e.target.value })}
                        />
                    </div>
                </div>
                <span className="form-help">{t('app.profileTab.nameHelp', 'Shown in the top bar instead of your username.')}</span>

                <div className="form-group">
                    <Label htmlFor="profile-username">{t('common.labels.username', 'Username')}</Label>
                    <Input
                        id="profile-username" type="text"
                        value={formData.username}
                        onChange={(e) => setFormData({ ...formData, username: e.target.value })}
                        required
                    />
                </div>

                <div className="form-group">
                    <Label htmlFor="profile-email">{t('app.profileTab.emailAddress', 'Email address')}</Label>
                    <Input
                        id="profile-email" type="email"
                        value={formData.email}
                        onChange={(e) => setFormData({ ...formData, email: e.target.value })}
                        required
                    />
                </div>

                <div className="form-group">
                    <Label htmlFor="profile-role">{t('app.profileTab.role', 'Role')}</Label>
                    <Input id="profile-role" type="text" value={user?.role || 'user'} readOnly />
                    <span className="form-help">{t('app.profileTab.contactAnAdministratorToChangeYour', 'Contact an administrator to change your role')}</span>
                </div>

                <div className="form-group">
                    <Label htmlFor="profile-member-since">{t('app.profileTab.memberSince', 'Member since')}</Label>
                    <Input
                        id="profile-member-since" type="text"
                        value={user?.created_at ? formatDate(user.created_at) : '-'}
                        readOnly
                    />
                </div>

                <div className="form-actions">
                    <Button type="submit" variant="default" disabled={loading}>
                        {loading ? t('common.saving', 'Saving…') : t('app.profileTab.saveChanges', 'Save changes')}
                    </Button>
                </div>
            </form>
        </div>
    );
};

export default ProfileTab;
