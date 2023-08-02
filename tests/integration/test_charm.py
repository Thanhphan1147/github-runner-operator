# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for github-runner charm."""

import pytest
from juju.application import Application
from juju.model import Model

from runner_manager import RunnerManager
from tests.integration.helpers import (
    assert_resource_lxd_profile,
    assesrt_num_of_runners,
    get_repo_policy_compliance_pip_info,
    install_repo_policy_compliance_from_git_source,
    remove_runner_bin,
)
from tests.status_name import ACTIVE_STATUS_NAME, BLOCKED_STATUS_NAME

REPO_POLICY_COMPLIANCE_VER_0_2_GIT_SOURCE = (
    "git+https://github.com/canonical/"
    "repo-policy-compliance@48b36c130b207278d20c3847ce651ac13fb9e9d7"
)


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_missing_config(app_no_token: Application) -> None:
    """
    arrange: An application without token configuration.
    act: Check the status the application.
    assert: The application is in blocked status.
    """
    assert app_no_token.status == BLOCKED_STATUS_NAME

    unit = app_no_token.units[0]
    assert unit.workload_status == BLOCKED_STATUS_NAME
    assert unit.workload_status_message == "Missing required charm configuration: ['token']"


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_update_dependencies_action_latest_service(
    model: Model, app_no_runner: Application
) -> None:
    """
    arrange: An working application with latest version of repo-policy-compliance service.
    act: Run update-dependencies action.
    assert:
        a. Service is installed in the charm.
        b. Action did not flushed the runners.
    """
    unit = app_no_runner.units[0]

    action = await unit.run_action("update-dependencies")
    await action.wait()
    await model.wait_for_idle(status=ACTIVE_STATUS_NAME)

    assert not action.results["flush"]
    assert await get_repo_policy_compliance_pip_info(unit) is not None


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_update_dependencies_action_no_service(
    model: Model, app_no_runner: Application
) -> None:
    """
    arrange: Remove repo-policy-compliance service installation.
    act: Run update-dependencies action.
    assert:
        a. Service is installed in the charm.
        b. Action flushed the runners.
    """
    unit = app_no_runner.units[0]

    await install_repo_policy_compliance_from_git_source(unit, None)
    assert await get_repo_policy_compliance_pip_info(unit) is None

    action = await unit.run_action("update-dependencies")
    await action.wait()
    await model.wait_for_idle(status=ACTIVE_STATUS_NAME)

    assert action.results["flush"]
    assert await get_repo_policy_compliance_pip_info(unit) is not None


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_update_dependencies_action_old_service(
    model: Model, app_no_runner: Application
) -> None:
    """
    arrange: Replace repo-policy-compliance service installation to a older version.
    act: Run update-dependencies action.
    assert:
        a. Service is installed in the charm.
        b. Action flushed the runners.
    """
    unit = app_no_runner.units[0]
    latest_version_info = await get_repo_policy_compliance_pip_info(unit)

    await install_repo_policy_compliance_from_git_source(
        unit, REPO_POLICY_COMPLIANCE_VER_0_2_GIT_SOURCE
    )
    assert await get_repo_policy_compliance_pip_info(unit) != latest_version_info

    action = await unit.run_action("update-dependencies")
    await action.wait()
    await model.wait_for_idle(status=ACTIVE_STATUS_NAME)

    assert action.results["flush"]
    assert await get_repo_policy_compliance_pip_info(unit) is not None


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_update_dependencies_action_on_runner_binary(
    model: Model, app_no_runner: Application
) -> None:
    """
    arrange: Remove runner binary if exists.
    act:
        1. Run update-dependencies action.
        2. Run update-dependencies action.
    assert:
        1.  a. Runner binary exists in the charm.
            b. Action flushed the runners.
        2.  a. Runner binary exists in the charm.
            b. Action did not flushed the runners.
    """
    unit = app_no_runner.units[0]

    await remove_runner_bin(unit)

    action = await unit.run_action("update-dependencies")
    await action.wait()
    await model.wait_for_idle(status=ACTIVE_STATUS_NAME)

    # The runners should be flushed on update of runner binary.
    assert action.results["flush"]

    action = await unit.run(f"test -f {RunnerManager.runner_bin_path}")
    await action.wait()
    assert action.results["return-code"] == 0

    action = await unit.run_action("update-dependencies")
    await action.wait()
    await model.wait_for_idle(status=ACTIVE_STATUS_NAME)

    # The runners should be flushed on update of runner binary.
    assert not action.results["flush"]

    action = await unit.run(f"test -f {RunnerManager.runner_bin_path}")
    await action.wait()
    assert action.results["return-code"] == 0


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_check_runners_no_runners(app_no_runner: Application) -> None:
    """
    arrange: An working application with no runners.
    act: Run check-runners action.
    assert: Action returns result with no runner.
    """
    unit = app_no_runner.units[0]

    action = await unit.run_action("check-runners")
    await action.wait()

    assert action.results["online"] == "0"
    assert action.results["offline"] == "0"
    assert action.results["unknown"] == "0"
    assert not action.results["runners"]


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_reconcile_runners(model: Model, app_no_runner: Application) -> None:
    """
    arrange: An working application with no runners.
    act:
        1.  a. Set virtual-machines config to 1.
            b. Run reconcile_runners action.
        2.  a. Set virtual-machiens config to 0.
            b. Run reconcile_runners action.
    assert:
        1. One runner should exist.
        2. No runner should exist.

    The two test is combine to maintain no runners in the application after the
    test.
    """
    # Rename since the app will have a runner.
    app = app_no_runner

    unit = app.units[0]

    # 1.
    await app.set_config({"virtual-machines": "1"})

    action = await unit.run_action("reconcile-runners")
    await action.wait()
    await model.wait_for_idle(status=ACTIVE_STATUS_NAME)

    await assesrt_num_of_runners(unit, 1)

    # 2.
    await app.set_config({"virtual-machines": "0"})

    action = await unit.run_action("reconcile-runners")
    await action.wait()
    await model.wait_for_idle(status=ACTIVE_STATUS_NAME)

    await assesrt_num_of_runners(unit, 0)


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_flush_runner_and_resource_config(app: Application) -> None:
    """
    arrange: An working application with one runner.
    act:
        1. Run Check_runner action. Record the runner name for later.
        2. Nothing.
        3. Change the virtual machine resource configuration.
        4. Run flush_runner action.

    assert:
        1. One runner exists.
        2. LXD profile of matching resource config exists.
        3. Nothing.
        4.  a. The runner name should be different to the runner prior running
                the action.
            b. LXD profile matching virtual machine resources of step 2 exists.

    Test are combined to reduce number of runner spawned.
    """
    unit = app.units[0]

    # 1.
    action = await app.units[0].run_action("check-runners")
    await action.wait()

    assert action.status == "completed"
    assert action.results["online"] == "1"
    assert action.results["offline"] == "0"
    assert action.results["unknown"] == "0"

    runner_names = action.results["runners"].split(", ")
    assert len(runner_names) == 1

    # 2.
    configs = await app.get_config()
    await assert_resource_lxd_profile(unit, configs)

    # 3.
    await app.set_config({"vm-cpu": "1", "vm-memory": "3GiB", "vm-disk": "5GiB"})

    # 4.
    action = await app.units[0].run_action("flush-runners")
    await action.wait()

    configs = await app.get_config()
    await assert_resource_lxd_profile(unit, configs)
    await assesrt_num_of_runners(unit, 1)

    action = await app.units[0].run_action("check-runners")
    await action.wait()

    assert action.status == "completed"
    assert action.results["online"] == "1"
    assert action.results["offline"] == "0"
    assert action.results["unknown"] == "0"

    new_runner_names = action.results["runners"].split(", ")
    assert len(new_runner_names) == 1
    assert new_runner_names[0] != runner_names[0]


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_check_runner(app: Application) -> None:
    """
    arrange: An working application with one runner.
    act: Run check_runner action.
    assert: Action returns result with one runner.
    """
    action = await app.units[0].run_action("check-runners")
    await action.wait()

    assert action.status == "completed"
    assert action.results["online"] == "1"
    assert action.results["offline"] == "0"
    assert action.results["unknown"] == "0"


@pytest.mark.asyncio
@pytest.mark.abort_on_fail
async def test_token_config_changed(model: Model, app: Application, token_alt: str) -> None:
    """
    arrange: An working application.
    act: Change the token configuration.
    assert: The repo-policy-compliance using the new token.
    """
    await app.set_config({"token": token_alt})
    await model.wait_for_idle(status=ACTIVE_STATUS_NAME)

    action = await app.units[0].run("cat /etc/systemd/system/repo-policy-compliance.service")
    await action.wait()

    assert action.status == "completed"
    assert f"GITHUB_TOKEN={token_alt}" in action.results["stdout"]
