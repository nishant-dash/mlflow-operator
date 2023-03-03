# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Integration tests for Seldon Core Operator/Charm."""

import logging
from pathlib import Path
from random import choices
from string import ascii_lowercase

import pytest
import requests
import yaml
from mlflow.tracking import MlflowClient
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
CHARM_NAME = METADATA["name"]
RELATIONAL_DB_CHARM_NAME = "charmed-osm-mariadb-k8s"
OBJECT_STORAGE_CHARM_NAME = "minio"
OBJECT_STORAGE_CONFIG = {
    "access-key": "minio",
    "secret-key": "minio123",
    "port": "9000",
}
TEST_EXPERIMENT_NAME = "test-experiment"


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    """Build and deploy the charm.

    Assert on the unit status.
    """
    charm_under_test = await ops_test.build_charm(".")
    image_path = METADATA["resources"]["oci-image"]["upstream-source"]
    resources = {"oci-image": image_path}

    await ops_test.model.deploy(
        charm_under_test, resources=resources, application_name=CHARM_NAME, trust=True
    )

    await ops_test.model.wait_for_idle(
        apps=[CHARM_NAME], status="waiting", raise_on_blocked=True, timeout=300
    )
    assert ops_test.model.applications[CHARM_NAME].units[0].workload_status == "waiting"
    assert (
        ops_test.model.applications[CHARM_NAME].units[0].workload_status_message
        == "Waiting for object-storage relation data"
    )


@pytest.mark.abort_on_fail
async def test_add_relational_db_with_relation_expect_active(ops_test: OpsTest):
    await ops_test.model.deploy(OBJECT_STORAGE_CHARM_NAME, config=OBJECT_STORAGE_CONFIG)
    await ops_test.model.deploy(RELATIONAL_DB_CHARM_NAME, channel="latest/edge", trust=True)
    await ops_test.model.wait_for_idle(
        apps=[OBJECT_STORAGE_CHARM_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=600,
        idle_period=300,
    )
    await ops_test.model.relate(OBJECT_STORAGE_CHARM_NAME, CHARM_NAME)
    await ops_test.model.relate(RELATIONAL_DB_CHARM_NAME, CHARM_NAME)

    await ops_test.model.wait_for_idle(
        apps=[CHARM_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=600,
        idle_period=60,
    )
    assert ops_test.model.applications[CHARM_NAME].units[0].workload_status == "active"


async def test_default_bucket_created(ops_test: OpsTest):
    """Tests whether the default bucket is auto-generated by mlflow.
    Note: We do not have a test coverage to assert if that the bucket is not created if
    create_default_artifact_root_if_missing==False.
    """
    config = await ops_test.model.applications[CHARM_NAME].get_config()
    default_bucket_name = config["default_artifact_root"]["value"]

    ret_code, stdout, stderr, kubectl_cmd = await does_minio_bucket_exist(
        default_bucket_name, ops_test
    )
    assert ret_code == 0, (
        f"Unable to find bucket named {default_bucket_name}, got "
        f"stdout=\n'{stdout}\n'stderr=\n{stderr}\nUsed command {kubectl_cmd}"
    )


async def does_minio_bucket_exist(bucket_name, ops_test: OpsTest):
    """Connects to the minio server and checks if a bucket exists, checking if a bucket exists.
    Returns:
        Tuple of the return code, stdout, and stderr
    """
    access_key = OBJECT_STORAGE_CONFIG["access-key"]
    secret_key = OBJECT_STORAGE_CONFIG["secret-key"]
    port = OBJECT_STORAGE_CONFIG["port"]
    obj_storage_name = OBJECT_STORAGE_CHARM_NAME
    model_name = ops_test.model_name

    obj_storage_url = f"http://{obj_storage_name}.{model_name}.svc.cluster.local:{port}"

    # Region is not used and doesn't matter, but must be set to run in github actions as explained
    # in: https://florian.ec/blog/github-actions-awscli-errors/
    aws_cmd = (
        f"aws --endpoint-url {obj_storage_url} --region us-east-1 s3api head-bucket"
        f" --bucket={bucket_name}"
    )

    # Add random suffix to pod name to avoid collision
    this_pod_name = f"{CHARM_NAME}-minio-bucket-test-{generate_random_string()}"

    kubectl_cmd = (
        "microk8s",
        "kubectl",
        "run",
        "--rm",
        "-i",
        "--restart=Never",
        f"--namespace={ops_test.model_name}",
        this_pod_name,
        f"--env=AWS_ACCESS_KEY_ID={access_key}",
        f"--env=AWS_SECRET_ACCESS_KEY={secret_key}",
        "--image=amazon/aws-cli",
        "--command",
        "--",
        "sh",
        "-c",
        aws_cmd,
    )

    (
        ret_code,
        stdout,
        stderr,
    ) = await ops_test.run(*kubectl_cmd)
    return ret_code, stdout, stderr, " ".join(kubectl_cmd)


@pytest.mark.abort_on_fail
async def test_can_create_experiment_with_mlflow_library(ops_test: OpsTest):
    config = await ops_test.model.applications[CHARM_NAME].get_config()
    url = f"http://localhost:{config['mlflow_nodeport']['value']}"
    client = MlflowClient(tracking_uri=url)
    response = requests.get(url)
    assert response.status_code == 200
    client.create_experiment(TEST_EXPERIMENT_NAME)
    all_experiments = client.search_experiments()
    assert len(list(filter(lambda e: e.name == TEST_EXPERIMENT_NAME, all_experiments))) == 1


def generate_random_string(length: int = 4):
    """Returns a random string of lower case alphabetic characters and given length."""
    return "".join(choices(ascii_lowercase, k=length))
