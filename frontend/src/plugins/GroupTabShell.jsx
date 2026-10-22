// A tab an extension adds to a core group (Security, Files, …) renders in the
// same shell as the group's own tabs: the page gutter, plus the group's page
// class and .tab-content that its card styles are scoped to
// (.security-page .tab-content .card). Without it, extension tabs sat flush
// against the sidebar with bare cards. Fork: arya-arav/ServerKit.
export default function GroupTabShell({ group, children }) {
    return (
        <div className={`sk-tabgroup__inner ${group}-page sk-ext-tab`}>
            <div className="tab-content">{children}</div>
        </div>
    );
}
