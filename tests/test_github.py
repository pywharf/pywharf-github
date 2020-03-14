from datetime import datetime
import tempfile
from typing import Tuple
import os

import shortuuid
import github

from private_pypi_testkit import TestKit, RepoInfoForTest
from private_pypi_core.backend import (
        BackendInstanceManager,
        UploadIndexStatus,
)
from private_pypi_backends.github.impl import (
        GitHubConfig,
        GitHubAuthToken,
)


class GitHubTestKit(TestKit):

    @classmethod
    def setup_pkg_repo(cls) -> Tuple[GitHubConfig, GitHubAuthToken, GitHubAuthToken]:
        name = f'gh-{shortuuid.uuid()}'
        org = 'private-pypi-github-test-org'

        gh_read_auth_token = os.getenv('GITHUB_READ_AUTH_TOKEN')
        gh_write_auth_token = os.getenv('GITHUB_WRITE_AUTH_TOKEN')
        assert gh_read_auth_token and gh_write_auth_token

        gh_client = github.Github(gh_write_auth_token)
        gh_user = gh_client.get_user()
        gh_entity = gh_client.get_organization(org)

        timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
        description = ('Autogen test repo for the project private-pypi/private-pypi-github, '
                       f'created by user {gh_user.login}.')
        repo_name = f'private-pypi-github-test-{timestamp}'
        gh_entity.create_repo(
                name=repo_name,
                description=description,
                homepage='https://github.com/private-pypi/private-pypi-github',
                has_issues=False,
                has_wiki=False,
                has_downloads=False,
                has_projects=False,
                auto_init=True,
        )

        pkg_repo_config = GitHubConfig(
                name=name,
                owner=org,
                repo=repo_name,
        )
        read_secret = GitHubAuthToken(
                name=name,
                raw=gh_read_auth_token,
        )
        write_secret = GitHubAuthToken(
                name=name,
                raw=gh_write_auth_token,
        )

        return pkg_repo_config, read_secret, write_secret

    @classmethod
    def update_repo_index(cls, repo: RepoInfoForTest) -> bool:
        pkg_repo_shstg = repo.wstat.name_to_pkg_repo_shstg[repo.name]
        pkg_repo = pkg_repo_shstg.get_item(repo.write_secret)

        with tempfile.NamedTemporaryFile() as ntf:
            pkg_refs = pkg_repo.collect_all_published_packages()
            BackendInstanceManager.dump_pkg_refs(ntf.name, pkg_refs)

            assert not pkg_repo.local_index_is_up_to_date(ntf.name)
            result = pkg_repo.upload_index(ntf.name)
            return result.status == UploadIndexStatus.SUCCEEDED


GitHubTestKit.pytest_injection()
