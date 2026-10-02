from __future__ import annotations

import pytest
from pydantic import ValidationError
from repoatlas.config.settings import Settings


def test_existing_settings_default_to_no_salt_installations() -> None:
    assert Settings.model_validate({}).salt.installations == ()


def test_salt_root_configuration_records_installation_and_master_file() -> None:
    settings = Settings.model_validate(
        {
            "salt": {
                "installations": [
                    {
                        "name": "production",
                        "master_config": {"repository": "ops", "path": "etc/salt/master"},
                        "roots": [
                            {
                                "repository": "pillars",
                                "path": "pillar",
                                "role": "pillar",
                                "environment": "prod",
                                "salt_path": "/srv/pillar/prod",
                            }
                        ],
                    }
                ]
            }
        }
    )

    [installation] = settings.salt.installations
    [root] = installation.roots
    assert installation.name == "production"
    assert installation.master_config is not None
    assert (installation.master_config.repository, installation.master_config.path) == (
        "ops",
        "etc/salt/master",
    )
    assert (root.repository, root.path, root.role, root.environment, root.salt_path) == (
        "pillars",
        "pillar",
        "pillar",
        "prod",
        "/srv/pillar/prod",
    )


@pytest.mark.parametrize(
    "path",
    ["../outside", "/absolute", ""],
)
def test_salt_file_mapping_paths_must_stay_repository_relative(path: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "salt": {
                    "installations": [
                        {"name": "prod", "roots": [{"repository": "pillars", "path": path}]}
                    ]
                }
            }
        )



def test_salt_master_roots_must_be_static_absolute_paths() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "salt": {
                    "installations": [
                        {
                            "name": "prod",
                            "roots": [
                                {
                                    "repository": "pillars",
                                    "path": "pillar",
                                    "role": "pillar",
                                    "environment": "prod",
                                    "salt_path": "$PILLAR_ROOT",
                                }
                            ],
                        }
                    ]
                }
            }
        )
