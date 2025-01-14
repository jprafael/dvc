import logging

import pytest

from dvc.exceptions import (
    NoMetricsFoundError,
    NoMetricsParsedError,
    OverlappingOutputPathsError,
)
from dvc.path_info import PathInfo
from dvc.utils.fs import remove
from dvc.utils.serialize import dump_yaml, modify_yaml
from tests.func.metrics.utils import _write_json


@pytest.mark.parametrize(
    "diff, metric_value",
    (
        (
            lambda repo, target, rev: repo.metrics.diff(
                targets=[target], a_rev=rev
            ),
            {"m": 1},
        ),
        (
            lambda repo, target, rev: repo.plots.diff(
                targets=[target], revs=[rev]
            ),
            [{"m": 1}, {"m": 2}],
        ),
    ),
)
def test_diff_no_file_on_target_rev(
    tmp_dir, scm, dvc, caplog, diff, metric_value
):
    with tmp_dir.branch("new_branch", new=True):
        _write_json(tmp_dir, metric_value, "metric.json")

        with caplog.at_level(logging.WARNING, "dvc"):
            diff(dvc, "metric.json", "master")

    assert "'metric.json' was not found at: 'master'." in caplog.text


@pytest.mark.parametrize(
    "show, malformed_metric",
    (
        (lambda repo, target: repo.metrics.show(targets=[target]), '{"m": 1'),
        (
            lambda repo, target: repo.plots.show(targets=[target]),
            '[{"m": 1}, {"m": 2}',
        ),
    ),
)
def test_show_malformed_metric(
    tmp_dir, scm, dvc, caplog, show, malformed_metric
):
    tmp_dir.gen("metric.json", malformed_metric)

    with pytest.raises(NoMetricsParsedError):
        show(dvc, "metric.json")


@pytest.mark.parametrize(
    "show",
    (lambda repo: repo.metrics.show(), lambda repo: repo.plots.show(),),
)
def test_show_no_metrics_files(tmp_dir, dvc, caplog, show):
    with pytest.raises(NoMetricsFoundError):
        show(dvc)


@pytest.mark.parametrize("clear_before_run", [True, False])
@pytest.mark.parametrize("typ", ["metrics", "plots"])
def test_metrics_show_overlap(
    tmp_dir, dvc, run_copy_metrics, clear_before_run, typ
):
    data_dir = PathInfo("data")
    (tmp_dir / data_dir).mkdir()

    outs = {typ: [str(data_dir / "m1.yaml")]}
    dump_yaml(data_dir / "m1_temp.yaml", {"a": {"b": {"c": 2, "d": 1}}})
    run_copy_metrics(
        str(data_dir / "m1_temp.yaml"),
        str(data_dir / "m1.yaml"),
        single_stage=False,
        commit=f"add m1 {typ}",
        name="cp-m1",
        **outs,
    )
    with modify_yaml("dvc.yaml") as d:
        # trying to make an output overlaps error
        d["stages"]["corrupted-stage"] = {
            "cmd": "mkdir data",
            "outs": ["data"],
        }

    # running by clearing and not clearing stuffs
    # so as it works even for optimized cases
    if clear_before_run:
        remove(data_dir)
        remove(dvc.cache.local.cache_dir)

    dvc._reset()

    show = dvc.metrics.show if typ == "metrics" else dvc.plots.show
    with pytest.raises(OverlappingOutputPathsError):
        show()
