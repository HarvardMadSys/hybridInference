export type Theme = 'light' | 'dark' | 'system';
export type ResolvedTheme = Exclude<Theme, 'system'>;
export const THEME_STORAGE_KEY = 'hybridinference-theme';
export const SYSTEM_THEME_QUERY = '(prefers-color-scheme: dark)';

// Runs in <head> before the console can paint. It only changes an appearance
// attribute, leaving the route owner's language and Site UI styling intact.
export const THEME_BOOTSTRAP_SCRIPT = `(()=>{let t='system';try{const v=localStorage.getItem('${THEME_STORAGE_KEY}');if(v==='light'||v==='dark'||v==='system')t=v}catch{}document.documentElement.dataset.consoleTheme=t==='system'?(typeof matchMedia==='function'&&matchMedia('${SYSTEM_THEME_QUERY}').matches?'dark':'light'):t})()`;
