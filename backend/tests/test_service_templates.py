"""Proving tests for the expanded service-template catalog (SERVICES_ROADMAP).

Universal contract (every bundled template): parses + passes the real
``TemplateService.validate_template``, has an ``id`` matching its filename, and
ships some icon. The roadmap templates added by this work additionally use a
registry-hosted icon (serverkit.ai proxies /imgs/template-icons/ from the
serverkit-templates repo; templates with no public logo keep an inline base64
icon), are brand-neutral, and use the list-shaped variables form. The
community-repo index generator (#28) must cover them all.
"""
import os

import pytest

from app.services.template_service import TemplateService

TEMPLATES_DIR = TemplateService.LOCAL_TEMPLATES_DIR

# Stable icon host: serverkit.ai proxies this path from the serverkit-templates
# repo's raw tree, so the URLs resolve for every logo the registry carries.
ICON_HOST = "https://serverkit.ai/imgs/template-icons/"

# Templates authored/converted for the services roadmap (Phases 0-3).
ROADMAP_IDS = sorted({
    "prompture-hub", "ollama-webui", "qdrant", "chroma", "litellm",
    "flowise", "langflow", "anythingllm", "librechat",
    "audiobookshelf", "calibre-web", "sonarr", "radarr", "prowlarr",
    "qbittorrent", "jellyseerr", "freshrss", "miniflux", "paperless-ngx",
    "stirling-pdf", "actualbudget", "firefly-iii", "memos", "vikunja", "plane",
    "authentik", "beszel", "signoz", "meilisearch", "typesense", "searxng",
    "gotify", "ntfy", "wg-easy", "pihole",
    "chatwoot", "documenso", "metabase", "posthog", "nodebb", "linkding",
    "karakeep",
})


def _template_files():
    return sorted(
        f for f in os.listdir(TEMPLATES_DIR) if f.endswith((".yaml", ".yml"))
    )


@pytest.mark.parametrize("filename", _template_files())
def test_template_parses_and_validates(filename):
    """Every bundled template parses, validates, has a matching id, and an icon."""
    path = os.path.join(TEMPLATES_DIR, filename)
    result = TemplateService.parse_template(path)
    assert result.get("success"), f"{filename}: {result.get('errors') or result.get('error')}"

    tmpl = result["template"]
    stem = filename.rsplit(".", 1)[0]
    assert tmpl.get("id") == stem, f"{filename}: id '{tmpl.get('id')}' != stem '{stem}'"
    assert tmpl.get("icon"), f"{filename}: no icon"


@pytest.mark.parametrize("tid", ROADMAP_IDS)
def test_roadmap_template_conventions(tid):
    """Roadmap templates use a registry-hosted icon, list-shaped vars, brand-neutral."""
    path = os.path.join(TEMPLATES_DIR, f"{tid}.yaml")
    assert os.path.exists(path), f"{tid}: template file missing"

    result = TemplateService.parse_template(path)
    assert result.get("success"), f"{tid}: {result.get('errors') or result.get('error')}"
    tmpl = result["template"]

    icon = str(tmpl.get("icon", ""))
    assert icon.startswith(ICON_HOST) or icon.startswith("data:image/svg+xml;base64,"), (
        f"{tid}: icon must be registry-hosted ({ICON_HOST}...) or an inline data URI"
    )

    variables = tmpl.get("variables", [])
    assert isinstance(variables, list), f"{tid}: variables should be a list"
    assert all(isinstance(v, dict) and "name" in v for v in variables), f"{tid}: bad variable"

    with open(path, encoding="utf-8") as fh:
        assert "coolify" not in fh.read().lower(), f"{tid}: templates must be brand-neutral"


def test_repo_index_covers_all_templates():
    idx = TemplateService.build_repo_index()
    assert idx["count"] == len(_template_files())
    assert idx["schema_version"] == TemplateService.SCHEMA_VERSION
    for t in idx["templates"]:
        assert t["id"] and t["name"] and t["version"], t
        assert t.get("icon"), t


@pytest.mark.parametrize("flag, expected", [("featured: true\n", True), ("", False)])
def test_featured_flag_reaches_the_catalog(tmp_path, flag, expected):
    """The Templates page features any template whose YAML says `featured: true`."""
    path = tmp_path / "demo.yaml"
    path.write_text(
        "id: demo\nname: Demo\nversion: '1'\ndescription: d\n" + flag
        + "compose:\n  services:\n    app:\n      image: nginx:1\n",
        encoding="utf-8",
    )
    entry = TemplateService._parse_local_entry(str(path), "demo")
    assert entry["featured"] is expected


def test_catalog_contains_roadmap_services():
    ids = {t["id"] for t in TemplateService.build_repo_index()["templates"]}
    missing = [e for e in ROADMAP_IDS if e not in ids]
    assert not missing, f"missing from catalog: {missing}"
