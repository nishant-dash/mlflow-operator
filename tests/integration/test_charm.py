# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#

"""Integration tests for Seldon Core Operator/Charm."""

import base64
import logging
import subprocess
import time
from pathlib import Path
from random import choices
from string import ascii_lowercase

import aiohttp
import lightkube
import pytest
import requests
import yaml
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from lightkube import codecs
from lightkube.generic_resource import (
    create_namespaced_resource,
    load_in_cluster_generic_resources,
)
from lightkube.resources.core_v1 import Secret, Service
from minio import Minio
from mlflow.tracking import MlflowClient
from pytest_operator.plugin import OpsTest
from tenacity import retry, stop_after_delay, wait_fixed

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
CHARM_NAME = METADATA["name"]
RELATIONAL_DB_CHARM_NAME = "mysql-k8s"
OBJECT_STORAGE_CHARM_NAME = "minio"
PROMETHEUS_CHARM_NAME = "prometheus-k8s"
GRAFANA_CHARM_NAME = "grafana-k8s"
RESOURCE_DISPATCHER_CHARM_NAME = "resource-dispatcher"
ISTIO_GATEWAY_CHARM_NAME = "istio-ingressgateway"
ISTIO_PILOT_CHARM_NAME = "istio-pilot"
METACONTROLLER_CHARM_NAME = "metacontroller-operator"
NAMESPACE_FILE = "./tests/integration/namespace.yaml"
PODDEFAULTS_CRD_TEMPLATE = "./tests/integration/crds/poddefaults.yaml"
PODDEFAULTS_SUFFIXES = ["-access-minio", "-minio"]
TESTING_LABELS = ["user.kubeflow.org/enabled"]  # Might be more than one in the future
OBJECT_STORAGE_CONFIG = {
    "access-key": "minio",
    "secret-key": "minio123",
    "port": "9000",
}
SECRET_SUFFIX = "-minio-artifact"
TEST_EXPERIMENT_NAME = "test-experiment"

PodDefault = create_namespaced_resource("kubeflow.org", "v1alpha1", "PodDefault", "poddefaults")


def _safe_load_file_to_text(filename: str) -> str:
    """Returns the contents of filename if it is an existing file, else it returns filename."""
    try:
        text = Path(filename).read_text()
    except FileNotFoundError:
        text = filename
    return text


def delete_all_from_yaml(yaml_text: str, lightkube_client: lightkube.Client = None):
    """Deletes all k8s resources listed in a YAML file via lightkube.

    Args:
        yaml_file (str or Path): Either a string filename or a string of valid YAML.  Will attempt
                                 to open a filename at this path, failing back to interpreting the
                                 string directly as YAML.
        lightkube_client: Instantiated lightkube client or None
    """

    if lightkube_client is None:
        lightkube_client = lightkube.Client()

    for obj in codecs.load_all_yaml(yaml_text):
        lightkube_client.delete(type(obj), obj.metadata.name)


@pytest.fixture(scope="session")
def lightkube_client() -> lightkube.Client:
    client = lightkube.Client(field_manager=CHARM_NAME)
    return client


def deploy_k8s_resources(template_files: str):
    lightkube_client = lightkube.Client(field_manager=CHARM_NAME)
    k8s_resource_handler = KubernetesResourceHandler(
        field_manager=CHARM_NAME, template_files=template_files, context={}
    )
    load_in_cluster_generic_resources(lightkube_client)
    k8s_resource_handler.apply()


async def fetch_url(url):
    """Fetch provided URL and return JSON."""
    result = None
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            result = await response.json()
    return result


@pytest.fixture(scope="session")
def namespace(lightkube_client: lightkube.Client):
    yaml_text = _safe_load_file_to_text(NAMESPACE_FILE)
    yaml_rendered = yaml.safe_load(yaml_text)
    for label in TESTING_LABELS:
        yaml_rendered["metadata"]["labels"][label] = "true"
    obj = codecs.from_dict(yaml_rendered)
    lightkube_client.apply(obj)

    yield obj.metadata.name

    delete_all_from_yaml(yaml_text, lightkube_client)


async def setup_istio(ops_test: OpsTest, istio_gateway: str, istio_pilot: str):
    """Deploy Istio Ingress Gateway and Istio Pilot."""
    await ops_test.model.deploy(
        entity_url="istio-gateway",
        application_name=istio_gateway,
        channel="1.16/stable",
        config={"kind": "ingress"},
        trust=True,
    )
    await ops_test.model.deploy(
        istio_pilot,
        channel="1.16/stable",
        config={"default-gateway": "test-gateway"},
        trust=True,
    )
    await ops_test.model.add_relation(istio_pilot, istio_gateway)

    await ops_test.model.wait_for_idle(
        apps=[istio_pilot, istio_gateway],
        status="active",
        timeout=60 * 5,
        raise_on_blocked=False,
        raise_on_error=False,
    )


def get_ingress_url(lightkube_client: lightkube.Client, model_name: str):
    gateway_svc = lightkube_client.get(
        Service, "istio-ingressgateway-workload", namespace=model_name
    )
    ingress_record = gateway_svc.status.loadBalancer.ingress[0]
    if ingress_record.ip:
        public_url = f"http://{ingress_record.ip}.nip.io"
    if ingress_record.hostname:
        public_url = f"http://{ingress_record.hostname}"  # Use hostname (e.g. EKS)
    return public_url


async def fetch_response(url, headers):
    """Fetch provided URL and return pair - status and text (int, string)."""
    result_status = 0
    result_text = ""
    async with aiohttp.ClientSession() as session:
        async with session.get(url=url, headers=headers) as response:
            result_status = response.status
            result_text = await response.text()
    return result_status, str(result_text)


class TestCharm:
    @staticmethod
    def generate_random_string(length: int = 4):
        """Returns a random string of lower case alphabetic characters and given length."""
        return "".join(choices(ascii_lowercase, k=length))

    @pytest.mark.abort_on_fail
    async def test_add_relational_db_with_relation_expect_active(self, ops_test: OpsTest):
        deploy_k8s_resources([PODDEFAULTS_CRD_TEMPLATE])
        await ops_test.model.deploy(
            OBJECT_STORAGE_CHARM_NAME, channel="ckf-1.7/stable", config=OBJECT_STORAGE_CONFIG
        )
        await ops_test.model.deploy(
            RELATIONAL_DB_CHARM_NAME,
            channel="8.0/stable",
            series="jammy",
            trust=True,
        )
        await ops_test.model.wait_for_idle(
            apps=[OBJECT_STORAGE_CHARM_NAME, RELATIONAL_DB_CHARM_NAME],
            status="active",
            raise_on_blocked=False,
            raise_on_error=False,
            timeout=600,
        )
        await ops_test.model.relate(OBJECT_STORAGE_CHARM_NAME, CHARM_NAME)
        await ops_test.model.relate(RELATIONAL_DB_CHARM_NAME, CHARM_NAME)

        await ops_test.model.wait_for_idle(
            apps=[CHARM_NAME],
            status="active",
            raise_on_blocked=False,
            raise_on_error=False,
            timeout=600,
        )
        assert ops_test.model.applications[CHARM_NAME].units[0].workload_status == "active"

    @retry(stop=stop_after_delay(300), wait=wait_fixed(10))
    @pytest.mark.abort_on_fail
    async def test_can_connect_exporter_and_get_metrics(self, ops_test: OpsTest):
        config = await ops_test.model.applications[CHARM_NAME].get_config()
        exporter_port = config["mlflow_prometheus_exporter_port"]["value"]
        mlflow_subprocess = subprocess.Popen(
            [
                "kubectl",
                "-n",
                f"{ops_test.model_name}",
                "port-forward",
                f"svc/{CHARM_NAME}",
                f"{exporter_port}:{exporter_port}",
            ]
        )
        time.sleep(10)  # Must wait for port-forward

        url = f"http://localhost:{exporter_port}/metrics"
        response = requests.get(url)
        assert response.status_code == 200
        metrics_text = response.text
        assert 'mlflow_metric{metric_name="num_experiments"} 1.0' in metrics_text
        assert 'mlflow_metric{metric_name="num_registered_models"} 0.0' in metrics_text
        assert 'mlflow_metric{metric_name="num_runs"} 0' in metrics_text

        mlflow_subprocess.terminate()

    @pytest.mark.abort_on_fail
    async def test_mlflow_bucket_exists(self, ops_test):
        config = await ops_test.model.applications[CHARM_NAME].get_config()
        default_bucket_name = config["default_artifact_root"]["value"]

        access_key = OBJECT_STORAGE_CONFIG["access-key"]
        secret_key = OBJECT_STORAGE_CONFIG["secret-key"]
        port = OBJECT_STORAGE_CONFIG["port"]

        minio_subproces = subprocess.Popen(
            [
                "kubectl",
                "-n",
                f"{ops_test.model_name}",
                "port-forward",
                f"svc/{OBJECT_STORAGE_CHARM_NAME}",
                f"{port}:{port}",
            ]
        )
        time.sleep(10)  # Must wait for port-forward

        minio_client = Minio(
            f"localhost:{port}",
            access_key=access_key,
            secret_key=secret_key,
            region="us-east-1",  # Must be set otherwise it is not working
            secure=False,  # Change to True if using HTTPS
        )
        # Check if the default_bucket_name bucket exists
        found = minio_client.bucket_exists(bucket_name=default_bucket_name)
        assert found, f"The '{default_bucket_name}' bucket does not exist"

        minio_subproces.terminate()

    @pytest.mark.abort_on_fail
    async def test_can_create_experiment_with_mlflow_library(self, ops_test: OpsTest):
        config = await ops_test.model.applications[CHARM_NAME].get_config()
        mlflow_port = config["mlflow_port"]["value"]
        mlflow_subprocess = subprocess.Popen(
            [
                "kubectl",
                "-n",
                f"{ops_test.model_name}",
                "port-forward",
                f"svc/{CHARM_NAME}",
                f"{mlflow_port}:{mlflow_port}",
            ]
        )
        time.sleep(10)  # Must wait for port-forward

        url = f"http://localhost:{mlflow_port}"
        client = MlflowClient(tracking_uri=url)
        response = requests.get(url)
        assert response.status_code == 200
        client.create_experiment(TEST_EXPERIMENT_NAME)
        all_experiments = client.search_experiments()
        assert len(list(filter(lambda e: e.name == TEST_EXPERIMENT_NAME, all_experiments))) == 1

        mlflow_subprocess.terminate()

    @pytest.mark.abort_on_fail
    async def test_deploy_resource_dispatcher(self, ops_test: OpsTest):
        await ops_test.model.deploy(
            entity_url=METACONTROLLER_CHARM_NAME,
            channel="latest/edge",
            trust=True,
        )
        await ops_test.model.wait_for_idle(
            apps=[METACONTROLLER_CHARM_NAME],
            status="active",
            raise_on_blocked=False,
            raise_on_error=False,
            timeout=120,
        )
        await ops_test.model.deploy(
            RESOURCE_DISPATCHER_CHARM_NAME, channel="latest/edge", trust=True
        )
        await ops_test.model.wait_for_idle(
            apps=[CHARM_NAME],
            status="active",
            raise_on_blocked=False,
            raise_on_error=False,
            timeout=120,
            idle_period=60,
        )

        await ops_test.model.relate(
            f"{CHARM_NAME}:pod-defaults", f"{RESOURCE_DISPATCHER_CHARM_NAME}:pod-defaults"
        )
        await ops_test.model.relate(
            f"{CHARM_NAME}:secrets", f"{RESOURCE_DISPATCHER_CHARM_NAME}:secrets"
        )

        await ops_test.model.wait_for_idle(
            apps=[RESOURCE_DISPATCHER_CHARM_NAME],
            status="active",
            raise_on_blocked=False,
            raise_on_error=False,
            timeout=1200,
        )

    async def test_ingress_relation(self, ops_test: OpsTest):
        """Setup Istio and relate it to the MLflow."""
        await setup_istio(ops_test, ISTIO_GATEWAY_CHARM_NAME, ISTIO_PILOT_CHARM_NAME)

        await ops_test.model.add_relation(
            f"{ISTIO_PILOT_CHARM_NAME}:ingress", f"{CHARM_NAME}:ingress"
        )

        await ops_test.model.wait_for_idle(apps=[CHARM_NAME], status="active", timeout=60 * 5)

    @retry(stop=stop_after_delay(600), wait=wait_fixed(10))
    @pytest.mark.abort_on_fail
    async def test_ingress_url(self, lightkube_client, ops_test: OpsTest):
        ingress_url = get_ingress_url(lightkube_client, ops_test.model_name)
        result_status, result_text = await fetch_response(f"{ingress_url}/mlflow/", {})

        # verify that UI is accessible
        assert result_status == 200
        assert len(result_text) > 0

    @pytest.mark.abort_on_fail
    async def test_new_user_namespace_has_manifests(
        self, ops_test: OpsTest, lightkube_client: lightkube.Client, namespace: str
    ):
        time.sleep(30)  # sync can take up to 10 seconds for reconciliation loop to trigger
        secret_name = f"{CHARM_NAME}{SECRET_SUFFIX}"
        secret = lightkube_client.get(Secret, secret_name, namespace=namespace)
        assert secret.data == {
            "AWS_ACCESS_KEY_ID": base64.b64encode(
                OBJECT_STORAGE_CONFIG["access-key"].encode("utf-8")
            ).decode("utf-8"),
            "AWS_SECRET_ACCESS_KEY": base64.b64encode(
                OBJECT_STORAGE_CONFIG["secret-key"].encode("utf-8")
            ).decode("utf-8"),
        }
        poddefaults_names = [f"{CHARM_NAME}{suffix}" for suffix in PODDEFAULTS_SUFFIXES]
        for name in poddefaults_names:
            pod_default = lightkube_client.get(PodDefault, name, namespace=namespace)
            assert pod_default is not None

    @pytest.mark.abort_on_fail
    async def test_mlflow_alert_rules(self, ops_test: OpsTest):
        await ops_test.model.deploy(PROMETHEUS_CHARM_NAME, channel="latest/stable", trust=True)
        await ops_test.model.relate(PROMETHEUS_CHARM_NAME, CHARM_NAME)
        await ops_test.model.wait_for_idle(
            apps=[PROMETHEUS_CHARM_NAME], status="active", raise_on_blocked=True, timeout=60 * 10
        )

        prometheus_subprocess = subprocess.Popen(
            [
                "kubectl",
                "-n",
                f"{ops_test.model_name}",
                "port-forward",
                f"svc/{PROMETHEUS_CHARM_NAME}",
                "9090:9090",
            ]
        )
        time.sleep(10)  # Must wait for port-forward

        prometheus_url = "localhost"

        # obtain scrape targets from Prometheus
        targets_result = await fetch_url(f"http://{prometheus_url}:9090/api/v1/targets")

        # verify that mlflow-server is in the target list
        assert targets_result is not None
        assert targets_result["status"] == "success"
        discovered_labels = targets_result["data"]["activeTargets"][0]["discoveredLabels"]
        assert discovered_labels["juju_application"] == CHARM_NAME

        # obtain alert rules from Prometheus
        rules_url = f"http://{prometheus_url}:9090/api/v1/rules"
        alert_rules_result = await fetch_url(rules_url)

        # verify alerts are available in Prometheus
        assert alert_rules_result is not None
        assert alert_rules_result["status"] == "success"
        rules = alert_rules_result["data"]["groups"][0]["rules"]

        # load alert rules from the rules file
        rules_file_alert_names = []
        with open("src/prometheus_alert_rules/mlflow-server.rule") as f:
            mlflow_server = yaml.safe_load(f.read())
            alerts_list = mlflow_server["groups"][0]["rules"]
            for alert in alerts_list:
                rules_file_alert_names.append(alert["alert"])

        # verify number of alerts is the same in Prometheus and in the rules file
        assert len(rules) == len(rules_file_alert_names)

        # verify that all Mlflow alert rules are in the list and that alerts obtained
        # from Prometheus match alerts in the rules file
        for rule in rules:
            assert rule["name"] in rules_file_alert_names

        prometheus_subprocess.terminate()

    @pytest.mark.abort_on_fail
    async def test_grafana_integration(self, ops_test: OpsTest):
        await ops_test.model.deploy(GRAFANA_CHARM_NAME, channel="latest/stable", trust=True)
        await ops_test.model.relate(GRAFANA_CHARM_NAME, CHARM_NAME)
        await ops_test.model.wait_for_idle(
            apps=[GRAFANA_CHARM_NAME], status="active", raise_on_blocked=True, timeout=60 * 20
        )
