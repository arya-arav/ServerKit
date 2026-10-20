"""GET /api/v1/apps/<id>/details — the service page's App details card.
Fork: arya-arav/ServerKit."""
import json

from factories import headers_for, make_application, make_user

from app.services.app_details_service import AppDetailsService


class FakeDocker:
    """Stands in for DockerService: one running PHP container."""

    @staticmethod
    def get_all_app_containers(app):
        return [{'id': 'c1', 'name': 'shop-php', 'service': 'php', 'state': 'running'}]

    @staticmethod
    def run(args, timeout=60, **kwargs):
        if args[0] == 'inspect':
            return {'success': True, 'output': json.dumps({
                'Config': {'Image': 'php:7.0-fpm'},
                'State': {'Status': 'running', 'Running': True, 'StartedAt': '2026-10-20T03:00:00Z'},
                'NetworkSettings': {'Ports': {'9000/tcp': None}},
            })}
        if args[0] == 'exec':
            return {'success': True, 'output': '7.0.33\n'}
        return {'success': False, 'output': ''}


def test_details_show_template_options_containers_and_php_version(app, client, tmp_path, monkeypatch):
    from app import db
    from app.services import docker_service
    monkeypatch.setattr(docker_service, 'DockerService', FakeDocker)
    (tmp_path / '.serverkit-template.json').write_text(json.dumps({
        'template_id': 'php-nginx-fpm', 'template_name': 'PHP (Nginx + PHP-FPM)',
        'template_version': '8.3', 'installed_at': '2026-10-20T03:00:00',
        'variables': {'PHP_VERSION': '7.0', 'DB_PASSWORD': 'hunter2', 'HTTP_PORT': '8110'},
    }))
    admin = make_user(db, role='admin')
    a = make_application(db, name='shop', root_path=str(tmp_path), port=8110, user_id=admin.id)

    r = client.get(f'/api/v1/apps/{a.id}/details', headers=headers_for(admin))
    assert r.status_code == 200
    body = r.get_json()
    assert body['template']['id'] == 'php-nginx-fpm'
    options = {v['name']: v for v in body['template']['variables']}
    assert options['PHP_VERSION']['value'] == '7.0'
    assert options['DB_PASSWORD']['secret'] is True
    assert 'hunter2' not in r.get_data(as_text=True)
    container = body['containers'][0]
    assert container['image'] == 'php:7.0-fpm'
    assert container['php_version'] == '7.0.33'
    assert container['ports'] == ['9000/tcp']
    assert body['service']['root_path'] == str(tmp_path)


def test_details_without_template_file_still_answers(app, client, tmp_path, monkeypatch):
    from app import db
    from app.services import docker_service
    monkeypatch.setattr(docker_service, 'DockerService', FakeDocker)
    admin = make_user(db, role='admin')
    a = make_application(db, name='plain', root_path=str(tmp_path), user_id=admin.id)
    r = client.get(f'/api/v1/apps/{a.id}/details', headers=headers_for(admin))
    assert r.status_code == 200
    assert r.get_json()['template'] is None


def test_details_need_access_to_the_app(app, client, tmp_path):
    from app import db
    owner = make_user(db, role='admin')
    stranger = make_user(db, role='developer')
    a = make_application(db, name='private', root_path=str(tmp_path), user_id=owner.id)
    r = client.get(f'/api/v1/apps/{a.id}/details', headers=headers_for(stranger))
    assert r.status_code in (403, 404)


def test_parse_ports_formats_bindings():
    ports = AppDetailsService.parse_ports({
        '80/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8071'}, {'HostIp': '127.0.0.1', 'HostPort': '8071'}],
        '9000/tcp': None,
    })
    assert ports == ['127.0.0.1:8071 → 80/tcp', '9000/tcp']
