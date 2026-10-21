// File name → CodeMirror language, loaded on demand so each language pack is
// its own small chunk. Fork: arya-arav/ServerKit.

const stream = (load) => async () => {
    const [{ StreamLanguage }, mode] = await Promise.all([import('@codemirror/language'), load()]);
    return StreamLanguage.define(mode);
};

const LOADERS = {
    javascript: async () => (await import('@codemirror/lang-javascript')).javascript({ jsx: true }),
    typescript: async () => (await import('@codemirror/lang-javascript')).javascript({ jsx: true, typescript: true }),
    php: async () => (await import('@codemirror/lang-php')).php(),
    python: async () => (await import('@codemirror/lang-python')).python(),
    html: async () => (await import('@codemirror/lang-html')).html(),
    css: async () => (await import('@codemirror/lang-css')).css(),
    json: async () => (await import('@codemirror/lang-json')).json(),
    yaml: async () => (await import('@codemirror/lang-yaml')).yaml(),
    markdown: async () => (await import('@codemirror/lang-markdown')).markdown(),
    xml: async () => (await import('@codemirror/lang-xml')).xml(),
    sql: async () => (await import('@codemirror/lang-sql')).sql(),
    nginx: stream(async () => (await import('@codemirror/legacy-modes/mode/nginx')).nginx),
    shell: stream(async () => (await import('@codemirror/legacy-modes/mode/shell')).shell),
    dockerfile: stream(async () => (await import('@codemirror/legacy-modes/mode/dockerfile')).dockerFile),
    properties: stream(async () => (await import('@codemirror/legacy-modes/mode/properties')).properties),
    toml: stream(async () => (await import('@codemirror/legacy-modes/mode/toml')).toml),
};

export const LANGUAGE_LABELS = {
    javascript: 'JavaScript', typescript: 'TypeScript', php: 'PHP', python: 'Python', html: 'HTML',
    css: 'CSS', json: 'JSON', yaml: 'YAML', markdown: 'Markdown', xml: 'XML', sql: 'SQL',
    nginx: 'Nginx', shell: 'Shell', dockerfile: 'Dockerfile', properties: 'Config', toml: 'TOML',
    text: 'Plain text',
};

const BY_EXT = {
    js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
    ts: 'typescript', tsx: 'typescript',
    php: 'php', phtml: 'php', inc: 'php',
    py: 'python',
    html: 'html', htm: 'html', vue: 'html', twig: 'html', blade: 'html',
    css: 'css', scss: 'css', less: 'css',
    json: 'json', lock: 'json', webmanifest: 'json',
    yml: 'yaml', yaml: 'yaml',
    md: 'markdown', markdown: 'markdown',
    xml: 'xml', svg: 'xml',
    sql: 'sql',
    sh: 'shell', bash: 'shell', zsh: 'shell',
    ini: 'properties', env: 'properties', cfg: 'properties', properties: 'properties', htaccess: 'properties',
    toml: 'toml',
};

const BY_NAME = {
    dockerfile: 'dockerfile', '.env': 'properties', '.htaccess': 'properties',
    '.bashrc': 'shell', '.profile': 'shell', '.bash_profile': 'shell', '.zshrc': 'shell',
    'nginx.conf': 'nginx',
};

/** The language id for a file, from its name and folder; 'text' when unknown. */
export function languageFor(path) {
    const name = (path.split('/').pop() || '').toLowerCase();
    if (BY_NAME[name]) return BY_NAME[name];
    if (name.startsWith('.env')) return 'properties';
    if (name.startsWith('dockerfile')) return 'dockerfile';
    const ext = name.includes('.') ? name.split('.').pop() : '';
    if (ext === 'conf' && /\/nginx\//.test(path)) return 'nginx';
    if (ext === 'blade' || name.endsWith('.blade.php')) return 'php';
    return BY_EXT[ext] || 'text';
}

/** The CodeMirror extension for a language id, or null for plain text. */
export async function loadLanguage(id) {
    const loader = LOADERS[id];
    if (!loader) return null;
    try {
        return await loader();
    } catch {
        return null; // a language that fails to load degrades to plain text
    }
}
