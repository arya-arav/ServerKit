from pathlib import Path

import pytest
import yaml

from app.services.template_service import TemplateService


@pytest.mark.parametrize('name', ['wordpress', 'wordpress-external-db'])
def test_wordpress_build_files_do_not_become_container_mounts(tmp_path, name):
    template = yaml.safe_load((Path(__file__).parents[1] / 'templates' / f'{name}.yaml').read_text())
    variables = {'APP_NAME': 'test-wp', 'VERSION': '7.1.3-php8.3-apache'}
    compose = tmp_path / 'docker-compose.yml'
    compose.write_text(TemplateService.generate_compose(template, variables))
    result = TemplateService._process_template_files(template['files'], str(tmp_path), str(compose), variables)
    assert result['success']
    config = yaml.safe_load(compose.read_text())
    assert config['services']['wordpress']['volumes'] == ['wordpress_html:/var/www/html']
    assert result['bind_mounts'] == 0
    dockerfile = (tmp_path / 'Dockerfile.serverkit-php').read_text()
    assert 'FROM wordpress:7.1.3-php8.3-apache' in dockerfile
    assert '8.3.36' in dockerfile and 'pdo_mysql' in dockerfile
    assert 'php_admin_flag session.upload_progress.enabled Off' in dockerfile
