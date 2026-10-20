"""The fork's "PHP (Nginx + PHP-FPM)" template renders a loopback-only Nginx in
front of a PHP-FPM build of the chosen version. Fork: arya-arav/ServerKit."""
import os

import yaml

from app.services.template_service import TemplateService

TEMPLATE = os.path.join(os.path.dirname(__file__), '..', 'templates', 'php-nginx-fpm.yaml')


def render(tmp_path, **overrides):
    with open(TEMPLATE) as fh:
        template = yaml.safe_load(fh)
    variables = {v['name']: v.get('default', '') for v in template['variables']}
    variables.update({'APP_NAME': 'shop', 'HTTP_PORT': '8123'}, **overrides)
    result = TemplateService._render_compose_and_files(template, variables, str(tmp_path))
    assert result['success'], result
    files = {os.path.basename(f['path']): f['content'] for f in result['files']}
    return yaml.safe_load(result['compose_content']), files


def test_web_port_is_loopback_only(tmp_path):
    compose, _ = render(tmp_path)
    assert compose['services']['web']['ports'] == ['127.0.0.1:8123:80']
    assert 'ports' not in compose['services']['php']


def test_dockerfile_uses_chosen_php_version_and_extensions(tmp_path):
    _, files = render(tmp_path, PHP_VERSION='7.0', PHP_EXTENSIONS='mysqli exif')
    assert files['Dockerfile'].startswith('FROM php:7.0-fpm\n')
    assert 'docker-php-ext-install mysqli exif' in files['Dockerfile']


def test_nginx_config_keeps_its_own_variables(tmp_path):
    _, files = render(tmp_path, DOCUMENT_ROOT='/var/www/html/public')
    conf = files['default.conf']
    assert 'root /var/www/html/public;' in conf
    assert 'try_files $uri $uri/ /index.php?$query_string;' in conf
    assert '$document_root$fastcgi_script_name' in conf
    assert 'fastcgi_pass php:9000;' in conf


def test_nginx_config_is_mounted_and_html_volume_kept(tmp_path):
    compose, _ = render(tmp_path)
    web_volumes = compose['services']['web']['volumes']
    assert './default.conf:/etc/nginx/conf.d/default.conf' in web_volumes
    assert 'html:/var/www/html:ro' in web_volumes
    assert 'html' in compose['volumes']
