"""Backups → Storage → Test connection sends the form, whose secrets are
masked; the test must use the stored ones. Fork: arya-arav/ServerKit."""
from app.services.storage_provider_service import StorageProviderService as S

SAVED = {'provider': 's3', 's3': {'bucket': 'b', 'region': 'r', 'access_key': 'AKIAREALACCESS',
                                  'secret_key': 'real-secret-value', 'endpoint_url': '',
                                  'path_prefix': 'serverkit-backups'}}


class FakeClient:
    def list_objects_v2(self, **kwargs):
        return {}


def test_masked_form_values_are_tested_with_the_saved_secrets(monkeypatch):
    monkeypatch.setattr(S, 'get_config', classmethod(lambda cls: {**SAVED, 's3': dict(SAVED['s3'])}))
    seen = {}

    def fake_client(cls, config=None):
        seen.update(config['s3'])
        return FakeClient(), 'b', 'serverkit-backups'

    monkeypatch.setattr(S, '_get_client', classmethod(fake_client))
    form = {'provider': 's3', 's3': {**SAVED['s3'], 'access_key': 'AKIA' + '*' * 10,
                                     'secret_key': 'real' + '*' * 13}}

    assert S.test_connection(form)['success'] is True
    assert seen['access_key'] == 'AKIAREALACCESS'
    assert seen['secret_key'] == 'real-secret-value'
    assert form['s3']['secret_key'].endswith('*')   # the caller's dict is left alone


def test_new_values_typed_into_the_form_are_tested_as_typed(monkeypatch):
    monkeypatch.setattr(S, 'get_config', classmethod(lambda cls: {**SAVED, 's3': dict(SAVED['s3'])}))
    seen = {}
    monkeypatch.setattr(S, '_get_client', classmethod(
        lambda cls, config=None: (seen.update(config['s3']), (FakeClient(), 'b', 'p'))[1]))

    S.test_connection({'provider': 's3', 's3': {**SAVED['s3'], 'secret_key': 'a-new-secret'}})

    assert seen['secret_key'] == 'a-new-secret'
