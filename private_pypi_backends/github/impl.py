from enum import Enum, auto
from dataclasses import dataclass
import os
import os.path
import traceback
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import fire
import github
import requests
import toml

from private_pypi_core.backend import (
        BackendInstanceManager,
        LocalPaths,
        PkgRef,
        PkgRepo,
        PkgRepoConfig,
        PkgRepoSecret,
        PkgRepoIndex,
        UploadPackageStatus,
        UploadPackageResult,
        UploadPackageContext,
        UploadIndexStatus,
        UploadIndexResult,
        DownloadIndexStatus,
        DownloadIndexResult,
        record_error_if_raises,
        basic_model_get_default,
)
from private_pypi_core.workflow import (
        LinkItem,
        PAGE_TEMPLATE,
        build_page_api_simple,
)
from private_pypi_core.utils import git_hash_sha, split_package_ext

GITHUB_TYPE = 'github'


class GitHubConfig(PkgRepoConfig):
    # Override.
    type: str = GITHUB_TYPE
    max_file_bytes: int = 2 * 1024**3 - 1
    # GitHub specific.
    owner: str
    repo: str
    branch: str = 'master'
    index_filename: str = 'index.toml'

    def __init__(self, **data):
        super().__init__(**data)

        if not self.owner or not self.repo:
            raise ValueError('owner or repo is empty.')


class GitHubAuthToken(PkgRepoSecret):
    # Override.
    type: str = GITHUB_TYPE

    @property
    def token(self) -> str:
        return self.raw


class GitHubPkgRef(PkgRef):
    # Override.
    type: str = GITHUB_TYPE
    # GitHub specific.
    url: str

    def auth_url(self, config: GitHubConfig, secret: GitHubAuthToken) -> str:
        headers = {
                'Accept': 'application/octet-stream',
                'Authorization': f'token {secret.token}',
        }
        retry = 3
        response = None
        while retry > 0:
            try:
                response = requests.get(
                        self.url,
                        headers=headers,
                        allow_redirects=False,
                        timeout=1.0,
                )
                break
            except requests.Timeout:
                retry -= 1
                response = None
                continue

        assert retry > 0
        response.raise_for_status()
        assert response.status_code == 302

        parsed = urlparse(response.next.url)
        assert not parsed.fragment

        return parsed._replace(fragment=f'sha256={self.sha256}').geturl()


class JobType(Enum):
    UPLOAD_PACKAGE = auto()
    DOWNLOAD_PACKAGE = auto()


class GitHubUploadPackageContext(UploadPackageContext):
    release: Optional[github.GitRelease.GitRelease] = None

    class Config:
        arbitrary_types_allowed = True


LOCK_TIMEOUT = 0.5


@dataclass
class GitHubPkgRepoPrivateFields:
    ready: bool
    err_msg: str
    client: Optional[github.Github] = None
    fullname: Optional[str] = None
    repo: Optional[github.Repository.Repository] = None
    username: Optional[str] = None
    permission: Optional[str] = None


class GitHubPkgRepo(PkgRepo):
    # Override.
    type: str = GITHUB_TYPE
    # GitHub specific.
    config: GitHubConfig
    secret: GitHubAuthToken

    __slots__ = ('_private_fields',)

    @property
    def _pvt(self) -> GitHubPkgRepoPrivateFields:
        return object.__getattribute__(self, '_private_fields')

    def __init__(self, **data):
        super().__init__(**data)
        object.__setattr__(
                self,
                '_private_fields',
                GitHubPkgRepoPrivateFields(ready=True, err_msg=''),
        )

        try:
            self._pvt.client = github.Github(self.secret.token)
            self._pvt.fullname = f'{self.config.owner}/{self.config.repo}'
            self._pvt.repo = self._pvt.client.get_repo(self._pvt.fullname)
            self._pvt.username = self._pvt.client.get_user().login
            self._pvt.permission = self._pvt.repo.get_collaborator_permission(self._pvt.username)

        except Exception:  # pylint: disable=broad-except
            self.record_error(traceback.format_exc())

    def record_error(self, error_message: str) -> None:
        self._pvt.ready = False
        self._pvt.err_msg = error_message

    def ready(self) -> Tuple[bool, str]:
        return self._pvt.ready, self._pvt.err_msg

    def auth_read(self) -> bool:
        return self._pvt.permission != 'none'

    def auth_write(self) -> bool:
        return self._pvt.permission in ('admin', 'write')

    def _check_if_published_release_not_exists(self, ctx: GitHubUploadPackageContext):
        try:
            self._pvt.repo.get_release(ctx.filename)
            ctx.failed = True
            ctx.message = f'package={ctx.filename} has already exists.'

        except github.UnknownObjectException:
            # Release not exists, do nothing.
            return

        except github.BadCredentialsException:
            ctx.failed = True
            ctx.message = f'cannot get package={ctx.filename} due to invalid credential.'

        except github.GithubException as ex:
            ctx.failed = True
            ctx.message = 'github exception in conflict validation.\n' + str(ex.data)

    def _create_draft_release(self, ctx: GitHubUploadPackageContext):
        try:
            ctx.release = self._pvt.repo.create_git_release(
                    tag=ctx.filename,
                    name=ctx.filename,
                    message='',
                    draft=True,
            )

        except github.BadCredentialsException:
            ctx.failed = True
            ctx.message = f'cannot upload package={ctx.filename} due to invalid credential.'

        except github.GithubException as ex:
            ctx.failed = True
            ctx.message = 'github exception in draft release creation.\n' + str(ex.data)

    @staticmethod
    def _upload_package_as_release_asset(ctx: GitHubUploadPackageContext):
        # Upload as release asset.
        try:
            ctx.release.upload_asset(
                    ctx.path,
                    content_type='application/zip',
                    name=ctx.filename,
            )

        except github.GithubException as ex:
            ctx.failed = True
            ctx.message = 'github exception in asset upload.\n' + str(ex.data)

    @staticmethod
    def _fill_meta_and_publish_release(ctx: GitHubUploadPackageContext):
        body = toml.dumps(ctx.meta)
        try:
            ctx.release.update_release(
                    tag_name=ctx.filename,
                    name=ctx.filename,
                    message=body,
                    draft=False,
            )

        except github.GithubException as ex:
            ctx.failed = True
            ctx.message = 'github exception in release publishing.\n' + str(ex.data)

    def upload_package_job(self, filename: str, meta: Dict[str, str], path: str):
        ctx = GitHubUploadPackageContext(filename=filename, meta=meta, path=path)

        for action in (
                lambda _: None,  # Validate the context initialization.
                self._check_if_published_release_not_exists,
                self._create_draft_release,
                self._upload_package_as_release_asset,
                self._fill_meta_and_publish_release,
        ):
            action(ctx)
            if ctx.failed:
                break

        return ctx

    @record_error_if_raises
    def upload_package(self, filename: str, meta: Dict[str, str], path: str) -> UploadPackageResult:
        ctx = self.upload_package_job(filename, meta, path)

        if not ctx.failed:
            status = UploadPackageStatus.SUCCEEDED
        elif 'already exist' in ctx.message:
            status = UploadPackageStatus.CONFLICT
        else:
            status = UploadPackageStatus.BAD_REQUEST

        return UploadPackageResult(
                status=status,
                message=ctx.message,
        )

    @record_error_if_raises
    def collect_all_published_packages(self) -> List[GitHubPkgRef]:
        pkg_refs: List[GitHubPkgRef] = []

        for release in self._pvt.repo.get_releases():
            if release.draft:
                continue

            try:
                meta: Dict[str, str] = toml.loads(release.body)
            except toml.TomlDecodeError:
                continue

            distrib = meta.get('distrib')
            sha256 = meta.get('sha256')
            if not distrib or not sha256:
                continue

            package, ext = split_package_ext(release.tag_name)
            if not ext:
                continue

            raw_assets = release._rawData.get('assets')  # pylint: disable=protected-access
            if not raw_assets:
                continue
            url = None
            for raw_asset in raw_assets:
                if raw_asset.get('name') == release.tag_name:
                    url = raw_asset.get('url')
                    if url:
                        break
            if not url:
                continue

            pkg_ref = GitHubPkgRef(
                    distrib=distrib,
                    package=package,
                    ext=ext,
                    sha256=sha256,
                    meta=meta,
                    url=url,
            )
            pkg_refs.append(pkg_ref)

        return pkg_refs

    def _get_index_sha(self) -> Optional[str]:
        root_tree = self._pvt.repo.get_git_tree(self.config.branch, recursive=False)
        for tree_element in root_tree.tree:
            if tree_element.path == self.config.index_filename:
                return tree_element.sha
        return None

    def upload_index(self, path: str) -> UploadIndexResult:
        try:
            index_sha = self._get_index_sha()
            if index_sha is None:
                with open(path, 'rb') as fin:
                    content = fin.read()
                # Index file not exists, create file.
                self._pvt.repo.create_file(
                        path=self.config.index_filename,
                        message='Index file created.',
                        branch=self.config.branch,
                        content=content,
                )

            elif git_hash_sha(path) != index_sha:
                with open(path, 'rb') as fin:
                    content = fin.read()
                # Index file exists, and need to update.
                self._pvt.repo.update_file(
                        path=self.config.index_filename,
                        message='Index file updated.',
                        branch=self.config.branch,
                        sha=index_sha,
                        content=content,
                )

            return UploadIndexResult(status=UploadIndexStatus.SUCCEEDED)

        except Exception:  # pylint: disable=broad-except
            error_message = traceback.format_exc()
            self.record_error(error_message)
            return UploadIndexResult(status=UploadIndexStatus.FAILED, message=error_message)

    # This function could raise exception.
    def local_index_is_up_to_date(self, path: str) -> bool:
        if not os.path.exists(path):
            raise FileNotFoundError(f'{path} not exists.')

        index_sha = self._get_index_sha()
        return index_sha is not None and index_sha == git_hash_sha(path)

    @record_error_if_raises
    def download_index(self, path: str) -> DownloadIndexResult:
        try:
            if os.path.exists(path) and self.local_index_is_up_to_date(path):
                # Same file, no need to download.
                return DownloadIndexResult(status=DownloadIndexStatus.SUCCEEDED)

            content_file = self._pvt.repo.get_contents(
                    self.config.index_filename,
                    ref=self.config.branch,
            )
            with open(path, 'wb') as fout:
                fout.write(content_file.decoded_content)

            return DownloadIndexResult(status=DownloadIndexStatus.SUCCEEDED)

        except github.UnknownObjectException:
            # Index file not exists in remote.
            BackendInstanceManager.dump_pkg_refs_and_mtime(path, [])
            return DownloadIndexResult(status=DownloadIndexStatus.SUCCEEDED)

        except Exception:  # pylint: disable=broad-except
            error_message = traceback.format_exc()
            self.record_error(error_message)
            return DownloadIndexResult(status=DownloadIndexStatus.FAILED, message=error_message)


def github_init_pkg_repo(
        name: str,
        token: str,
        repo: str,
        owner: Optional[str] = None,
        branch: str = basic_model_get_default(GitHubConfig, 'branch'),
        index_filename: str = basic_model_get_default(GitHubConfig, 'index_filename'),
        sync_index_interval: int = basic_model_get_default(GitHubConfig, 'sync_index_interval'),
        private_pypi_version: str = '0.1.0a12',
        enable_gh_pages: bool = False,
        dry_run: bool = False,
):
    docker_image = f'docker://privatepypi/private-pypi:{private_pypi_version}'

    main_yaml = f'''\
name: update-index
on:
  push:
  schedule:
    - cron: "* * * * *"
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Split owner/repo
        run: |
          echo "::set-env name=OWNER::$(cut -d/ -f1 <<< ${{{{ github.repository }}}})"
          echo "::set-env name=REPO::$(cut -d/ -f2 <<< ${{{{ github.repository }}}})"
      - name: Update index.
        uses: {docker_image}
        with:
          args: >-
            update_index
            --type github
            --name ${{{{ env.REPO }}}}
            --secret ${{{{ github.token }}}}
            --owner ${{{{ env.OWNER }}}}
            --repo ${{{{ env.REPO }}}}
            --branch {branch}
            --index_filename {index_filename}
'''
    build_gh_pages_yaml = f'''
      - run: mkdir gh-pages
      - name: Generate public github pages.
        uses: {docker_image}
        with:
          args: >-
            github.gen_gh_pages
            --name ${{{{ env.REPO }}}}
            --token ${{{{ github.token }}}}
            --owner ${{{{ env.OWNER }}}}
            --repo ${{{{ env.REPO }}}}
            --branch {branch}
            --index_filename {index_filename}
            --output_folder gh-pages
      - name: Publish public github pages.
        run: |
          cd gh-pages
          git config --global user.email "github-actions@github.com"
          git config --global user.name "github-actions[bot]"
          git init
          git checkout -b gh-pages
          git add --all
          git commit -m "Publish github pages."
          git remote add origin https://${{{{ github.actor }}}}:${{{{ github.token }}}}@github.com/${{{{ env.OWNER }}}}/${{{{ env.REPO }}}}.git
          git push -u origin gh-pages --force
'''
    if enable_gh_pages:
        main_yaml += build_gh_pages_yaml

    if dry_run:
        print(main_yaml)
        return

    gh_client = github.Github(token)
    gh_user = gh_client.get_user()

    if owner is None or owner == gh_user.login:
        gh_entity = gh_user
    else:
        gh_entity = gh_client.get_organization(owner)

    # Create repo.
    description = ('Autogen package repository of private-pypi/private-pypi, '
                   f'created by user {gh_user.login}. ')
    gh_repo = gh_entity.create_repo(
            name=repo,
            description=description,
            homepage='https://github.com/private-pypi/private-pypi',
            has_issues=False,
            has_wiki=False,
            has_downloads=False,
            has_projects=False,
            auto_init=True,
    )
    # GitHub might take some time to create the READMD.md.
    time.sleep(3.0)

    # Default branch setup.
    master_ref = gh_repo.get_git_ref('heads/master')
    master_ref_sha = master_ref._rawData['object']['sha']  # pylint: disable=protected-access
    if branch != 'master':
        gh_repo.create_git_ref(f'refs/heads/{branch}', master_ref_sha)
        gh_repo.edit(default_branch=branch)

    gh_repo.create_file(
            path='.github/workflows/main.yml',
            message='Workflow update-index created.',
            branch=branch,
            content=main_yaml,
    )

    if enable_gh_pages:
        # Create branch gh-pages.
        gh_repo.create_git_ref(f'refs/heads/gh-pages', master_ref_sha)
        # Setup index.html.
        gh_repo.create_file(
                path='index.html',
                message='Workflow update-index created.',
                branch='gh-pages',
                content='Initialized.',
        )
        # Request page build to active github page feature.
        gh_repo._requester.requestJsonAndCheck(  # pylint: disable=protected-access
                'POST',
                gh_repo.url + '/pages/builds',
        )

    # Print config.
    github_config = GitHubConfig(
            name=name,
            owner=owner or gh_user.login,
            repo=repo,
            branch=branch,
            index_filename=index_filename,
    )

    github_config_dict = github_config.dict()
    github_config_dict.pop('name')

    # Pop the default settings.
    github_config_dict.pop('max_file_bytes')
    if branch == basic_model_get_default(GitHubConfig, 'branch'):
        github_config_dict.pop('branch')
    if index_filename == basic_model_get_default(GitHubConfig, 'index_filename'):
        github_config_dict.pop('index_filename')
    if sync_index_interval == basic_model_get_default(GitHubConfig, 'sync_index_interval'):
        github_config_dict.pop('sync_index_interval')

    print('Package repository TOML config (please add to your private-pypi config file):\n')
    print(toml.dumps({name: github_config_dict}))


def github_gen_gh_pages(
        name: str,
        token: str,
        owner: str,
        repo: str,
        output_folder: str,
        branch: str = basic_model_get_default(GitHubConfig, 'branch'),
        index_filename: str = basic_model_get_default(GitHubConfig, 'index_filename'),
):
    if not os.path.isdir(output_folder):
        raise FileNotFoundError(output_folder)

    pkg_repo_config = GitHubConfig(
            name=name,
            owner=owner,
            repo=repo,
            branch=branch,
            index_filename=index_filename,
    )
    pkg_repo_secret = GitHubAuthToken(
            name=name,
            raw=token,
    )
    local_paths = LocalPaths(
            index='/dev/null',
            log='/dev/null',
            lock='/dev/null',
            job='/dev/null',
            cache='/dev/null',
    )
    pkg_repo = GitHubPkgRepo(
            config=pkg_repo_config,
            secret=pkg_repo_secret,
            local_paths=local_paths,
    )

    # Download index file.
    index_path = os.path.join(output_folder, index_filename)
    result = pkg_repo.download_index(index_path)
    if result.status != DownloadIndexStatus.SUCCEEDED:
        raise RuntimeError(result.message)

    # Parse.
    bim = BackendInstanceManager()
    pkg_refs, mtime = bim.load_pkg_refs_and_mtime(index_path)
    pkg_repo_index = PkgRepoIndex(pkg_refs, mtime)

    # Homepage.
    index_html = build_page_api_simple(pkg_repo_index)
    with open(os.path.join(output_folder, 'index.html'), 'w') as fout:
        fout.write(index_html)

    # Distribution page.
    for distrib in pkg_repo_index.all_distributions:
        distrib_pkg_refs: List[GitHubPkgRef] = pkg_repo_index.get_pkg_refs(distrib)

        link_items = []
        for distrib_pkg_ref in distrib_pkg_refs:
            response = requests.get(distrib_pkg_ref.url)
            if response.status_code != 200:
                raise RuntimeError(f'Failed to request {distrib_pkg_ref.url}')
            browser_download_url = response.json()['browser_download_url']

            link_items.append(
                    LinkItem(
                            href=f'{browser_download_url}#sha256={distrib_pkg_ref.sha256}',
                            text=f'{distrib_pkg_ref.package}.{distrib_pkg_ref.ext}',
                    ))

        distrib_html = PAGE_TEMPLATE.render(
                title=f'Links for {distrib}',
                link_items=link_items,
        )

        distrib_folder = os.path.join(output_folder, distrib)
        os.mkdir(distrib_folder)
        with open(os.path.join(distrib_folder, 'index.html'), 'w') as fout:
            fout.write(distrib_html)


github_init_pkg_repo_cli = lambda: fire.Fire(github_init_pkg_repo)  # pylint: disable=invalid-name
github_gen_gh_pages_cli = lambda: fire.Fire(github_gen_gh_pages)  # pylint: disable=invalid-name
