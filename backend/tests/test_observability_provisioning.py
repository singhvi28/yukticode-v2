from pathlib import Path
import json
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_prometheus_configuration():
    prom_path = REPO_ROOT / "monitoring" / "prometheus" / "prometheus.yml"
    assert prom_path.exists(), "prometheus.yml must exist"

    with open(prom_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    assert config is not None
    assert config.get("global", {}).get("scrape_interval") == "5s"

    scrape_jobs = {job["job_name"]: job for job in config.get("scrape_configs", [])}
    assert "yukti-backend" in scrape_jobs
    assert "yukti-worker" in scrape_jobs

    backend_targets = [
        target
        for sc in scrape_jobs["yukti-backend"].get("static_configs", [])
        for target in sc.get("targets", [])
    ]
    assert "backend:9000" in backend_targets
    assert scrape_jobs["yukti-backend"].get("metrics_path") == "/metrics"

    worker_targets = [
        target
        for sc in scrape_jobs["yukti-worker"].get("static_configs", [])
        for target in sc.get("targets", [])
    ]
    assert "worker:9100" in worker_targets
    assert scrape_jobs["yukti-worker"].get("metrics_path") == "/metrics"


def test_grafana_datasource_provisioning():
    ds_path = (
        REPO_ROOT
        / "monitoring"
        / "grafana"
        / "provisioning"
        / "datasources"
        / "datasources.yml"
    )
    assert ds_path.exists(), "datasources.yml must exist"

    with open(ds_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    assert config.get("apiVersion") == 1
    datasources = config.get("datasources", [])
    assert len(datasources) > 0

    prom_ds = next((ds for ds in datasources if ds.get("type") == "prometheus"), None)
    assert prom_ds is not None
    assert prom_ds.get("url") == "http://prometheus:9090"
    assert prom_ds.get("isDefault") is True


def test_grafana_dashboard_provisioning():
    dash_prov_path = (
        REPO_ROOT
        / "monitoring"
        / "grafana"
        / "provisioning"
        / "dashboards"
        / "dashboards.yml"
    )
    assert dash_prov_path.exists(), "dashboards.yml must exist"

    with open(dash_prov_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    assert config.get("apiVersion") == 1
    providers = config.get("providers", [])
    assert len(providers) > 0

    file_prov = next((p for p in providers if p.get("type") == "file"), None)
    assert file_prov is not None
    assert file_prov.get("options", {}).get("path") == "/var/lib/grafana/dashboards"


def test_grafana_judge_sre_overview_dashboard():
    dash_json_path = (
        REPO_ROOT
        / "monitoring"
        / "grafana"
        / "dashboards"
        / "judge_sre_overview.json"
    )
    assert dash_json_path.exists(), "judge_sre_overview.json must exist"

    with open(dash_json_path, "r", encoding="utf-8") as f:
        dash = json.load(f)

    assert dash.get("title") == "YuktiCode v2 - SRE Overview"
    panels = dash.get("panels", [])
    assert len(panels) >= 9

    panel_titles = {p.get("title"): p for p in panels}

    # API RED signals
    assert "Request Rate (RPS)" in panel_titles
    assert "Error Rate (5xx / 4xx %)" in panel_titles
    assert "Latency Percentiles (p50, p95, p99)" in panel_titles

    # Queue & broker saturation
    assert "Pending Queue Depth" in panel_titles
    queue_gauge = panel_titles["Pending Queue Depth"]
    assert queue_gauge.get("type") == "gauge"
    queue_exprs = [t.get("expr", "") for t in queue_gauge.get("targets", [])]
    assert any('judge_queue_depth{status="queued"}' in expr for expr in queue_exprs)

    assert "Running Jobs" in panel_titles
    assert "Queue Depth Timeline" in panel_titles

    # Worker throughput & sandboxing
    assert "Jobs Processed/sec (by Kind and Verdict)" in panel_titles
    assert "Container Reaps Counter" in panel_titles
    reap_panel = panel_titles["Container Reaps Counter"]
    reap_exprs = [t.get("expr", "") for t in reap_panel.get("targets", [])]
    assert any("judge_worker_reaped_containers_total" in expr for expr in reap_exprs)

    assert "Lease Expirations Counter" in panel_titles
    lease_panel = panel_titles["Lease Expirations Counter"]
    lease_exprs = [t.get("expr", "") for t in lease_panel.get("targets", [])]
    assert any("judge_worker_requeued_leases_total" in expr for expr in lease_exprs)


def test_docker_compose_observability_services():
    compose_path = REPO_ROOT / "docker-compose.yml"
    with open(compose_path, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})

    # Prometheus
    assert "prometheus" in services
    prom = services["prometheus"]
    assert prom.get("image") == "prom/prometheus:v2.51.0"
    assert "9090:9090" in prom.get("ports", [])
    assert any(
        "prometheus.yml:/etc/prometheus/prometheus.yml:ro" in vol
        for vol in prom.get("volumes", [])
    )

    # Grafana
    assert "grafana" in services
    grafana = services["grafana"]
    assert grafana.get("image") == "grafana/grafana:10.4.1"
    assert "3000:3000" in grafana.get("ports", [])
    assert "GF_AUTH_ANONYMOUS_ENABLED=true" in grafana.get("environment", [])
    assert any(
        "provisioning:/etc/grafana/provisioning:ro" in vol
        for vol in grafana.get("volumes", [])
    )
    assert any(
        "dashboards:/var/lib/grafana/dashboards:ro" in vol
        for vol in grafana.get("volumes", [])
    )

    # Worker exposed port 9100
    assert "worker" in services
    worker = services["worker"]
    worker_expose = worker.get("expose", [])
    assert "9100" in worker_expose or 9100 in worker_expose


def test_nginx_metrics_proxy():
    nginx_path = REPO_ROOT / "frontend" / "nginx.conf"
    with open(nginx_path, "r", encoding="utf-8") as f:
        nginx_conf = f.read()

    assert "location /api/metrics" in nginx_conf
    assert "proxy_pass         http://backend/metrics;" in nginx_conf
