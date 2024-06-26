# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for github runner charm integration tests."""
import logging
import random
import secrets
from pathlib import Path
from time import sleep
from typing import Any, AsyncIterator, Generator, Iterator, Optional

import nest_asyncio
import openstack
import openstack.connection
import pytest
import pytest_asyncio
import yaml
from git import Repo
from github import Github, GithubException
from github.Branch import Branch
from github.Repository import Repository
from juju.application import Application
from juju.client._definitions import FullStatus, UnitStatus
from juju.model import Model
from openstack.exceptions import ConflictException
from pytest_operator.plugin import OpsTest

from charm_state import (
    LABELS_CONFIG_NAME,
    OPENSTACK_CLOUDS_YAML_CONFIG_NAME,
    OPENSTACK_FLAVOR_CONFIG_NAME,
    OPENSTACK_NETWORK_CONFIG_NAME,
    PATH_CONFIG_NAME,
    USE_APROXY_CONFIG_NAME,
    VIRTUAL_MACHINES_CONFIG_NAME,
)
from github_client import GithubClient
from tests.integration.helpers.common import (
    InstanceHelper,
    deploy_github_runner_charm,
    inject_lxd_profile,
    reconcile,
    wait_for,
)
from tests.integration.helpers.lxd import LXDInstanceHelper, ensure_charm_has_runner
from tests.integration.helpers.openstack import OpenStackInstanceHelper
from tests.status_name import ACTIVE

# The following line is required because we are using request.getfixturevalue in conjunction
# with pytest-asyncio. See https://github.com/pytest-dev/pytest-asyncio/issues/112
nest_asyncio.apply()


@pytest.fixture(scope="module")
def metadata() -> dict[str, Any]:
    """Metadata information of the charm."""
    metadata = Path("./metadata.yaml")
    data = yaml.safe_load(metadata.read_text())
    return data


@pytest.fixture(scope="module")
def existing_app(pytestconfig: pytest.Config) -> Optional[str]:
    """The existing application name to use for the test."""
    return pytestconfig.getoption("--use-existing-app")


@pytest.fixture(scope="module")
def app_name(existing_app: Optional[str]) -> str:
    """Randomized application name."""
    # Randomized app name to avoid collision when runner is connecting to GitHub.
    return existing_app or f"integration-id{secrets.token_hex(2)}"


@pytest.fixture(scope="module")
def charm_file(
    pytestconfig: pytest.Config, loop_device: Optional[str], openstack_clouds_yaml: Optional[str]
) -> str:
    """Path to the built charm."""
    charm = pytestconfig.getoption("--charm-file")
    assert charm, "Please specify the --charm-file command line option"
    charm_path_str = f"./{charm}"

    if openstack_clouds_yaml:
        return charm_path_str

    inject_lxd_profile(charm_file=Path(charm_path_str), loop_device=loop_device)
    return charm_path_str


@pytest.fixture(scope="module")
def path(pytestconfig: pytest.Config) -> str:
    """Configured path setting."""
    path = pytestconfig.getoption("--path")
    assert path, (
        "Please specify the --path command line option with repository "
        "path of <org>/<repo> or <user>/<repo> format."
    )
    return path


@pytest.fixture(scope="module")
def token(pytestconfig: pytest.Config) -> str:
    """Configured token setting."""
    token = pytestconfig.getoption("--token")
    assert token, "Please specify the --token command line option"
    tokens = {token.strip() for token in token.split(",")}
    random_token = random.choice(list(tokens))
    return random_token


@pytest.fixture(scope="module")
def token_alt(pytestconfig: pytest.Config, token: str) -> str:
    """Configured token_alt setting."""
    token_alt = pytestconfig.getoption("--token-alt")
    assert token_alt, (
        "Please specify the --token-alt command line option with GitHub Personal "
        "Access Token value."
    )
    assert token_alt != token, "Please specify a different token for --token-alt"
    return token_alt


@pytest.fixture(scope="module")
def http_proxy(pytestconfig: pytest.Config) -> str:
    """Configured http_proxy setting."""
    http_proxy = pytestconfig.getoption("--http-proxy")
    return "" if http_proxy is None else http_proxy


@pytest.fixture(scope="module")
def https_proxy(pytestconfig: pytest.Config) -> str:
    """Configured https_proxy setting."""
    https_proxy = pytestconfig.getoption("--https-proxy")
    return "" if https_proxy is None else https_proxy


@pytest.fixture(scope="module")
def no_proxy(pytestconfig: pytest.Config) -> str:
    """Configured no_proxy setting."""
    no_proxy = pytestconfig.getoption("--no-proxy")
    return "" if no_proxy is None else no_proxy


@pytest.fixture(scope="module")
def loop_device(pytestconfig: pytest.Config) -> Optional[str]:
    """Configured loop_device setting."""
    return pytestconfig.getoption("--loop-device")


@pytest.fixture(scope="module")
def openstack_clouds_yaml(pytestconfig: pytest.Config) -> Optional[str]:
    """Configured clouds-yaml setting."""
    clouds_yaml = pytestconfig.getoption("--openstack-clouds-yaml")
    return Path(clouds_yaml).read_text(encoding="utf-8") if clouds_yaml else None


@pytest.fixture(scope="module", name="openstack_connection")
def openstack_connection_fixture(
    openstack_clouds_yaml: Optional[str],
) -> Generator[openstack.connection.Connection, None, None]:
    """The openstack connection instance."""
    assert openstack_clouds_yaml, "Openstack clouds yaml was not provided."

    openstack_clouds_yaml_yaml = yaml.safe_load(openstack_clouds_yaml)
    clouds_yaml_path = Path.cwd() / "clouds.yaml"
    clouds_yaml_path.write_text(data=openstack_clouds_yaml, encoding="utf-8")
    first_cloud = next(iter(openstack_clouds_yaml_yaml["clouds"].keys()))
    with openstack.connect(first_cloud) as conn:
        yield conn


@pytest.fixture(scope="module", name="openstack_flavor")
def openstack_flavor_fixture(
    openstack_connection: openstack.connection.Connection,
) -> str:
    """Name of the openstack flavor for runner."""
    flavor_name = "runner"
    try:
        openstack_connection.create_flavor(flavor_name, 4096, 2, 10)
    except ConflictException:
        # Do nothing if flavor already exists.
        pass
    return flavor_name


@pytest.fixture(scope="module")
def model(ops_test: OpsTest) -> Model:
    """Juju model used in the test."""
    assert ops_test.model is not None
    return ops_test.model


@pytest.fixture(scope="module")
def runner_manager_github_client(token: str) -> GithubClient:
    return GithubClient(token=token)


@pytest_asyncio.fixture(scope="module")
async def app_no_runner(
    model: Model,
    charm_file: str,
    app_name: str,
    path: str,
    token: str,
    http_proxy: str,
    https_proxy: str,
    no_proxy: str,
) -> AsyncIterator[Application]:
    """Application with no runner."""
    # Set the scheduled event to 1 hour to avoid interfering with the tests.
    application = await deploy_github_runner_charm(
        model=model,
        charm_file=charm_file,
        app_name=app_name,
        path=path,
        token=token,
        runner_storage="memory",
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        reconcile_interval=60,
    )
    return application


@pytest_asyncio.fixture(scope="module", name="app_openstack_runner")
async def app_openstack_runner_fixture(
    model: Model,
    charm_file: str,
    app_name: str,
    path: str,
    token: str,
    http_proxy: str,
    https_proxy: str,
    no_proxy: str,
    openstack_clouds_yaml: str,
    openstack_flavor: str,
    existing_app: Optional[str],
) -> AsyncIterator[Application]:
    """Application launching VMs and no runners."""
    if existing_app:
        application = model.applications[existing_app]
    else:
        application = await deploy_github_runner_charm(
            model=model,
            charm_file=charm_file,
            app_name=app_name,
            path=path,
            token=token,
            runner_storage="juju-storage",
            http_proxy=http_proxy,
            https_proxy=https_proxy,
            no_proxy=no_proxy,
            reconcile_interval=60,
            constraints={
                "root-disk": 50 * 1024,
                "cores": 4,
                "mem": 16 * 1024,
                "arch": "arm64",
            },
            config={
                OPENSTACK_CLOUDS_YAML_CONFIG_NAME: openstack_clouds_yaml,
                # this is set by microstack sunbeam, see scripts/setup-microstack.sh
                OPENSTACK_NETWORK_CONFIG_NAME: "demo-network",
                OPENSTACK_FLAVOR_CONFIG_NAME: openstack_flavor,
                USE_APROXY_CONFIG_NAME: "true",
                LABELS_CONFIG_NAME: app_name,
            },
            wait_idle=False,
            use_local_lxd=False,
        )
    await model.wait_for_idle(apps=[application.name], status=ACTIVE, timeout=90 * 60)

    return application


@pytest_asyncio.fixture(scope="module")
async def app_one_runner(model: Model, app_no_runner: Application) -> AsyncIterator[Application]:
    """Application with a single runner.

    Test should ensure it returns with the application in a good state and has
    one runner.
    """
    await ensure_charm_has_runner(app=app_no_runner, model=model)

    return app_no_runner


@pytest_asyncio.fixture(scope="module")
async def app_scheduled_events(
    model: Model,
    charm_file: str,
    app_name: str,
    path: str,
    token: str,
    http_proxy: str,
    https_proxy: str,
    no_proxy: str,
) -> AsyncIterator[Application]:
    """Application with no token.

    Test should ensure it returns with the application having one runner.

    This fixture has to deploy a new application. The scheduled events are set
    to one hour in other application to avoid conflicting with the tests.
    Changes to the duration of scheduled interval only takes effect after the
    next trigger. Therefore, it would take a hour for the duration change to
    take effect.
    """
    application = await deploy_github_runner_charm(
        model=model,
        charm_file=charm_file,
        app_name=app_name,
        path=path,
        token=token,
        runner_storage="memory",
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        reconcile_interval=8,
    )

    await application.set_config({VIRTUAL_MACHINES_CONFIG_NAME: "1"})
    await reconcile(app=application, model=model)

    return application


@pytest_asyncio.fixture(scope="module")
async def app_runner(
    model: Model,
    charm_file: str,
    app_name: str,
    path: str,
    token: str,
    http_proxy: str,
    https_proxy: str,
    no_proxy: str,
) -> AsyncIterator[Application]:
    """Application to test runners."""
    # Use a different app_name so workflows can select runners from this deployment.
    application = await deploy_github_runner_charm(
        model=model,
        charm_file=charm_file,
        app_name=f"{app_name}-test",
        path=path,
        token=token,
        runner_storage="memory",
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        reconcile_interval=60,
    )
    return application


@pytest_asyncio.fixture(scope="module", name="app_no_wait")
async def app_no_wait_fixture(
    model: Model,
    charm_file: str,
    app_name: str,
    path: str,
    token: str,
    http_proxy: str,
    https_proxy: str,
    no_proxy: str,
) -> AsyncIterator[Application]:
    """Github runner charm application without waiting for active."""
    app: Application = await deploy_github_runner_charm(
        model=model,
        charm_file=charm_file,
        app_name=app_name,
        path=path,
        token=token,
        runner_storage="juju-storage",
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        reconcile_interval=60,
        wait_idle=False,
    )
    await app.set_config({VIRTUAL_MACHINES_CONFIG_NAME: "1"})
    return app


@pytest_asyncio.fixture(scope="module", name="tmate_ssh_server_app")
async def tmate_ssh_server_app_fixture(
    model: Model, app_no_wait: Application
) -> AsyncIterator[Application]:
    """tmate-ssh-server charm application related to GitHub-Runner app charm."""
    tmate_app: Application = await model.deploy("tmate-ssh-server", channel="edge")
    await app_no_wait.relate("debug-ssh", f"{tmate_app.name}:debug-ssh")
    await model.wait_for_idle(apps=[tmate_app.name], status=ACTIVE, timeout=60 * 30)

    return tmate_app


@pytest_asyncio.fixture(scope="module", name="tmate_ssh_server_unit_ip")
async def tmate_ssh_server_unit_ip_fixture(
    model: Model,
    tmate_ssh_server_app: Application,
) -> bytes | str:
    """tmate-ssh-server charm unit ip."""
    status: FullStatus = await model.get_status([tmate_ssh_server_app.name])
    try:
        unit_status: UnitStatus = next(
            iter(status.applications[tmate_ssh_server_app.name].units.values())
        )
        assert unit_status.public_address, "Invalid unit address"
        return unit_status.public_address
    except StopIteration as exc:
        raise StopIteration("Invalid unit status") from exc


@pytest.fixture(scope="module")
def github_client(token: str) -> Github:
    """Returns the github client."""
    gh = Github(token)
    rate_limit = gh.get_rate_limit()
    logging.info("GitHub token rate limit: %s", rate_limit.core)
    return gh


@pytest.fixture(scope="module")
def github_repository(github_client: Github, path: str) -> Repository:
    """Returns client to the Github repository."""
    return github_client.get_repo(path)


@pytest.fixture(scope="module")
def forked_github_repository(
    github_repository: Repository,
) -> Iterator[Repository]:
    """Create a fork for a GitHub repository."""
    forked_repository = github_repository.create_fork(name=f"test-{github_repository.name}")

    # Wait for repo to be ready
    for _ in range(10):
        try:
            sleep(10)
            forked_repository.get_branches()
            break
        except GithubException:
            pass
    else:
        assert False, "timed out whilst waiting for repository creation"

    return forked_repository

    # Parallel runs of this test module is allowed. Therefore, the forked repo is not removed.


@pytest.fixture(scope="module")
def forked_github_branch(
    github_repository: Repository, forked_github_repository: Repository
) -> Iterator[Branch]:
    """Create a new forked branch for testing."""
    branch_name = f"test/{secrets.token_hex(4)}"

    main_branch = forked_github_repository.get_branch(github_repository.default_branch)
    branch_ref = forked_github_repository.create_git_ref(
        ref=f"refs/heads/{branch_name}", sha=main_branch.commit.sha
    )

    for _ in range(10):
        try:
            branch = forked_github_repository.get_branch(branch_name)
            break
        except GithubException as err:
            if err.status == 404:
                sleep(5)
                continue
            raise
    else:
        assert (
            False
        ), "Failed to get created branch in fork repo, the issue with GitHub or network."

    yield branch

    branch_ref.delete()


@pytest_asyncio.fixture(scope="module")
async def app_with_forked_repo(
    model: Model, basic_app: Application, forked_github_repository: Repository
) -> Application:
    """Application with no runner on a forked repo.

    Test should ensure it returns with the application in a good state and has
    one runner.
    """
    await basic_app.set_config({PATH_CONFIG_NAME: forked_github_repository.full_name})

    return basic_app


@pytest_asyncio.fixture(scope="module")
async def app_juju_storage(
    model: Model,
    charm_file: str,
    app_name: str,
    path: str,
    token: str,
    http_proxy: str,
    https_proxy: str,
    no_proxy: str,
) -> AsyncIterator[Application]:
    """Application with juju storage setup."""
    # Set the scheduled event to 1 hour to avoid interfering with the tests.
    application = await deploy_github_runner_charm(
        model=model,
        charm_file=charm_file,
        app_name=app_name,
        path=path,
        token=token,
        runner_storage="juju-storage",
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        reconcile_interval=60,
    )
    return application


@pytest_asyncio.fixture(scope="module", name="test_github_branch")
async def test_github_branch_fixture(github_repository: Repository) -> AsyncIterator[Branch]:
    """Create a new branch for testing, from latest commit in current branch."""
    test_branch = f"test-{secrets.token_hex(4)}"
    branch_ref = github_repository.create_git_ref(
        ref=f"refs/heads/{test_branch}", sha=Repo().head.commit.hexsha
    )

    def get_branch():
        """Get newly created branch.

        Raises:
            GithubException: if unexpected GithubException has happened apart from repository not \
                found.

        Returns:
            New branch if successful, False otherwise.
        """
        try:
            branch = github_repository.get_branch(test_branch)
        except GithubException as err:
            if err.status == 404:
                return False
            raise
        return branch

    await wait_for(get_branch)

    yield get_branch()

    branch_ref.delete()


@pytest_asyncio.fixture(scope="module", name="app_with_grafana_agent")
async def app_with_grafana_agent_integrated_fixture(
    model: Model,
    basic_app: Application,
    existing_app: Optional[str],
) -> AsyncIterator[Application]:
    """Setup the charm to be integrated with grafana-agent using the cos-agent integration."""
    if not existing_app:
        grafana_agent = await model.deploy(
            "grafana-agent",
            application_name=f"grafana-agent-{basic_app.name}",
            channel="latest/edge",
            revision=108,
        )
        await model.relate(f"{basic_app.name}:cos-agent", f"{grafana_agent.name}:cos-agent")
        await model.wait_for_idle(apps=[basic_app.name], status=ACTIVE)
        await model.wait_for_idle(apps=[grafana_agent.name])

    yield basic_app


@pytest_asyncio.fixture(scope="module", name="basic_app")
async def basic_app_fixture(
    request: pytest.FixtureRequest, pytestconfig: pytest.Config
) -> Application:
    """Setup the charm with the basic configuration."""
    # Due to scope being module we cannot use request.node.get_closes_marker as openstack
    # mark is not available in this scope.
    openstack_marker = pytestconfig.getoption("-m") == "openstack"

    if openstack_marker:
        app = request.getfixturevalue("app_openstack_runner")
    else:
        app = request.getfixturevalue("app_no_runner")
    return app


@pytest_asyncio.fixture(scope="function", name="instance_helper")
async def instance_helper_fixture(request: pytest.FixtureRequest) -> InstanceHelper:
    """Instance helper fixture."""
    openstack_marker = request.node.get_closest_marker("openstack")
    helper: InstanceHelper
    if openstack_marker:
        openstack_connection = request.getfixturevalue("openstack_connection")
        helper = OpenStackInstanceHelper(openstack_connection=openstack_connection)
    else:
        helper = LXDInstanceHelper()
    return helper
