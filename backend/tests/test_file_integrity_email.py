"""The file-integrity alert says which files changed. Fork: arya-arav/ServerKit.

The event carried only scope/counts/sample_paths, which the generic email
template doesn't render: the email read "File integrity changes detected"
and nothing else.
"""
from app.notifications import catalog, rendering
from app.services.file_integrity_service import FIM_EVENT_KEY, FileIntegrityService

RESULT = {
    'added': ['sites-enabled/new-site'],
    'removed': [],
    'modified': [{'path': 'sites-enabled/shop', 'what': ['hash', 'size']}],
    'baseline_created_at': '2026-10-22T04:22:18.390020+00:00',
}
COUNTS = {'added': 1, 'removed': 0, 'modified': 1}


def test_summary_and_facts_name_the_files():
    data = FileIntegrityService._describe_changes('nginx', COUNTS, RESULT)
    assert data['summary'].startswith('2 files changed in the nginx configuration')
    assert 'modified (hash, size): sites-enabled/shop' in data['summary']
    assert 'added: sites-enabled/new-site' in data['summary']
    assert {'label': 'Baseline from', 'value': '2026-10-22 04:22 UTC'} in data['facts']


def test_more_changes_than_listed_are_counted():
    many = {'added': [f'f{i}' for i in range(10)], 'removed': [], 'modified': []}
    data = FileIntegrityService._describe_changes('systemd', {'added': 25, 'removed': 0, 'modified': 0}, many)
    assert '(+15 more)' in data['summary']


def test_rendered_email_contains_the_changed_file(app):
    with app.app_context():
        FileIntegrityService.register_jobs()
        data = FileIntegrityService._describe_changes('nginx', COUNTS, RESULT)
        meta = catalog.resolve(FIM_EVENT_KEY, data)
        email = rendering.render_email(template=meta['template'], subject=meta['title'],
                                       severity='warning', data=data, recipient={})
        html = email['html'] if isinstance(email, dict) else str(email)
        assert 'sites-enabled/shop' in html


def test_alert_links_to_the_integrity_tab():
    assert catalog._LINKS[FIM_EVENT_KEY] == '/security/integrity'
