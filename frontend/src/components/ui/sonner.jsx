import { Toaster as SonnerToaster } from 'sonner';

export function Toaster() {
  return (
    <SonnerToaster
      // Bottom-right, clear of the 30px status bar; the top right holds the
      // app top bar's bell and account menu. Fork: arya-arav/ServerKit.
      position="bottom-right"
      offset={{ bottom: 42, right: 16 }}
      mobileOffset={{ bottom: 42 }}
      expand
      richColors
      toastOptions={{
        style: {
          background: 'var(--bg-card)',
          color: 'var(--text-primary)',
          border: '1px solid var(--border-default)',
        },
      }}
    />
  );
}
