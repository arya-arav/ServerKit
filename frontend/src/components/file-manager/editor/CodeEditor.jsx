import { useEffect, useRef } from 'react';
import { EditorState, Compartment } from '@codemirror/state';
import {
    EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
    drawSelection, dropCursor, highlightSpecialChars, rectangularSelection, crosshairCursor,
} from '@codemirror/view';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search';
import { syntaxHighlighting, indentOnInput, bracketMatching, foldGutter, foldKeymap } from '@codemirror/language';
import { closeBrackets, closeBracketsKeymap, autocompletion, completionKeymap } from '@codemirror/autocomplete';
import { classHighlighter } from '@lezer/highlight';
import { loadLanguage } from './languages';

// One CodeMirror 6 view per open tab. The parent owns the text (`value` is the
// tab's draft); this component keeps the view in sync without re-creating it,
// so undo history, scroll and selection survive re-renders. Colours come from
// SCSS (.cm-* and .tok-* classes over theme tokens), not from JS.
// Fork: arya-arav/ServerKit.
export default function CodeEditor({
    value, language, readOnly = false, wrap = false,
    onChange, onSave, onCursor, autoFocus = false, ariaLabel,
}) {
    const hostRef = useRef(null);
    const viewRef = useRef(null);
    const handlers = useRef({ onChange, onSave, onCursor });
    handlers.current = { onChange, onSave, onCursor };
    const compartments = useRef({
        language: new Compartment(), readOnly: new Compartment(), wrap: new Compartment(),
    });

    useEffect(() => {
        const c = compartments.current;
        const state = EditorState.create({
            doc: value ?? '',
            extensions: [
                lineNumbers(), highlightActiveLineGutter(), highlightSpecialChars(), history(),
                foldGutter(), drawSelection(), dropCursor(), EditorState.allowMultipleSelections.of(true),
                indentOnInput(), syntaxHighlighting(classHighlighter), bracketMatching(), closeBrackets(),
                autocompletion(), rectangularSelection(), crosshairCursor(), highlightActiveLine(),
                highlightSelectionMatches(),
                keymap.of([
                    { key: 'Mod-s', preventDefault: true, run: () => { handlers.current.onSave?.(); return true; } },
                    ...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap,
                    ...foldKeymap, ...completionKeymap, indentWithTab,
                ]),
                c.language.of([]),
                c.readOnly.of(EditorState.readOnly.of(readOnly)),
                c.wrap.of(wrap ? EditorView.lineWrapping : []),
                EditorView.contentAttributes.of({ 'aria-label': ariaLabel || 'Code editor' }),
                EditorView.updateListener.of((update) => {
                    if (update.docChanged) handlers.current.onChange?.(update.state.doc.toString());
                    if (update.selectionSet || update.docChanged) {
                        const head = update.state.selection.main.head;
                        const line = update.state.doc.lineAt(head);
                        handlers.current.onCursor?.({ line: line.number, col: head - line.from + 1 });
                    }
                }),
            ],
        });
        const view = new EditorView({ state, parent: hostRef.current });
        viewRef.current = view;
        if (autoFocus) view.focus();
        return () => { view.destroy(); viewRef.current = null; };
        // The view is created once per mount; props below are synced by effects.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Text replaced from outside (Reload after a conflict).
    useEffect(() => {
        const view = viewRef.current;
        if (!view) return;
        const current = view.state.doc.toString();
        if (value !== undefined && value !== current) {
            view.dispatch({ changes: { from: 0, to: current.length, insert: value } });
        }
    }, [value]);

    useEffect(() => {
        let cancelled = false;
        loadLanguage(language).then((ext) => {
            if (!cancelled && viewRef.current) {
                viewRef.current.dispatch({
                    effects: compartments.current.language.reconfigure(ext || []),
                });
            }
        });
        return () => { cancelled = true; };
    }, [language]);

    useEffect(() => {
        viewRef.current?.dispatch({
            effects: compartments.current.readOnly.reconfigure(EditorState.readOnly.of(readOnly)),
        });
    }, [readOnly]);

    useEffect(() => {
        viewRef.current?.dispatch({
            effects: compartments.current.wrap.reconfigure(wrap ? EditorView.lineWrapping : []),
        });
    }, [wrap]);

    return <div className="fm-code-editor" ref={hostRef} />;
}
