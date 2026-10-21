import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Save, SaveAll, WrapText, X, Lock, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import CodeEditor from './CodeEditor';
import { languageFor, LANGUAGE_LABELS } from './languages';

const WRAP_KEY = 'serverkit.fileEditor.wrap';

function readWrap() {
    try { return localStorage.getItem(WRAP_KEY) === '1'; } catch { return false; }
}

// The file manager's code editor: open files as tabs over the file list, each
// a CodeMirror view that stays mounted (undo history and scroll survive tab
// switches). The file manager owns the tab list and does the saving.
// Fork: arya-arav/ServerKit.
export default function EditorWorkspace({
    tabs, activePath, savingPaths, onActivate, onChange, onSave, onSaveAll, onReload, onCloseTab, onExit,
}) {
    const { t } = useTranslation();
    const [wrap, setWrap] = useState(readWrap);
    const [cursor, setCursor] = useState({ line: 1, col: 1 });
    const active = tabs.find((tab) => tab.path === activePath) || tabs[0];
    const dirtyCount = tabs.filter((tab) => tab.draft !== tab.content).length;

    const toggleWrap = () => {
        setWrap((on) => {
            try { localStorage.setItem(WRAP_KEY, on ? '0' : '1'); } catch { /* ignore */ }
            return !on;
        });
    };

    // Ctrl+S while focus is on the tab bar or buttons rather than the editor.
    useEffect(() => {
        const onKey = (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && active) {
                e.preventDefault();
                onSave(active.path);
            }
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [active, onSave]);

    if (!active) return null;
    const activeDirty = active.draft !== active.content;
    const lang = languageFor(active.path);

    return (
        <section className="fm-editor" aria-label={t('app.fileEditor.title', 'Code editor')}>
            <div className="fm-editor__bar">
                <div className="fm-editor__tabs" role="tablist">
                    {tabs.map((tab) => {
                        const dirty = tab.draft !== tab.content;
                        const isActive = tab.path === active.path;
                        return (
                            <div key={tab.path}
                                className={`fm-editor__tab${isActive ? ' is-active' : ''}${dirty ? ' is-dirty' : ''}`}>
                                <Button variant="unstyled" type="button" role="tab" aria-selected={isActive}
                                    className="fm-editor__tab-name" title={tab.path}
                                    onClick={() => onActivate(tab.path)}>
                                    {tab.readOnly && <Lock size={12} aria-hidden="true" />}
                                    {tab.name}
                                    {dirty && <span className="fm-editor__dot" aria-label={t('app.fileEditor.unsaved', 'Unsaved changes')} />}
                                </Button>
                                <Button variant="unstyled" type="button" className="fm-editor__tab-close"
                                    onClick={() => onCloseTab(tab.path)}
                                    aria-label={t('app.fileEditor.closeTab', 'Close {{name}}', { name: tab.name })}>
                                    <X size={13} aria-hidden="true" />
                                </Button>
                            </div>
                        );
                    })}
                </div>
                <div className="fm-editor__actions">
                    <Button variant="ghost" size="sm" onClick={() => onReload(active.path)}
                        title={t('app.fileEditor.reloadHint', 'Load the version on the server')}>
                        <RotateCcw size={15} aria-hidden="true" /> {t('app.fileEditor.reload', 'Reload')}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={toggleWrap} aria-pressed={wrap}
                        title={t('app.fileEditor.wrapHint', 'Wrap long lines')}>
                        <WrapText size={15} aria-hidden="true" /> {t('app.fileEditor.wrap', 'Wrap')}
                    </Button>
                    {dirtyCount > 1 && (
                        <Button variant="outline" size="sm" onClick={onSaveAll}>
                            <SaveAll size={15} aria-hidden="true" /> {t('app.fileEditor.saveAll', 'Save all')}
                        </Button>
                    )}
                    <Button size="sm" onClick={() => onSave(active.path)}
                        disabled={!activeDirty || active.readOnly || savingPaths.has(active.path)}
                        title={t('app.fileEditor.saveHint', 'Save (Ctrl+S)')}>
                        <Save size={15} aria-hidden="true" />
                        {savingPaths.has(active.path) ? t('common.saving', 'Saving…') : t('common.actions.save', 'Save')}
                    </Button>
                    <Button variant="unstyled" type="button" className="fm-editor__exit" onClick={onExit}
                        aria-label={t('app.fileEditor.closeEditor', 'Close the editor')}
                        title={t('app.fileEditor.closeEditor', 'Close the editor')}>
                        <X size={18} aria-hidden="true" />
                    </Button>
                </div>
            </div>

            <div className="fm-editor__body">
                {tabs.map((tab) => (
                    <div key={tab.path} className="fm-editor__pane" hidden={tab.path !== active.path}>
                        <CodeEditor
                            value={tab.draft}
                            language={languageFor(tab.path)}
                            readOnly={tab.readOnly}
                            wrap={wrap}
                            autoFocus={tab.path === active.path}
                            ariaLabel={tab.name}
                            onChange={(text) => onChange(tab.path, text)}
                            onSave={() => onSave(tab.path)}
                            onCursor={tab.path === active.path ? setCursor : undefined}
                        />
                    </div>
                ))}
            </div>

            <div className="fm-editor__status">
                <span className="fm-editor__path" title={active.path}>{active.path}</span>
                <span className="fm-editor__spacer" />
                {active.readOnly && <span>{t('app.fileEditor.readOnly', 'Read-only')}</span>}
                <span className={activeDirty ? 'fm-editor__state is-dirty' : 'fm-editor__state'}>
                    {activeDirty ? t('app.fileEditor.unsaved', 'Unsaved changes') : t('app.fileEditor.saved', 'Saved')}
                </span>
                <span>{t('app.fileEditor.cursor', 'Ln {{line}}, Col {{col}}', cursor)}</span>
                <span>{LANGUAGE_LABELS[lang] || lang}</span>
                <span className="fm-editor__hint">{t('app.fileEditor.keys', 'Ctrl+S save · Ctrl+F find')}</span>
            </div>
        </section>
    );
}
