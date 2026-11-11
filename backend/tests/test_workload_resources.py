from pathlib import Path
from threading import Event

from scripts.sample_workload_resources import Process, Trees, memory_bytes, parse_stat, run


def test_reads_live_proc_stat_without_command_or_environment():
    import os
    item = parse_stat(Path('/proc/self/stat').read_text(), os.sysconf('SC_PAGE_SIZE'))
    assert item.pid == os.getpid() and item.parent == os.getppid()
    assert item.started > 0 and item.cpu_ticks >= 0 and item.rss_bytes > 0
    assert set(item.__dataclass_fields__) == {'pid', 'parent', 'started', 'cpu_ticks', 'rss_bytes'}


def test_membership_exit_reparenting_and_pid_reuse_preserve_only_observed_cpu():
    trees = Trees({'api': 100, 'worker': 200}, ticks_per_second=100)
    first = {100: Process(100, 1, 10, 500, 1000), 200: Process(200, 1, 20, 200, 2000),
             201: Process(201, 200, 21, 100, 3000), 999: Process(999, 1, 99, 100, 9000)}
    initial = trees.observe(first, 0)
    assert initial['worker']['rss_sum_bytes'] == 5000
    assert initial['worker']['cpu_cumulative_seconds'] == 0
    # Worker child becomes orphaned but its identity stays associated. A new
    # descendant contributes its CPU since birth. Unrelated PID reuse is ignored.
    second = {100: Process(100, 1, 10, 600, 1100), 200: Process(200, 1, 20, 230, 2100),
              201: Process(201, 1, 21, 150, 3200), 202: Process(202, 200, 22, 40, 4000)}
    sample = trees.observe(second, 1)
    assert sample['worker']['processes'] == 3
    assert sample['worker']['cpu_cumulative_seconds'] == 1.2
    retired = trees.observe({201: Process(201, 1, 999, 5000, 10000)}, 1)
    assert retired['worker']['rss_sum_bytes'] == 0
    assert retired['worker']['cpu_cumulative_seconds'] == 1.2
    assert trees.summary()['worker']['peak_rss_sum_bytes'] == 9300
    assert trees.summary()['worker']['observed_process_identities'] == 3


def test_empty_scan_is_recorded_and_late_root_does_not_attach_unrelated_process():
    trees = Trees({'worker': 200}, 100)
    assert trees.observe({}, 0)['worker']['root_present'] is False
    trees.observe({200: Process(200, 1, 20, 100, 1000)}, 1)
    current = trees.observe({200: Process(200, 1, 99, 10000, 9000)}, 1)
    assert current['worker']['processes'] == 0
    assert trees.summary()['worker']['observed_process_identities'] == 1


def test_stop_file_produces_bounded_evidence_with_no_docker_dependency(tmp_path):
    import os
    stop_file = tmp_path / 'stop'
    stop_file.touch()
    report = run({'driver': os.getpid()}, {}, stop_file=stop_file, max_seconds=1, stop=Event())
    assert report['sample_count'] == 1
    assert report['groups']['driver']['observed_process_identities'] >= 1
    assert report['containers'] == {} and report['container_sample_errors'] == 0


def test_docker_memory_units_remain_distinct_from_process_rss():
    assert memory_bytes('1.5MiB') == 1572864
    assert memory_bytes('2GB') == 2000000000
