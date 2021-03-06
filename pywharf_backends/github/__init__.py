from pywharf_core.backend import BackendRegistration
from pywharf_backends.github.impl import (
        GITHUB_TYPE,
        GitHubConfig,
        GitHubAuthToken,
        GitHubPkgRepo,
        GitHubPkgRef,
        github_init_pkg_repo_cli,
        github_gen_gh_pages_cli,
)


class GitHubRegistration(BackendRegistration):
    type = GITHUB_TYPE
    pkg_repo_config_cls = GitHubConfig
    pkg_repo_secret_cls = GitHubAuthToken
    pkg_repo_cls = GitHubPkgRepo
    pkg_ref_cls = GitHubPkgRef
    cli_name_to_func = {
            'init_pkg_repo': github_init_pkg_repo_cli,
            'gen_gh_pages': github_gen_gh_pages_cli,
    }
